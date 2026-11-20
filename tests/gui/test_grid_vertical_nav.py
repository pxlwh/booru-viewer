"""Up/Down row navigation, shared by the grid's own keys and the main
window's Up/Down shortcuts (which work wherever focus is).

Pure Python: calls ThumbnailGrid.nav_vertical on a stand-in. Skipped in
CI, where grid.py's Qt import is unavailable.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from booru_viewer.gui.grid import ThumbnailGrid  # noqa: E402


def _grid(selected: int, total: int = 10, cols: int = 4):
    g = SimpleNamespace(
        _selected_index=selected,
        _thumbs=[None] * total,
        _flow=SimpleNamespace(columns=cols),
        selected=None,
        emitted=[],
    )
    g._select = lambda i: setattr(g, "selected", i)
    g.nav_past_end = SimpleNamespace(emit=lambda: g.emitted.append("end"))
    g.nav_before_start = SimpleNamespace(emit=lambda: g.emitted.append("start"))
    return g


def test_down_moves_one_row():
    g = _grid(1)
    ThumbnailGrid.nav_vertical(g, 1)
    assert g.selected == 5


def test_up_moves_one_row():
    g = _grid(6)
    ThumbnailGrid.nav_vertical(g, -1)
    assert g.selected == 2


def test_down_from_short_last_row_goes_to_last_post():
    g = _grid(7)  # 10 posts, 4 cols: row 2 holds 8,9; 7+4=11 is past the end
    ThumbnailGrid.nav_vertical(g, 1)
    assert g.selected == 9


def test_down_on_last_post_turns_the_page():
    g = _grid(9)
    ThumbnailGrid.nav_vertical(g, 1)
    assert g.emitted == ["end"] and g.selected is None


def test_up_from_first_row_goes_to_first_post_then_turns_back():
    g = _grid(2)
    ThumbnailGrid.nav_vertical(g, -1)
    assert g.selected == 0
    g = _grid(0)
    ThumbnailGrid.nav_vertical(g, -1)
    assert g.emitted == ["start"]
