"""Mutable per-search state container."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SearchState:
    """Mutable state that resets on every new search."""
    shown_post_ids: set[tuple[int | None, int]] = field(default_factory=set)
    page_cache: dict[int, list] = field(default_factory=dict)
    infinite_exhausted: bool = False
    infinite_last_page: int = 0
    infinite_api_exhausted: bool = False
    nav_page_turn: str | None = None
    append_queue: list = field(default_factory=list)


@dataclass
class SearchSnapshot:
    """Everything needed to put a finished search back on screen."""
    tags: str
    page: int
    search: SearchState
    posts: list
    selected: int
    scroll: int


class SearchHistory:
    """Back stack of searches the user left. Newest last, capped."""

    def __init__(self, limit: int = 20) -> None:
        self._stack: list[SearchSnapshot] = []
        self._limit = limit

    def push(self, snap: SearchSnapshot) -> None:
        self._stack.append(snap)
        del self._stack[:-self._limit]

    def pop(self) -> SearchSnapshot | None:
        return self._stack.pop() if self._stack else None

    def clear(self) -> None:
        self._stack.clear()

    def __len__(self) -> int:
        return len(self._stack)
