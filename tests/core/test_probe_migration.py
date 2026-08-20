"""Tests for the user_version 1 migration that clears poisoned
batch-tag-API probe negatives.

Before v0.2.8 a transient network error could persist
``__batch_api_probe__ = "false"`` for a site permanently, and
``CategoryFetcher._do_ensure`` never re-probes a False. v0.2.8 stopped
new poisoning but left existing rows stuck; this migration clears them
once on upgrade.

Pure SQLite — no Qt, no network.
"""

from __future__ import annotations

import sqlite3

import pytest

from booru_viewer.core.db import BATCH_API_PROBE_KEY, Database


SITE = 1


def _seed(path, labels: dict[str, str], user_version: int = 0) -> None:
    """Write tag_types rows directly, bypassing Database, so the test
    can simulate a pre-migration file at a chosen user_version."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tag_types (
            site_id    INTEGER NOT NULL,
            name       TEXT NOT NULL,
            label      TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (site_id, name)
        )
    """)
    for name, label in labels.items():
        conn.execute(
            "INSERT OR REPLACE INTO tag_types VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
            (SITE, name, label),
        )
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.commit()
    conn.close()


def _labels(db) -> dict[str, str]:
    rows = db.conn.execute(
        "SELECT name, label FROM tag_types WHERE site_id = ?", (SITE,)
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def test_poisoned_false_sentinel_is_dropped(tmp_path):
    p = tmp_path / "poisoned.db"
    _seed(p, {BATCH_API_PROBE_KEY: "false"})
    db = Database(p)
    assert BATCH_API_PROBE_KEY not in _labels(db)


def test_true_sentinel_survives(tmp_path):
    """A confirmed-working batch API is correct and cheap to keep."""
    p = tmp_path / "good.db"
    _seed(p, {BATCH_API_PROBE_KEY: "true"})
    db = Database(p)
    assert _labels(db)[BATCH_API_PROBE_KEY] == "true"


def test_real_tag_rows_are_untouched(tmp_path):
    """The DELETE is keyed on the sentinel name, not on the label."""
    p = tmp_path / "tags.db"
    _seed(p, {
        BATCH_API_PROBE_KEY: "false",
        "hatsune_miku": "Character",
        "false": "Artist",          # a tag literally named "false"
        "some_tag": "false",        # a tag whose label is "false"
    })
    db = Database(p)
    got = _labels(db)
    assert BATCH_API_PROBE_KEY not in got
    assert got["hatsune_miku"] == "Character"
    assert got["false"] == "Artist"
    assert got["some_tag"] == "false"


def test_migration_marks_user_version(tmp_path):
    p = tmp_path / "ver.db"
    _seed(p, {BATCH_API_PROBE_KEY: "false"})
    db = Database(p)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] >= 1


def test_probe_written_after_migration_is_not_wiped(tmp_path):
    """The gate is what stops a genuinely broken API from re-probing on
    every launch: delete, probe, write "false", delete, forever."""
    p = tmp_path / "loop.db"
    _seed(p, {BATCH_API_PROBE_KEY: "false"})
    db = Database(p)
    assert BATCH_API_PROBE_KEY not in _labels(db)

    # A real probe now concludes the API is broken and persists that.
    db.set_tag_labels(SITE, {BATCH_API_PROBE_KEY: "false"})
    db.close()

    reopened = Database(p)
    assert _labels(reopened)[BATCH_API_PROBE_KEY] == "false"


def test_fresh_database_is_at_version_1(tmp_path):
    """A new DB should not run the cleanup again on its second open."""
    p = tmp_path / "fresh.db"
    db = Database(p)
    db.conn  # force lazy init
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] >= 1
