"""Tests for `booru_viewer.core.db` — folder name validation, INSERT OR
IGNORE collision handling, and LIKE escaping.

These tests lock in the `54ccc40` security/correctness fixes:
- `_validate_folder_name` rejects path-traversal shapes before they hit the
  filesystem in `saved_folder_dir`
- `add_bookmark` re-SELECTs the actual row id after an INSERT OR IGNORE
  collision so the returned `Bookmark.id` is never the bogus 0 that broke
  `update_bookmark_cache_path`
- `get_bookmarks` escapes the SQL LIKE wildcards `_` and `%` so a search for
  `cat_ear` doesn't bleed into `catear` / `catXear`
"""

from __future__ import annotations

import pytest

from booru_viewer.core.db import _validate_folder_name


# -- _validate_folder_name --

def test_validate_folder_name_rejects_traversal():
    """Every shape that could escape the saved-images dir or hit a hidden
    file must raise ValueError. One assertion per rejection rule so a
    failure points at the exact case."""
    with pytest.raises(ValueError):
        _validate_folder_name("")          # empty
    with pytest.raises(ValueError):
        _validate_folder_name("..")        # dotdot literal
    with pytest.raises(ValueError):
        _validate_folder_name(".")         # dot literal
    with pytest.raises(ValueError):
        _validate_folder_name("/foo")      # forward slash
    with pytest.raises(ValueError):
        _validate_folder_name("foo/bar")   # embedded forward slash
    with pytest.raises(ValueError):
        _validate_folder_name("\\foo")     # backslash
    with pytest.raises(ValueError):
        _validate_folder_name(".hidden")   # leading dot
    with pytest.raises(ValueError):
        _validate_folder_name("~user")     # leading tilde


def test_validate_folder_name_accepts_unicode_and_punctuation():
    """Common real-world folder names must pass through unchanged. The
    guard is meant to block escape shapes, not normal naming."""
    assert _validate_folder_name("miku(lewd)") == "miku(lewd)"
    assert _validate_folder_name("cat ear") == "cat ear"
    assert _validate_folder_name("日本語") == "日本語"
    assert _validate_folder_name("foo-bar") == "foo-bar"
    assert _validate_folder_name("foo.bar") == "foo.bar"  # dot OK if not leading


# -- add_bookmark INSERT OR IGNORE collision --

def test_add_bookmark_collision_returns_existing_id(tmp_db):
    """Calling `add_bookmark` twice with the same (site_id, post_id) must
    return the same row id on the second call, not the stale `lastrowid`
    of 0 that INSERT OR IGNORE leaves behind. Without the re-SELECT fix,
    any downstream `update_bookmark_cache_path(id=0, ...)` silently
    no-ops, breaking the cache-path linkage."""
    site = tmp_db.add_site("test", "http://example.test", "danbooru")
    bm1 = tmp_db.add_bookmark(
        site_id=site.id, post_id=42, file_url="http://example.test/42.jpg",
        preview_url=None, tags="cat",
    )
    bm2 = tmp_db.add_bookmark(
        site_id=site.id, post_id=42, file_url="http://example.test/42.jpg",
        preview_url=None, tags="cat",
    )
    assert bm1.id != 0
    assert bm2.id == bm1.id


# -- get_bookmarks LIKE escaping --

def test_get_bookmarks_like_escaping(tmp_db):
    """A search for the literal tag `cat_ear` must NOT match `catear` or
    `catXear`. SQLite's LIKE treats `_` as a single-char wildcard unless
    explicitly escaped — without `ESCAPE '\\\\'` the search would return
    all three rows."""
    site = tmp_db.add_site("test", "http://example.test", "danbooru")
    tmp_db.add_bookmark(
        site_id=site.id, post_id=1, file_url="http://example.test/1.jpg",
        preview_url=None, tags="cat_ear",
    )
    tmp_db.add_bookmark(
        site_id=site.id, post_id=2, file_url="http://example.test/2.jpg",
        preview_url=None, tags="catear",
    )
    tmp_db.add_bookmark(
        site_id=site.id, post_id=3, file_url="http://example.test/3.jpg",
        preview_url=None, tags="catXear",
    )
    results = tmp_db.get_bookmarks(search="cat_ear")
    tags_returned = {b.tags for b in results}
    assert tags_returned == {"cat_ear"}
