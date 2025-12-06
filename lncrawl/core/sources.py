import gzip
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Dict, List, Optional, Set, Type
from urllib.parse import urlparse

import requests
from packaging import version

from ..assets.languages import language_codes
from ..assets.version import get_version
from ..utils.platforms import Platform
from .arguments import get_args
from .crawler import Crawler
from .display import new_version_news
# FIX: Changed to 'exeptions' to match your file structure
from .exeptions import LNException
from .taskman import TaskManager

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #

__all__ = [
    "load_sources",
    "crawler_list",
    "rejected_sources",
    "update_sources",
]

template_list: Set[Type[Crawler]] = set()
crawler_list: Dict[str, Type[Crawler]] = {}
rejected_sources: Dict[str, str] = {}

# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #

__executor = TaskManager()

# --------------------------------------------------------------------------- #
# Loading sources
# --------------------------------------------------------------------------- #

__cache_crawlers: Dict[Path, List[Type[Crawler]]] = {}
__url_regex = re.compile(r"^^(https?|ftp)://[^\s/$.?#].[^\s]*$", re.I)


def __can_do(crawler: Type[Crawler], prop_name: str):
    if not hasattr(crawler, prop_name):
        return False
    if not hasattr(Crawler, prop_name):
        return True
    return getattr(crawler, prop_name) != getattr(Crawler, prop_name)


def __update_rejected(url: str, reason: str):
    no_www = url.replace("://www.", "://")
    url_host = urlparse(url).hostname
    no_www_host = urlparse(no_www).hostname
    rejected_sources.setdefault(url, reason)
    rejected_sources.setdefault(no_www, reason)
    if url_host:
        rejected_sources.setdefault(url_host, reason)
    if no_www_host:
        rejected_sources.setdefault(no_www_host, reason)


def __import_crawlers(file_path: Path, no_cache=False) -> List[Type[Crawler]]:
    if not no_cache:
        if file_path in __cache_crawlers:
            return __cache_crawlers[file_path]

    if not file_path.is_file():
        # Silently fail if file doesn't exist to avoid spam
        return []

    try:
        module_name = hashlib.md5(file_path.name.encode()).hexdigest()
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    except Exception as e:
        logger.warning("Module load failed: %s | %s", file_path, e)
        return []

    language_code = ""
    for part in reversed(file_path.parts):
        if part in language_codes:
            language_code = part
            break

    crawlers = []
    for key in dir(module):
        crawler = getattr(module, key)
        if type(crawler) is not type(Crawler) or not issubclass(crawler, Crawler):
            continue

        if crawler.__dict__.get("is_template"):
            template_list.add(crawler)
            continue

        urls = getattr(crawler, "base_url", [])
        urls = [urls] if isinstance(urls, str) else list(urls)
        urls = list(set([str(url).lower().strip("/") + "/" for url in urls]))
        if not urls:
            continue
        for url in urls:
            if not __url_regex.match(url):
                logger.debug(f"Invalid base url: {url} @{file_path}")
                continue

        for method in ["read_novel_info", "download_chapter_body"]:
            if not hasattr(crawler, method):
                logger.debug(f"Required method not found: {method} @{file_path}")
                continue
            if not callable(getattr(crawler, method)):
                logger.debug(f"Should be callable: {method} @{file_path}")
                continue

        if crawler.is_disabled:
            for url in urls:
                __update_rejected(
                    url,
                    crawler.disable_reason or 'Crawler is disabled'
                )

        setattr(crawler, "base_url", urls)
        setattr(crawler, "language", language_code)
        setattr(crawler, "file_path", str(file_path.absolute()))
        setattr(crawler, "can_login", __can_do(crawler, 'login'))
        setattr(crawler, "can_logout", __can_do(crawler, 'logout'))
        setattr(crawler, "can_search", __can_do(crawler, 'search_novel'))

        crawlers.append(crawler)

    if not no_cache:
        __cache_crawlers[file_path] = crawlers
    return crawlers


def __add_crawlers_from_path(path: Path, no_cache=False):
    if path.name.startswith("_") or not path.name[0].isalnum():
        return

    if not path.exists():
        return

    if path.is_dir():
        for py_file in path.glob("**/*.py"):
            __add_crawlers_from_path(py_file, no_cache)
        return

    try:
        crawlers = __import_crawlers(path, no_cache)
        for crawler in crawlers:
            setattr(crawler, "file_path", str(path.absolute()))
            base_urls: list[str] = getattr(crawler, "base_url")
            for url in base_urls:
                no_www = url.replace("://www.", "://")
                hostname = urlparse(url).hostname
                no_www_hostname = urlparse(no_www).hostname
                crawler_list[url] = crawler
                crawler_list[no_www] = crawler
                if hostname:
                    crawler_list[hostname] = crawler
                if no_www_hostname:
                    crawler_list[no_www_hostname] = crawler
    except Exception as e:
        logger.warning("Could not load crawlers from %s. Error: %s", path, e)


# --------------------------------------------------------------------------- #
# Public methods
# --------------------------------------------------------------------------- #
# Points to the 'sources' folder inside the current project
__local_data_path = Path(__file__).parent.parent.absolute()
if not (__local_data_path / "sources").is_dir():
    __local_data_path = __local_data_path.parent


def load_sources():
    # MODIFIED: Completely removed update checks and external downloads.
    # It now ONLY loads what is physically present in the project folder.
    
    # 1. Load local sources
    local_sources = __local_data_path / "sources"
    logger.info(f"Loading sources from: {local_sources}")
    __add_crawlers_from_path(local_sources)

    # 2. Load manually specified crawlers (if any)
    args = get_args()
    if args.crawler:
        for crawler_file in args.crawler:
            __add_crawlers_from_path(Path(crawler_file))


def update_sources():
    # MODIFIED: Disable updating to prevent re-downloading unwanted sources
    logger.info("Source updates are disabled in this configuration.")
    return 0


def prepare_crawler(url: str, crawler_file: Optional[str] = None) -> Crawler:
    parsed_url = urlparse(url)
    hostname = parsed_url.hostname
    no_www = url.replace("://www.", "://")
    no_www_hostname = urlparse(no_www).hostname
    
    if not hostname or not no_www_hostname:
        raise LNException("No crawler defined for empty hostname")

    if crawler_file:
        __add_crawlers_from_path(Path(crawler_file), True)

    if url in rejected_sources:
        raise LNException("Source is rejected. Reason: " + rejected_sources[url])

    CrawlerType = (
        crawler_list.get(url)
        or crawler_list.get(hostname)
        or crawler_list.get(no_www)
        or crawler_list.get(no_www_hostname)
    )
    
    if not CrawlerType:
        # Fallback: If we can't find a specific crawler, checks if it's fanmtl
        # This is a safety net if DNS/Hostname parsing is slightly off
        if 'fanmtl' in url:
            # Try to find fanmtl manually in the loaded list
            for key, val in crawler_list.items():
                if 'fanmtl' in key:
                    CrawlerType = val
                    break
    
    if not CrawlerType:
        raise LNException("No crawler found for " + hostname)

    home_url = f"{parsed_url.scheme}://{hostname}/"

    logger.info(
        f"Initializing crawler for: {home_url} [%s]",
        getattr(CrawlerType, "file_path", "."),
    )
    crawler = CrawlerType()
    crawler.novel_url = url
    crawler.home_url = home_url
    crawler.initialize()
    return crawler
