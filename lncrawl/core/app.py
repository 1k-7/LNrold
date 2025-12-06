# lncrawl/core/app.py
import logging
import os
import json # <--- ADDED
from pathlib import Path
from threading import Event
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from readability import Document  # type: ignore
from slugify import slugify

from .. import constants as C
from ..binders import generate_books
from ..core.exeptions import LNException
from ..core.sources import crawler_list, prepare_crawler
from ..models import Chapter, CombinedSearchResult, OutputFormat
from .browser import Browser
from .crawler import Crawler
from .download_chapters import fetch_chapter_body, get_chapter_file # <--- MODIFIED
from .download_images import fetch_chapter_images
from .exeptions import ScraperErrorGroup
from .metadata import save_metadata
from .novel_info import format_novel
from .novel_search import search_novels
from .scraper import Scraper
from .sources import rejected_sources

logger = logging.getLogger(__name__)


class App:
    """Bots are based on top of an instance of this app"""

    def __init__(self):
        self.user_input: Optional[str] = None
        self.crawler_links: List[str] = []
        self.crawler: Optional[Crawler] = None
        self.login_data: Optional[Tuple[str, str]] = None
        self.search_results: List[CombinedSearchResult] = []
        self.output_path = C.DEFAULT_OUTPUT_PATH
        self.pack_by_volume = False
        self.chapters: List[Chapter] = []
        self.novel_status: str = "PENDING" # <--- ADDED: PENDING, HALTED, FAILED, COMPLETED
        self.book_cover: Optional[str] = None
        self.output_formats: Dict[OutputFormat, bool] = {}
        self.generated_books: Dict[OutputFormat, List[str]] = {}
        self.generated_archives: Dict[OutputFormat, str] = {}
        self.archived_outputs: Optional[List[str]] = None
        self.good_file_name: str = ""
        self.no_suffix_after_filename = False
        self.search_progress: float = 0
        self.fetch_novel_progress: float = 0
        self.fetch_chapter_progress: float = 0
        self.fetch_images_progress: float = 0
        self.binding_progress: float = 0
        # REMOVED: atexit.register(self.destroy)

    @property
    def progress(self):
        if self.search_progress > 0:
            return self.search_progress
        info_w = 0.02
        img_w = 0.08
        chap_w = 1 - img_w
        fmt_w = 0.015 * len(self.output_formats)
        content_w = 1 - fmt_w - info_w
        if self.crawler and self.crawler.has_manga:
            img_w = 0.84
            chap_w = 1 - img_w
        img_w *= content_w
        chap_w *= content_w
        return (
            self.fetch_novel_progress * 0.02
            + self.fetch_chapter_progress * chap_w
            + self.fetch_images_progress * img_w
            + self.binding_progress * fmt_w
        )

    # ----------------------------------------------------------------------- #

    def destroy(self):
        # REMOVED: atexit.unregister(self.destroy)
        if self.crawler:
            self.crawler.close()
            self.crawler = None
        self.chapters = []
        self.novel_status = "PENDING"
        self.login_data = None
        self.book_cover = None
        self.search_progress = 0
        self.fetch_novel_progress = 0
        self.fetch_chapter_progress = 0
        self.fetch_images_progress = 0
        self.binding_progress = 0
        self.generated_books = {}
        self.generated_archives = {}
        self.archived_outputs = None
        logger.debug("DONE")

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        self.destroy()

    # ----------------------------------------------------------------------- #

    def prepare_search(self):
        """Requires: user_input"""
        """Produces: [crawler, output_path] or [crawler_links]"""
        if not self.user_input:
            raise LNException("User input is not valid")

        if self.user_input.startswith("http"):
            logger.info("Detected URL input")
            crawler = prepare_crawler(self.user_input)
            if not self.crawler or self.crawler.home_url != crawler.home_url:
                self.crawler = crawler
        else:
            logger.info("Detected query input")
            self.crawler_links = [
                str(link)
                for link, crawler in crawler_list.items()
                if crawler.search_novel != Crawler.search_novel
                and link.startswith("http")
                and link not in rejected_sources
            ]

    def guess_novel_title(self, url: str) -> str:
        try:
            scraper = Scraper(url)
            response = scraper.get_response(url)
            reader = Document(response.text)
        except ScraperErrorGroup as e:
            if logger.isEnabledFor(logging.DEBUG):
                logger.exception("Failed to get response: %s", e)
            # Removed browser fallback to prevent accidental memory leaks
            # with Browser() as browser:
            #     browser.visit(url)
            #     browser.wait("body")
            #     reader = Document(browser.html)
            raise e
        return reader.short_title()

    def search_novel(self):
        """Requires: user_input, crawler_links"""
        """Produces: search_results"""
        logger.info("Searching for novels in %d sites...",
                    len(self.crawler_links))

        self.fetch_novel_progress = 0
        self.fetch_chapter_progress = 0
        self.fetch_images_progress = 0
        self.binding_progress = 0

        search_novels(self)

        if not self.search_results:
            raise LNException("No results for: %s" % self.user_input)

        logger.info(
            "Total %d novels found from %d sites",
            len(self.search_results),
            len(self.crawler_links),
        )

    # ----------------------------------------------------------------------- #

    def can_do(self, prop_name):
        if not hasattr(self.crawler.__class__, prop_name):
            return False
        if not hasattr(Crawler, prop_name):
            return True
        return getattr(self.crawler.__class__, prop_name) != getattr(Crawler, prop_name)

    def get_novel_info(self):
        """Requires: crawler, login_data"""
        """Produces: output_path"""
        self.search_progress = 0

        if not isinstance(self.crawler, Crawler):
            raise LNException("No crawler is selected")

        if self.can_do("login") and self.login_data:
            logger.debug("Login with %s", self.login_data)
            self.crawler.login(*list(self.login_data))

        self.fetch_novel_progress = 0
        self.crawler.read_novel_info()
        format_novel(self.crawler)
        self.fetch_novel_progress = 100

        if not len(self.crawler.chapters):
            raise Exception("No chapters found")
        if not len(self.crawler.volumes):
            raise Exception("No volumes found")

        self.prepare_novel_output_path()
        save_metadata(self)

    def prepare_novel_output_path(self):
        assert self.crawler

        if not self.good_file_name:
            self.good_file_name = slugify(
                self.crawler.novel_title,
                max_length=50,
                separator=" ",
                lowercase=False,
                word_boundary=True,
            )

        host = urlparse(self.crawler.novel_url).netloc
        no_www = host.replace('www.', '')
        source_name = slugify(no_www)

        self.output_path = str(
            Path(C.DEFAULT_OUTPUT_PATH) / source_name / self.good_file_name
        )
        os.makedirs(self.output_path, exist_ok=True)

    # ----------------------------------------------------------------------- #

    def start_download(self, signal=Event()):
        """Requires: crawler, chapters, output_path"""
        if not self.output_path:
            raise LNException("Output path is not defined")
        if not Path(self.output_path).is_dir():
            raise LNException(
                f"Output path does not exists: ({self.output_path})"
            )
        
        self.novel_status = "PENDING" # Reset status at start

        save_metadata(self)
        if signal.is_set():
            return  # canceled

        # 1. FETCH CHAPTER BODY
        for _ in fetch_chapter_body(self, signal):
            if self.novel_status == "HALTED": # Check status signal from download threads
                return # Stop the generator
            yield

        # 2. INTEGRITY CHECK: All chapters must be successful
        failed_chapters = [c for c in self.chapters if not c.success]
        
        if failed_chapters:
            if self.novel_status != "HALTED":
                self.novel_status = "FAILED"
            
            logger.error(
                "Download failed: %d/%d chapters were not fetched properly.", 
                len(failed_chapters), 
                len(self.chapters)
            )
            logger.warning("Skipping binding and image download due to failed chapters.")
            return # Stop the generator (Prevents execution of steps 3 & 4)

        self.novel_status = "COMPLETED"
        save_metadata(self)
        if signal.is_set():
            return  # canceled

        yield from fetch_chapter_images(self, signal) # Only runs if COMPLETED
        save_metadata(self, True)
        if signal.is_set():
            return  # canceled

        if self.crawler and self.can_do("logout"):
            self.crawler.logout()

    # ----------------------------------------------------------------------- #

    def bind_books(self, signal=Event()):
        """
        Requires: crawler, chapters, output_path, pack_by_volume, book_cover,
        output_formats
        """
        if self.novel_status != "COMPLETED":
            logger.warning("Skipping bind_books: Novel status is %s", self.novel_status)
            return

        logger.info("Processing data for binding")
        assert self.crawler

        # 1. Group chapters (Same as before)
        data = {}
        if self.pack_by_volume:
            for vol in self.crawler.volumes:
                filename_suffix = "Volume %d" % vol['id']
                data[filename_suffix] = [
                    x for x in self.chapters if x["volume"] == vol["id"]
                ]
        else:
            if not self.chapters:
                return
            first_id = self.chapters[0]["id"]
            last_id = self.chapters[-1]["id"]
            data[f"c{first_id}-{last_id}"] = self.chapters

        # 2. Process each group (Volume/Book) one by one
        for vol_name, chapters in data.items():
            if not chapters:
                continue
                
            # --- RELOAD: Load content from disk into RAM ---
            logger.info("Loading %d chapters for %s...", len(chapters), vol_name)
            for chapter in chapters:
                if chapter.body: 
                    continue
                try:
                    file_path = get_chapter_file(chapter, self.output_path, self.pack_by_volume)
                    if os.path.exists(file_path):
                        with open(file_path, 'r', encoding="utf-8") as f:
                            saved_data = json.load(f)
                            # Ensure we use saved body, even if it's the placeholder
                            chapter.body = saved_data.get('body', '') 
                except Exception as e:
                    logger.error(f"Failed to reload chapter {chapter.id}: {e}")

            # --- BIND: Generate the files (EPUB, etc.) ---
            for fmt in generate_books(self, {vol_name: chapters}):
                save_metadata(self)
                if signal.is_set():
                    break
                yield fmt, self.generated_archives[fmt]

            # --- UNLOAD: Clear RAM immediately ---
            for chapter in chapters:
                chapter.body = None
            
            import gc
            gc.collect()

            if signal.is_set():
                break

        if self.crawler and self.can_do("logout"):
            self.crawler.logout()
