"""SQLite database for bookmarks, sites, and cache metadata."""

from __future__ import annotations

import os
import sqlite3
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import db_path


def _validate_folder_name(name: str) -> str:
    """Reject folder names that could break out of the saved-images dir.

    Folder names hit the filesystem in `core.config.saved_folder_dir` (joined
    with `saved_dir()` and `mkdir`'d). Without this guard, an attacker — or a
    user pasting nonsense — could create / delete files anywhere by passing
    `..` segments, an absolute path, or an OS-native separator. We refuse
    those at write time so the DB never stores a poisoned name in the first
    place.

    Permits anything else (Unicode, spaces, parentheses, hyphens) so existing
    folders like `miku(lewd)` keep working.
    """
    if not name:
        raise ValueError("Folder name cannot be empty")
    if name in (".", ".."):
        raise ValueError(f"Invalid folder name: {name!r}")
    if "/" in name or "\\" in name or os.sep in name:
        raise ValueError(f"Folder name may not contain path separators: {name!r}")
    if name.startswith(".") or name.startswith("~"):
        raise ValueError(f"Folder name may not start with {name[0]!r}: {name!r}")
    # Reject any embedded `..` segment (e.g. `foo..bar` is fine, but `..` alone
    # is already caught above; this catches `..` inside slash-rejected paths
    # if someone tries to be clever — defensive belt for the suspenders).
    if ".." in name.split(os.sep):
        raise ValueError(f"Invalid folder name: {name!r}")
    return name

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    url         TEXT    NOT NULL,
    api_type    TEXT    NOT NULL,  -- danbooru | gelbooru | moebooru
    api_key     TEXT,
    api_user    TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    added_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS favorites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id     INTEGER NOT NULL REFERENCES sites(id),
    post_id     INTEGER NOT NULL,
    file_url    TEXT    NOT NULL,
    preview_url TEXT,
    tags        TEXT    NOT NULL DEFAULT '',
    rating      TEXT,
    score       INTEGER,
    source      TEXT,
    cached_path TEXT,
    folder      TEXT,
    favorited_at TEXT   NOT NULL,
    UNIQUE(site_id, post_id)
);

CREATE INDEX IF NOT EXISTS idx_favorites_tags ON favorites(tags);
CREATE INDEX IF NOT EXISTS idx_favorites_site ON favorites(site_id);
CREATE INDEX IF NOT EXISTS idx_favorites_folder ON favorites(folder);
CREATE INDEX IF NOT EXISTS idx_favorites_favorited_at ON favorites(favorited_at DESC);

