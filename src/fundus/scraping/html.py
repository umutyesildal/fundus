import gzip
import time
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from typing import (
    AsyncIterable,
    AsyncIterator,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    List,
    Optional,
    Union,
)

import aiohttp
import feedparser
import lxml.html
import validators
from aiohttp.client_exceptions import ClientError
from aiohttp.http_exceptions import HttpProcessingError
from aiohttp.web_exceptions import HTTPError
from lxml.cssselect import CSSSelector
from lxml.etree import XPath

from fundus.logging import basic_logger
from fundus.scraping.filter import URLFilter, inverse
from fundus.utils.more_async import make_iterable_async

_default_header = {"user-agent": "Fundus"}


class SessionHandler:
    """Object for handling  project global aiohttp.ClientSessions

    The session life cycle consists of three steps which can be repeated indefinitely:
    Build, Supply, Teardown.
    Initially there is no session build within the session handler. When a session is requested
    with get_Session() either a new one is created with _session_factory() or an existing
    one returned. Every subsequent call to get_session() will return the same aiohttp.ClientSession
    object. If close_current_session() is called, the current session will be tear-downed and
    the next call to get_session() will build a new session.
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    @staticmethod
    async def _session_factory() -> aiohttp.ClientSession:
        """Builds a new ClientSession

        This returns a new client session build from pre-defined configurations
        and trace configs set. These trace configs are: on_request_start, on_request_end

        Returns:
            An new ClientSession
        """
        timings: Dict[Optional[str], float] = dict()

        async def on_request_start(
            session: aiohttp.ClientSession, context: types.SimpleNamespace, params: aiohttp.TraceRequestStartParams
        ):
            timings[params.url.host] = time.time()

        async def on_request_end(
            session: aiohttp.ClientSession, context: types.SimpleNamespace, params: aiohttp.TraceRequestEndParams
        ):
            assert params.url.host
            history = params.response.history
            basic_logger.debug(
                f"({params.response.status}) <{params.method} {params.url!r}> "
                f"in {time.time() - timings[params.url.host if not history else history[0].url.host]}"
            )

        trace_config = aiohttp.TraceConfig()
        trace_config.on_request_start.append(on_request_start)
        trace_config.on_request_end.append(on_request_end)

        _connector = aiohttp.TCPConnector(limit=50)
        async_session = aiohttp.ClientSession(connector=_connector, trace_configs=[trace_config])
        return async_session

    async def get_session(self) -> aiohttp.ClientSession:
        """Requests the current build session

        If called for the first time or after close_current_session was called,
        this function will build a new session. Every subsequent call will return
        the same session object until the session is closed with close_current_session().

        Returns:
            aiohttp.ClientSession: The current build session
        """
        if not self._session:
            self._session = await self._session_factory()
        return self._session

    async def close_current_session(self) -> None:
        """Tears down the current build session

        Returns:
            None
        """
        session = await self.get_session()
        basic_logger.debug(f"Close session {session}")
        await session.close()
        self._session = None


session_handler = SessionHandler()


class _ArchiveDecompressor:
    def __init__(self):
        self.archive_mapping: Dict[str, Callable[[bytes], bytes]] = {"application/x-gzip": self._decompress_gzip}

    @staticmethod
    def _decompress_gzip(compressed_content: bytes) -> bytes:
        decompressed_content = gzip.decompress(compressed_content)
        return decompressed_content

    def decompress(self, content: bytes, file_format: "str") -> bytes:
        decompress_function = self.archive_mapping[file_format]
        return decompress_function(content)

    @cached_property
    def supported_file_formats(self) -> List[str]:
        return list(self.archive_mapping.keys())


@dataclass
class URLSource(AsyncIterable[str], ABC):
    url: str

    _request_header: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self._request_header:
            self._request_header = _default_header
        if not validators.url(self.url):
            raise ValueError(f"Invalid url '{self.url}'")

    def set_header(self, request_header: Dict[str, str]) -> None:
        self._request_header = request_header

    @abstractmethod
    def _get_pre_filtered_urls(self) -> AsyncIterator[str]:
        pass

    async def __aiter__(self) -> AsyncIterator[str]:
        async for url in self._get_pre_filtered_urls():
            yield url


@dataclass
class RSSFeed(URLSource):
    async def _get_pre_filtered_urls(self) -> AsyncIterator[str]:
        session = await session_handler.get_session()
        async with session.get(self.url, headers=self._request_header) as response:
            html = await response.text()
            rss_feed = feedparser.parse(html)
            if exception := rss_feed.get("bozo_exception"):
                basic_logger.warn(f"Warning! Couldn't parse rss feed '{self.url}' because of {exception}")
                return
            else:
                for url in (entry["link"] for entry in rss_feed["entries"]):
                    yield url


@dataclass
class Sitemap(URLSource):
    recursive: bool = True
    reverse: bool = False
    sitemap_filter: URLFilter = lambda url: not bool(url)

    _decompressor: ClassVar[_ArchiveDecompressor] = _ArchiveDecompressor()
    _sitemap_selector: ClassVar[XPath] = CSSSelector("sitemap > loc")
    _url_selector: ClassVar[XPath] = CSSSelector("url > loc")

    async def _get_pre_filtered_urls(self) -> AsyncIterator[str]:
        async def yield_recursive(sitemap_url: str) -> AsyncIterator[str]:
            session = await session_handler.get_session()
            if not validators.url(sitemap_url):
                basic_logger.info(f"Skipped sitemap '{sitemap_url}' because the URL is malformed")
            async with session.get(url=sitemap_url, headers=self._request_header) as response:
                try:
                    response.raise_for_status()
                except (HTTPError, ClientError, HttpProcessingError) as error:
                    basic_logger.warning(f"Warning! Couldn't reach sitemap '{sitemap_url}' because of {error}")
                    return
                content = await response.content.read()
                if response.content_type in self._decompressor.supported_file_formats:
                    content = self._decompressor.decompress(content, response.content_type)
                if not content:
                    basic_logger.warning(f"Warning! Empty sitemap at '{sitemap_url}'")
                    return
                tree = lxml.html.fromstring(content)
                urls = [node.text_content() for node in self._url_selector(tree)]
                if urls:
                    for new_url in reversed(urls) if self.reverse else urls:
                        yield new_url
                elif self.recursive:
                    sitemap_locs = [node.text_content() for node in self._sitemap_selector(tree)]
                    filtered_locs = list(filter(inverse(self.sitemap_filter), sitemap_locs))
                    for loc in reversed(filtered_locs) if self.reverse else filtered_locs:
                        async for new_url in yield_recursive(loc):
                            yield new_url

        async for url in yield_recursive(self.url):
            yield url


@dataclass
class NewsMap(Sitemap):
    pass


@dataclass(frozen=True)
class HTML:
    requested_url: str
    responded_url: str
    content: str
    crawl_date: datetime
    source: "HTMLSource"


class HTMLSource:
    def __init__(
        self,
        url_source: Union[AsyncIterable[str], Iterable[str]],
        publisher: Optional[str],
        url_filter: Optional[URLFilter] = None,
        request_header: Optional[Dict[str, str]] = None,
    ):
        if isinstance(url_source, AsyncIterable):
            self.url_source = url_source
        else:
            self.url_source = make_iterable_async(url_source)
        self.publisher = publisher
        self.url_filter = url_filter
        self.request_header = request_header or _default_header
        if isinstance(url_source, URLSource):
            url_source.set_header(self.request_header)

    async def fetch(self, url_filter: Optional[URLFilter] = None) -> AsyncIterator[HTML]:
        combined_filters = ([self.url_filter] if self.url_filter else []) + ([url_filter] if url_filter else [])

        def filter_url(u: str) -> bool:
            return any(f(u) for f in combined_filters)

        async for url in self.url_source:
            if not validators.url(url):
                basic_logger.debug(f"Skipped requested URL '{url}' because the URL is malformed")
                continue

            if filter_url(url):
                basic_logger.debug(f"Skipped requested URL '{url}' because of URL filter")
                continue

            session = await session_handler.get_session()

            try:
                async with session.get(url, headers=self.request_header) as response:
                    if filter_url(str(response.url)):
                        basic_logger.debug(f"Skipped responded URL '{str(response.url)}' because of URL filter")
                        continue
                    html = await response.text()
                    response.raise_for_status()

            except (HTTPError, ClientError, HttpProcessingError, UnicodeError) as error:
                basic_logger.info(f"Skipped requested URL '{url}' because of '{error}'")
                continue

            except Exception as error:
                basic_logger.warn(f"Warning! Skipped  requested URL '{url}' because of an unexpected error {error}")
                continue

            if response.history:
                basic_logger.debug(f"Got redirected {len(response.history)} time(s) from {url} -> {response.url}")

            yield HTML(
                requested_url=url,
                responded_url=str(response.url),
                content=html,
                crawl_date=datetime.now(),
                source=self,
            )
