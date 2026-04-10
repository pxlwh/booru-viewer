"""Abstract booru client and shared Post dataclass."""

from __future__ import annotations

import asyncio
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from ..config import USER_AGENT, DEFAULT_PAGE_SIZE
from ..cache import log_connection

log = logging.getLogger("booru")


@dataclass
class Post:
    id: int
    file_url: str
    preview_url: str | None
    tags: str
    score: int
    rating: str | None
    source: str | None
    width: int = 0
    height: int = 0
    created_at: str = ""  # YYYY-MM-DD
    tag_categories: dict[str, list[str]] = field(default_factory=dict)

    @property
    def tag_list(self) -> list[str]:
        return self.tags.split()


def _parse_date(raw) -> str:
    """Normalize various booru date formats to YYYY-MM-DD."""
    if not raw:
        return ""
    if isinstance(raw, dict):
        raw = raw.get("s", 0)
    if isinstance(raw, (int, float)):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(raw, tz=timezone.utc).strftime("%Y-%m-%d")
    s = str(raw)
    # ISO 8601
    if len(s) >= 10 and s[4] == '-' and s[7] == '-':
        return s[:10]
    # Gelbooru style: "Thu Jun 06 08:16:14 -0500 2024"
    from datetime import datetime
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


class BooruClient(ABC):
    """Base class for booru API clients."""

    api_type: str = ""

    # Shared httpx client across all BooruClient instances for connection
    # reuse. Lazily created on first access; the threading.Lock guards the
    # check-and-set so concurrent first-callers can't both build a client
    # and leak one. The lock is per-class, lives for the process lifetime.
    #
    # Loop affinity: by convention every async call goes through
    # `core.concurrency.run_on_app_loop`, which schedules on the persistent
    # event loop in `gui/app.py`. The first lazy init therefore binds the
    # client to that loop, and every subsequent use is on the same loop.
    # This is the contract that PR2 enforces — see core/concurrency.py.
    _shared_client: httpx.AsyncClient | None = None
    _shared_client_lock: threading.Lock = threading.Lock()

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_user: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_user = api_user
        # Set externally by client_for_type when db + site_id are
        # available. Gelbooru-shape and Moebooru clients use it to
        # populate post.tag_categories via HTML scrape / batch API.
        # Danbooru and e621 leave it None (inline categorization).
        self.category_fetcher = None  # CategoryFetcher | None

    @property
    def client(self) -> httpx.AsyncClient:
        # Fast path: client exists and is open. No lock needed for the read.
        c = BooruClient._shared_client
        if c is not None and not c.is_closed:
            return c
        # Slow path: build it. Lock so two coroutines on the same loop don't
        # both construct + leak.
        with BooruClient._shared_client_lock:
            c = BooruClient._shared_client
            if c is None or c.is_closed:
                c = httpx.AsyncClient(
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                    timeout=20.0,
                    event_hooks={"request": [self._log_request]},
                    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                )
                BooruClient._shared_client = c
            return c

    @classmethod
    async def aclose_shared(cls) -> None:
        """Cleanly aclose the shared client. Safe to call from any coroutine
        running on the loop the client is bound to. No-op if not initialized."""
        with cls._shared_client_lock:
            c = cls._shared_client
            cls._shared_client = None
        if c is not None and not c.is_closed:
            try:
                await c.aclose()
            except Exception as e:
                log.warning("BooruClient shared aclose failed: %s", e)

    @staticmethod
    async def _log_request(request: httpx.Request) -> None:
        log_connection(str(request.url))

    _RETRYABLE_STATUS = frozenset({429, 503})

    async def _request(
        self, method: str, url: str, *, params: dict | None = None
    ) -> httpx.Response:
        """Issue an HTTP request with a single retry on 429/503/timeout/network error."""
        for attempt in range(2):
            try:
                resp = await self.client.request(method, url, params=params)
                if resp.status_code not in self._RETRYABLE_STATUS or attempt == 1:
                    return resp
                wait = 1.0
                if resp.status_code == 429:
                    retry_after = resp.headers.get("retry-after")
                    if retry_after:
                        try:
                            wait = min(float(retry_after), 5.0)
                        except (ValueError, TypeError):
                            wait = 2.0
                    else:
                        wait = 2.0
                log.info(f"Retrying {url} after {resp.status_code} (wait {wait}s)")
                await asyncio.sleep(wait)
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
                # Retry on transient DNS/TCP/timeout failures. Without this,
                # a single DNS hiccup or RST blows up the whole search.
                if attempt == 1:
                    raise
                log.info(f"Retrying {url} after {type(e).__name__}: {e}")
                await asyncio.sleep(1.0)
        return resp  # unreachable in practice, satisfies type checker

    async def close(self) -> None:
        # Per-instance close is a no-op — the shared pool is owned by the
        # class. Use `await BooruClient.aclose_shared()` from app shutdown
        # to actually release the connection pool.
        pass

    @abstractmethod
    async def search(
        self, tags: str = "", page: int = 1, limit: int = DEFAULT_PAGE_SIZE
    ) -> list[Post]:
        ...

    @abstractmethod
    async def get_post(self, post_id: int) -> Post | None:
        ...

    async def autocomplete(self, query: str, limit: int = 10) -> list[str]:
        """Tag autocomplete. Override in subclasses that support it."""
        return []

    def _post_view_url(self, post: Post) -> str | None:
        """Return the URL for a post's HTML detail page, or None.

        Override in subclasses whose booru exposes tag categories in
        the post-view HTML via ``class="tag-type-X"`` markup.
        CategoryFetcher.fetch_post uses this to scrape categories.
        Returning None means "no HTML scrape path" — the default for
        Danbooru and e621 which categorize inline via JSON.
        """
        return None

    def _tag_api_url(self) -> str | None:
        """Return the base URL for the batch tag DAPI, or None.

        Override in Gelbooru-shaped subclasses to enable the fast
        path in CategoryFetcher.fetch_via_tag_api. The fetcher
        appends ``?page=dapi&s=tag&q=index&...`` query params.
        Returning None disables the fast path; the fetcher falls
        back to per-post HTML scrape.
        """
        return None

    async def test_connection(self) -> tuple[bool, str]:
        """Test connection. Returns (success, detail_message).

        Deliberately does NOT echo the response body in the error string —
        when used from `detect_site_type` (which follows redirects), echoing
        the body of an arbitrary HTTP response back into UI text becomes a
        body-leak gadget if the URL ever points anywhere unexpected.
        """
        try:
            posts = await self.search(limit=1)
            return True, f"OK — got {len(posts)} post(s)"
        except httpx.HTTPStatusError as e:
            reason = e.response.reason_phrase or ""
            return False, f"HTTP {e.response.status_code} {reason}".strip()
        except Exception as e:
            return False, str(e)
