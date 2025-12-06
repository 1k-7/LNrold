import base64
import logging
import os
import re
from io import BytesIO
from typing import Any, Callable, Dict, MutableMapping, Optional, Tuple, Union
from urllib.parse import ParseResult, urlparse

from bs4 import BeautifulSoup
from ..cloudscraper import create_scraper
from PIL import Image, UnidentifiedImageError
from requests import Response, Session
from requests.exceptions import ProxyError
from requests.structures import CaseInsensitiveDict
from tenacity import (RetryCallState, retry, retry_if_exception_type,
                      stop_after_attempt, wait_random_exponential)

from .exeptions import RetryErrorGroup
from .proxy import get_a_proxy, remove_faulty_proxies
from .soup import SoupMaker
from .taskman import TaskManager

logger = logging.getLogger(__name__)


class Scraper(TaskManager, SoupMaker):
    def __init__(
        self,
        origin: str,
        workers: Optional[int] = None,
        parser: Optional[str] = None,
    ) -> None:
        self.home_url = origin
        self.last_soup_url = ""
        self.use_proxy = os.getenv("use_proxy")

        self.init_scraper()
        self.init_parser(parser)
        self.init_executor(workers)

    def close(self) -> None:
        if hasattr(self, "scraper"):
            self.scraper.close()
        super().close()

    def init_parser(self, parser: Optional[str] = None):
        self._soup_tool = SoupMaker(parser)
        self.make_tag = self._soup_tool.make_tag  # type:ignore
        self.make_soup = self._soup_tool.make_soup  # type:ignore

    def init_scraper(self, session: Optional[Session] = None):
        try:
            self.scraper = create_scraper(
                # [TURBO FIX] Allow 403 retries (required) but make them fast
                auto_refresh_on_403=True, 
                max_403_retries=3,             

                # [TURBO FIX] Zero delay, High Concurrency
                min_request_interval=0,        
                max_concurrent_requests=100,     
                rotate_tls_ciphers=True,       
                session_refresh_interval=900,  

                enable_stealth=True,
                stealth_options={
                    'min_delay': 0,            
                    'max_delay': 0,            
                    'human_like_delays': False, 
                    'randomize_headers': True,
                    'browser_quirks': True
                },

                browser={
                    'browser': 'chrome',
                    'platform': 'windows',
                    'desktop': True,
                    'mobile': False,
                },
            )
            
            # [CRITICAL SPEED HACK]
            # Cloudscraper creates custom adapters (CipherSuiteAdapter). 
            # We must manually boost their pool size to allow parallel downloads.
            if hasattr(self.scraper, 'adapters'):
                for adapter in self.scraper.adapters.values():
                    adapter.pool_connections = 100
                    adapter.pool_maxsize = 100
                    
        except Exception:
            logger.exception("Failed to initialize cloudscraper")
            self.scraper = session or Session()

    def __get_proxies(self, scheme, timeout: float = 0):
        if self.use_proxy and scheme:
            return {scheme: get_a_proxy(scheme, timeout)}
        return {}

    def __process_request(
        self,
        method: str,
        url: str,
        *args,
        max_retries: Optional[int] = None,
        headers: Optional[MutableMapping] = {},
        **kwargs,
    ):
        method_call: Callable[..., Response] = getattr(self.scraper, method)
        if not callable(method_call):
            raise Exception(f"No request method: {method}")

        _parsed = urlparse(url)

        kwargs = kwargs or dict()
        kwargs.setdefault("allow_redirects", True)
        kwargs["proxies"] = self.__get_proxies(_parsed.scheme)

        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Origin", self.home_url.strip("/"))
        headers.setdefault("Referer", self.last_soup_url or self.home_url)

        def _after_retry(retry_state: RetryCallState):
            future = retry_state.outcome
            if future:
                e = future.exception()
                if isinstance(e, RetryErrorGroup):
                    logger.debug(f"{repr(e)} | Retrying...")
                    if isinstance(e, ProxyError):
                        for proxy_url in kwargs.get("proxies", {}).values():
                            remove_faulty_proxies(proxy_url)
                        kwargs["proxies"] = self.__get_proxies(_parsed.scheme, 5)

        @retry(
            stop=stop_after_attempt(max_retries or 0),
            wait=wait_random_exponential(multiplier=0.5, max=60),
            retry=retry_if_exception_type(RetryErrorGroup),
            after=_after_retry,
            reraise=True,
        )
        def _do_request():
            with self.domain_gate(_parsed.hostname):
                response = method_call(
                    url,
                    *args,
                    **kwargs,
                    headers=headers,
                )
                response.raise_for_status()
                response.encoding = "utf8"

            self.cookies.update({x.name: x.value for x in response.cookies})
            return response

        logger.debug(
            f"[{method.upper()}] {url}\n"
            + "\n".join([f"    {k} = {v}" for k, v in kwargs.items()])
        )
        return _do_request()

    @property
    def origin(self) -> ParseResult:
        return urlparse(self.home_url)

    @property
    def headers(self) -> Dict[str, Union[str, bytes]]:
        return dict(self.scraper.headers)

    def set_header(self, key: str, value: str) -> None:
        self.scraper.headers[key] = value

    @property
    def cookies(self) -> Dict[str, Optional[str]]:
        return {x.name: x.value for x in self.scraper.cookies}

    def set_cookie(self, name: str, value: str) -> None:
        self.scraper.cookies.set(name, value)

    def absolute_url(self, url: Any, page_url: Optional[str] = None) -> str:
        url = str(url or "").strip().rstrip("/")
        if not url:
            return url
        if url.startswith("data:"):
            return url
        if not page_url:
            page_url = str(self.last_soup_url or self.home_url)
        if url.startswith("//"):
            return self.home_url.split(":")[0] + ":" + url
        if url.startswith("/"):
            return self.home_url.strip("/") + url
        if re.match(r'^https?://.*$', url):
            return url
        if page_url:
            return page_url.strip("/") + "/" + url
        return self.home_url + url

    def _ping_request(self, url: str, timeout=5, **kwargs):
        return self.__process_request("head", url, **kwargs, max_retries=2, timeout=timeout)

    def get_response(
        self,
        url: str,
        timeout: Optional[Union[float, Tuple[float, float]]] = (7, 301),
        **kwargs,
    ) -> Response:
        return self.__process_request(
            "get",
            url,
            timeout=timeout,
            max_retries=2,
            **kwargs,
        )

    def post_response(
        self,
        url: str,
        data: Optional[MutableMapping] = {},
        max_retries: Optional[int] = 0,
        **kwargs
    ) -> Response:
        return self.__process_request(
            "post",
            url,
            data=data,
            max_retries=max_retries,
            **kwargs,
        )

    def submit_form(
        self,
        url: str,
        data: Optional[MutableMapping] = None,
        multipart: bool = False,
        headers: Optional[MutableMapping] = {},
        **kwargs
    ) -> Response:
        headers = CaseInsensitiveDict(headers)
        headers.setdefault(
            "Content-Type",
            (
                "multipart/form-data"
                if multipart
                else "application/x-www-form-urlencoded; charset=UTF-8"
            ),
        )
        return self.post_response(url, data=data, headers=headers, **kwargs)

    def download_file(
        self,
        url: str,
        output_file: str,
        **kwargs
    ) -> None:
        response = self.__process_request("get", url, **kwargs)
        with open(output_file, "wb") as f:
            f.write(response.content)

    def download_image(
        self,
        url: str,
        headers: Optional[MutableMapping] = {},
        **kwargs
    ):
        if url.startswith("data:"):
            content = base64.b64decode(url.split("base64,")[-1])
            return Image.open(BytesIO(content))

        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Origin", None)
        headers.setdefault("Referer", None)
        timeout = kwargs.pop('timeout', None) or (3, 30)

        try:
            response = self.__process_request(
                "get",
                url,
                headers=headers,
                timeout=timeout,
                max_retries=2,
                **kwargs,
            )
            content = response.content
            return Image.open(BytesIO(content))
        except UnidentifiedImageError:
            headers.setdefault(
                "Accept",
                "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.9",
            )
            response = self.__process_request("get", url, headers=headers, **kwargs)
            content = response.content
            return Image.open(BytesIO(content))

    def get_json(
        self,
        url: str,
        headers: Optional[MutableMapping] = {},
        **kwargs
    ) -> Any:
        headers = CaseInsensitiveDict(headers)
        headers.setdefault(
            "Accept",
            "application/json,text/plain,*/*",
        )
        response = self.get_response(url, headers=headers, **kwargs)
        return response.json()

    def post_json(
        self,
        url: str,
        data: Optional[MutableMapping] = {},
        headers: Optional[MutableMapping] = {},
        **kwargs,
    ) -> Any:
        headers = CaseInsensitiveDict(headers)
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault(
            "Accept",
            "application/json,text/plain,*/*",
        )
        response = self.post_response(url, data=data, headers=headers, **kwargs)
        return response.json()

    def submit_form_json(
        self,
        url: str,
        data: Optional[MutableMapping] = {},
        headers: Optional[MutableMapping] = {},
        multipart: Optional[bool] = False,
        **kwargs
    ) -> Any:
        headers = CaseInsensitiveDict(headers)
        headers.setdefault(
            "Accept",
            "application/json,text/plain,*/*",
        )
        response = self.submit_form(
            url,
            data=data,
            headers=headers,
            multipart=bool(multipart),
            **kwargs
        )
        return response.json()

    def get_soup(
        self,
        url: str,
        headers: Optional[MutableMapping] = {},
        encoding: Optional[str] = None,
        **kwargs,
    ) -> BeautifulSoup:
        headers = CaseInsensitiveDict(headers)
        headers.setdefault(
            "Accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9",
        )
        response = self.get_response(
            url,
            headers=headers,
            **kwargs,
        )
        self.last_soup_url = url
        return self.make_soup(response, encoding)

    def post_soup(
        self,
        url: str,
        data: Optional[MutableMapping] = {},
        headers: Optional[MutableMapping] = {},
        encoding: Optional[str] = None,
        **kwargs
    ) -> BeautifulSoup:
        headers = CaseInsensitiveDict(headers)
        headers.setdefault(
            "Accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9",
        )
        response = self.post_response(
            url,
            data=data,
            headers=headers,
            **kwargs,
        )
        return self.make_soup(response, encoding)

    def submit_form_for_soup(
        self,
        url: str,
        data: Optional[MutableMapping] = {},
        headers: Optional[MutableMapping] = {},
        multipart: Optional[bool] = False,
        encoding: Optional[str] = None,
        **kwargs
    ) -> BeautifulSoup:
        headers = CaseInsensitiveDict(headers)
        headers.setdefault(
            "Accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9",
        )
        response = self.submit_form(
            url,
            data=data,
            headers=headers,
            multipart=bool(multipart),
            **kwargs,
        )
        return self.make_soup(response, encoding)
