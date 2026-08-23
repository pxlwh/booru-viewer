"""The saved-dot lookup must be site-aware.

Keyed on post_id alone, saving danbooru 12345 made gelbooru's 12345 show
a saved-dot for a file that was never saved from there.
"""

from __future__ import annotations

import pytest

from booru_viewer.core.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def test_returns_site_post_tuples(db):
    db.save_library_meta(5, 12345)
    assert db.get_saved_post_ids() == {(5, 12345)}


def test_a_save_on_one_site_does_not_mark_another(db):
    db.save_library_meta(5, 12345)
    saved = db.get_saved_post_ids()
    assert (5, 12345) in saved
    assert (8, 12345) not in saved


def test_empty_library_is_an_empty_set(db):
    assert db.get_saved_post_ids() == set()


def test_both_sites_present_when_both_saved(db):
    db.save_library_meta(5, 12345)
    db.save_library_meta(8, 12345)
    assert db.get_saved_post_ids() == {(5, 12345), (8, 12345)}


# -- site-filtered library membership (multi-search) --

def test_is_post_in_library_filters_by_site(db):
    db.save_library_meta(5, 12345)
    assert db.is_post_in_library(12345) is True
    assert db.is_post_in_library(12345, site_id=5) is True
    assert db.is_post_in_library(12345, site_id=8) is False


def test_sentinel_rows_count_for_any_site(db):
    """A legacy site-0 row's true site is unknown; treating it as saved
    everywhere matches pre-multi behaviour instead of hiding the dot."""
    db.save_library_meta(0, 777)
    assert db.is_post_in_library(777, site_id=5) is True


def test_get_bookmarked_keys_returns_site_post_tuples(db):
    site_a = db.add_site("a", "http://a.test", "danbooru")
    site_b = db.add_site("b", "http://b.test", "danbooru")
    db.add_bookmark(site_id=site_a.id, post_id=11, file_url="https://x/a.jpg", preview_url=None, tags="")
    db.add_bookmark(site_id=site_b.id, post_id=11, file_url="https://x/b.jpg", preview_url=None, tags="")
    assert db.get_bookmarked_keys() == {(site_a.id, 11), (site_b.id, 11)}


def test_get_bookmarked_keys_empty(db):
    assert db.get_bookmarked_keys() == set()
