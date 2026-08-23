"""library_meta accessors are keyed on (site_id, post_id).

The regression these lock down: the same post id saved from two boorus
must be two independent rows, not one overwriting the other.
"""

from __future__ import annotations

import pytest

from booru_viewer.core.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def test_same_post_id_from_two_sites_are_separate_rows(db):
    db.save_library_meta(5, 12345, tags="cat", filename="a.jpg")
    db.save_library_meta(8, 12345, tags="dog", filename="b.jpg")
    assert db.get_library_meta(5, 12345)["tags"] == "cat"
    assert db.get_library_meta(8, 12345)["tags"] == "dog"


def test_resave_same_key_updates_in_place(db):
    db.save_library_meta(5, 1, tags="cat")
    db.save_library_meta(5, 1, tags="cat dog")
    assert db.get_library_meta(5, 1)["tags"] == "cat dog"
    rows = db.conn.execute("SELECT count(*) n FROM library_meta").fetchone()["n"]
    assert rows == 1


def test_get_missing_key_returns_none(db):
    db.save_library_meta(5, 1)
    assert db.get_library_meta(8, 1) is None


def test_remove_only_affects_the_named_site(db):
    db.save_library_meta(5, 1)
    db.save_library_meta(8, 1)
    db.remove_library_meta(5, 1)
    assert db.get_library_meta(5, 1) is None
    assert db.get_library_meta(8, 1) is not None


def test_search_returns_site_post_tuples(db):
    db.save_library_meta(5, 1, tags="cat")
    db.save_library_meta(8, 2, tags="cat")
    assert db.search_library_meta("cat") == {(5, 1), (8, 2)}


def test_key_by_filename_returns_both_parts(db):
    db.save_library_meta(5, 12345, filename="pic.jpg")
    assert db.get_library_key_by_filename("pic.jpg") == (5, 12345)


def test_key_by_filename_missing_returns_none(db):
    assert db.get_library_key_by_filename("nope.jpg") is None
