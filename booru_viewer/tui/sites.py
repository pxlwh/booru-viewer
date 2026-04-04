"""Site manager panel for the TUI."""

from __future__ import annotations

import asyncio

from textual.widgets import Static, Input, Button, Label
from textual.containers import Vertical
from textual.app import ComposeResult

from ..core.db import Database
from ..core.api.detect import detect_site_type
from ..core.config import GREEN, DIM_GREEN, BG


class SitePanel(Static):
    """Site management panel."""

    def __init__(self, db: Database, **kwargs) -> None:
        super().__init__(**kwargs)
        self._db = db

    def on_mount(self) -> None:
        self.refresh_list()

    def refresh_list(self) -> None:
        sites = self._db.get_sites(enabled_only=False)
        if not sites:
            self.update(
                "  No sites configured.\n\n"
                "  Use the GUI (booru-gui) to add sites,\n"
                "  or add them via Python:\n\n"
                "    from booru_viewer.core.db import Database\n"
                "    db = Database()\n"
                "    db.add_site('Danbooru', 'https://danbooru.donmai.us', 'danbooru')\n"
            )
            return

        lines = ["  Sites:\n"]
        for site in sites:
            status = "ON" if site.enabled else "OFF"
            lines.append(
                f"  [{status}] {site.name}  ({site.api_type})  {site.url}"
            )
        lines.append("\n  (Manage sites via GUI or Python API)")
        self.update("\n".join(lines))
