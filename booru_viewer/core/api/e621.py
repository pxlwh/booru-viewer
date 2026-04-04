"""e621 API client — Danbooru fork with different response structure."""

from __future__ import annotations

import logging

import httpx

from ..config import DEFAULT_PAGE_SIZE, USER_AGENT
from .base import BooruClient, Post

log = logging.getLogger("booru")


class E621Client(BooruClient):
    api_type = "e621"

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # e621 requires a descriptive User-Agent with username
            ua = USER_AGENT
            if self.api_user:
                ua = f"{USER_AGENT} (by {self.api_user} on e621)"
            self._client = httpx.AsyncClient(
                headers={"User-Agent": ua},
                follow_redirects=True,
                timeout=20.0,
            )
        return self._client

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
        resp = await self.client.get(url, params=params)
        log.info(f"  -> {resp.status_code}")
        if resp.status_code != 200:
            log.warning(f"  body: {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()

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
                )
            )
        return posts

    async def get_post(self, post_id: int) -> Post | None:
        params: dict = {}
        if self.api_key and self.api_user:
            params["login"] = self.api_user
            params["api_key"] = self.api_key

        resp = await self.client.get(
            f"{self.base_url}/posts/{post_id}.json", params=params
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
        )

    async def autocomplete(self, query: str, limit: int = 10) -> list[str]:
        try:
            resp = await self.client.get(
                f"{self.base_url}/tags.json",
                params={
                    "search[name_matches]": f"{query}*",
                    "search[order]": "count",
                    "limit": limit,
                },
            )
            resp.raise_for_status()
            return [item.get("name", "") for item in resp.json() if item.get("name")]
        except Exception:
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
