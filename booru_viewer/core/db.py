"""SQLite database for favorites, sites, and cache metadata."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from .config import db_path

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
"""

_DEFAULTS = {
    "max_cache_mb": "2048",
    "auto_evict": "1",
    "thumbnail_size": "180",
    "page_size": "40",
    "default_rating": "all",
    "default_score": "0",
    "confirm_favorites": "0",
    "preload_thumbnails": "1",
    "file_dialog_platform": "qt",
    "blacklist_enabled": "1",
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
class Favorite:
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
    favorited_at: str


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or db_path()
        self._conn: sqlite3.Connection | None = None

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

    def _migrate(self) -> None:
        """Add columns that may not exist in older databases."""
        cur = self._conn.execute("PRAGMA table_info(favorites)")
        cols = {row[1] for row in cur.fetchall()}
        if "folder" not in cols:
            self._conn.execute("ALTER TABLE favorites ADD COLUMN folder TEXT")
            self._conn.commit()
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_favorites_folder ON favorites(folder)")

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
        cur = self.conn.execute(
            "INSERT INTO sites (name, url, api_type, api_key, api_user, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, url.rstrip("/"), api_type, api_key, api_user, now),
        )
        self.conn.commit()
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
        self.conn.execute("DELETE FROM favorites WHERE site_id = ?", (site_id,))
        self.conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
        self.conn.commit()

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
        self.conn.execute(
            f"UPDATE sites SET {', '.join(sets)} WHERE id = ?", vals
        )
        self.conn.commit()

    # -- Favorites --

    def add_favorite(
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
    ) -> Favorite:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO favorites "
            "(site_id, post_id, file_url, preview_url, tags, rating, score, source, cached_path, folder, favorited_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (site_id, post_id, file_url, preview_url, tags, rating, score, source, cached_path, folder, now),
        )
        self.conn.commit()
        return Favorite(
            id=cur.lastrowid,  # type: ignore[arg-type]
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
            favorited_at=now,
        )

    def add_favorites_batch(self, favorites: list[dict]) -> None:
        """Add multiple favorites in a single transaction."""
        for fav in favorites:
            self.conn.execute(
                "INSERT OR IGNORE INTO favorites "
                "(site_id, post_id, file_url, preview_url, tags, rating, score, source, cached_path, folder, favorited_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fav['site_id'], fav['post_id'], fav['file_url'], fav.get('preview_url'),
                 fav.get('tags', ''), fav.get('rating'), fav.get('score'), fav.get('source'),
                 fav.get('cached_path'), fav.get('folder'), fav.get('favorited_at', datetime.now(timezone.utc).isoformat())),
            )
        self.conn.commit()

    def remove_favorite(self, site_id: int, post_id: int) -> None:
        self.conn.execute(
            "DELETE FROM favorites WHERE site_id = ? AND post_id = ?",
            (site_id, post_id),
        )
        self.conn.commit()

    def is_favorited(self, site_id: int, post_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM favorites WHERE site_id = ? AND post_id = ?",
            (site_id, post_id),
        ).fetchone()
        return row is not None

    def get_favorites(
        self,
        search: str | None = None,
        site_id: int | None = None,
        folder: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Favorite]:
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
                q += " AND tags LIKE ?"
                params.append(f"%{tag}%")
        q += " ORDER BY favorited_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self.conn.execute(q, params).fetchall()
        return [self._row_to_favorite(r) for r in rows]

    @staticmethod
    def _row_to_favorite(r) -> Favorite:
        return Favorite(
            id=r["id"],
            site_id=r["site_id"],
            post_id=r["post_id"],
            file_url=r["file_url"],
            preview_url=r["preview_url"],
            tags=r["tags"],
            rating=r["rating"],
            score=r["score"],
            source=r["source"],
            cached_path=r["cached_path"],
            folder=r["folder"] if "folder" in r.keys() else None,
            favorited_at=r["favorited_at"],
        )

    def update_favorite_cache_path(self, fav_id: int, cached_path: str) -> None:
        self.conn.execute(
            "UPDATE favorites SET cached_path = ? WHERE id = ?",
            (cached_path, fav_id),
        )
        self.conn.commit()

    def favorite_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM favorites").fetchone()
        return row[0]

    # -- Folders --

    def get_folders(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM favorite_folders ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    def add_folder(self, name: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO favorite_folders (name) VALUES (?)", (name.strip(),)
        )
        self.conn.commit()

    def remove_folder(self, name: str) -> None:
        self.conn.execute(
            "UPDATE favorites SET folder = NULL WHERE folder = ?", (name,)
        )
        self.conn.execute("DELETE FROM favorite_folders WHERE name = ?", (name,))
        self.conn.commit()

    def rename_folder(self, old: str, new: str) -> None:
        self.conn.execute(
            "UPDATE favorites SET folder = ? WHERE folder = ?", (new.strip(), old)
        )
        self.conn.execute(
            "UPDATE favorite_folders SET name = ? WHERE name = ?", (new.strip(), old)
        )
        self.conn.commit()

    def move_favorite_to_folder(self, fav_id: int, folder: str | None) -> None:
        self.conn.execute(
            "UPDATE favorites SET folder = ? WHERE id = ?", (folder, fav_id)
        )
        self.conn.commit()

    # -- Blacklist --

    def add_blacklisted_tag(self, tag: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO blacklisted_tags (tag) VALUES (?)",
            (tag.strip().lower(),),
        )
        self.conn.commit()

    def remove_blacklisted_tag(self, tag: str) -> None:
        self.conn.execute(
            "DELETE FROM blacklisted_tags WHERE tag = ?",
            (tag.strip().lower(),),
        )
        self.conn.commit()

    def get_blacklisted_tags(self) -> list[str]:
        rows = self.conn.execute("SELECT tag FROM blacklisted_tags ORDER BY tag").fetchall()
        return [r["tag"] for r in rows]

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
        self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        self.conn.commit()

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
        self.conn.commit()

    def get_search_history(self, limit: int = 20) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT query FROM search_history ORDER BY searched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["query"] for r in rows]

    def clear_search_history(self) -> None:
        self.conn.execute("DELETE FROM search_history")
        self.conn.commit()

    def remove_search_history(self, query: str) -> None:
        self.conn.execute("DELETE FROM search_history WHERE query = ?", (query,))
        self.conn.commit()

    # -- Saved Searches --

    def add_saved_search(self, name: str, query: str, site_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO saved_searches (name, query, site_id) VALUES (?, ?, ?)",
            (name.strip(), query.strip(), site_id),
        )
        self.conn.commit()

    def get_saved_searches(self) -> list[tuple[int, str, str]]:
        """Returns list of (id, name, query)."""
        rows = self.conn.execute(
            "SELECT id, name, query FROM saved_searches ORDER BY name"
        ).fetchall()
        return [(r["id"], r["name"], r["query"]) for r in rows]

    def remove_saved_search(self, search_id: int) -> None:
        self.conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
        self.conn.commit()
