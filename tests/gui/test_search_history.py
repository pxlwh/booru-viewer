"""Back navigation: leaving a search keeps it, Back puts it back.

Pure Python. No Qt, no network. GitHub issue pxlwh/booru-viewer#4.
"""

from __future__ import annotations

from types import SimpleNamespace

from booru_viewer.gui.search_controller import SearchController
from booru_viewer.gui.search_state import SearchHistory, SearchSnapshot, SearchState


def _snap(tags: str) -> SearchSnapshot:
    return SearchSnapshot(tags=tags, page=1, search=SearchState(),
                          posts=[], selected=-1, scroll=0)


def test_history_pops_newest_first():
    h = SearchHistory()
    h.push(_snap("a"))
    h.push(_snap("b"))
    assert h.pop().tags == "b"
    assert h.pop().tags == "a"
    assert h.pop() is None


def test_history_is_capped_keeping_newest():
    h = SearchHistory(limit=2)
    for t in "abc":
        h.push(_snap(t))
    assert len(h) == 2
    assert [h.pop().tags, h.pop().tags] == ["c", "b"]


# -- SearchController.on_search pushes the search being left --


def _controller(posts):
    grid = SimpleNamespace(
        selected_index=7,
        verticalScrollBar=lambda: SimpleNamespace(value=lambda: 1234),
    )
    btn = SimpleNamespace(enabled=None)
    btn.setEnabled = lambda v: setattr(btn, "enabled", v)
    app = SimpleNamespace(
        _posts=posts,
        _grid=grid,
        _back_btn=btn,
        _score_spin=SimpleNamespace(value=lambda: 0),
        _preview=SimpleNamespace(clear=lambda: None),
        _next_page_btn=SimpleNamespace(setVisible=lambda v: None),
        _prev_page_btn=SimpleNamespace(setVisible=lambda v: None),
        _selected_sites=lambda: [],
        _status=SimpleNamespace(showMessage=lambda m: None),
        _db=SimpleNamespace(get_setting_bool=lambda k: False),
    )
    c = SearchController(app)
    c._current_tags = "cat_ears"
    c._current_page = 3
    return c, app


def test_new_search_snapshots_the_one_being_left():
    c, app = _controller(["p1", "p2"])
    old_state = c._search
    c.on_search("artist_name")
    snap = c.history.pop()
    assert (snap.tags, snap.page, snap.selected, snap.scroll) == ("cat_ears", 3, 7, 1234)
    assert snap.posts == ["p1", "p2"]
    assert snap.search is old_state
    assert c._search is not old_state


def test_back_button_enables_after_push():
    c, app = _controller(["p1"])
    c.on_search("artist_name")
    assert app._back_btn.enabled is True


def test_empty_grid_pushes_nothing():
    c, _ = _controller([])
    c.on_search("artist_name")
    assert len(c.history) == 0


def test_same_query_again_pushes_nothing():
    c, _ = _controller(["p1"])
    c.on_search("cat_ears")
    assert len(c.history) == 0


def test_back_while_loading_keeps_history():
    c, _ = _controller(["p1"])
    c.on_search("artist_name")
    c._loading = True
    c.go_back()
    assert len(c.history) == 1


def test_reset_clears_history_and_disables_back():
    c, app = _controller(["p1"])
    c.on_search("artist_name")
    c.reset()
    assert len(c.history) == 0
    assert app._back_btn.enabled is False
