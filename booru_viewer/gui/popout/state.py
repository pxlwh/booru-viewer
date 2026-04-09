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

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union

from .effects import (
    ApplyLoopMode,
    ApplyMute,
    ApplyVolume,
    Effect,
    EmitClosed,
    EmitNavigate,
    EmitPlayNextRequested,
    EnterFullscreen,
    ExitFullscreen,
    FitWindowToContent,
    LoadImage,
    LoadVideo,
    SeekVideoTo,
    StopMedia,
    TogglePlay,
)
from .viewport import Viewport


log = logging.getLogger("booru.popout.state")


class InvalidTransition(Exception):
    """Raised by `StateMachine.dispatch()` when an event arrives in a
    state that doesn't accept it.

    Only raised when `BOORU_VIEWER_STRICT_STATE` is set in the
    environment. In release mode (the default), illegal transitions
    are dropped silently and a `log.debug` line is emitted instead.
    Production runs in release mode; development and the test suite
    can opt into strict mode to catch programmer errors at the
    dispatch boundary instead of letting them silently no-op.

    The strict-mode raise is the structural alternative to "wait for
    a downstream symptom and then bisect to find the bad dispatch."
    """

    def __init__(self, state, event):
        super().__init__(
            f"Invalid event {type(event).__name__} in state {state.name}"
        )
        self.state = state
        self.event = event


def _strict_mode_enabled() -> bool:
    """Read the strict-mode env var at dispatch time.

    Per-dispatch read (not cached at import) so monkeypatch in tests
    works correctly. Cheap — `os.environ.get` is microseconds.
    """
    return bool(os.environ.get("BOORU_VIEWER_STRICT_STATE"))


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
# Effect descriptors live in the sibling `effects.py` module — see the
# import block at the top of this file. They're re-exported here via
# `__all__` so callers can `from booru_viewer.gui.popout.state import
# LoadImage` without needing to know the file split.


# ----------------------------------------------------------------------
# Legality map: which events are valid in which states
# ----------------------------------------------------------------------
#
# Used by `StateMachine.dispatch()` for the env-gated strict-mode
# `InvalidTransition` raise. In release mode, illegal events are
# dropped silently (log.debug + return []). In strict mode, they raise
# to catch programmer errors at the dispatch boundary.
#
# A few events are GLOBALLY legal in any non-Closing state:
#   - NavigateRequested
#   - MuteToggleRequested / VolumeSet / LoopModeSet
#   - FullscreenToggled
#   - WindowMoved / WindowResized / HyprlandDriftDetected
#   - CloseRequested
#   - ContentArrived (the adapter can replace media at any time)
#
# State-specific events are listed per state. Some events are
# "legal-but-no-op" — most importantly VideoEofReached in LoadingVideo
# and SeekingVideo (the EOF race fix accepts these and drops them
# without acting). Those count as legal because the state machine
# intentionally observes them; the dropping IS the behavior.

_GLOBAL_NON_CLOSING_EVENTS: frozenset[type] = frozenset({
    ContentArrived,
    NavigateRequested,
    MuteToggleRequested,
    VolumeSet,
    LoopModeSet,
    FullscreenToggled,
    WindowMoved,
    WindowResized,
    HyprlandDriftDetected,
    CloseRequested,
})

