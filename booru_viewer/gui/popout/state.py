"""Pure-Python state machine for the popout viewer.

This module is the source of truth for the popout's lifecycle. All
state transitions, all decisions about which effects to fire on which
events, and all of the persistent fields (`viewport`, `mute`, `volume`,
`seek_target_ms`, etc.) live here. The Qt-side adapter in
`popout/window.py` is responsible only for translating Qt events into
state machine events and applying the returned effects to widgets.

**Hard constraint**: this module MUST NOT import anything from PySide6,
mpv, httpx, subprocess, or any module that does. The state machine's
test suite imports it directly without standing up a `QApplication` —
if those imports fail, the tests fail to collect, and the test suite
becomes the forcing function that keeps this module pure.

The architecture, state diagram, invariant→transition mapping, and
event/effect lists are documented in `docs/POPOUT_ARCHITECTURE.md`.
This module's job is to be the executable form of that document.

This is the **commit 2 skeleton**: every state, every event type, every
effect type, and the `StateMachine` class with all fields initialized.
The `dispatch` method routes events to per-event handlers that all
currently return empty effect lists. Real transitions land in
commits 4-11 of `docs/POPOUT_REFACTOR_PLAN.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union

from .viewport import Viewport


# ----------------------------------------------------------------------
# States
# ----------------------------------------------------------------------


class State(Enum):
    """The popout's discrete media-lifecycle states.

    Six states, each with a clearly-defined set of valid input events
    (see `_VALID_EVENTS_BY_STATE` below and the architecture doc's
    transition table). Fullscreen, Privacy, Mute, Volume, LoopMode,
    and Viewport are state FIELDS, not states — they're orthogonal to
    the media lifecycle.
    """

    AWAITING_CONTENT = "AwaitingContent"
    DISPLAYING_IMAGE = "DisplayingImage"
    LOADING_VIDEO = "LoadingVideo"
    PLAYING_VIDEO = "PlayingVideo"
    SEEKING_VIDEO = "SeekingVideo"
    CLOSING = "Closing"


class MediaKind(Enum):
    """What kind of content the `ContentArrived` event is delivering."""

    IMAGE = "image"        # static image (jpg, png, webp)
    GIF = "gif"            # animated gif (or animated png/webp)
    VIDEO = "video"        # mp4, webm, mkv (mpv-backed)


class LoopMode(Enum):
    """The user's choice for end-of-video behavior.

    Mirrors `VideoPlayer._loop_state` integer values verbatim so the
    adapter can pass them through to mpv without translation:
    - LOOP: mpv `loop-file=inf`, video repeats forever
    - ONCE: mpv `loop-file=no`, video stops at end
    - NEXT: mpv `loop-file=no`, popout advances to next post on EOF
    """

    LOOP = 0
    ONCE = 1
    NEXT = 2


# ----------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------
#
# Events are frozen dataclasses so they're hashable, comparable, and
# immutable once dispatched. The dispatcher uses Python 3.10+ structural
# pattern matching (`match event:`) to route by event type.


@dataclass(frozen=True)
class Open:
    """Initial event dispatched once at popout construction.

    The adapter reads `FullscreenPreview._saved_geometry` and
    `_saved_fullscreen` (the class-level fields that survive across
    popout open/close cycles within one process) and passes them in
    here. The state machine stashes them as `state.saved_geo` and
    `state.saved_fullscreen` and consults them on the first
    `ContentArrived` to seed the viewport.
    """

    saved_geo: Optional[tuple[int, int, int, int]]  # (x, y, w, h) or None
    saved_fullscreen: bool
    monitor: str


@dataclass(frozen=True)
class ContentArrived:
    """The adapter (called by main_window via `popout.open_post(...)`)
    is delivering new media to the popout. Replaces the current
    `set_media` direct method call.
    """

    path: str
    info: str
    kind: MediaKind
    width: int = 0           # API-reported dimensions, 0 if unknown
    height: int = 0
    referer: Optional[str] = None  # for streaming http(s) URLs


@dataclass(frozen=True)
class NavigateRequested:
    """User pressed an arrow key, tilted the wheel, or otherwise
    requested navigation. Direction is +1 / -1 for left/right or
    ±grid_cols for up/down (matches the current `_navigate_preview`
    convention).
    """

    direction: int


@dataclass(frozen=True)
class VideoStarted:
    """Adapter has observed mpv's `playback-restart` event AND the
    state machine is currently in LoadingVideo. Translates to
    LoadingVideo → PlayingVideo. Note: the adapter is responsible for
    deciding "this playback-restart is a load completion, not a seek
    completion" by checking the current state — only the LoadingVideo
    case becomes VideoStarted; the SeekingVideo case becomes
    SeekCompleted.
    """


@dataclass(frozen=True)
class VideoEofReached:
    """mpv's `eof-reached` property flipped to True. Only valid in
    PlayingVideo — every other state drops it. This is the structural
    fix for the EOF race that fda3b10b's 250ms timestamp window
    papered over.
    """


@dataclass(frozen=True)
class VideoSizeKnown:
    """mpv's `video-params` observer fired with new (w, h) dimensions.
    Triggers a viewport-based fit.
    """

    width: int
    height: int


@dataclass(frozen=True)
class SeekRequested:
    """User clicked the slider, pressed +/- keys, or otherwise asked
    to seek. Transitions PlayingVideo → SeekingVideo and stashes
    `target_ms` so the slider can pin to it.
    """

    target_ms: int


@dataclass(frozen=True)
class SeekCompleted:
    """Adapter has observed mpv's `playback-restart` event AND the
    state machine is currently in SeekingVideo. Translates to
    SeekingVideo → PlayingVideo. Replaces the 500ms `_seek_pending_until`
    timestamp window from 96a0a9d.
    """


@dataclass(frozen=True)
class MuteToggleRequested:
    """User clicked the mute button. Updates `state.mute` regardless
    of which state the machine is in (mute is persistent across loads).
    """


@dataclass(frozen=True)
class VolumeSet:
    """User adjusted the volume slider or scroll-wheeled over the
    video area. Updates `state.volume`.
    """

    value: int


@dataclass(frozen=True)
class LoopModeSet:
    """User clicked the Loop / Once / Next button cycle."""

    mode: LoopMode


@dataclass(frozen=True)
class TogglePlayRequested:
    """User pressed Space (or clicked the play button). Only valid in
    PlayingVideo.
    """


@dataclass(frozen=True)
class FullscreenToggled:
    """User pressed F11. Snapshots `viewport` into
    `pre_fullscreen_viewport` on enter, restores from it on exit.
    """


@dataclass(frozen=True)
class WindowMoved:
    """Qt `moveEvent` fired (non-Hyprland only — Hyprland gates this
    in the adapter because Wayland doesn't expose absolute window
    position to clients). Updates `state.viewport`.
    """

    rect: tuple[int, int, int, int]  # (x, y, w, h)


@dataclass(frozen=True)
class WindowResized:
    """Qt `resizeEvent` fired (non-Hyprland only). Updates
    `state.viewport`.
    """

    rect: tuple[int, int, int, int]  # (x, y, w, h)


@dataclass(frozen=True)
class HyprlandDriftDetected:
    """The fit-time hyprctl read showed the current window rect drifted
    from the last dispatched rect by more than `_DRIFT_TOLERANCE`. The
    user moved or resized the window externally (Super+drag, corner
    resize, window manager intervention). Updates `state.viewport`
    from the current rect.
    """

    rect: tuple[int, int, int, int]  # (x, y, w, h)


@dataclass(frozen=True)
class CloseRequested:
    """User pressed Esc, Q, X, or otherwise requested close. Transitions
    to Closing from any non-Closing state.
    """


# Type alias for the union of all events. Used as the type annotation
# on `dispatch(event: Event)`.
Event = Union[
    Open,
    ContentArrived,
    NavigateRequested,
    VideoStarted,
    VideoEofReached,
    VideoSizeKnown,
    SeekRequested,
    SeekCompleted,
    MuteToggleRequested,
    VolumeSet,
    LoopModeSet,
    TogglePlayRequested,
    FullscreenToggled,
    WindowMoved,
    WindowResized,
    HyprlandDriftDetected,
    CloseRequested,
]


# ----------------------------------------------------------------------
# Effects
# ----------------------------------------------------------------------
#
# Effects are descriptors of what the adapter should do. The dispatcher
# returns a list of these from each `dispatch()` call. The adapter
# pattern-matches by type and applies them in order.


# -- Media-control effects --


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


# -- Window/geometry effects --


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


# -- Outbound signal effects --


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


# ----------------------------------------------------------------------
# StateMachine
# ----------------------------------------------------------------------


class StateMachine:
    """Pure-Python state machine for the popout viewer.

    All decisions about media lifecycle, navigation, fullscreen, mute,
    volume, viewport, and seeking live here. The Qt adapter in
    `popout/window.py` is responsible only for:
      1. Translating Qt events into state machine event objects
      2. Calling `dispatch(event)`
      3. Applying the returned effects to actual widgets / mpv / etc.

    The state machine never imports Qt or mpv. It never calls into the
    adapter. The communication is one-directional: events in, effects
    out.

    **This is the commit 2 skeleton**: all state fields are initialized,
    `dispatch` is wired but every transition handler is a stub that
    returns an empty effect list. Real transitions land in commits 4-11.
    """

    def __init__(self) -> None:
        # -- Core lifecycle state --
        self.state: State = State.AWAITING_CONTENT

        # -- First-content one-shot --
        # See docs/POPOUT_ARCHITECTURE.md "is_first_content_load
        # lifecycle" section for the full explanation. True at
        # construction, flips to False inside the first ContentArrived
        # handler. Selects between "seed viewport from saved_geo" and
        # "use persistent viewport".
        self.is_first_content_load: bool = True

        # -- Persistent fields (orthogonal to state) --
        self.fullscreen: bool = False
        self.mute: bool = False
        self.volume: int = 50
        self.loop_mode: LoopMode = LoopMode.LOOP

        # -- Viewport / geometry --
        self.viewport: Optional[Viewport] = None
        self.pre_fullscreen_viewport: Optional[Viewport] = None
        self.last_dispatched_rect: Optional[tuple[int, int, int, int]] = None

        # -- Seek state (valid only in SeekingVideo) --
        self.seek_target_ms: int = 0

        # -- Current content snapshot --
        self.current_path: Optional[str] = None
        self.current_info: str = ""
        self.current_kind: Optional[MediaKind] = None
        # API-reported dimensions for the current content. Used by
        # FitWindowToContent on first fit before VideoSizeKnown
        # arrives from mpv.
        self.current_width: int = 0
        self.current_height: int = 0

        # -- Open-event payload (consumed on first ContentArrived) --
        self.saved_geo: Optional[tuple[int, int, int, int]] = None
        self.saved_fullscreen: bool = False
        self.monitor: str = ""

        # -- Grid columns for keyboard nav (Up/Down map to ±cols) --
        self.grid_cols: int = 3

    # ------------------------------------------------------------------
    # Read-path queries
    # ------------------------------------------------------------------
    #
    # Properties of the current state, computed without dispatching.
    # Pure functions of `self`. Called by the adapter to render the UI
    # without going through the dispatch machinery.

    def compute_slider_display_ms(self, mpv_pos_ms: int) -> int:
        """Return what the seek slider should display.

        While in SeekingVideo, the slider must show the user's seek
        target — not mpv's lagging or keyframe-rounded `time_pos` —
        because mpv may take tens to hundreds of ms to land at the
        target, and during that window the user-perceived slider must
        not snap backward. After the seek completes (SeekingVideo →
        PlayingVideo via SeekCompleted), the slider resumes tracking
        mpv's actual position.

        This is the structural replacement for the 500ms
        `_seek_pending_until` timestamp window. There's no timestamp
        — there's just the SeekingVideo state, which lasts exactly
        until mpv reports the seek is done.
        """
        if self.state == State.SEEKING_VIDEO:
            return self.seek_target_ms
        return mpv_pos_ms

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    #
    # The single mutation point. All state changes happen inside
    # dispatch() and only inside dispatch(). The adapter is forbidden
    # from writing to state fields directly — it only calls dispatch
    # and reads back the returned effects + the post-dispatch state.

    def dispatch(self, event: Event) -> list[Effect]:
        """Process one event and return the effect list.

        **Skeleton (commit 2):** every event handler currently returns
        an empty effect list. Real transitions land in commits 4-11.
        Tests written in commit 3 will document what each transition
        is supposed to do; they fail at this point and progressively
        pass as the transitions land.
        """
        # Closing is terminal — drop everything once we're done.
        if self.state == State.CLOSING:
            return []

        # Skeleton routing. Real handlers land in later commits.
        match event:
            case Open():
                return self._on_open(event)
            case ContentArrived():
                return self._on_content_arrived(event)
            case NavigateRequested():
                return self._on_navigate_requested(event)
            case VideoStarted():
                return self._on_video_started(event)
            case VideoEofReached():
                return self._on_video_eof_reached(event)
            case VideoSizeKnown():
                return self._on_video_size_known(event)
            case SeekRequested():
                return self._on_seek_requested(event)
            case SeekCompleted():
                return self._on_seek_completed(event)
            case MuteToggleRequested():
                return self._on_mute_toggle_requested(event)
            case VolumeSet():
                return self._on_volume_set(event)
            case LoopModeSet():
                return self._on_loop_mode_set(event)
            case TogglePlayRequested():
                return self._on_toggle_play_requested(event)
            case FullscreenToggled():
                return self._on_fullscreen_toggled(event)
            case WindowMoved():
                return self._on_window_moved(event)
            case WindowResized():
                return self._on_window_resized(event)
            case HyprlandDriftDetected():
                return self._on_hyprland_drift_detected(event)
            case CloseRequested():
                return self._on_close_requested(event)
            case _:
                # Unknown event type. Returning [] keeps the skeleton
                # safe; the illegal-transition handler in commit 11
                # will replace this with the env-gated raise.
                return []

    # ------------------------------------------------------------------
    # Per-event stub handlers (commit 2 — all return [])
    # ------------------------------------------------------------------

    def _on_open(self, event: Open) -> list[Effect]:
        # Real implementation: stash saved_geo / saved_fullscreen /
        # monitor on self for the first ContentArrived to consume.
        # Lands in commit 5.
        return []

    def _on_content_arrived(self, event: ContentArrived) -> list[Effect]:
        # Real implementation: routes to LoadImage or LoadVideo,
        # transitions to DisplayingImage / LoadingVideo, emits
        # FitWindowToContent. First-time path consumes saved_geo;
        # subsequent paths use persistent viewport. Lands in commits
        # 4 (video) + 10 (image).
        return []

    def _on_navigate_requested(self, event: NavigateRequested) -> list[Effect]:
        # Real implementation: emits StopMedia + EmitNavigate,
        # transitions to AwaitingContent. Lands in commit 5.
        return []

    def _on_video_started(self, event: VideoStarted) -> list[Effect]:
        # Real implementation: LoadingVideo → PlayingVideo, emits
        # ApplyMute / ApplyVolume / ApplyLoopMode. Lands in commit 4.
        return []

    def _on_video_eof_reached(self, event: VideoEofReached) -> list[Effect]:
        # Real implementation: only valid in PlayingVideo. Loop=Next
        # emits EmitPlayNextRequested. Loop=Once emits TogglePlay (to
        # pause). Loop=Loop is a no-op (mpv handles it). Other states
        # drop. Lands in commit 4 — this is the EOF race fix.
        return []

    def _on_video_size_known(self, event: VideoSizeKnown) -> list[Effect]:
        # Real implementation: emits FitWindowToContent. Lands in
        # commits 4 + 8.
        return []

    def _on_seek_requested(self, event: SeekRequested) -> list[Effect]:
        # Real implementation: PlayingVideo → SeekingVideo, sets
        # seek_target_ms, emits SeekVideoTo. Lands in commit 6.
        return []

    def _on_seek_completed(self, event: SeekCompleted) -> list[Effect]:
        # Real implementation: SeekingVideo → PlayingVideo. Lands in
        # commit 6.
        return []

    def _on_mute_toggle_requested(
        self, event: MuteToggleRequested
    ) -> list[Effect]:
        # Real implementation: flips state.mute, emits ApplyMute.
        # Lands in commit 9.
        return []

    def _on_volume_set(self, event: VolumeSet) -> list[Effect]:
        # Real implementation: sets state.volume, emits ApplyVolume.
        # Lands in commit 9.
        return []

    def _on_loop_mode_set(self, event: LoopModeSet) -> list[Effect]:
        # Real implementation: sets state.loop_mode, emits
        # ApplyLoopMode. Lands in commit 9.
        return []

    def _on_toggle_play_requested(
        self, event: TogglePlayRequested
    ) -> list[Effect]:
        # Real implementation: only valid in PlayingVideo. Emits
        # TogglePlay. Lands in commit 4.
        return []

    def _on_fullscreen_toggled(self, event: FullscreenToggled) -> list[Effect]:
        # Real implementation: enter snapshots viewport into
        # pre_fullscreen_viewport. Exit restores. Lands in commit 7.
        return []

    def _on_window_moved(self, event: WindowMoved) -> list[Effect]:
        # Real implementation: updates state.viewport from rect (move
        # only — preserves long_side). Lands in commit 8.
        return []

    def _on_window_resized(self, event: WindowResized) -> list[Effect]:
        # Real implementation: updates state.viewport from rect
        # (resize — long_side becomes max(w, h)). Lands in commit 8.
        return []

    def _on_hyprland_drift_detected(
        self, event: HyprlandDriftDetected
    ) -> list[Effect]:
        # Real implementation: rebuilds state.viewport from rect.
        # Lands in commit 8.
        return []

    def _on_close_requested(self, event: CloseRequested) -> list[Effect]:
        # Real implementation: transitions to Closing, emits StopMedia
        # + EmitClosed. Lands in commit 10.
        return []


__all__ = [
    # Enums
    "State",
    "MediaKind",
    "LoopMode",
    # Events
    "Open",
    "ContentArrived",
    "NavigateRequested",
    "VideoStarted",
    "VideoEofReached",
    "VideoSizeKnown",
    "SeekRequested",
    "SeekCompleted",
    "MuteToggleRequested",
    "VolumeSet",
    "LoopModeSet",
    "TogglePlayRequested",
    "FullscreenToggled",
    "WindowMoved",
    "WindowResized",
    "HyprlandDriftDetected",
    "CloseRequested",
    "Event",
    # Effects
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
    # Machine
    "StateMachine",
]
