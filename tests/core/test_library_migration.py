"""The user_version 2 migration: library_meta gains a site_id.

Builds a synthetic pre-v2 database by hand, opens it through Database,
and checks the migration resolved every row without losing data.
"""

from __future__ import annotations

import sqlite3

import pytest

from booru_viewer.core.db import Database


def _make_v1_db(path, rows, sites):
    """A pre-migration database: post_id-keyed library_meta, user_version 1."""
    c = sqlite3.connect(str(path))
    c.execute("""
        CREATE TABLE library_meta (
            post_id        INTEGER PRIMARY KEY,
            tags           TEXT NOT NULL DEFAULT '',
            tag_categories TEXT DEFAULT '',
            score          INTEGER DEFAULT 0,
            rating         TEXT,
            source         TEXT,
            file_url       TEXT,
            saved_at       TEXT,
            filename       TEXT NOT NULL DEFAULT ''
        )
    """)
    c.execute("""
        CREATE TABLE sites (
            id INTEGER PRIMARY KEY, name TEXT, url TEXT, api_type TEXT,
            api_key TEXT, api_user TEXT, added_at TEXT
        )
    """)
    for sid, name, url in sites:
        c.execute("INSERT INTO sites (id, name, url, api_type) VALUES (?,?,?,'gelbooru')",
                  (sid, name, url))
    for pid, url, tags in rows:
        c.execute(
            "INSERT INTO library_meta (post_id, file_url, tags, score, saved_at, filename) "
            "VALUES (?,?,?,?,?,?)",
            (pid, url, tags, 7, "2026-01-01T00:00:00Z", f"{pid}.jpg"),
        )
    c.execute("PRAGMA user_version = 1")
    c.commit()
    c.close()


SITES = [(5, "Gelbooru", "https://gelbooru.com"), (10, "Rule34", "https://rule34.xxx")]


def test_every_row_gains_a_site(tmp_path):
    p = tmp_path / "v1.db"
    _make_v1_db(p, [
        (1, "https://img2.gelbooru.com/a.jpg", "cat"),
        (2, "https://api-cdn.rule34.xxx/b.jpg", "dog"),
    ], SITES)
    db = Database(p)
    rows = db.conn.execute("SELECT site_id, post_id FROM library_meta ORDER BY post_id").fetchall()
    assert [(r["site_id"], r["post_id"]) for r in rows] == [(5, 1), (10, 2)]


def test_row_count_is_preserved(tmp_path):
    p = tmp_path / "v1.db"
    _make_v1_db(p, [(i, "https://img2.gelbooru.com/a.jpg", "cat") for i in range(1, 21)], SITES)
    db = Database(p)
    assert db.conn.execute("SELECT count(*) n FROM library_meta").fetchone()["n"] == 20


def test_other_columns_survive(tmp_path):
    p = tmp_path / "v1.db"
    _make_v1_db(p, [(1, "https://img2.gelbooru.com/a.jpg", "cat dog")], SITES)
    db = Database(p)
    r = db.conn.execute("SELECT * FROM library_meta").fetchone()
    assert r["tags"] == "cat dog"
    assert r["score"] == 7
    assert r["filename"] == "1.jpg"
    assert r["saved_at"] == "2026-01-01T00:00:00Z"


def test_unresolvable_row_gets_the_sentinel(tmp_path):
    p = tmp_path / "v1.db"
    _make_v1_db(p, [(1, "", "cat")], SITES)
    db = Database(p)
    assert db.conn.execute("SELECT site_id FROM library_meta").fetchone()["site_id"] == 0


def test_composite_key_rejects_duplicates(tmp_path):
    p = tmp_path / "v1.db"
    _make_v1_db(p, [(1, "https://img2.gelbooru.com/a.jpg", "cat")], SITES)
    db = Database(p)
    with pytest.raises(sqlite3.IntegrityError):
        with db.conn:
            db.conn.execute(
                "INSERT INTO library_meta (site_id, post_id) VALUES (5, 1)")


def test_same_post_id_from_two_sites_coexist(tmp_path):
    """The whole point: this INSERT was impossible before."""
    p = tmp_path / "v1.db"
    _make_v1_db(p, [(1, "https://img2.gelbooru.com/a.jpg", "cat")], SITES)
    db = Database(p)
    with db.conn:
        db.conn.execute("INSERT INTO library_meta (site_id, post_id) VALUES (10, 1)")
    assert db.conn.execute("SELECT count(*) n FROM library_meta").fetchone()["n"] == 2


def test_user_version_is_bumped(tmp_path):
    p = tmp_path / "v1.db"
    _make_v1_db(p, [(1, "https://img2.gelbooru.com/a.jpg", "cat")], SITES)
    db = Database(p)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] >= 2


def test_second_open_is_a_noop(tmp_path):
    """Re-running must not rebuild or duplicate."""
    p = tmp_path / "v1.db"
    _make_v1_db(p, [(1, "https://img2.gelbooru.com/a.jpg", "cat")], SITES)
    db = Database(p); db.conn; db.close()
    db2 = Database(p)
    assert db2.conn.execute("SELECT count(*) n FROM library_meta").fetchone()["n"] == 1


def test_backup_file_is_created(tmp_path):
    p = tmp_path / "v1.db"
    _make_v1_db(p, [(1, "https://img2.gelbooru.com/a.jpg", "cat")], SITES)
    Database(p).conn
    assert (tmp_path / "v1.db.pre-v2.bak").exists()


def test_existing_backup_is_not_overwritten(tmp_path):
    """A retry after a failed upgrade must not clobber the good copy."""
    p = tmp_path / "v1.db"
    _make_v1_db(p, [(1, "https://img2.gelbooru.com/a.jpg", "cat")], SITES)
    bak = tmp_path / "v1.db.pre-v2.bak"
    bak.write_text("sentinel")
    Database(p).conn
    assert bak.read_text() == "sentinel"


def test_fresh_database_is_at_version_2(tmp_path):
    db = Database(tmp_path / "new.db")
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] >= 2
