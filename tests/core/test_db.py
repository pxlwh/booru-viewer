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

import os
import sys

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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod check")
def test_db_file_chmod_600(tmp_db):
    """Audit finding #4: the SQLite file must be 0o600 on POSIX so the
    plaintext api_key/api_user columns aren't readable by other local
    users on shared workstations."""
    # The conn property triggers _restrict_perms() the first time it's
    # accessed; tmp_db calls it via add_site/etc., but a defensive
    # access here makes the assertion order-independent.
    _ = tmp_db.conn
    mode = os.stat(tmp_db._path).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod check")
def test_db_wal_sidecar_chmod_600(tmp_db):
    """The -wal sidecar created by PRAGMA journal_mode=WAL must also
    be 0o600. It carries in-flight transactions including the most
    recent api_key writes — same exposure as the main DB file."""
    # Force a write so the WAL file actually exists.
    tmp_db.add_site("test", "http://example.test", "danbooru")
    # Re-trigger the chmod pass now that the sidecar exists.
    tmp_db._restrict_perms()
    wal = type(tmp_db._path)(str(tmp_db._path) + "-wal")
    if wal.exists():
        mode = os.stat(wal).st_mode & 0o777
        assert mode == 0o600, f"expected 0o600 on WAL sidecar, got {oct(mode)}"


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


# -- delete_site cascading cleanup --

def _seed_site(db, name, site_id_out=None):
    """Create a site and populate all child tables for it."""
    site = db.add_site(name, f"http://{name}.test", "danbooru")
    db.add_bookmark(
        site_id=site.id, post_id=1, file_url=f"http://{name}.test/1.jpg",
        preview_url=None, tags="test",
    )
    db.add_search_history("test query", site_id=site.id)
    db.add_saved_search("my search", "saved query", site_id=site.id)
    db.set_tag_labels(site.id, {"artist:bob": "artist"})
    return site


def _count_rows(db, table, site_id, *, id_col="site_id"):
    """Count rows in *table* belonging to *site_id*."""
    return db.conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {id_col} = ?", (site_id,)
    ).fetchone()[0]


def test_delete_site_cascades_all_related_rows(tmp_db):
    """Deleting a site must remove rows from all five related tables."""
    site = _seed_site(tmp_db, "doomed")
    tmp_db.delete_site(site.id)
    assert _count_rows(tmp_db, "sites", site.id, id_col="id") == 0
    assert _count_rows(tmp_db, "favorites", site.id) == 0
    assert _count_rows(tmp_db, "tag_types", site.id) == 0
    assert _count_rows(tmp_db, "search_history", site.id) == 0
    assert _count_rows(tmp_db, "saved_searches", site.id) == 0


def test_delete_site_does_not_affect_other_sites(tmp_db):
    """Deleting site A must leave site B's rows in every table untouched."""
    site_a = _seed_site(tmp_db, "site-a")
    site_b = _seed_site(tmp_db, "site-b")

    before = {
        t: _count_rows(tmp_db, t, site_b.id, id_col="id" if t == "sites" else "site_id")
        for t in ("sites", "favorites", "tag_types", "search_history", "saved_searches")
    }

    tmp_db.delete_site(site_a.id)

    for table, expected in before.items():
        id_col = "id" if table == "sites" else "site_id"
        assert _count_rows(tmp_db, table, site_b.id, id_col=id_col) == expected, (
            f"{table} rows for site B changed after deleting site A"
        )


# -- reconcile_library_meta --

def test_reconcile_library_meta_removes_orphans(tmp_db, tmp_library):
    """Rows whose files are missing on disk are deleted; present files kept."""
    (tmp_library / "12345.jpg").write_bytes(b"\xff")
    tmp_db.save_library_meta(post_id=12345, tags="test", filename="12345.jpg")
    tmp_db.save_library_meta(post_id=99999, tags="orphan", filename="99999.jpg")

    removed = tmp_db.reconcile_library_meta()

    assert removed == 1
    assert tmp_db.is_post_in_library(12345) is True
    assert tmp_db.is_post_in_library(99999) is False


def test_reconcile_library_meta_skips_empty_dir(tmp_db, tmp_library):
    """An empty library dir signals a possible unmounted drive — refuse to
    reconcile and leave orphan rows intact."""
    tmp_db.save_library_meta(post_id=12345, tags="test", filename="12345.jpg")

    removed = tmp_db.reconcile_library_meta()

    assert removed == 0
    assert tmp_db.is_post_in_library(12345) is True


# -- tag cache pruning --

def test_prune_tag_cache(tmp_db):
    """After inserting more tags than the cap, only the newest entries survive."""
    from booru_viewer.core.db import Database

    original_cap = Database._TAG_CACHE_MAX_ROWS
    try:
        Database._TAG_CACHE_MAX_ROWS = 5

        site = tmp_db.add_site("test", "http://test.test", "danbooru")

        # Insert 8 rows with explicit, distinct fetched_at timestamps so
        # pruning order is deterministic.
        with tmp_db._write():
            for i in range(8):
                tmp_db.conn.execute(
                    "INSERT OR REPLACE INTO tag_types "
                    "(site_id, name, label, fetched_at) VALUES (?, ?, ?, ?)",
                    (site.id, f"tag_{i}", "general", f"2025-01-01T00:00:{i:02d}Z"),
                )
            tmp_db._prune_tag_cache()

        count = tmp_db.conn.execute("SELECT COUNT(*) FROM tag_types").fetchone()[0]
        assert count == 5

        surviving = {
            r["name"]
            for r in tmp_db.conn.execute("SELECT name FROM tag_types").fetchall()
        }
        # The 3 oldest (tag_0, tag_1, tag_2) should have been pruned
        assert surviving == {"tag_3", "tag_4", "tag_5", "tag_6", "tag_7"}
    finally:
        Database._TAG_CACHE_MAX_ROWS = original_cap
