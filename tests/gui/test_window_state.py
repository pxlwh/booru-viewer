"""Tests for window_state -- geometry parsing, Hyprland command building.

Pure Python. No Qt, no subprocess, no Hyprland.
"""

from __future__ import annotations

import pytest

from booru_viewer.gui.window_state import (
    build_hyprctl_restore_cmds,
    format_geometry,
    parse_geometry,
    parse_splitter_sizes,
)


# ======================================================================
# parse_geometry
# ======================================================================


def test_parse_geometry_valid():
    assert parse_geometry("100,200,800,600") == (100, 200, 800, 600)


def test_parse_geometry_wrong_count():
    assert parse_geometry("100,200,800") is None


def test_parse_geometry_non_numeric():
    assert parse_geometry("abc,200,800,600") is None


def test_parse_geometry_empty():
    assert parse_geometry("") is None


# ======================================================================
# format_geometry
# ======================================================================


def test_format_geometry_basic():
    assert format_geometry(10, 20, 1920, 1080) == "10,20,1920,1080"


def test_format_and_parse_round_trip():
    geo = (100, 200, 800, 600)
    assert parse_geometry(format_geometry(*geo)) == geo


# ======================================================================
# parse_splitter_sizes
# ======================================================================


def test_parse_splitter_sizes_valid_2():
    assert parse_splitter_sizes("300,700", 2) == [300, 700]


def test_parse_splitter_sizes_valid_3():
    assert parse_splitter_sizes("200,500,300", 3) == [200, 500, 300]


def test_parse_splitter_sizes_wrong_count():
    assert parse_splitter_sizes("300,700", 3) is None


def test_parse_splitter_sizes_negative():
    assert parse_splitter_sizes("300,-1", 2) is None


def test_parse_splitter_sizes_all_zero():
    assert parse_splitter_sizes("0,0", 2) is None


def test_parse_splitter_sizes_non_numeric():
    assert parse_splitter_sizes("abc,700", 2) is None


def test_parse_splitter_sizes_empty():
    assert parse_splitter_sizes("", 2) is None


# ======================================================================
# build_hyprctl_restore_cmds
# ======================================================================


def test_floating_to_floating_no_toggle():
    """Already floating, want floating: no togglefloating needed."""
    cmds = build_hyprctl_restore_cmds(
        addr="0xdead", x=100, y=200, w=800, h=600,
        want_floating=True, cur_floating=True,
    )
    assert not any("togglefloating" in c for c in cmds)
    assert any("resizewindowpixel" in c for c in cmds)
    assert any("movewindowpixel" in c for c in cmds)


def test_tiled_to_floating_has_toggle():
    """Currently tiled, want floating: one togglefloating to enter float."""
    cmds = build_hyprctl_restore_cmds(
        addr="0xdead", x=100, y=200, w=800, h=600,
        want_floating=True, cur_floating=False,
    )
    toggle_cmds = [c for c in cmds if "togglefloating" in c]
    assert len(toggle_cmds) == 1


def test_tiled_primes_floating_cache():
    """Want tiled: primes Hyprland's floating cache with 2 toggles + no_anim."""
    cmds = build_hyprctl_restore_cmds(
        addr="0xdead", x=100, y=200, w=800, h=600,
        want_floating=False, cur_floating=False,
    )
    toggle_cmds = [c for c in cmds if "togglefloating" in c]
    no_anim_on = [c for c in cmds if "no_anim 1" in c]
    no_anim_off = [c for c in cmds if "no_anim 0" in c]
    # Two toggles: tiled->float (to prime), float->tiled (to restore)
    assert len(toggle_cmds) == 2
    assert len(no_anim_on) == 1
    assert len(no_anim_off) == 1


def test_floating_to_tiled_one_toggle():
    """Currently floating, want tiled: one toggle to tile."""
    cmds = build_hyprctl_restore_cmds(
        addr="0xdead", x=100, y=200, w=800, h=600,
        want_floating=False, cur_floating=True,
    )
    toggle_cmds = [c for c in cmds if "togglefloating" in c]
    # Only the final toggle at the end of the tiled branch
    assert len(toggle_cmds) == 1


def test_correct_address_in_all_cmds():
    """Every command references the given address."""
    addr = "0xbeef"
    cmds = build_hyprctl_restore_cmds(
        addr=addr, x=0, y=0, w=1920, h=1080,
        want_floating=True, cur_floating=False,
    )
    for cmd in cmds:
        assert addr in cmd
