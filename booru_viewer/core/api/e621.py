"""e621 API client — Danbooru fork with different response structure."""

from __future__ import annotations

import logging
import threading

import httpx

from ..config import DEFAULT_PAGE_SIZE, USER_AGENT
from .base import BooruClient, Post, _parse_date

log = logging.getLogger("booru")


class E621Client(BooruClient):
    api_type = "e621"

    # Same shared-singleton pattern as BooruClient, but e621 needs a custom
    # User-Agent (their TOS requires identifying the app + user). When the
    # UA changes (api_user edit) we need to rebuild — and we explicitly
    # close the old client to avoid leaking its connection pool.
    _e621_client: httpx.AsyncClient | None = None
    _e621_ua: str = ""
    _e621_lock: threading.Lock = threading.Lock()
    # Old clients pending aclose. We can't await from a sync property, so
    # we stash them here and the app's shutdown coroutine drains them.
    _e621_to_close: list[httpx.AsyncClient] = []

    @property
    def client(self) -> httpx.AsyncClient:
        ua = USER_AGENT
        if self.api_user:
            ua = f"{USER_AGENT} (by {self.api_user} on e621)"
        # Fast path
        c = E621Client._e621_client
        if c is not None and not c.is_closed and E621Client._e621_ua == ua:
            return c
        with E621Client._e621_lock:
            c = E621Client._e621_client
            if c is None or c.is_closed or E621Client._e621_ua != ua:
                # Stash old client for shutdown cleanup if it's still open.
                if c is not None and not c.is_closed:
                    E621Client._e621_to_close.append(c)
                E621Client._e621_ua = ua
                c = httpx.AsyncClient(
                    headers={"User-Agent": ua},
                    follow_redirects=True,
                    timeout=20.0,
                    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                )
                E621Client._e621_client = c
            return c

    @classmethod
    async def aclose_shared(cls) -> None:
        """Cleanly aclose the active client and any UA-change leftovers."""
        with cls._e621_lock:
            current = cls._e621_client
            cls._e621_client = None
            pending = cls._e621_to_close
            cls._e621_to_close = []
        for c in [current, *pending]:
            if c is not None and not c.is_closed:
                try:
                    await c.aclose()
                except Exception as e:
                    log.warning("E621Client aclose failed: %s", e)

    async def search(
        self, tags: str = "", page: int = 1, limit: int = DEFAULT_PAGE_SIZE
    ) -> list[Post]:
        params: dict = {"tags": tags, "page": page, "limit": min(limit, 320)}
        if self.api_key and self.api_user:
            params["login"] = self.api_user
            params["api_key"] = self.api_key

        url = f"{self.base_url}/posts.json"
        log.info(f"GET {url}")
        log.debug(f"  params: {params}")
        resp = await self._request("GET", url, params=params)
        log.info(f"  -> {resp.status_code}")
        if resp.status_code != 200:
            log.warning(f"  body: {resp.text[:500]}")
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception as e:
            log.warning("e621 search JSON parse failed: %s: %s — body: %s",
                        type(e).__name__, e, resp.text[:200])
            return []

        # e621 wraps posts in {"posts": [...]}
        if isinstance(data, dict):
            data = data.get("posts", [])

        posts = []
        for item in data:
            file_url = self._get_file_url(item)
            if not file_url:
                continue
            posts.append(
                Post(
                    id=item["id"],
                    file_url=file_url,
                    preview_url=self._get_nested(item, "preview", "url"),
                    tags=self._extract_tags(item),
                    score=self._get_score(item),
                    rating=item.get("rating"),
                    source=self._get_source(item),
                    width=self._get_nested(item, "file", "width") or 0,
                    height=self._get_nested(item, "file", "height") or 0,
                    created_at=_parse_date(item.get("created_at")),
                    tag_categories=self._extract_tag_categories(item),
                )
            )
        return posts

    async def get_post(self, post_id: int) -> Post | None:
        params: dict = {}
        if self.api_key and self.api_user:
            params["login"] = self.api_user
            params["api_key"] = self.api_key

        resp = await self._request(
            "GET", f"{self.base_url}/posts/{post_id}.json", params=params
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        item = data.get("post", data) if isinstance(data, dict) else data

        file_url = self._get_file_url(item)
        if not file_url:
            return None
        return Post(
            id=item["id"],
            file_url=file_url,
            preview_url=self._get_nested(item, "preview", "url"),
            tags=self._extract_tags(item),
            score=self._get_score(item),
            rating=item.get("rating"),
            source=self._get_source(item),
            width=self._get_nested(item, "file", "width") or 0,
            height=self._get_nested(item, "file", "height") or 0,
            created_at=_parse_date(item.get("created_at")),
            tag_categories=self._extract_tag_categories(item),
        )

    async def autocomplete(self, query: str, limit: int = 10) -> list[str]:
        try:
            resp = await self._request(
                "GET", f"{self.base_url}/tags.json",
                params={
                    "search[name_matches]": f"{query}*",
                    "search[order]": "count",
                    "limit": limit,
                },
            )
            resp.raise_for_status()
            return [item.get("name", "") for item in resp.json() if item.get("name")]
        except Exception as e:
            log.warning("e621 autocomplete failed for %r: %s: %s",
                        query, type(e).__name__, e)
            return []

    @staticmethod
    def _get_file_url(item: dict) -> str:
        """Extract file URL from e621's nested structure."""
        # e621: item["file"]["url"], fallback to item["sample"]["url"]
        f = item.get("file")
        if isinstance(f, dict) and f.get("url"):
            return f["url"]
        s = item.get("sample")
        if isinstance(s, dict) and s.get("url"):
            return s["url"]
        # Some posts have null URLs (deleted/flagged)
        return ""

    @staticmethod
    def _get_nested(item: dict, *keys) -> str | int | None:
        """Safely get nested dict value."""
        current = item
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                return None
        return current

    @staticmethod
    def _extract_tags(item: dict) -> str:
        """e621 tags are a dict of category -> list[str]."""
        tags_obj = item.get("tags")
        if isinstance(tags_obj, dict):
            all_tags = []
            for category in ("general", "artist", "copyright", "character",
                             "species", "meta", "lore"):
                tag_list = tags_obj.get(category, [])
                if isinstance(tag_list, list):
                    all_tags.extend(tag_list)
            return " ".join(all_tags)
        if isinstance(tags_obj, str):
            return tags_obj
        return ""

    @staticmethod
    def _extract_tag_categories(item: dict) -> dict[str, list[str]]:
        tags_obj = item.get("tags")
        if not isinstance(tags_obj, dict):
            return {}
        cats: dict[str, list[str]] = {}
        mapping = {
            "artist": "Artist", "character": "Character",
            "copyright": "Copyright", "species": "Species",
            "general": "General", "meta": "Meta", "lore": "Lore",
        }
        for key, label in mapping.items():
            tag_list = tags_obj.get(key, [])
            if isinstance(tag_list, list) and tag_list:
                cats[label] = tag_list
        return cats

    @staticmethod
    def _get_score(item: dict) -> int:
        """e621 score is a dict with up/down/total."""
        score = item.get("score")
        if isinstance(score, dict):
            return score.get("total", 0)
        if isinstance(score, int):
            return score
        return 0

    @staticmethod
    def _get_source(item: dict) -> str | None:
        """e621 sources is a list."""
        sources = item.get("sources")
        if isinstance(sources, list) and sources:
            return sources[0]
        return item.get("source")