_LEGAL_EVENTS_BY_STATE: dict[State, frozenset[type]] = {
    State.AWAITING_CONTENT: _GLOBAL_NON_CLOSING_EVENTS | frozenset({Open}),
    State.DISPLAYING_IMAGE: _GLOBAL_NON_CLOSING_EVENTS,
    State.LOADING_VIDEO: _GLOBAL_NON_CLOSING_EVENTS | frozenset({
        VideoStarted,
        VideoEofReached,    # legal-but-no-op (EOF race fix)
        VideoSizeKnown,
    }),
    State.PLAYING_VIDEO: _GLOBAL_NON_CLOSING_EVENTS | frozenset({
        VideoEofReached,
        VideoSizeKnown,
        SeekRequested,
        TogglePlayRequested,
    }),
    State.SEEKING_VIDEO: _GLOBAL_NON_CLOSING_EVENTS | frozenset({
        VideoEofReached,    # legal-but-no-op (drops during seek)
        VideoSizeKnown,
        SeekRequested,
        SeekCompleted,
    }),
    # Closing is terminal — every event drops at the dispatch entry,
    # so the legal set is empty (no event reaches the legality check).
    State.CLOSING: frozenset(),
}


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

        # Legality check: env-gated strict mode (BOORU_VIEWER_STRICT_STATE)
        # raises InvalidTransition; release mode drops + logs at debug.
        # The legality map distinguishes "intentionally legal-but-no-op"
        # (e.g. VideoEofReached in LoadingVideo — the EOF race fix) from
        # "structurally invalid" (e.g. SeekRequested in DisplayingImage —
        # no video to seek into).
        legal_events = _LEGAL_EVENTS_BY_STATE.get(self.state, frozenset())
        if type(event) not in legal_events:
            if _strict_mode_enabled():
                raise InvalidTransition(self.state, event)
            log.debug(
                "Dropping illegal event %s in state %s",
                type(event).__name__,
                self.state.name,
            )
            return []

        # Routing.
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
        """Initial popout-open event from the adapter.

        Stashes the cross-popout-session class-level state
        (`_saved_geometry`, `_saved_fullscreen`, the chosen monitor)
        on the state machine instance for the first ContentArrived
        handler to consume. After Open the machine is still in
        AwaitingContent — the actual viewport seeding from saved_geo
        happens inside the first ContentArrived (commit 8 wires the
        actual viewport math; this commit just stashes the inputs).

        No effects: the popout window is already constructed and
        showing. The first content load triggers the first fit.
        """
        self.saved_geo = event.saved_geo
        self.saved_fullscreen = event.saved_fullscreen
        self.monitor = event.monitor
        return []

    def _on_content_arrived(self, event: ContentArrived) -> list[Effect]:
        """Route the new content by media kind.

        Snapshot the content into `current_*` fields regardless of
        kind so the rest of the state machine can read them. Then
        transition to LoadingVideo (video) or DisplayingImage (image,
        commit 10) and emit the appropriate load + fit effects.

        The first-content-load one-shot consumes `saved_geo` to seed
        the viewport before the first fit (commit 8 wires the actual
        seeding). After this commit, every ContentArrived flips
        `is_first_content_load` to False — the saved_geo path runs at
        most once per popout open.
        """
        self.current_path = event.path
        self.current_info = event.info
        self.current_kind = event.kind
        self.current_width = event.width
        self.current_height = event.height

        if event.kind == MediaKind.VIDEO:
            self.is_first_content_load = False
            self.state = State.LOADING_VIDEO
            return [
                LoadVideo(
                    path=event.path,
                    info=event.info,
                    referer=event.referer,
                ),
                FitWindowToContent(
                    content_w=event.width,
                    content_h=event.height,
                ),
            ]
        # Image or GIF: transition straight to DisplayingImage and
        # emit LoadImage. The is_gif flag tells the adapter which
        # ImageViewer method to call (set_gif vs set_image).
        self.is_first_content_load = False
        self.state = State.DISPLAYING_IMAGE
        return [
            LoadImage(
                path=event.path,
                is_gif=(event.kind == MediaKind.GIF),
            ),
            FitWindowToContent(
                content_w=event.width,
                content_h=event.height,
            ),
        ]

    def _on_navigate_requested(self, event: NavigateRequested) -> list[Effect]:
        """**Double-load race fix (replaces 31d02d3c's upstream signal-
        chain trust fix at the popout layer).**

        From a media-bearing state (DisplayingImage / LoadingVideo /
        PlayingVideo / SeekingVideo): transition to AwaitingContent
        and emit `[StopMedia, EmitNavigate]`. The StopMedia clears the
        current surface so mpv doesn't keep playing the previous video
        during the async download wait. The EmitNavigate tells
        main_window to advance selection and eventually deliver the
        new content via ContentArrived.

        From AwaitingContent itself (rapid Right-arrow spam, second
        nav before main_window has delivered): emit EmitNavigate
        ALONE — no StopMedia, because there's nothing to stop. The
        state stays AwaitingContent. **The state machine never
        produces two LoadVideo / LoadImage effects for the same
        navigation cycle, no matter how many NavigateRequested events
        the user fires off.** That structural property is what makes
        the eof race impossible at the popout layer.
        """
        if self.state == State.AWAITING_CONTENT:
            return [EmitNavigate(direction=event.direction)]
        # Media-bearing state: clear current media + emit nav
        self.state = State.AWAITING_CONTENT
        return [
            StopMedia(),
            EmitNavigate(direction=event.direction),
        ]

    def _on_video_started(self, event: VideoStarted) -> list[Effect]:
        """LoadingVideo → PlayingVideo. Persistence effects fire here.

        The state machine pushes its persistent values (mute, volume,
        loop_mode) into mpv on the entry edge. The mute value is the
        critical one — it survives lazy mpv creation by being held on
        the state machine instead of mpv (replaces the
        VideoPlayer._pending_mute pattern at the popout layer).

        Only valid in LoadingVideo. PlayingVideo→PlayingVideo would
        be illegal (no entry edge to fire on); SeekingVideo→PlayingVideo
        is the SeekCompleted path, not VideoStarted.
        """
        if self.state != State.LOADING_VIDEO:
            return []
        self.state = State.PLAYING_VIDEO
        return [
            ApplyMute(value=self.mute),
            ApplyVolume(value=self.volume),
            ApplyLoopMode(value=self.loop_mode.value),
        ]

    def _on_video_eof_reached(self, event: VideoEofReached) -> list[Effect]:
        """**EOF race fix (replaces fda3b10b's 250ms timestamp window).**

        Only valid input in PlayingVideo. In every other state — most
        importantly LoadingVideo, where the stale-eof race lived —
        the event is dropped without changing state or emitting
        effects. This is the structural fix: the previous fix used
        a wall-clock window to suppress eof events arriving within
        250ms of `play_file`; the state machine subsumes that by
        only accepting eof when we're actually in PlayingVideo.

        In PlayingVideo:
        - Loop=Next: emit EmitPlayNextRequested so main_window
          advances to the next post.
        - Loop=Once: emit nothing — mpv with keep_open=yes naturally
          pauses at the end of the file. No state transition; the
          user can manually click Play to restart.
        - Loop=Loop: emit nothing — mpv's loop-file=inf handles
          the restart internally.
        """
        if self.state != State.PLAYING_VIDEO:
            return []
        if self.loop_mode == LoopMode.NEXT:
            return [EmitPlayNextRequested()]
        return []

    def _on_video_size_known(self, event: VideoSizeKnown) -> list[Effect]:
        """mpv reported new dimensions — refit the popout window.

        Valid in LoadingVideo (first frame) and PlayingVideo
        (mid-playback aspect change, rare but possible with
        anamorphic sources). Other states drop.
        """
        if self.state in (State.LOADING_VIDEO, State.PLAYING_VIDEO):
            return [FitWindowToContent(
                content_w=event.width,
                content_h=event.height,
            )]
        return []

    def _on_seek_requested(self, event: SeekRequested) -> list[Effect]:
        """**Slider pin replaces 96a0a9d's 500ms _seek_pending_until.**

        Two valid source states:

        - PlayingVideo: enter SeekingVideo, stash target_ms, emit
          SeekVideoTo. The slider pin behavior is read-path:
          `compute_slider_display_ms` returns `seek_target_ms`
          while in SeekingVideo regardless of mpv's lagging or
          keyframe-rounded `time_pos`.

        - SeekingVideo: a second seek before the first one completed.
          Replace the target — the user clicked again, so the new
          target is what they want pinned. Emit a fresh SeekVideoTo.
          Stay in SeekingVideo. mpv handles back-to-back seeks fine;
          its own playback-restart event for the latest seek is what
          will eventually fire SeekCompleted.

        SeekRequested in any other state (AwaitingContent /
        DisplayingImage / LoadingVideo / Closing): drop. There's no
        video to seek into.

        No timestamp window. The state machine subsumes the 500ms
        suppression by holding SeekingVideo until SeekCompleted
        arrives (which is mpv's `playback-restart` after the seek,
        wired in the adapter).
        """
        if self.state in (State.PLAYING_VIDEO, State.SEEKING_VIDEO):
            self.state = State.SEEKING_VIDEO
            self.seek_target_ms = event.target_ms
            return [SeekVideoTo(target_ms=event.target_ms)]
        return []

    def _on_seek_completed(self, event: SeekCompleted) -> list[Effect]:
        """SeekingVideo → PlayingVideo.

        Triggered by the adapter receiving mpv's `playback-restart`
        event AND finding the state machine in SeekingVideo (the
        adapter distinguishes load-restart from seek-restart by
        checking current state — see VideoStarted handler).

        After this transition, `compute_slider_display_ms` returns
        the actual mpv `time_pos` again instead of the pinned target.
        """
        if self.state == State.SEEKING_VIDEO:
            self.state = State.PLAYING_VIDEO
        return []

    def _on_mute_toggle_requested(
        self, event: MuteToggleRequested
    ) -> list[Effect]:
        """**Pending mute fix structural (replaces 0a68182's
        _pending_mute lazy-replay pattern at the popout layer).**

        Flip `state.mute` unconditionally — independent of which
        media state we're in, independent of whether mpv exists.
        Emit `ApplyMute` so the adapter pushes the new value into
        mpv if mpv is currently alive.

        For the "user mutes before any video has loaded" case, the
        ApplyMute effect is still emitted but the adapter's apply
        handler routes it through `VideoPlayer.is_muted = value`,
        which uses VideoPlayer's existing `_pending_mute` field as
        defense in depth (the pre-mpv buffer survives until
        `_ensure_mpv` runs). Either way, the mute value persists.

        On the next LoadingVideo → PlayingVideo transition,
        `_on_video_started` emits ApplyMute(state.mute) again as
        part of the entry effects, so the freshly-loaded video
        starts in the right mute state regardless of when the user
        toggled.

        Valid in every non-Closing state.
        """
        self.mute = not self.mute
        return [ApplyMute(value=self.mute)]

    def _on_volume_set(self, event: VolumeSet) -> list[Effect]:
        """User adjusted the volume slider or scroll-wheeled over the
        video area. Update `state.volume` (clamped to 0-100), emit
        ApplyVolume.

        Same persistence pattern as mute: state.volume is the source
        of truth, replayed on every PlayingVideo entry.

        Valid in every non-Closing state.
        """
        self.volume = max(0, min(100, event.value))
        return [ApplyVolume(value=self.volume)]

    def _on_loop_mode_set(self, event: LoopModeSet) -> list[Effect]:
        """User clicked the Loop / Once / Next button cycle. Update
        `state.loop_mode`, emit ApplyLoopMode.

        loop_mode also gates `_on_video_eof_reached`'s decision
        between EmitPlayNextRequested (Next), no-op (Once and Loop),
        so changing it during PlayingVideo affects what happens at
        the next EOF without needing any other state mutation.

        Valid in every non-Closing state.
        """
        self.loop_mode = event.mode
        return [ApplyLoopMode(value=self.loop_mode.value)]

    def _on_toggle_play_requested(
        self, event: TogglePlayRequested
    ) -> list[Effect]:
        """Space key / play button. Only valid in PlayingVideo —
        toggling play during a load or seek would race with mpv's
        own state machine and produce undefined behavior."""
        if self.state == State.PLAYING_VIDEO:
            return [TogglePlay()]
        return []

    def _on_fullscreen_toggled(self, event: FullscreenToggled) -> list[Effect]:
        """**F11 round-trip viewport preservation (705e6c6 made
        structural).**

        Enter (current state: not fullscreen):
          - Snapshot `viewport` into `pre_fullscreen_viewport`
          - Set `fullscreen = True`
          - Emit `EnterFullscreen` effect

        Exit (current state: fullscreen):
          - Restore `viewport` from `pre_fullscreen_viewport`
          - Clear `pre_fullscreen_viewport`
          - Set `fullscreen = False`
          - Emit `ExitFullscreen` effect (which causes the adapter
            to defer a FitWindowToContent on the next event-loop
            tick — matching the current QTimer.singleShot(0, ...)
            pattern at popout/window.py:1023)

        The viewport snapshot at the moment of entering is the key.
        Whether the user got to that position via Super+drag (no Qt
        moveEvent on Wayland), nav (which doesn't update viewport
        unless drift is detected), or external resize, the
        `pre_fullscreen_viewport` snapshot captures the viewport
        AS IT IS RIGHT NOW. F11 exit restores it exactly.

        The 705e6c6 commit fixed this in the legacy code by
        explicitly writing the current Hyprland window state into
        `_viewport` inside `_enter_fullscreen` — the state machine
        version is structurally equivalent. The adapter's
        EnterFullscreen handler reads the current Hyprland geometry
        and dispatches a `HyprlandDriftDetected` event before the
        FullscreenToggled, which updates `viewport` to current
        reality, then FullscreenToggled snapshots that into
        `pre_fullscreen_viewport`.

        Valid in every non-Closing state. Closing drops it (handled
        at the dispatch entry).
        """
        if not self.fullscreen:
            self.pre_fullscreen_viewport = self.viewport
            self.fullscreen = True
            return [EnterFullscreen()]
        # Exiting fullscreen
        if self.pre_fullscreen_viewport is not None:
            self.viewport = self.pre_fullscreen_viewport
        self.pre_fullscreen_viewport = None
        self.fullscreen = False
        return [ExitFullscreen()]

    def _on_window_moved(self, event: WindowMoved) -> list[Effect]:
        """Qt `moveEvent` fired (non-Hyprland only — Hyprland gates
        this in the adapter because Wayland doesn't expose absolute
        window position to clients).

        Move-only update: preserve `long_side` from the existing
        viewport (moves don't change size), but recompute the center
        from the new rect. If there's no existing viewport yet (first
        move before any fit), build a fresh one from the rect.

        Skipped during fullscreen — moves while fullscreen aren't
        user intent for the windowed viewport. Skipped in Closing.
        """
        if self.fullscreen or self.state == State.CLOSING:
            return []
        x, y, w, h = event.rect
        if w <= 0 or h <= 0:
            return []
        long_side = (
            self.viewport.long_side if self.viewport is not None
            else float(max(w, h))
        )
        self.viewport = Viewport(
            center_x=x + w / 2,
            center_y=y + h / 2,
            long_side=long_side,
        )
        return []

    def _on_window_resized(self, event: WindowResized) -> list[Effect]:
        """Qt `resizeEvent` fired (non-Hyprland only).

        Full rebuild from the rect: long_side becomes the new
        max(w, h), center becomes the rect center. Resizes change
        the user's intent for popout size.

        Skipped during fullscreen and Closing.
        """
        if self.fullscreen or self.state == State.CLOSING:
            return []
        x, y, w, h = event.rect
        if w <= 0 or h <= 0:
            return []
        self.viewport = Viewport(
            center_x=x + w / 2,
            center_y=y + h / 2,
            long_side=float(max(w, h)),
        )
        return []

    def _on_hyprland_drift_detected(
        self, event: HyprlandDriftDetected
    ) -> list[Effect]:
        """Hyprland-side drift detector found that the current window
        rect differs from the last dispatched rect by more than
        `_DRIFT_TOLERANCE`. The user moved or resized the window
        externally (Super+drag, corner resize, window manager
        intervention).

        On Wayland, Qt's moveEvent / resizeEvent never fire for
        external compositor-driven movement (xdg-toplevel doesn't
        expose absolute position). So this event is the only path
        that captures Hyprland Super+drag.

        Adopt the new state as the viewport's intent: rebuild
        viewport from the current rect.

        Skipped during fullscreen — drifts while in fullscreen
        aren't meaningful for the windowed viewport.
        """
        if self.fullscreen or self.state == State.CLOSING:
            return []
        x, y, w, h = event.rect
        if w <= 0 or h <= 0:
            return []
        self.viewport = Viewport(
            center_x=x + w / 2,
            center_y=y + h / 2,
            long_side=float(max(w, h)),
        )
        return []

    def _on_close_requested(self, event: CloseRequested) -> list[Effect]:
        """Esc / Q / X / closeEvent. Transition to Closing from any
        non-Closing source state. Closing is terminal — every
        subsequent event returns [] regardless of type (handled at
        the dispatch entry).

        Entry effects: StopMedia (clear current surface) + EmitClosed
        (tell main_window the popout is closing). The adapter is
        responsible for any cleanup beyond clearing media — closing
        the Qt window, removing the event filter, persisting
        geometry to the class-level _saved_geometry — but those are
        adapter-side concerns, not state machine concerns.
        """
        if self.state == State.CLOSING:
            return []
        self.state = State.CLOSING
        return [StopMedia(), EmitClosed()]


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
    "InvalidTransition",
]
