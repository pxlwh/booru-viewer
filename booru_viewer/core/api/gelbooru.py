"""Gelbooru-style API client."""

from __future__ import annotations

import logging

from ..config import DEFAULT_PAGE_SIZE
from .base import BooruClient, Post

log = logging.getLogger("booru")


class GelbooruClient(BooruClient):
    api_type = "gelbooru"

    async def search(
        self, tags: str = "", page: int = 1, limit: int = DEFAULT_PAGE_SIZE
    ) -> list[Post]:
        # Gelbooru uses pid (0-indexed page) not page number
        params: dict = {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "json": "1",
            "tags": tags,
            "limit": limit,
            "pid": page - 1,
        }
        if self.api_key and self.api_user:
            # Only send if they look like real values, not leftover URL fragments
            key = self.api_key.strip().lstrip("&")
            user = self.api_user.strip().lstrip("&")
            if key and not key.startswith("api_key="):
                params["api_key"] = key
            if user and not user.startswith("user_id="):
                params["user_id"] = user

        url = f"{self.base_url}/index.php"
        log.info(f"GET {url}")
        log.debug(f"  params: {params}")
        resp = await self.client.get(url, params=params)
        log.info(f"  -> {resp.status_code}")
        if resp.status_code != 200:
            log.warning(f"  body: {resp.text[:500]}")
        resp.raise_for_status()

        data = resp.json()
        log.debug(f"  json type: {type(data).__name__}, keys: {list(data.keys()) if isinstance(data, dict) else f'list[{len(data)}]'}")
        # Gelbooru wraps posts in {"post": [...]} or returns {"post": []}
        if isinstance(data, dict):
            data = data.get("post", [])
        if not isinstance(data, list):
            return []

        posts = []
        for item in data:
            file_url = item.get("file_url", "")
            if not file_url:
                continue
            posts.append(
                Post(
                    id=item["id"],
                    file_url=file_url,
                    preview_url=item.get("preview_url"),
                    tags=self._decode_tags(item.get("tags", "")),
                    score=item.get("score", 0),
                    rating=item.get("rating"),
                    source=item.get("source"),
                    width=item.get("width", 0),
                    height=item.get("height", 0),
                )
            )
        return posts

    @staticmethod
    def _decode_tags(tags: str) -> str:
        from html import unescape
        return unescape(tags)

    async def get_post(self, post_id: int) -> Post | None:
        params: dict = {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "json": "1",
            "id": post_id,
        }
        if self.api_key and self.api_user:
            params["api_key"] = self.api_key
            params["user_id"] = self.api_user

        resp = await self.client.get(f"{self.base_url}/index.php", params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("post", [])
        if not data:
            return None
        item = data[0]
        file_url = item.get("file_url", "")
        if not file_url:
            return None
        return Post(
            id=item["id"],
            file_url=file_url,
            preview_url=item.get("preview_url"),
            tags=item.get("tags", ""),
            score=item.get("score", 0),
            rating=item.get("rating"),
            source=item.get("source"),
            width=item.get("width", 0),
            height=item.get("height", 0),
        )

    async def autocomplete(self, query: str, limit: int = 10) -> list[str]:
        try:
            resp = await self.client.get(
                f"{self.base_url}/index.php",
                params={
                    "page": "dapi",
                    "s": "tag",
                    "q": "index",
                    "json": "1",
                    "name_pattern": f"%{query}%",
                    "limit": limit,
                    "orderby": "count",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                data = data.get("tag", [])
            return [t.get("name", "") for t in data if t.get("name")]
        except Exception:
            return []
