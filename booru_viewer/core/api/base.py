"""Abstract booru client and shared Post dataclass."""

from __future__ import annotations

import logging
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
    tag_categories: dict[str, list[str]] = field(default_factory=dict)

    @property
    def tag_list(self) -> list[str]:
        return self.tags.split()


class BooruClient(ABC):
    """Base class for booru API clients."""

    api_type: str = ""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_user: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_user = api_user
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=20.0,
                event_hooks={"request": [self._log_request]},
            )
        return self._client

    @staticmethod
    async def _log_request(request: httpx.Request) -> None:
        log_connection(str(request.url))

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

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

    async def test_connection(self) -> tuple[bool, str]:
        """Test connection. Returns (success, detail_message)."""
        try:
            posts = await self.search(limit=1)
            return True, f"OK — got {len(posts)} post(s)"
        except httpx.HTTPStatusError as e:
            return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:
            return False, str(e)
