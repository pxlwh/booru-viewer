"""Recover which booru a saved file came from, using its URL.

Pure and offline on purpose: this runs inside a schema migration on cold
start, where the network may not exist.
"""

from __future__ import annotations

import pytest

from booru_viewer.core.db import resolve_site_id


SITES = [
    (5, "https://gelbooru.com"),
    (6, "https://safebooru.donmai.us"),
    (8, "https://danbooru.donmai.us"),
    (9, "https://safebooru.org"),
    (10, "https://rule34.xxx"),
]


def test_exact_host_match():
    assert resolve_site_id("https://safebooru.org/images/1/a.jpg", SITES) == 9


def test_cdn_subdomain_matches_its_site():
    """CDNs do not live at the site hostname."""
    assert resolve_site_id("https://img2.gelbooru.com/images/1/a.jpg", SITES) == 5


def test_second_cdn_host_for_the_same_site():
    assert resolve_site_id("https://video-cdn4.gelbooru.com/images/1/a.mp4", SITES) == 5


def test_sibling_cdn_ties_break_to_lowest_site_id():
    """cdn.donmai.us serves both donmai sites; neither host is a subdomain
    of the other, so both match on the registrable domain. Siblings share
    post ids, so either answer names the same image."""
    assert resolve_site_id("https://cdn.donmai.us/original/f7/e9/x.jpg", SITES) == 6


def test_rule34_cdn():
    assert resolve_site_id("https://api-cdn.rule34.xxx/images/1/a.jpg", SITES) == 10


def test_unknown_host_gives_sentinel():
    assert resolve_site_id("https://example.com/a.jpg", SITES) == 0


def test_empty_url_gives_sentinel():
    assert resolve_site_id("", SITES) == 0


def test_none_url_gives_sentinel():
    assert resolve_site_id(None, SITES) == 0


def test_malformed_url_gives_sentinel():
    assert resolve_site_id("not a url at all", SITES) == 0


def test_empty_site_list_gives_sentinel():
    assert resolve_site_id("https://gelbooru.com/a.jpg", []) == 0


def test_result_is_deterministic():
    """Same inputs, same answer, regardless of site list order."""
    a = resolve_site_id("https://cdn.donmai.us/x.jpg", SITES)
    b = resolve_site_id("https://cdn.donmai.us/x.jpg", list(reversed(SITES)))
    assert a == b == 6


def test_host_case_is_ignored():
    assert resolve_site_id("https://IMG2.GELBOORU.COM/a.jpg", SITES) == 5
