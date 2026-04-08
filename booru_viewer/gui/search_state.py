"""Mutable per-search state container."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SearchState:
    """Mutable state that resets on every new search."""
    shown_post_ids: set[int] = field(default_factory=set)
    page_cache: dict[int, list] = field(default_factory=dict)
    infinite_exhausted: bool = False
    infinite_last_page: int = 0
    infinite_api_exhausted: bool = False
    nav_page_turn: str | None = None
    append_queue: list = field(default_factory=list)
