"""Search input widget for the TUI."""

from __future__ import annotations

from textual.widgets import Input


class SearchInput(Input):
    """Tag search input with styling."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
