"""Moebooru-style API client (Yande.re, Konachan, etc.)."""

from __future__ import annotations

import logging

from ..config import DEFAULT_PAGE_SIZE
from .base import BooruClient, Post, _parse_date

log = logging.getLogger("booru")


class MoebooruClient(BooruClient):
    api_type = "moebooru"

    def _post_view_url(self, post: Post) -> str:
        return f"{self.base_url}/post/show/{post.id}"

    async def search(
        self, tags: str = "", page: int = 1, limit: int = DEFAULT_PAGE_SIZE
    ) -> list[Post]:
        params: dict = {"tags": tags, "page": page, "limit": limit}
        if self.api_key and self.api_user:
            params["login"] = self.api_user
            params["password_hash"] = self.api_key

        resp = await self._request("GET", f"{self.base_url}/post.json", params=params)
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception as e:
            log.warning("Moebooru search JSON parse failed: %s: %s — body: %s",
                        type(e).__name__, e, resp.text[:200])
            return []
        if isinstance(data, dict):
            data = data.get("posts", data.get("post", []))
        if not isinstance(data, list):
            return []

        posts = []
        for item in data:
            file_url = item.get("file_url") or item.get("jpeg_url") or ""
            if not file_url:
                continue
            posts.append(
                Post(
                    id=item["id"],
                    file_url=file_url,
                    preview_url=item.get("preview_url") or item.get("actual_preview_url"),
                    tags=item.get("tags", ""),
                    score=item.get("score", 0),
                    rating=item.get("rating"),
                    source=item.get("source"),
                    width=item.get("width", 0),
                    height=item.get("height", 0),
                    created_at=_parse_date(item.get("created_at")),
                )
            )
        if self.category_fetcher is not None:
            await self.category_fetcher.prefetch_batch(posts)
        return posts

    async def get_post(self, post_id: int) -> Post | None:
        params: dict = {"tags": f"id:{post_id}"}
        if self.api_key and self.api_user:
            params["login"] = self.api_user
            params["password_hash"] = self.api_key

        resp = await self._request("GET", f"{self.base_url}/post.json", params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("posts", data.get("post", []))
        if not data:
            return None
        item = data[0]
        file_url = item.get("file_url") or item.get("jpeg_url") or ""
        if not file_url:
            return None
        post = Post(
            id=item["id"],
            file_url=file_url,
            preview_url=item.get("preview_url") or item.get("actual_preview_url"),
            tags=item.get("tags", ""),
            score=item.get("score", 0),
            rating=item.get("rating"),
            source=item.get("source"),
            width=item.get("width", 0),
            height=item.get("height", 0),
            created_at=_parse_date(item.get("created_at")),
        )
        if self.category_fetcher is not None:
            await self.category_fetcher.prefetch_batch([post])
        return post

    async def autocomplete(self, query: str, limit: int = 10) -> list[str]:
        try:
            resp = await self._request(
                "GET", f"{self.base_url}/tag.json",
                params={"name": f"*{query}*", "order": "count", "limit": limit},
            )
            resp.raise_for_status()
            return [t["name"] for t in resp.json() if "name" in t]
        except Exception as e:
            log.warning("Moebooru autocomplete failed for %r: %s: %s",
                        query, type(e).__name__, e)
            return []
