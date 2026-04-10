"""Tests for CategoryFetcher: HTML parser, tag API parser, cache compose,
probe persistence, dispatch logic, and canonical ordering.

All pure Python — no Qt, no network. Uses tmp_db fixture for cache tests
and synthetic HTML/JSON/XML for parser tests.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from booru_viewer.core.api.category_fetcher import (
    CategoryFetcher,
    _canonical_order,
    _parse_post_html,
    _parse_tag_response,
    _LABEL_MAP,
    _GELBOORU_TYPE_MAP,
)


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

@dataclass
class FakePost:
    id: int = 1
    tags: str = ""
    tag_categories: dict = field(default_factory=dict)

    @property
    def tag_list(self) -> list[str]:
        return self.tags.split() if self.tags else []


class FakeClient:
    """Minimal mock of BooruClient for CategoryFetcher construction."""
    api_key = None
    api_user = None

    def __init__(self, post_view_url=None, tag_api_url=None, api_key=None, api_user=None):
        self._pv_url = post_view_url
        self._ta_url = tag_api_url
        self.api_key = api_key
        self.api_user = api_user

    def _post_view_url(self, post):
        return self._pv_url

    def _tag_api_url(self):
        return self._ta_url

    async def _request(self, method, url, params=None):
        raise NotImplementedError("mock _request not configured")


class FakeResponse:
    """Minimal httpx.Response stand-in for parser tests."""
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


# ---------------------------------------------------------------------------
# HTML parser tests (_parse_post_html)
# ---------------------------------------------------------------------------

class TestParsePostHtml:
    """Test the two-pass regex HTML parser against synthetic markup."""

    def test_rule34_style_two_links(self):
        """Standard Gelbooru-fork layout: ? wiki link + tag search link."""
        html = '''
        <li class="tag-type-character">
            <a href="index.php?page=wiki&s=list&search=hatsune_miku">?</a>
            <a href="index.php?page=post&amp;s=list&amp;tags=hatsune_miku">hatsune miku</a>
            <span class="tag-count">12345</span>
        </li>
        <li class="tag-type-artist">
            <a href="index.php?page=wiki&s=list&search=someartist">?</a>
            <a href="index.php?page=post&amp;s=list&amp;tags=someartist">someartist</a>
            <span class="tag-count">100</span>
        </li>
        <li class="tag-type-general">
            <a href="index.php?page=wiki&s=list&search=1girl">?</a>
            <a href="index.php?page=post&amp;s=list&amp;tags=1girl">1girl</a>
            <span class="tag-count">9999999</span>
        </li>
        '''
        cats, labels = _parse_post_html(html)
        assert "Character" in cats
        assert "Artist" in cats
        assert "General" in cats
        assert cats["Character"] == ["hatsune_miku"]
        assert cats["Artist"] == ["someartist"]
        assert cats["General"] == ["1girl"]
        assert labels["hatsune_miku"] == "Character"
        assert labels["someartist"] == "Artist"

    def test_moebooru_style(self):
        """yande.re / Konachan: /post?tags=NAME format."""
        html = '''
        <li class="tag-type-artist">
            <a href="/artist/show?name=anmi">?</a>
            <a href="/post?tags=anmi">anmi</a>
        </li>
        <li class="tag-type-copyright">
            <a href="/wiki/show?title=vocaloid">?</a>
            <a href="/post?tags=vocaloid">vocaloid</a>
        </li>
        '''
        cats, labels = _parse_post_html(html)
        assert cats["Artist"] == ["anmi"]
        assert cats["Copyright"] == ["vocaloid"]

    def test_combined_class_konachan(self):
        """Konachan uses class="tag-link tag-type-character"."""
        html = '''
        <span class="tag-link tag-type-character">
            <a href="/wiki/show?title=miku">?</a>
            <a href="/post?tags=hatsune_miku">hatsune miku</a>
        </span>
        '''
        cats, _ = _parse_post_html(html)
        assert cats["Character"] == ["hatsune_miku"]

    def test_gelbooru_proper_returns_empty(self):
        """Gelbooru proper only has ? links with no tags= param."""
        html = '''
        <li class="tag-type-artist">
            <a href="index.php?page=wiki&amp;s=list&amp;search=ooiaooi">?</a>
        </li>
        <li class="tag-type-character">
            <a href="index.php?page=wiki&amp;s=list&amp;search=hatsune_miku">?</a>
        </li>
        '''
        cats, labels = _parse_post_html(html)
        assert cats == {}
        assert labels == {}

    def test_metadata_maps_to_meta(self):
        """class="tag-type-metadata" should map to label "Meta"."""
        html = '''
        <li class="tag-type-metadata">
            <a href="?">?</a>
            <a href="index.php?tags=highres">highres</a>
        </li>
        '''
        cats, labels = _parse_post_html(html)
        assert "Meta" in cats
        assert cats["Meta"] == ["highres"]

    def test_url_encoded_tag_names(self):
        """Tags with special chars get URL-encoded in the href."""
        html = '''
        <li class="tag-type-character">
            <a href="?">?</a>
            <a href="index.php?tags=miku_%28shinkalion%29">miku (shinkalion)</a>
        </li>
        '''
        cats, labels = _parse_post_html(html)
        assert cats["Character"] == ["miku_(shinkalion)"]

    def test_empty_html(self):
        cats, labels = _parse_post_html("")
        assert cats == {}
        assert labels == {}

    def test_no_tag_type_elements(self):
        html = '<div class="content"><p>Hello world</p></div>'
        cats, labels = _parse_post_html(html)
        assert cats == {}

    def test_unknown_type_class_ignored(self):
        """Tag types not in _LABEL_MAP are silently skipped."""
        html = '''
        <li class="tag-type-faults">
            <a href="?">?</a>
            <a href="index.php?tags=broken">broken</a>
        </li>
        '''
        cats, _ = _parse_post_html(html)
        assert cats == {}

    def test_multiple_tags_same_category(self):
        html = '''
        <li class="tag-type-character">
            <a href="?">?</a>
            <a href="index.php?tags=miku">miku</a>
        </li>
        <li class="tag-type-character">
            <a href="?">?</a>
            <a href="index.php?tags=rin">rin</a>
        </li>
        '''
        cats, _ = _parse_post_html(html)
        assert cats["Character"] == ["miku", "rin"]


# ---------------------------------------------------------------------------
# Tag API response parser tests (_parse_tag_response)
# ---------------------------------------------------------------------------

class TestParseTagResponse:

    def test_json_response(self):
        resp = FakeResponse(json.dumps({
            "@attributes": {"limit": 100, "offset": 0, "count": 2},
            "tag": [
                {"id": 1, "name": "hatsune_miku", "count": 12345, "type": 4, "ambiguous": 0},
                {"id": 2, "name": "1girl", "count": 9999, "type": 0, "ambiguous": 0},
            ]
        }))
        result = _parse_tag_response(resp)
        assert ("hatsune_miku", 4) in result
        assert ("1girl", 0) in result

    def test_xml_response(self):
        resp = FakeResponse(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<tags type="array">'
            '<tag type="4" count="12345" name="hatsune_miku" ambiguous="false" id="1"/>'
            '<tag type="0" count="9999" name="1girl" ambiguous="false" id="2"/>'
            '</tags>'
        )
        result = _parse_tag_response(resp)
        assert ("hatsune_miku", 4) in result
        assert ("1girl", 0) in result

    def test_empty_response(self):
        resp = FakeResponse("")
        assert _parse_tag_response(resp) == []

    def test_json_flat_list(self):
        """Some endpoints return a flat list instead of wrapping in {"tag": [...]}."""
        resp = FakeResponse(json.dumps([
            {"name": "solo", "type": 0, "count": 5000},
        ]))
        result = _parse_tag_response(resp)
        assert ("solo", 0) in result

    def test_malformed_xml(self):
        resp = FakeResponse("<broken><xml")
        result = _parse_tag_response(resp)
        assert result == []

    def test_malformed_json(self):
        resp = FakeResponse("{not valid json!!!")
        result = _parse_tag_response(resp)
        assert result == []


# ---------------------------------------------------------------------------
# Canonical ordering
# ---------------------------------------------------------------------------

class TestCanonicalOrder:

    def test_standard_order(self):
        cats = {
            "General": ["1girl"],
            "Artist": ["anmi"],
            "Meta": ["highres"],
            "Character": ["miku"],
            "Copyright": ["vocaloid"],
        }
        ordered = _canonical_order(cats)
        keys = list(ordered.keys())
        assert keys == ["Artist", "Character", "Copyright", "General", "Meta"]

    def test_species_position(self):
        cats = {
            "General": ["1girl"],
            "Species": ["cat_girl"],
            "Artist": ["anmi"],
        }
        ordered = _canonical_order(cats)
        keys = list(ordered.keys())
        assert keys == ["Artist", "Species", "General"]

    def test_unknown_category_appended(self):
        cats = {
            "Artist": ["anmi"],
            "Circle": ["some_circle"],
        }
        ordered = _canonical_order(cats)
        keys = list(ordered.keys())
        assert "Artist" in keys
        assert "Circle" in keys
        assert keys.index("Artist") < keys.index("Circle")

    def test_empty_dict(self):
        assert _canonical_order({}) == {}


# ---------------------------------------------------------------------------
# Cache compose (try_compose_from_cache)
# ---------------------------------------------------------------------------

class TestCacheCompose:

    def test_full_coverage_returns_true(self, tmp_db):
        client = FakeClient()
        fetcher = CategoryFetcher(client, tmp_db, site_id=1)
        tmp_db.set_tag_labels(1, {
            "1girl": "General",
            "hatsune_miku": "Character",
            "vocaloid": "Copyright",
        })
        post = FakePost(tags="1girl hatsune_miku vocaloid")
        result = fetcher.try_compose_from_cache(post)
        assert result is True
        assert "Character" in post.tag_categories
        assert "Copyright" in post.tag_categories
        assert "General" in post.tag_categories

    def test_partial_coverage_returns_false_but_populates(self, tmp_db):
        client = FakeClient()
        fetcher = CategoryFetcher(client, tmp_db, site_id=1)
        tmp_db.set_tag_labels(1, {"hatsune_miku": "Character"})
        post = FakePost(tags="1girl hatsune_miku vocaloid")
        result = fetcher.try_compose_from_cache(post)
        assert result is False
        # Still populated with what IS cached
        assert "Character" in post.tag_categories
        assert post.tag_categories["Character"] == ["hatsune_miku"]

    def test_zero_coverage_returns_false(self, tmp_db):
        client = FakeClient()
        fetcher = CategoryFetcher(client, tmp_db, site_id=1)
        post = FakePost(tags="1girl hatsune_miku vocaloid")
        result = fetcher.try_compose_from_cache(post)
        assert result is False
        assert post.tag_categories == {}

    def test_empty_tags_returns_true(self, tmp_db):
        client = FakeClient()
        fetcher = CategoryFetcher(client, tmp_db, site_id=1)
        post = FakePost(tags="")
        assert fetcher.try_compose_from_cache(post) is True

    def test_canonical_order_applied(self, tmp_db):
        client = FakeClient()
        fetcher = CategoryFetcher(client, tmp_db, site_id=1)
        tmp_db.set_tag_labels(1, {
            "1girl": "General",
            "anmi": "Artist",
            "miku": "Character",
        })
        post = FakePost(tags="1girl anmi miku")
        fetcher.try_compose_from_cache(post)
        keys = list(post.tag_categories.keys())
        assert keys == ["Artist", "Character", "General"]

    def test_per_site_isolation(self, tmp_db):
        client = FakeClient()
        fetcher_1 = CategoryFetcher(client, tmp_db, site_id=1)
        fetcher_2 = CategoryFetcher(client, tmp_db, site_id=2)
        tmp_db.set_tag_labels(1, {"miku": "Character"})
        # Site 2 has nothing cached
        post = FakePost(tags="miku")
        assert fetcher_1.try_compose_from_cache(post) is True
        post2 = FakePost(tags="miku")
        assert fetcher_2.try_compose_from_cache(post2) is False


# ---------------------------------------------------------------------------
# Probe persistence
# ---------------------------------------------------------------------------

class TestProbePersistence:

    def test_initial_state_none(self, tmp_db):
        fetcher = CategoryFetcher(FakeClient(), tmp_db, site_id=1)
        assert fetcher._batch_api_works is None

    def test_save_true_persists(self, tmp_db):
        fetcher = CategoryFetcher(FakeClient(), tmp_db, site_id=1)
        fetcher._save_probe_result(True)
        fetcher2 = CategoryFetcher(FakeClient(), tmp_db, site_id=1)
        assert fetcher2._batch_api_works is True

    def test_save_false_persists(self, tmp_db):
        fetcher = CategoryFetcher(FakeClient(), tmp_db, site_id=1)
        fetcher._save_probe_result(False)
        fetcher2 = CategoryFetcher(FakeClient(), tmp_db, site_id=1)
        assert fetcher2._batch_api_works is False

    def test_per_site_isolation(self, tmp_db):
        f1 = CategoryFetcher(FakeClient(), tmp_db, site_id=1)
        f1._save_probe_result(True)
        f2 = CategoryFetcher(FakeClient(), tmp_db, site_id=2)
        f2._save_probe_result(False)
        assert CategoryFetcher(FakeClient(), tmp_db, site_id=1)._batch_api_works is True
        assert CategoryFetcher(FakeClient(), tmp_db, site_id=2)._batch_api_works is False

    def test_clear_tag_cache_wipes_probe(self, tmp_db):
        fetcher = CategoryFetcher(FakeClient(), tmp_db, site_id=1)
        fetcher._save_probe_result(True)
        tmp_db.clear_tag_cache(site_id=1)
        fetcher2 = CategoryFetcher(FakeClient(), tmp_db, site_id=1)
        assert fetcher2._batch_api_works is None


# ---------------------------------------------------------------------------
# Batch API availability check
# ---------------------------------------------------------------------------

class TestBatchApiAvailable:

    def test_available_with_url_and_auth(self, tmp_db):
        client = FakeClient(tag_api_url="http://example.com", api_key="k", api_user="u")
        fetcher = CategoryFetcher(client, tmp_db, site_id=1)
        assert fetcher._batch_api_available() is True

    def test_not_available_without_url(self, tmp_db):
        client = FakeClient(api_key="k", api_user="u")
        fetcher = CategoryFetcher(client, tmp_db, site_id=1)
        assert fetcher._batch_api_available() is False

    def test_not_available_without_auth(self, tmp_db):
        client = FakeClient(tag_api_url="http://example.com")
        fetcher = CategoryFetcher(client, tmp_db, site_id=1)
        assert fetcher._batch_api_available() is False


# ---------------------------------------------------------------------------
# Label map and type map coverage
# ---------------------------------------------------------------------------

class TestMaps:

    def test_label_map_covers_common_types(self):
        for name in ["general", "artist", "character", "copyright", "metadata", "meta", "species"]:
            assert name in _LABEL_MAP

    def test_gelbooru_type_map_covers_standard_codes(self):
        assert _GELBOORU_TYPE_MAP[0] == "General"
        assert _GELBOORU_TYPE_MAP[1] == "Artist"
        assert _GELBOORU_TYPE_MAP[3] == "Copyright"
        assert _GELBOORU_TYPE_MAP[4] == "Character"
        assert _GELBOORU_TYPE_MAP[5] == "Meta"
        assert 2 not in _GELBOORU_TYPE_MAP  # Deprecated intentionally omitted
