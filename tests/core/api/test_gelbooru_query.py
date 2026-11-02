"""Users type Danbooru query syntax everywhere; the Gelbooru client
translates it. OR and order: are covered here.

`~a ~b` is the one OR syntax users type; Gelbooru-engine sites need it
rewritten. Gelbooru proper wants `{a ~ b}`, Rule34 and Safebooru.org want
`( a ~ b )`, and each answers 0 posts to the other's form (live-checked
2026_11_02), so the client probes once per site and remembers the winner.
"""

from __future__ import annotations

import asyncio

import pytest

from booru_viewer.core.api import gelbooru
from booru_viewer.core.api.detect import client_for_type
from booru_viewer.core.api.gelbooru import translate_or


def test_no_or_tokens_returns_none():
    assert translate_or("cat_ears -dog", "{{{}}}") is None


def test_braces_style():
    assert translate_or("~a ~b", "{{{}}}") == "{a ~ b}"


def test_parens_style_keeps_and_tags_and_negations():
    assert translate_or("~a c ~b -d", "( {} )") == "( a ~ b ) c -d"


def test_single_or_token_is_plain_tag():
    assert translate_or("~a c", "{{{}}}") == "a c"


def test_bare_tilde_is_not_an_or_token():
    assert translate_or("~ c", "{{{}}}") is None


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


_POST = {"id": 1, "file_url": "https://x/i.jpg", "tags": "a", "score": 0}


@pytest.fixture(autouse=True)
def _clear_cache():
    gelbooru._OR_STYLE.clear()
    yield
    gelbooru._OR_STYLE.clear()


def _client_answering(monkeypatch, working_query):
    c = client_for_type("gelbooru", "https://rule34.example")
    sent = []

    async def fake_request(method, url, params):
        sent.append(params["tags"])
        return _FakeResponse({"post": [_POST] if params["tags"] == working_query else []})

    monkeypatch.setattr(c, "_request", fake_request)
    return c, sent


def test_probe_falls_through_to_parens_and_remembers(monkeypatch):
    c, sent = _client_answering(monkeypatch, "( a ~ b )")
    assert asyncio.run(c.search("~a ~b"))
    assert sent == ["{a ~ b}", "( a ~ b )"]

    c2, sent2 = _client_answering(monkeypatch, "( a ~ b )")
    asyncio.run(c2.search("~a ~b"))
    assert sent2 == ["( a ~ b )"], "a new client for the same site must skip the probe"


def test_empty_result_caches_nothing(monkeypatch):
    c, sent = _client_answering(monkeypatch, "never")
    assert asyncio.run(c.search("~a ~b")) == []
    assert sent == ["{a ~ b}", "( a ~ b )"]
    assert gelbooru._OR_STYLE == {}


def test_plain_query_is_untouched(monkeypatch):
    c, sent = _client_answering(monkeypatch, "a b")
    asyncio.run(c.search("a b"))
    assert sent == ["a b"]


# --- order: -> sort: ---------------------------------------------------
# Danbooru-family `order:` is the one sort syntax users type. Every
# Gelbooru-engine site tested (Gelbooru, Rule34, Safebooru.org) wants
# `sort:` and returns 0 posts for `order:` (live-checked 2026_11_02).

from booru_viewer.core.api.gelbooru import translate_order


@pytest.mark.parametrize("given, want", [
    ("cat order:score", "cat sort:score:desc"),
    ("order:score_asc cat", "sort:score:asc cat"),
    ("cat order:id", "cat sort:id:asc"),
    ("cat order:id_desc", "cat sort:id:desc"),
    ("cat order:random", "cat sort:random"),
    ("cat order:favcount", "cat order:favcount"),
    ("cat", "cat"),
])
def test_translate_order(given, want):
    assert translate_order(given) == want


def test_search_sends_translated_order(monkeypatch):
    c, sent = _client_answering(monkeypatch, "a sort:score:desc")
    asyncio.run(c.search("a order:score"))
    assert sent == ["a sort:score:desc"]


def test_order_and_or_together(monkeypatch):
    c, sent = _client_answering(monkeypatch, "{a ~ b} sort:random")
    assert asyncio.run(c.search("~a ~b order:random"))
    assert sent == ["{a ~ b} sort:random"]
