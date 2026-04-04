"""Favorites browser panel for the TUI."""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Static, Input
from textual.app import ComposeResult

from ..core.db import Database, Favorite
from ..core.config import GREEN, DIM_GREEN


class FavoritesPanel(Static):
    """Browse local favorites."""

    def __init__(self, db: Database, **kwargs) -> None:
        super().__init__(**kwargs)
        self._db = db
        self._favorites: list[Favorite] = []

    def on_mount(self) -> None:
        self.refresh_list()

    def refresh_list(self, search: str | None = None) -> None:
        self._favorites = self._db.get_favorites(search=search, limit=100)
        total = self._db.favorite_count()

        if not self._favorites:
            self.update("  No favorites yet.\n  Press 'f' on a post to favorite it.")
            return

        lines = [f"  Favorites ({len(self._favorites)}/{total}):\n"]
        for fav in self._favorites:
            cached = "cached" if fav.cached_path and Path(fav.cached_path).exists() else "remote"
            tags_preview = " ".join(fav.tags.split()[:5])
            if len(fav.tags.split()) > 5:
                tags_preview += "..."
            lines.append(
                f"  #{fav.post_id}  [{cached}]  {tags_preview}"
            )
        self.update("\n".join(lines))
