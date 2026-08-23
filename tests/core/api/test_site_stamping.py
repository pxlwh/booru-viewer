"""Posts must carry the site they came from.

`Post.id` is unique only within one booru, so every consumer that infers
the site from the selector is wrong the moment two sites are in play.
"""

from __future__ import annotations

import asyncio

import pytest

from booru_viewer.core.api.base import BooruClient, Post
from booru_viewer.core.api.detect import client_for_type


def test_post_defaults_to_no_site():
    p = Post(id=1, file_url="u", preview_url=None, tags="a", score=0, rating="s", source=None)
    assert p.site_id is None


def test_client_for_type_records_site_id():
    c = client_for_type("danbooru", "https://danbooru.donmai.us", site_id=7)
    assert c.site_id == 7


def test_client_for_type_without_site_id_is_none():
    c = client_for_type("danbooru", "https://danbooru.donmai.us")
    assert c.site_id is None


def test_stamp_sets_site_id_on_every_post():
    c = client_for_type("gelbooru", "https://gelbooru.com", site_id=3)
    posts = [
        Post(id=1, file_url="u", preview_url=None, tags="", score=0, rating=None, source=None),
        Post(id=2, file_url="u", preview_url=None, tags="", score=0, rating=None, source=None),
    ]
    out = c._stamp(posts)
    assert [p.site_id for p in out] == [3, 3]


def test_stamp_returns_the_same_list_object():
    """Callers do `return self._stamp(posts)`; it must not copy."""
    c = client_for_type("gelbooru", "https://gelbooru.com", site_id=3)
    posts: list[Post] = []
    assert c._stamp(posts) is posts


def test_stamp_with_no_site_id_leaves_none():
    c = client_for_type("gelbooru", "https://gelbooru.com")
    p = Post(id=1, file_url="u", preview_url=None, tags="", score=0, rating=None, source=None)
    assert c._stamp([p])[0].site_id is None


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_danbooru_search_stamps_site_id(monkeypatch):
    c = client_for_type("danbooru", "https://danbooru.donmai.us", site_id=11)

    async def fake_request(*a, **k):
        return _FakeResponse([{
            "id": 5, "file_url": "https://x/i.jpg", "preview_file_url": None,
            "tag_string": "cat", "score": 3, "rating": "g", "source": "",
            "image_width": 10, "image_height": 20, "created_at": "",
        }])

    monkeypatch.setattr(c, "_request", fake_request)
    posts = asyncio.run(c.search(tags="cat", limit=1))
    assert posts and all(p.site_id == 11 for p in posts)


def test_gelbooru_search_stamps_site_id(monkeypatch):
    c = client_for_type("gelbooru", "https://gelbooru.com", site_id=12)

    async def fake_request(*a, **k):
        return _FakeResponse({"post": [{
            "id": 6, "file_url": "https://x/i.jpg", "preview_url": None,
            "tags": "dog", "score": 1, "rating": "general", "source": "",
            "width": 10, "height": 20, "created_at": "",
        }]})

    monkeypatch.setattr(c, "_request", fake_request)
    posts = asyncio.run(c.search(tags="dog", limit=1))
    assert posts and all(p.site_id == 12 for p in posts)
