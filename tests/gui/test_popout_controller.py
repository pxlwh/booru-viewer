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
