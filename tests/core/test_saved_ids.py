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
