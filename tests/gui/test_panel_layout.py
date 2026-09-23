"""Tests for panel_layout -- layout string, panel moves, sizes, drop zones.

Pure Python. No QApplication.
"""

from __future__ import annotations

from booru_viewer.gui.panel_layout import (
    default_layout, format_layout, format_sizes, hit_zone, legacy_sizes,
    move_panel, parse_layout, parse_sizes, zone_rects,
)

DEFAULT = [["results"], ["preview", "info"]]


# ======================================================================
# parse / format
# ======================================================================


def test_default_matches_pre_drag_layout():
    assert default_layout(False) == DEFAULT
    assert default_layout(True) == [["preview", "info"], ["results"]]


def test_round_trip():
    for layout in (DEFAULT, [["info"], ["preview"], ["results"]], [["results", "info", "preview"]]):
        assert parse_layout(format_layout(layout)) == layout


def test_parse_rejects_missing_duplicate_unknown():
    assert parse_layout("") is None
    assert parse_layout("results,preview") is None
    assert parse_layout("results,preview/info,info") is None
    assert parse_layout("results,preview/bogus") is None


# ======================================================================
# move_panel
# ======================================================================


def test_issue_layout_info_left_of_preview():
    flipped = default_layout(True)
    assert move_panel(flipped, "info", "preview", "left") == [["info"], ["preview"], ["results"]]


def test_side_column_right():
    assert move_panel(DEFAULT, "info", "preview", "right") == [["results"], ["preview"], ["info"]]


def test_stack_top_and_bottom():
    assert move_panel(DEFAULT, "results", "info", "bottom") == [["preview", "info", "results"]]
    assert move_panel(DEFAULT, "info", "preview", "top") == [["results"], ["info", "preview"]]


def test_moving_last_panel_out_drops_empty_column():
    # results' own column empties and goes; it lands after preview's column
    assert move_panel(DEFAULT, "results", "preview", "right") == [["preview", "info"], ["results"]]


def test_drop_on_self_is_noop():
    assert move_panel(DEFAULT, "info", "info", "left") == DEFAULT


def test_every_move_stays_valid():
    zones = ("left", "right", "top", "bottom")
    for panel in ("results", "preview", "info"):
        for target in ("results", "preview", "info"):
            for zone in zones:
                out = move_panel(DEFAULT, panel, target, zone)
                assert parse_layout(format_layout(out)) == out


# ======================================================================
# sizes
# ======================================================================


def test_sizes_round_trip_and_skip_garbage():
    assert parse_sizes(format_sizes({"results": 600, "preview/info": 500})) == {"results": 600, "preview/info": 500}
    assert parse_sizes("results:abc,info:-5,preview:0,:3,info:200") == {"info": 200}
    assert parse_sizes("") == {}


def test_legacy_sizes_follow_flip():
    assert legacy_sizes(False, "700,400", "500,0,200") == ({"results": 700, "preview/info": 400}, {"preview": 500, "info": 200})
    assert legacy_sizes(True, "400,700", "") == ({"results": 700, "preview/info": 400}, {})


def test_legacy_sizes_ignore_malformed():
    assert legacy_sizes(False, "1,2,3", "x,y") == ({}, {})


# ======================================================================
# zones
# ======================================================================

RECTS = {"results": (0, 0, 400, 600), "preview": (400, 0, 400, 600)}


def test_zones_of_each_panel():
    assert hit_zone(RECTS, 410, 300, None) == ("preview", "left")
    assert hit_zone(RECTS, 790, 300, None) == ("preview", "right")
    assert hit_zone(RECTS, 600, 20, None) == ("preview", "top")
    assert hit_zone(RECTS, 600, 590, None) == ("preview", "bottom")
    assert hit_zone(RECTS, 600, 300, None) is None


def test_dragged_panel_is_not_a_target():
    assert hit_zone(RECTS, 10, 300, "results") is None


def test_zones_do_not_overlap():
    rects = list(zone_rects(0, 0, 400, 600).values())
    for i, (ax, ay, aw, ah) in enumerate(rects):
        for bx, by, bw, bh in rects[i + 1:]:
            assert ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay
