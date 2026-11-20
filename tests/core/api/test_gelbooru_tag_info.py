"""Rule34 and Safebooru.org categorize tags inline via fields=tag_info.

Their other category sources are dead: rule34's batch tag API ignores
`names=`, and its post page on api.rule34.xxx is an empty 200 body
(live-checked 2026_11_19). gelbooru.com ignores the param and keeps
using its batch tag API.
"""

from __future__ import annotations

import asyncio

from booru_viewer.core.api.detect import client_for_type


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _client(monkeypatch, item):
    c = client_for_type("gelbooru", "https://api.rule34.example")
    sent = []

    async def fake_request(method, url, params):
        sent.append(params)
        return _FakeResponse([item])

    monkeypatch.setattr(c, "_request", fake_request)
    return c, sent


_RULE34_ITEM = {
    "id": 1, "file_url": "https://x/i.jpg", "score": 0,
    "tags": "solo kogure_nodoka some_artist tom_&amp;_jerry highres",
    "tag_info": [
        {"tag": "solo", "type": "tag", "count": 1},
        {"tag": "kogure_nodoka", "type": "character", "count": 1},
        {"tag": "some_artist", "type": "artist", "count": 1},
        {"tag": "tom_&amp;_jerry", "type": "copyright", "count": 1},
        {"tag": "highres", "type": "metadata", "count": 1},
    ],
}


def test_search_requests_tag_info(monkeypatch):
    c, sent = _client(monkeypatch, _RULE34_ITEM)
    asyncio.run(c.search("solo"))
    assert sent[0]["fields"] == "tag_info"


def test_search_builds_categories_in_canonical_order(monkeypatch):
    c, _ = _client(monkeypatch, _RULE34_ITEM)
    post = asyncio.run(c.search("solo"))[0]
    assert post.tag_categories == {
        "Artist": ["some_artist"],
        "Character": ["kogure_nodoka"],
        "Copyright": ["tom_&_jerry"],
        "General": ["solo"],
        "Meta": ["highres"],
    }


def test_get_post_builds_categories(monkeypatch):
    c, sent = _client(monkeypatch, _RULE34_ITEM)
    post = asyncio.run(c.get_post(1))
    assert sent[0]["fields"] == "tag_info"
    assert post.tag_categories["Character"] == ["kogure_nodoka"]


def test_missing_tag_info_leaves_categories_empty(monkeypatch):
    item = {k: v for k, v in _RULE34_ITEM.items() if k != "tag_info"}
    c, _ = _client(monkeypatch, item)
    assert asyncio.run(c.search("solo"))[0].tag_categories == {}


def test_unknown_type_is_dropped(monkeypatch):
    item = dict(_RULE34_ITEM, tag_info=[
        {"tag": "solo", "type": "tag"},
        {"tag": "old_name", "type": "deprecated"},
    ])
    c, _ = _client(monkeypatch, item)
    assert asyncio.run(c.search("solo"))[0].tag_categories == {"General": ["solo"]}
