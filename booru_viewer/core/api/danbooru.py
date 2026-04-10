"""Danbooru-style API client (Danbooru, Safebooru, Szurubooru variants)."""

from __future__ import annotations

import logging

from ..config import DEFAULT_PAGE_SIZE
from .base import BooruClient, Post, _parse_date

log = logging.getLogger("booru")


class DanbooruClient(BooruClient):
    api_type = "danbooru"

    async def search(
        self, tags: str = "", page: int = 1, limit: int = DEFAULT_PAGE_SIZE
    ) -> list[Post]:
        params: dict = {"tags": tags, "page": page, "limit": limit}
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
            log.warning("Danbooru search JSON parse failed: %s: %s — body: %s",
                        type(e).__name__, e, resp.text[:200])
            return []

        # Some Danbooru forks wrap in {"posts": [...]}
        if isinstance(data, dict):
            data = data.get("posts", [])

        posts = []
        for item in data:
            file_url = item.get("file_url") or item.get("large_file_url") or ""
            if not file_url:
                continue
            posts.append(
                Post(
                    id=item["id"],
                    file_url=file_url,
                    preview_url=item.get("preview_file_url") or item.get("preview_url"),
                    tags=self._extract_tags(item),
                    score=item.get("score", 0),
                    rating=item.get("rating"),
                    source=item.get("source"),
                    width=item.get("image_width", 0),
                    height=item.get("image_height", 0),
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
        item = resp.json()
        file_url = item.get("file_url") or item.get("large_file_url") or ""
        if not file_url:
            return None
        return Post(
            id=item["id"],
            file_url=file_url,
            preview_url=item.get("preview_file_url") or item.get("preview_url"),
            tags=self._extract_tags(item),
            score=item.get("score", 0),
            rating=item.get("rating"),
            source=item.get("source"),
            width=item.get("image_width", 0),
            height=item.get("image_height", 0),
            created_at=_parse_date(item.get("created_at")),
            tag_categories=self._extract_tag_categories(item),
        )

    async def autocomplete(self, query: str, limit: int = 10) -> list[str]:
        try:
            resp = await self._request(
                "GET", f"{self.base_url}/autocomplete.json",
                params={"search[query]": query, "search[type]": "tag_query", "limit": limit},
            )
            resp.raise_for_status()
            return [item.get("value", item.get("label", "")) for item in resp.json()]
        except Exception as e:
            log.warning("Danbooru autocomplete failed for %r: %s: %s",
                        query, type(e).__name__, e)
            return []

    @staticmethod
    def _extract_tags(item: dict) -> str:
        """Pull tags from Danbooru's split tag fields or a single tag_string."""
        if "tag_string" in item:
            return item["tag_string"]
        parts = []
        for key in ("tag_string_general", "tag_string_character",
                     "tag_string_copyright", "tag_string_artist", "tag_string_meta"):
            if key in item and item[key]:
                parts.append(item[key])
        return " ".join(parts) if parts else ""

    @staticmethod
    def _extract_tag_categories(item: dict) -> dict[str, list[str]]:
        cats: dict[str, list[str]] = {}
        mapping = {
            "tag_string_artist": "Artist",
            "tag_string_character": "Character",
            "tag_string_copyright": "Copyright",
            "tag_string_general": "General",
            "tag_string_meta": "Meta",
        }
        for key, label in mapping.items():
            val = item.get(key, "")
            if val and val.strip():
                cats[label] = val.split()
        return cats
