"""Tests for popout_controller -- video state sync dict.

Pure Python. No Qt, no mpv.
"""

from __future__ import annotations

from booru_viewer.gui.popout_controller import build_video_sync_dict


# ======================================================================
# build_video_sync_dict
# ======================================================================


def test_shape():
    result = build_video_sync_dict(
        volume=50, mute=False, autoplay=True, loop_state=0, position_ms=0,
    )
    assert isinstance(result, dict)
    assert len(result) == 5


def test_defaults():
    result = build_video_sync_dict(
        volume=50, mute=False, autoplay=True, loop_state=0, position_ms=0,
    )
    assert result["volume"] == 50
    assert result["mute"] is False
    assert result["autoplay"] is True
    assert result["loop_state"] == 0
    assert result["position_ms"] == 0


def test_has_all_5_keys():
    result = build_video_sync_dict(
        volume=80, mute=True, autoplay=False, loop_state=2, position_ms=5000,
    )
    expected_keys = {"volume", "mute", "autoplay", "loop_state", "position_ms"}
    assert set(result.keys()) == expected_keys
    assert result["volume"] == 80
    assert result["mute"] is True
    assert result["autoplay"] is False
    assert result["loop_state"] == 2
    assert result["position_ms"] == 5000


# -- Saved volume (persists across restarts) --

from types import SimpleNamespace  # noqa: E402

from booru_viewer.gui.popout_controller import (  # noqa: E402
    PopoutController,
    parse_saved_volume,
)


def test_saved_volume_parses_and_clamps():
    assert parse_saved_volume("30") == 30
    assert parse_saved_volume("0") == 0
    assert parse_saved_volume("150") == 100
    assert parse_saved_volume("-5") == 0


def test_saved_volume_falls_back_on_junk():
    assert parse_saved_volume("") == 50
    assert parse_saved_volume("loud") == 50


def _ctrl(popout=None):
    pv = SimpleNamespace(volume=40, is_muted=False)
    app = SimpleNamespace(_preview=SimpleNamespace(_video_player=pv))
    c = PopoutController.__new__(PopoutController)
    c._app = app
    c._fullscreen_window = popout
    return c


def test_live_state_reads_preview_when_popout_closed():
    assert _ctrl().live_video_state() == (40, False)


def test_live_state_reads_popout_while_open():
    popout = SimpleNamespace(get_video_state=lambda: {"volume": 75, "mute": True})
    assert _ctrl(popout).live_video_state() == (75, True)
