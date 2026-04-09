"""Effect descriptors for the popout state machine.

Pure-Python frozen dataclasses describing what the Qt-side adapter
should do in response to a state machine dispatch. The state machine
in `popout/state.py` returns a list of these from each `dispatch()`
call; the adapter pattern-matches by type and applies them in order.

**Hard constraint**: this module MUST NOT import anything from
PySide6, mpv, httpx, subprocess, or any module that does. Same purity
gate as `state.py` — the test suite imports both directly without
standing up a QApplication.

The effect types are documented in detail in
`docs/POPOUT_ARCHITECTURE.md` "Effects" section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union


# ----------------------------------------------------------------------
# Media-control effects
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class LoadImage:
    """Display a static image or animated GIF. The adapter routes by
    `is_gif`: True → ImageViewer.set_gif, False → set_image.
    """

    path: str
    is_gif: bool


@dataclass(frozen=True)
class LoadVideo:
    """Hand a path or URL to mpv via `VideoPlayer.play_file`. If
    `referer` is set, the adapter passes it to play_file's per-file
    referrer option (current behavior at media/video_player.py:343-347).
    """

    path: str
    info: str
    referer: Optional[str] = None


@dataclass(frozen=True)
class StopMedia:
    """Clear both surfaces (image viewer and video player). Used on
    navigation away from current media and on close.
    """


@dataclass(frozen=True)
class ApplyMute:
    """Push `state.mute` to mpv. Adapter calls
    `self._video.is_muted = value` which goes through VideoPlayer's
    setter (which already handles the lazy-mpv case via _pending_mute
    as defense in depth).
    """

    value: bool


@dataclass(frozen=True)
class ApplyVolume:
    """Push `state.volume` to mpv via the existing
    `VideoPlayer.volume = value` setter (which writes through the
    slider widget, which is the persistent storage).
    """

    value: int


@dataclass(frozen=True)
class ApplyLoopMode:
    """Push `state.loop_mode` to mpv via the existing
    `VideoPlayer.loop_state = value` setter.
    """

    value: int  # LoopMode.value, kept as int for cross-process portability


@dataclass(frozen=True)
class SeekVideoTo:
    """Adapter calls `mpv.seek(target_ms / 1000.0, 'absolute')`. Note
    the use of plain 'absolute' (keyframe seek), not 'absolute+exact' —
    matches the current slider behavior at video_player.py:405. The
    seek pin behavior is independent: the slider shows
    `state.seek_target_ms` while in SeekingVideo, regardless of mpv's
    keyframe-rounded actual position.
    """

    target_ms: int


@dataclass(frozen=True)
class TogglePlay:
    """Toggle mpv's `pause` property. Adapter calls
    `VideoPlayer._toggle_play()`.
    """


# ----------------------------------------------------------------------
# Window/geometry effects
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class FitWindowToContent:
    """Compute the new window rect for the given content aspect using
    `state.viewport` and dispatch it to Hyprland (or `setGeometry()`
    on non-Hyprland). The adapter delegates the rect math + dispatch
    to `popout/hyprland.py`'s helper, which lands in commit 13.
    """

    content_w: int
    content_h: int


@dataclass(frozen=True)
class EnterFullscreen:
    """Adapter calls `self.showFullScreen()`."""


@dataclass(frozen=True)
class ExitFullscreen:
    """Adapter calls `self.showNormal()` then defers a
    FitWindowToContent on the next event-loop tick (matching the
    current `QTimer.singleShot(0, ...)` pattern at
    popout/window.py:1023).
    """


# ----------------------------------------------------------------------
# Outbound signal effects
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class EmitNavigate:
    """Tell main_window to navigate to the next/previous post.
    Adapter emits `self.navigate.emit(direction)`.
    """

    direction: int


@dataclass(frozen=True)
class EmitPlayNextRequested:
    """Tell main_window the video ended in Loop=Next mode. Adapter
    emits `self.play_next_requested.emit()`.
    """


@dataclass(frozen=True)
class EmitClosed:
    """Tell main_window the popout is closing. Fired on entry to
    Closing state. Adapter emits `self.closed.emit()`.
    """


# Type alias for the union of all effects.
Effect = Union[
    LoadImage,
    LoadVideo,
    StopMedia,
    ApplyMute,
    ApplyVolume,
    ApplyLoopMode,
    SeekVideoTo,
    TogglePlay,
    FitWindowToContent,
    EnterFullscreen,
    ExitFullscreen,
    EmitNavigate,
    EmitPlayNextRequested,
    EmitClosed,
]


__all__ = [
    "LoadImage",
    "LoadVideo",
    "StopMedia",
    "ApplyMute",
    "ApplyVolume",
    "ApplyLoopMode",
    "SeekVideoTo",
    "TogglePlay",
    "FitWindowToContent",
    "EnterFullscreen",
    "ExitFullscreen",
    "EmitNavigate",
    "EmitPlayNextRequested",
    "EmitClosed",
    "Effect",
]