CREATE TABLE IF NOT EXISTS favorite_folders (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS blacklisted_tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tag  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS blacklisted_posts (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    url  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS library_meta (
    post_id        INTEGER PRIMARY KEY,
    tags           TEXT NOT NULL DEFAULT '',
    tag_categories TEXT DEFAULT '',
    score          INTEGER DEFAULT 0,
    rating         TEXT,
    source         TEXT,
    file_url       TEXT,
    saved_at       TEXT,
    filename       TEXT NOT NULL DEFAULT ''
);
-- The idx_library_meta_filename index is created in _migrate(), not here.
-- _SCHEMA runs before _migrate against legacy databases that don't yet have
-- the filename column, so creating the index here would fail with "no such
-- column" before the migration could ALTER the column in.

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    query     TEXT NOT NULL,
    site_id   INTEGER,
    searched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_searches (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL,
    query TEXT NOT NULL,
    site_id INTEGER
);

CREATE TABLE IF NOT EXISTS tag_types (
    site_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    label      TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (site_id, name)
);
"""

_DEFAULTS = {
    "max_cache_mb": "2048",
    "max_thumb_cache_mb": "500",
    "auto_evict": "1",
    "thumbnail_size": "180",
    "page_size": "40",
    "default_rating": "all",
    "default_score": "0",
    "confirm_favorites": "0",
    "preload_thumbnails": "1",
    "file_dialog_platform": "qt",
    "blacklist_enabled": "1",
    "prefetch_adjacent": "0",
    "clear_cache_on_exit": "0",
    "slideshow_monitor": "",
    "library_dir": "",
    "infinite_scroll": "0",
    "library_filename_template": "",
}


@dataclass
class Site:
    id: int
    name: str
    url: str
    api_type: str
    api_key: str | None = None
    api_user: str | None = None
    enabled: bool = True


@dataclass
class Bookmark:
    id: int
    site_id: int
    post_id: int
    file_url: str
    preview_url: str | None
    tags: str
    rating: str | None
    score: int | None
    source: str | None
    cached_path: str | None
    folder: str | None
    bookmarked_at: str
    tag_categories: dict = field(default_factory=dict)


# Back-compat alias — will be removed in a future version.
Favorite = Bookmark


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or db_path()
        self._conn: sqlite3.Connection | None = None
        # Single writer lock for the connection. Reads happen concurrently
        # under WAL without contention; writes from multiple threads (Qt
        # main + the persistent asyncio loop thread) need explicit
        # serialization to avoid interleaved multi-statement methods.
        # RLock so a writing method can call another writing method on the
        # same thread without self-deadlocking.
        self._write_lock = threading.RLock()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._migrate()
        return self._conn

    @contextmanager
    def _write(self):
        """Context manager for write methods.

        Acquires the write lock for cross-thread serialization, then enters
        sqlite3's connection context manager (which BEGINs and COMMIT/ROLLBACKs
        atomically). Use this in place of `with self.conn:` whenever a method
        writes — it composes the two guarantees we want:
          1. Multi-statement atomicity (sqlite3 handles)
          2. Cross-thread write serialization (the RLock handles)
        Reads do not need this — they go through `self.conn.execute(...)` directly
        and rely on WAL for concurrent-reader isolation.
        """
        with self._write_lock:
            with self.conn:
                yield self.conn

    def _migrate(self) -> None:
        """Add columns that may not exist in older databases.

        All ALTERs are wrapped in a single transaction so a crash partway
        through can't leave the schema half-migrated. Note: this runs from
        the `conn` property's lazy init, where `_write_lock` exists but the
        connection is being built — we only need to serialize writes via
        the lock; the connection context manager handles atomicity.
        """
        with self._write_lock:
            with self._conn:
                cur = self._conn.execute("PRAGMA table_info(favorites)")
                cols = {row[1] for row in cur.fetchall()}
                if "folder" not in cols:
                    self._conn.execute("ALTER TABLE favorites ADD COLUMN folder TEXT")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_favorites_folder ON favorites(folder)")
                # Add tag_categories to library_meta if missing
                tables = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if "library_meta" in tables:
                    cur = self._conn.execute("PRAGMA table_info(library_meta)")
                    meta_cols = {row[1] for row in cur.fetchall()}
                    if "tag_categories" not in meta_cols:
                        self._conn.execute("ALTER TABLE library_meta ADD COLUMN tag_categories TEXT DEFAULT ''")
                    # Add filename column. Empty-string default acts as the
                    # "unknown" sentinel for legacy v0.2.3 rows whose on-disk
                    # filenames are digit stems — library scan code falls
                    # back to int(stem) when filename is empty.
                    if "filename" not in meta_cols:
                        self._conn.execute("ALTER TABLE library_meta ADD COLUMN filename TEXT NOT NULL DEFAULT ''")
                    self._conn.execute("CREATE INDEX IF NOT EXISTS idx_library_meta_filename ON library_meta(filename)")
                # Add tag_categories to favorites if missing
                if "tag_categories" not in cols:
                    self._conn.execute("ALTER TABLE favorites ADD COLUMN tag_categories TEXT DEFAULT ''")
                # Tag-type cache for boorus that don't return
                # categorized tags inline (Gelbooru-shape, Moebooru).
                # Per-site keying so forks don't cross-contaminate.
                # Uses string labels ("Artist", "Character", ...)
                # instead of integer codes — the labels come from
                # the HTML class names directly.
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS tag_types (
                        site_id    INTEGER NOT NULL,
                        name       TEXT NOT NULL,
                        label      TEXT NOT NULL,
                        fetched_at TEXT NOT NULL,
                        PRIMARY KEY (site_id, name)
                    )
                """)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # -- Sites --

    def add_site(
        self,
        name: str,
        url: str,
        api_type: str,
        api_key: str | None = None,
        api_user: str | None = None,
    ) -> Site:
        now = datetime.now(timezone.utc).isoformat()
        with self._write():
            cur = self.conn.execute(
                "INSERT INTO sites (name, url, api_type, api_key, api_user, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, url.rstrip("/"), api_type, api_key, api_user, now),
            )
        return Site(
            id=cur.lastrowid,  # type: ignore[arg-type]
            name=name,
            url=url.rstrip("/"),
            api_type=api_type,
            api_key=api_key,
            api_user=api_user,
        )

    def get_sites(self, enabled_only: bool = True) -> list[Site]:
        q = "SELECT * FROM sites"
        if enabled_only:
            q += " WHERE enabled = 1"
        q += " ORDER BY name"
        rows = self.conn.execute(q).fetchall()
        return [
            Site(
                id=r["id"],
                name=r["name"],
                url=r["url"],
                api_type=r["api_type"],
                api_key=r["api_key"],
                api_user=r["api_user"],
                enabled=bool(r["enabled"]),
            )
            for r in rows
        ]

    def delete_site(self, site_id: int) -> None:
        with self._write():
            self.conn.execute("DELETE FROM tag_types WHERE site_id = ?", (site_id,))
            self.conn.execute("DELETE FROM search_history WHERE site_id = ?", (site_id,))
            self.conn.execute("DELETE FROM saved_searches WHERE site_id = ?", (site_id,))
            self.conn.execute("DELETE FROM favorites WHERE site_id = ?", (site_id,))
            self.conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))

    def update_site(self, site_id: int, **fields: str | None) -> None:
        allowed = {"name", "url", "api_type", "api_key", "api_user", "enabled"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return
        vals.append(site_id)
        with self._write():
            self.conn.execute(
                f"UPDATE sites SET {', '.join(sets)} WHERE id = ?", vals
            )

    # -- Bookmarks --

    def add_bookmark(
        self,
        site_id: int,
        post_id: int,
        file_url: str,
        preview_url: str | None,
        tags: str,
        rating: str | None = None,
        score: int | None = None,
        source: str | None = None,
        cached_path: str | None = None,
        folder: str | None = None,
        tag_categories: dict | None = None,
    ) -> Bookmark:
        now = datetime.now(timezone.utc).isoformat()
        cats_json = json.dumps(tag_categories) if tag_categories else ""
        with self._write():
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO favorites "
                "(site_id, post_id, file_url, preview_url, tags, rating, score, source, cached_path, folder, favorited_at, tag_categories) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (site_id, post_id, file_url, preview_url, tags, rating, score, source, cached_path, folder, now, cats_json),
            )
            if cur.rowcount == 0:
                # Row already existed (UNIQUE collision on site_id, post_id);
                # INSERT OR IGNORE leaves lastrowid stale, so re-SELECT the
                # actual id. Without this, the returned Bookmark.id is bogus
                # (e.g. 0) and any subsequent update keyed on that id silently
                # no-ops — see app.py update_bookmark_cache_path callsite.
                row = self.conn.execute(
                    "SELECT id, favorited_at FROM favorites WHERE site_id = ? AND post_id = ?",
                    (site_id, post_id),
                ).fetchone()
                bm_id = row["id"]
                bookmarked_at = row["favorited_at"]
            else:
                bm_id = cur.lastrowid
                bookmarked_at = now
        return Bookmark(
            id=bm_id,
            site_id=site_id,
            post_id=post_id,
            file_url=file_url,
            preview_url=preview_url,
            tags=tags,
            rating=rating,
            score=score,
            source=source,
            cached_path=cached_path,
            folder=folder,
            bookmarked_at=bookmarked_at,
        )

    # Back-compat shim
    add_favorite = add_bookmark

    def add_bookmarks_batch(self, bookmarks: list[dict]) -> None:
        """Add multiple bookmarks in a single transaction."""
        with self._write():
            for fav in bookmarks:
                self.conn.execute(
                    "INSERT OR IGNORE INTO favorites "
                    "(site_id, post_id, file_url, preview_url, tags, rating, score, source, cached_path, folder, favorited_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (fav['site_id'], fav['post_id'], fav['file_url'], fav.get('preview_url'),
                     fav.get('tags', ''), fav.get('rating'), fav.get('score'), fav.get('source'),
                     fav.get('cached_path'), fav.get('folder'), fav.get('favorited_at', datetime.now(timezone.utc).isoformat())),
                )

    # Back-compat shim
    add_favorites_batch = add_bookmarks_batch

    def remove_bookmark(self, site_id: int, post_id: int) -> None:
        with self._write():
            self.conn.execute(
                "DELETE FROM favorites WHERE site_id = ? AND post_id = ?",
                (site_id, post_id),
            )

    # Back-compat shim
    remove_favorite = remove_bookmark

    def is_bookmarked(self, site_id: int, post_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM favorites WHERE site_id = ? AND post_id = ?",
            (site_id, post_id),
        ).fetchone()
        return row is not None

    # Back-compat shim
    is_favorited = is_bookmarked

    def get_bookmarks(
        self,
        search: str | None = None,
        site_id: int | None = None,
        folder: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Bookmark]:
        q = "SELECT * FROM favorites WHERE 1=1"
        params: list = []
        if site_id is not None:
            q += " AND site_id = ?"
            params.append(site_id)
        if folder is not None:
            q += " AND folder = ?"
            params.append(folder)
        if search:
            for tag in search.strip().split():
                # Escape SQL LIKE wildcards in user input. Without ESCAPE,
                # `_` matches any single char and `%` matches any sequence,
                # so searching `cat_ear` would also match `catear`/`catxear`.
                escaped = (
                    tag.replace("\\", "\\\\")
                       .replace("%", "\\%")
                       .replace("_", "\\_")
                )
                q += " AND tags LIKE ? ESCAPE '\\'"
                params.append(f"%{escaped}%")
        q += " ORDER BY favorited_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self.conn.execute(q, params).fetchall()
        return [self._row_to_bookmark(r) for r in rows]

    # Back-compat shim
    get_favorites = get_bookmarks

    @staticmethod
    def _row_to_bookmark(r) -> Bookmark:
        cats_raw = r["tag_categories"] if "tag_categories" in r.keys() else ""
        cats = json.loads(cats_raw) if cats_raw else {}
        return Bookmark(
            id=r["id"],
            site_id=r["site_id"],
            post_id=r["post_id"],
            file_url=r["file_url"],
            preview_url=r["preview_url"] if "preview_url" in r.keys() else None,
            tags=r["tags"],
            rating=r["rating"],
            score=r["score"],
            source=r["source"],
            cached_path=r["cached_path"],
            folder=r["folder"] if "folder" in r.keys() else None,
            bookmarked_at=r["favorited_at"],
            tag_categories=cats,
        )

    # Back-compat shim
    _row_to_favorite = _row_to_bookmark

    def update_bookmark_cache_path(self, fav_id: int, cached_path: str) -> None:
        with self._write():
            self.conn.execute(
                "UPDATE favorites SET cached_path = ? WHERE id = ?",
                (cached_path, fav_id),
            )

    # Back-compat shim
    update_favorite_cache_path = update_bookmark_cache_path

    def bookmark_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM favorites").fetchone()
        return row[0]

    # Back-compat shim
    favorite_count = bookmark_count

    # -- Folders --

    def get_folders(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM favorite_folders ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    def add_folder(self, name: str) -> None:
        clean = _validate_folder_name(name.strip())
        with self._write():
            self.conn.execute(
                "INSERT OR IGNORE INTO favorite_folders (name) VALUES (?)", (clean,)
            )

    def remove_folder(self, name: str) -> None:
        with self._write():
            self.conn.execute(
                "UPDATE favorites SET folder = NULL WHERE folder = ?", (name,)
            )
            self.conn.execute("DELETE FROM favorite_folders WHERE name = ?", (name,))

    def rename_folder(self, old: str, new: str) -> None:
        new_name = _validate_folder_name(new.strip())
        with self._write():
            self.conn.execute(
                "UPDATE favorites SET folder = ? WHERE folder = ?", (new_name, old)
            )
            self.conn.execute(
                "UPDATE favorite_folders SET name = ? WHERE name = ?", (new_name, old)
            )

    def move_bookmark_to_folder(self, fav_id: int, folder: str | None) -> None:
        with self._write():
            self.conn.execute(
                "UPDATE favorites SET folder = ? WHERE id = ?", (folder, fav_id)
            )

    # Back-compat shim
    move_favorite_to_folder = move_bookmark_to_folder

    # -- Blacklist --

    def add_blacklisted_tag(self, tag: str) -> None:
        with self._write():
            self.conn.execute(
                "INSERT OR IGNORE INTO blacklisted_tags (tag) VALUES (?)",
                (tag.strip().lower(),),
            )

    def remove_blacklisted_tag(self, tag: str) -> None:
        with self._write():
            self.conn.execute(
                "DELETE FROM blacklisted_tags WHERE tag = ?",
                (tag.strip().lower(),),
            )

    def get_blacklisted_tags(self) -> list[str]:
        rows = self.conn.execute("SELECT tag FROM blacklisted_tags ORDER BY tag").fetchall()
        return [r["tag"] for r in rows]

    # -- Blacklisted Posts --

    def add_blacklisted_post(self, url: str) -> None:
        with self._write():
            self.conn.execute("INSERT OR IGNORE INTO blacklisted_posts (url) VALUES (?)", (url,))

    def remove_blacklisted_post(self, url: str) -> None:
        with self._write():
            self.conn.execute("DELETE FROM blacklisted_posts WHERE url = ?", (url,))

    def get_blacklisted_posts(self) -> set[str]:
        rows = self.conn.execute("SELECT url FROM blacklisted_posts").fetchall()
        return {r["url"] for r in rows}

    # -- Library Metadata --

    def save_library_meta(self, post_id: int, tags: str = "", tag_categories: dict = None,
                          score: int = 0, rating: str = None, source: str = None,
                          file_url: str = None, filename: str = "") -> None:
        cats_json = json.dumps(tag_categories) if tag_categories else ""
        with self._write():
            self.conn.execute(
                "INSERT OR REPLACE INTO library_meta "
                "(post_id, tags, tag_categories, score, rating, source, file_url, saved_at, filename) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (post_id, tags, cats_json, score, rating, source, file_url,
                 datetime.now(timezone.utc).isoformat(), filename),
            )

    def reconcile_library_meta(self) -> int:
        """Drop library_meta rows whose files are no longer on disk.

        Walks every row, checks for both digit-stem (legacy v0.2.3)
        and templated (post-refactor) filenames in saved_dir() + one
        level of subdirectories, and deletes rows where neither is
        found. Returns the number of rows removed.

        Cleans up the orphan rows that were leaked by the old
        delete_from_library before it learned to clean up after
        itself. Safe to call repeatedly — a no-op once the DB is
        consistent with disk.

        Skips reconciliation entirely if saved_dir() is missing or
        empty (defensive — a removable drive temporarily unmounted
        shouldn't trigger a wholesale meta wipe).
        """
        from .config import saved_dir, MEDIA_EXTENSIONS
        sd = saved_dir()
        if not sd.is_dir():
            return 0

        # Build the set of (post_id present on disk). Walks shallow:
        # root + one level of subdirectories.
        on_disk_files: list[Path] = []
        for entry in sd.iterdir():
            if entry.is_file() and entry.suffix.lower() in MEDIA_EXTENSIONS:
                on_disk_files.append(entry)
            elif entry.is_dir():
                for sub in entry.iterdir():
                    if sub.is_file() and sub.suffix.lower() in MEDIA_EXTENSIONS:
                        on_disk_files.append(sub)
        if not on_disk_files:
            # No files at all — refuse to reconcile. Could be an
            # unmounted drive, a freshly-cleared library, etc. The
            # cost of a false positive (wiping every meta row) is
            # higher than the cost of leaving stale rows.
            return 0

        present_post_ids: set[int] = set()
        for f in on_disk_files:
            if f.stem.isdigit():
                present_post_ids.add(int(f.stem))
        # Templated files: look up by filename
        for f in on_disk_files:
            if not f.stem.isdigit():
                row = self.conn.execute(
                    "SELECT post_id FROM library_meta WHERE filename = ? LIMIT 1",
                    (f.name,),
                ).fetchone()
                if row is not None:
                    present_post_ids.add(row["post_id"])

        all_meta_ids = self.get_saved_post_ids()
        stale = all_meta_ids - present_post_ids
        if not stale:
            return 0

        with self._write():
            BATCH = 500
            stale_list = list(stale)
            for i in range(0, len(stale_list), BATCH):
                chunk = stale_list[i:i + BATCH]
                placeholders = ",".join("?" * len(chunk))
                self.conn.execute(
                    f"DELETE FROM library_meta WHERE post_id IN ({placeholders})",
                    chunk,
                )
        return len(stale)

    def is_post_in_library(self, post_id: int) -> bool:
        """True iff a `library_meta` row exists for `post_id`.

        Cheap, indexed lookup. Use this instead of walking the
        filesystem when you only need a yes/no for a single post —
        e.g. the bookmark context-menu's "Unsave from Library"
        visibility check, or the bookmark→library copy's existence
        guard. Replaces digit-stem matching, which can't see
        templated filenames.
        """
        row = self.conn.execute(
            "SELECT 1 FROM library_meta WHERE post_id = ? LIMIT 1",
            (post_id,),
        ).fetchone()
        return row is not None

    def get_saved_post_ids(self) -> set[int]:
        """Return every post_id that has a library_meta row.

        Used for batch saved-locally dot population on grids — load
        the set once, do per-thumb membership checks against it.
        Single SELECT, much cheaper than per-post DB lookups or
        per-grid filesystem walks. Format-agnostic: handles both
        templated and digit-stem filenames as long as the file's
        save flow wrote a meta row (every save site does after the
        unified save_post_file refactor).
        """
        rows = self.conn.execute(
            "SELECT post_id FROM library_meta"
        ).fetchall()
        return {r["post_id"] for r in rows}

    def get_library_post_id_by_filename(self, filename: str) -> int | None:
        """Look up which post a saved-library file belongs to, by basename.

        Returns the post_id if a `library_meta` row exists with that
        filename, or None if no row matches. Used by the unified save
        flow's same-post-on-disk check to make re-saves idempotent and
        to apply sequential `_1`, `_2`, ... suffixes only when a name
        collides with a *different* post.

        Empty-string filenames (the legacy v0.2.3 sentinel) deliberately
        do not match — callers fall back to the digit-stem heuristic for
        those rows.
        """
        if not filename:
            return None
        row = self.conn.execute(
            "SELECT post_id FROM library_meta WHERE filename = ? LIMIT 1",
            (filename,),
        ).fetchone()
        return row["post_id"] if row else None

    def get_library_meta(self, post_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM library_meta WHERE post_id = ?", (post_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        cats = d.get("tag_categories", "")
        d["tag_categories"] = json.loads(cats) if cats else {}
        return d

    def search_library_meta(self, query: str) -> set[int]:
        """Search library metadata by tags. Returns matching post IDs."""
        rows = self.conn.execute(
            "SELECT post_id FROM library_meta WHERE tags LIKE ?",
            (f"%{query}%",),
        ).fetchall()
        return {r["post_id"] for r in rows}

    def remove_library_meta(self, post_id: int) -> None:
        with self._write():
            self.conn.execute("DELETE FROM library_meta WHERE post_id = ?", (post_id,))

    # -- Tag-type cache --

    def get_tag_labels(self, site_id: int, names: list[str]) -> dict[str, str]:
        """Return cached string labels for `names` on `site_id`.

        Result dict only contains tags with a cache entry — callers
        fetch the misses via CategoryFetcher and call set_tag_labels
        to backfill. Chunked to stay under SQLite's variable limit.
        """
        if not names:
            return {}
        result: dict[str, str] = {}
        BATCH = 500
        for i in range(0, len(names), BATCH):
            chunk = names[i:i + BATCH]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT name, label FROM tag_types WHERE site_id = ? AND name IN ({placeholders})",
                [site_id, *chunk],
            ).fetchall()
            for r in rows:
                result[r["name"]] = r["label"]
        return result

    def set_tag_labels(self, site_id: int, mapping: dict[str, str]) -> None:
        """Bulk INSERT OR REPLACE (name -> label) entries for one site.

        Auto-prunes oldest entries when the table exceeds
        _TAG_CACHE_MAX_ROWS to prevent unbounded growth.
        """
        if not mapping:
            return
        now = datetime.now(timezone.utc).isoformat()
        rows = [(site_id, name, label, now) for name, label in mapping.items()]
        with self._write():
            self.conn.executemany(
                "INSERT OR REPLACE INTO tag_types (site_id, name, label, fetched_at) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            self._prune_tag_cache()

    _TAG_CACHE_MAX_ROWS = 50_000  # ~50k tags ≈ several months of browsing

    def _prune_tag_cache(self) -> None:
        """Delete the oldest tag_types rows if the table exceeds the cap.

        Keeps the most-recently-fetched entries. Runs inside an
        existing _write() context from set_tag_labels, so no extra
        transaction overhead. The cap is generous enough that
        normal usage never hits it; it's a safety valve for users
        who browse dozens of boorus over months without clearing.
        """
        count = self.conn.execute("SELECT COUNT(*) FROM tag_types").fetchone()[0]
        if count <= self._TAG_CACHE_MAX_ROWS:
            return
        excess = count - self._TAG_CACHE_MAX_ROWS
        self.conn.execute(
            "DELETE FROM tag_types WHERE rowid IN ("
            "  SELECT rowid FROM tag_types ORDER BY fetched_at ASC LIMIT ?"
            ")",
            (excess,),
        )

    def clear_tag_cache(self, site_id: int | None = None) -> int:
        """Delete cached tag types. Pass site_id to clear one site,
        or None to clear all. Returns rows deleted. Exposed for
        future Settings UI "Clear tag cache" button."""
        with self._write():
            if site_id is not None:
                cur = self.conn.execute("DELETE FROM tag_types WHERE site_id = ?", (site_id,))
            else:
                cur = self.conn.execute("DELETE FROM tag_types")
            return cur.rowcount

    # -- Settings --

    def get_setting(self, key: str) -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row:
            return row["value"]
        return _DEFAULTS.get(key, "")

    def get_setting_int(self, key: str) -> int:
        return int(self.get_setting(key) or "0")

    def get_setting_bool(self, key: str) -> bool:
        return self.get_setting(key) == "1"

    def set_setting(self, key: str, value: str) -> None:
        with self._write():
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )

    def get_all_settings(self) -> dict[str, str]:
        result = dict(_DEFAULTS)
        rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        for r in rows:
            result[r["key"]] = r["value"]
        return result

    # -- Search History --

    def add_search_history(self, query: str, site_id: int | None = None) -> None:
        if not query.strip():
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._write():
            # Remove duplicate if exists, keep latest
            self.conn.execute(
                "DELETE FROM search_history WHERE query = ? AND (site_id = ? OR (site_id IS NULL AND ? IS NULL))",
                (query.strip(), site_id, site_id),
            )
            self.conn.execute(
                "INSERT INTO search_history (query, site_id, searched_at) VALUES (?, ?, ?)",
                (query.strip(), site_id, now),
            )
            # Keep only last 50
            self.conn.execute(
                "DELETE FROM search_history WHERE id NOT IN "
                "(SELECT id FROM search_history ORDER BY searched_at DESC LIMIT 50)"
            )

    def get_search_history(self, limit: int = 20) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT query FROM search_history ORDER BY searched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["query"] for r in rows]

    def clear_search_history(self) -> None:
        with self._write():
            self.conn.execute("DELETE FROM search_history")

    def remove_search_history(self, query: str) -> None:
        with self._write():
            self.conn.execute("DELETE FROM search_history WHERE query = ?", (query,))

    # -- Saved Searches --

    def add_saved_search(self, name: str, query: str, site_id: int | None = None) -> None:
        with self._write():
            self.conn.execute(
                "INSERT OR REPLACE INTO saved_searches (name, query, site_id) VALUES (?, ?, ?)",
                (name.strip(), query.strip(), site_id),
            )

    def get_saved_searches(self) -> list[tuple[int, str, str]]:
        """Returns list of (id, name, query)."""
        rows = self.conn.execute(
            "SELECT id, name, query FROM saved_searches ORDER BY name"
        ).fetchall()
        return [(r["id"], r["name"], r["query"]) for r in rows]

    def remove_saved_search(self, search_id: int) -> None:
        with self._write():
            self.conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
