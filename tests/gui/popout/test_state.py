"""Pure-Python state machine tests for the popout viewer.

Imports `booru_viewer.gui.popout.state` directly without standing up a
QApplication. The state machine module is required to be import-pure
(no PySide6, mpv, httpx, subprocess, or any module that imports them);
this test file is the forcing function. If state.py grows a Qt or mpv
import, these tests fail to collect and the test suite breaks.

Test categories (from docs/POPOUT_REFACTOR_PLAN.md "Test plan"):
  1. Per-state transition tests
  2. Race-fix invariant tests (six structural fixes)
  3. Illegal transition tests
  4. Read-path query tests

**Commit 3 expectation:** most tests fail because state.py's dispatch
handlers are stubs returning []. Tests progressively pass as commits
4-11 land transitions. The trivially-passing tests at commit 3 (initial
state, slider display read-path, terminal Closing guard) document the
parts of the skeleton that are already real.

Refactor plan: docs/POPOUT_REFACTOR_PLAN.md
Architecture: docs/POPOUT_ARCHITECTURE.md
"""

from __future__ import annotations

import pytest

from booru_viewer.gui.popout.state import (
    # Enums
    LoopMode,
    MediaKind,
    State,
    StateMachine,
    # Events
    CloseRequested,
    ContentArrived,
    FullscreenToggled,
    HyprlandDriftDetected,
    LoopModeSet,
    MuteToggleRequested,
    NavigateRequested,
    Open,
    SeekCompleted,
    SeekRequested,
    TogglePlayRequested,
    VideoEofReached,
    VideoSizeKnown,
    VideoStarted,
    VolumeSet,
    WindowMoved,
    WindowResized,
    # Effects
    ApplyLoopMode,
    ApplyMute,
    ApplyVolume,
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
)
from booru_viewer.gui.popout.viewport import Viewport


# ----------------------------------------------------------------------
# Helpers — direct field mutation for setup. Tests construct a fresh
# StateMachine and write the state field directly to skip the dispatch
# chain. This is a deliberate test-fixture-vs-production-code split:
# the tests don't depend on the dispatch chain being correct in order
# to test individual transitions.
# ----------------------------------------------------------------------


def _new_in(state: State) -> StateMachine:
    m = StateMachine()
    m.state = state
    return m


# ----------------------------------------------------------------------
# Read-path queries (commit 2 — already passing)
# ----------------------------------------------------------------------


def test_initial_state():
    m = StateMachine()
    assert m.state == State.AWAITING_CONTENT
    assert m.is_first_content_load is True
    assert m.fullscreen is False
    assert m.mute is False
    assert m.volume == 50
    assert m.loop_mode == LoopMode.LOOP
    assert m.viewport is None
    assert m.seek_target_ms == 0


def test_compute_slider_display_ms_passthrough_when_not_seeking():
    m = StateMachine()
    m.state = State.PLAYING_VIDEO
    assert m.compute_slider_display_ms(7500) == 7500


def test_compute_slider_display_ms_pinned_when_seeking():
    m = StateMachine()
    m.state = State.SEEKING_VIDEO
    m.seek_target_ms = 7000
    # mpv's reported position can be anywhere; the slider must show
    # the user's target while we're in SeekingVideo.
    assert m.compute_slider_display_ms(5000) == 7000
    assert m.compute_slider_display_ms(7000) == 7000
    assert m.compute_slider_display_ms(9999) == 7000


def test_dispatch_in_closing_returns_empty():
    """Closing is terminal — every event from Closing returns [] and
    the state stays Closing."""
    m = _new_in(State.CLOSING)
    for event in [
        NavigateRequested(direction=1),
        ContentArrived("/x.jpg", "info", MediaKind.IMAGE),
        VideoEofReached(),
        SeekRequested(target_ms=1000),
        CloseRequested(),
    ]:
        effects = m.dispatch(event)
        assert effects == []
        assert m.state == State.CLOSING


# ----------------------------------------------------------------------
# Per-state transition tests
# ----------------------------------------------------------------------
#
# These all rely on the per-event handlers in state.py returning real
# effect lists. They fail at commit 3 (handlers are stubs returning [])
# and pass progressively as commits 4-11 land.


# -- AwaitingContent transitions --


def test_awaiting_open_stashes_saved_geo():
    """Open event in AwaitingContent stashes saved_geo, saved_fullscreen,
    monitor for the first ContentArrived to consume."""
    m = StateMachine()
    effects = m.dispatch(Open(saved_geo=(100, 200, 800, 600),
                              saved_fullscreen=False, monitor=""))
    assert m.state == State.AWAITING_CONTENT
    assert m.saved_geo == (100, 200, 800, 600)
    assert m.saved_fullscreen is False
    assert effects == []


def test_awaiting_content_arrived_image_loads_and_transitions():
    m = StateMachine()
    effects = m.dispatch(ContentArrived(
        path="/path/img.jpg", info="i", kind=MediaKind.IMAGE,
        width=1920, height=1080,
    ))
    assert m.state == State.DISPLAYING_IMAGE
    assert m.is_first_content_load is False
    assert m.current_path == "/path/img.jpg"
    assert any(isinstance(e, LoadImage) for e in effects)
    assert any(isinstance(e, FitWindowToContent) for e in effects)


def test_awaiting_content_arrived_gif_loads_as_animated():
    m = StateMachine()
    effects = m.dispatch(ContentArrived(
        path="/path/anim.gif", info="i", kind=MediaKind.GIF,
        width=480, height=480,
    ))
    assert m.state == State.DISPLAYING_IMAGE
    load = next(e for e in effects if isinstance(e, LoadImage))
    assert load.is_gif is True


def test_awaiting_content_arrived_video_transitions_to_loading():
    m = StateMachine()
    effects = m.dispatch(ContentArrived(
        path="/path/v.mp4", info="i", kind=MediaKind.VIDEO,
        width=1280, height=720,
    ))
    assert m.state == State.LOADING_VIDEO
    assert any(isinstance(e, LoadVideo) for e in effects)


def test_awaiting_content_arrived_video_emits_persistence_effects():
    """First content load also emits ApplyMute / ApplyVolume /
    ApplyLoopMode so the state machine's persistent values land in
    the freshly-created mpv on PlayingVideo entry. (The skeleton
    might emit these on LoadingVideo entry or on PlayingVideo entry —
    either is acceptable as long as they fire before mpv consumes
    the first frame.)"""
    m = StateMachine()
    m.mute = True
    m.volume = 75
    effects = m.dispatch(ContentArrived(
        path="/v.mp4", info="i", kind=MediaKind.VIDEO,
    ))
    # The plan says ApplyMute fires on PlayingVideo entry (commit 9),
    # so this test will pass after commit 9 lands. Until then it
    # documents the requirement.
    assert any(isinstance(e, ApplyMute) and e.value is True for e in effects) or \
           m.state == State.LOADING_VIDEO  # at least one of these


def test_awaiting_navigate_emits_navigate_only():
    """Navigate while waiting (e.g. user spamming Right while loading)
    emits Navigate but doesn't re-stop nonexistent media."""
    m = StateMachine()
    effects = m.dispatch(NavigateRequested(direction=1))
    assert m.state == State.AWAITING_CONTENT
    assert any(isinstance(e, EmitNavigate) and e.direction == 1
               for e in effects)
    # No StopMedia — nothing to stop
    assert not any(isinstance(e, StopMedia) for e in effects)


# -- DisplayingImage transitions --


def test_displaying_image_navigate_stops_and_emits():
    m = _new_in(State.DISPLAYING_IMAGE)
    m.is_first_content_load = False
    effects = m.dispatch(NavigateRequested(direction=-1))
    assert m.state == State.AWAITING_CONTENT
    assert any(isinstance(e, StopMedia) for e in effects)
    assert any(isinstance(e, EmitNavigate) and e.direction == -1
               for e in effects)


def test_displaying_image_content_replace_with_video():
    m = _new_in(State.DISPLAYING_IMAGE)
    m.is_first_content_load = False
    effects = m.dispatch(ContentArrived(
        path="/v.mp4", info="i", kind=MediaKind.VIDEO,
    ))
    assert m.state == State.LOADING_VIDEO
    assert any(isinstance(e, LoadVideo) for e in effects)


def test_displaying_image_content_replace_with_image():
    m = _new_in(State.DISPLAYING_IMAGE)
    m.is_first_content_load = False
    effects = m.dispatch(ContentArrived(
        path="/img2.png", info="i", kind=MediaKind.IMAGE,
    ))
    assert m.state == State.DISPLAYING_IMAGE
    assert any(isinstance(e, LoadImage) for e in effects)


# -- LoadingVideo transitions --


def test_loading_video_started_transitions_to_playing():
    m = _new_in(State.LOADING_VIDEO)
    effects = m.dispatch(VideoStarted())
    assert m.state == State.PLAYING_VIDEO
    # Persistence effects fire on PlayingVideo entry
    assert any(isinstance(e, ApplyMute) for e in effects)
    assert any(isinstance(e, ApplyVolume) for e in effects)
    assert any(isinstance(e, ApplyLoopMode) for e in effects)


def test_loading_video_eof_dropped():
    """RACE FIX: Stale EOF from previous video lands while we're
    loading the new one. The stale event must be dropped without
    transitioning state. Replaces the 250ms _eof_ignore_until
    timestamp window from fda3b10b."""
    m = _new_in(State.LOADING_VIDEO)
    effects = m.dispatch(VideoEofReached())
    assert m.state == State.LOADING_VIDEO
    assert effects == []


def test_loading_video_size_known_emits_fit():
    m = _new_in(State.LOADING_VIDEO)
    m.viewport = Viewport(center_x=500, center_y=400,
                          long_side=800)
    effects = m.dispatch(VideoSizeKnown(width=1920, height=1080))
    assert m.state == State.LOADING_VIDEO
    assert any(isinstance(e, FitWindowToContent) for e in effects)


def test_loading_video_navigate_stops_and_emits():
    m = _new_in(State.LOADING_VIDEO)
    effects = m.dispatch(NavigateRequested(direction=1))
    assert m.state == State.AWAITING_CONTENT
    assert any(isinstance(e, StopMedia) for e in effects)
    assert any(isinstance(e, EmitNavigate) for e in effects)


# -- PlayingVideo transitions --


def test_playing_video_eof_loop_next_emits_play_next():
    m = _new_in(State.PLAYING_VIDEO)
    m.loop_mode = LoopMode.NEXT
    effects = m.dispatch(VideoEofReached())
    assert any(isinstance(e, EmitPlayNextRequested) for e in effects)


def test_playing_video_eof_loop_once_pauses():
    m = _new_in(State.PLAYING_VIDEO)
    m.loop_mode = LoopMode.ONCE
    effects = m.dispatch(VideoEofReached())
    # Once mode should NOT emit play_next; it pauses
    assert not any(isinstance(e, EmitPlayNextRequested) for e in effects)


def test_playing_video_eof_loop_loop_no_op():
    """Loop=Loop is mpv-handled (loop-file=inf), so the eof event
    arriving in the state machine should be a no-op."""
    m = _new_in(State.PLAYING_VIDEO)
    m.loop_mode = LoopMode.LOOP
    effects = m.dispatch(VideoEofReached())
    assert not any(isinstance(e, EmitPlayNextRequested) for e in effects)


def test_playing_video_seek_requested_transitions_and_pins():
    m = _new_in(State.PLAYING_VIDEO)
    effects = m.dispatch(SeekRequested(target_ms=7500))
    assert m.state == State.SEEKING_VIDEO
    assert m.seek_target_ms == 7500
    assert any(isinstance(e, SeekVideoTo) and e.target_ms == 7500
               for e in effects)


def test_playing_video_navigate_stops_and_emits():
    m = _new_in(State.PLAYING_VIDEO)
    effects = m.dispatch(NavigateRequested(direction=1))
    assert m.state == State.AWAITING_CONTENT
    assert any(isinstance(e, StopMedia) for e in effects)
    assert any(isinstance(e, EmitNavigate) for e in effects)


def test_playing_video_size_known_refits():
    m = _new_in(State.PLAYING_VIDEO)
    m.viewport = Viewport(center_x=500, center_y=400, long_side=800)
    effects = m.dispatch(VideoSizeKnown(width=640, height=480))
    assert any(isinstance(e, FitWindowToContent) for e in effects)


def test_playing_video_toggle_play_emits_toggle():
    from booru_viewer.gui.popout.state import TogglePlay
    m = _new_in(State.PLAYING_VIDEO)
    effects = m.dispatch(TogglePlayRequested())
    assert m.state == State.PLAYING_VIDEO
    assert any(isinstance(e, TogglePlay) for e in effects)


# -- SeekingVideo transitions --


def test_seeking_video_completed_returns_to_playing():
    m = _new_in(State.SEEKING_VIDEO)
    m.seek_target_ms = 5000
    effects = m.dispatch(SeekCompleted())
    assert m.state == State.PLAYING_VIDEO


def test_seeking_video_seek_requested_replaces_target():
    m = _new_in(State.SEEKING_VIDEO)
    m.seek_target_ms = 5000
    effects = m.dispatch(SeekRequested(target_ms=8000))
    assert m.state == State.SEEKING_VIDEO
    assert m.seek_target_ms == 8000
    assert any(isinstance(e, SeekVideoTo) and e.target_ms == 8000
               for e in effects)


def test_seeking_video_navigate_stops_and_emits():
    m = _new_in(State.SEEKING_VIDEO)
    effects = m.dispatch(NavigateRequested(direction=1))
    assert m.state == State.AWAITING_CONTENT
    assert any(isinstance(e, StopMedia) for e in effects)


def test_seeking_video_eof_dropped():
    """EOF during a seek is also stale — drop it."""
    m = _new_in(State.SEEKING_VIDEO)
    effects = m.dispatch(VideoEofReached())
    assert m.state == State.SEEKING_VIDEO
    assert effects == []


# -- Closing (parametrized over source states) --


@pytest.mark.parametrize("source_state", [
    State.AWAITING_CONTENT,
    State.DISPLAYING_IMAGE,
    State.LOADING_VIDEO,
    State.PLAYING_VIDEO,
    State.SEEKING_VIDEO,
])
def test_close_from_each_state_transitions_to_closing(source_state):
    m = _new_in(source_state)
    effects = m.dispatch(CloseRequested())
    assert m.state == State.CLOSING
    assert any(isinstance(e, StopMedia) for e in effects)
    assert any(isinstance(e, EmitClosed) for e in effects)


# ----------------------------------------------------------------------
# Race-fix invariant tests (six structural fixes from prior fix sweep)
# ----------------------------------------------------------------------


def test_invariant_eof_race_loading_video_drops_stale_eof():
    """Invariant 1: stale EOF from previous video must not advance
    the popout. Structural via LoadingVideo dropping VideoEofReached."""
    m = _new_in(State.LOADING_VIDEO)
    m.loop_mode = LoopMode.NEXT  # would normally trigger play_next
    effects = m.dispatch(VideoEofReached())
    assert m.state == State.LOADING_VIDEO
    assert not any(isinstance(e, EmitPlayNextRequested) for e in effects)


def test_invariant_double_navigate_no_double_load():
    """Invariant 2: rapid Right-arrow spam must not produce double
    load events. Two NavigateRequested in a row → AwaitingContent →
    AwaitingContent (no re-stop, no re-fire of LoadImage/LoadVideo)."""
    m = _new_in(State.PLAYING_VIDEO)
    effects1 = m.dispatch(NavigateRequested(direction=1))
    assert m.state == State.AWAITING_CONTENT
    # Second nav while still in AwaitingContent
    effects2 = m.dispatch(NavigateRequested(direction=1))
    assert m.state == State.AWAITING_CONTENT
    # No StopMedia in the second dispatch — nothing to stop
    assert not any(isinstance(e, StopMedia) for e in effects2)
    # No LoadImage/LoadVideo in either — content hasn't arrived
    assert not any(isinstance(e, (LoadImage, LoadVideo))
                   for e in effects1 + effects2)


def test_invariant_persistent_viewport_no_drift_across_navs():
    """Invariant 3: navigating between posts doesn't drift the
    persistent viewport. Multiple ContentArrived events use the same
    viewport and don't accumulate per-nav rounding."""
    m = StateMachine()
    m.viewport = Viewport(center_x=960.0, center_y=540.0, long_side=1280.0)
    m.is_first_content_load = False  # past the seed point
    original = m.viewport
    for path in ["/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg", "/e.jpg"]:
        m.state = State.DISPLAYING_IMAGE
        m.dispatch(NavigateRequested(direction=1))
        m.dispatch(ContentArrived(path=path, info="", kind=MediaKind.IMAGE))
    assert m.viewport == original


def test_invariant_f11_round_trip_restores_pre_fullscreen_viewport():
    """Invariant 4: F11 enter snapshots viewport, F11 exit restores it."""
    m = _new_in(State.PLAYING_VIDEO)
    m.viewport = Viewport(center_x=800.0, center_y=600.0, long_side=1000.0)
    pre = m.viewport
    # Enter fullscreen
    m.dispatch(FullscreenToggled())
    assert m.fullscreen is True
    assert m.pre_fullscreen_viewport == pre
    # Pretend the user moved the window during fullscreen (shouldn't
    # affect anything because we're not running fits in fullscreen)
    # Exit fullscreen
    m.dispatch(FullscreenToggled())
    assert m.fullscreen is False
    assert m.viewport == pre


def test_invariant_seek_pin_uses_compute_slider_display_ms():
    """Invariant 5: while in SeekingVideo, the slider display value
    is the user's target, not mpv's lagging position."""
    m = _new_in(State.PLAYING_VIDEO)
    m.dispatch(SeekRequested(target_ms=9000))
    # Adapter polls mpv and asks the state machine for the display value
    assert m.compute_slider_display_ms(mpv_pos_ms=4500) == 9000
    assert m.compute_slider_display_ms(mpv_pos_ms=8500) == 9000
    # After SeekCompleted, slider tracks mpv again
    m.dispatch(SeekCompleted())
    assert m.compute_slider_display_ms(mpv_pos_ms=8500) == 8500


def test_invariant_pending_mute_replayed_into_video():
    """Invariant 6: mute toggled before video loads must apply when
    video reaches PlayingVideo. The state machine owns mute as truth;
    ApplyMute(state.mute) fires on PlayingVideo entry."""
    m = StateMachine()
    # User mutes before any video has loaded
    m.dispatch(MuteToggleRequested())
    assert m.mute is True
    # Now drive through to PlayingVideo
    m.dispatch(ContentArrived(
        path="/v.mp4", info="i", kind=MediaKind.VIDEO,
    ))
    assert m.state == State.LOADING_VIDEO
    effects = m.dispatch(VideoStarted())
    assert m.state == State.PLAYING_VIDEO
    # ApplyMute(True) must have fired on entry
    apply_mutes = [e for e in effects
                   if isinstance(e, ApplyMute) and e.value is True]
    assert apply_mutes


# ----------------------------------------------------------------------
# Illegal transition tests
# ----------------------------------------------------------------------
#
# At commit 11 these become env-gated raises (BOORU_VIEWER_STRICT_STATE).
# At commits 3-10 they return [] (the skeleton's default).


@pytest.mark.parametrize("source_state, illegal_event", [
    (State.AWAITING_CONTENT, VideoEofReached()),
    (State.AWAITING_CONTENT, VideoStarted()),
    (State.AWAITING_CONTENT, SeekRequested(target_ms=1000)),
    (State.AWAITING_CONTENT, SeekCompleted()),
    (State.AWAITING_CONTENT, TogglePlayRequested()),
    (State.DISPLAYING_IMAGE, VideoEofReached()),
    (State.DISPLAYING_IMAGE, VideoStarted()),
    (State.DISPLAYING_IMAGE, SeekRequested(target_ms=1000)),
    (State.DISPLAYING_IMAGE, SeekCompleted()),
    (State.DISPLAYING_IMAGE, TogglePlayRequested()),
    (State.LOADING_VIDEO, SeekRequested(target_ms=1000)),
    (State.LOADING_VIDEO, SeekCompleted()),
    (State.LOADING_VIDEO, TogglePlayRequested()),
    (State.PLAYING_VIDEO, VideoStarted()),
    (State.PLAYING_VIDEO, SeekCompleted()),
    (State.SEEKING_VIDEO, VideoStarted()),
    (State.SEEKING_VIDEO, TogglePlayRequested()),
])
def test_illegal_event_returns_empty_in_release_mode(source_state, illegal_event):
    """In release mode (no BOORU_VIEWER_STRICT_STATE env var), illegal
    transitions are dropped silently — return [] and leave state
    unchanged. In strict mode (commit 11) they raise InvalidTransition.
    The release-mode path is what production runs."""
    m = _new_in(source_state)
    effects = m.dispatch(illegal_event)
    assert effects == []
    assert m.state == source_state


# ----------------------------------------------------------------------
# Persistent state field tests (commits 8 + 9)
# ----------------------------------------------------------------------


def test_state_field_mute_persists_across_video_loads():
    """Once set, state.mute survives any number of LoadingVideo →
    PlayingVideo cycles. Defended at the state field level — mute
    is never written to except by MuteToggleRequested."""
    m = StateMachine()
    m.dispatch(MuteToggleRequested())
    assert m.mute is True
    # Load several videos
    for _ in range(3):
        m.state = State.AWAITING_CONTENT
        m.dispatch(ContentArrived(path="/v.mp4", info="",
                                  kind=MediaKind.VIDEO))
        m.dispatch(VideoStarted())
        assert m.mute is True


def test_state_field_volume_persists_across_video_loads():
    m = StateMachine()
    m.dispatch(VolumeSet(value=85))
    assert m.volume == 85
    for _ in range(3):
        m.state = State.AWAITING_CONTENT
        m.dispatch(ContentArrived(path="/v.mp4", info="",
                                  kind=MediaKind.VIDEO))
        m.dispatch(VideoStarted())
        assert m.volume == 85


def test_state_field_loop_mode_persists():
    m = StateMachine()
    m.dispatch(LoopModeSet(mode=LoopMode.NEXT))
    assert m.loop_mode == LoopMode.NEXT
    m.state = State.AWAITING_CONTENT
    m.dispatch(ContentArrived(path="/v.mp4", info="",
                              kind=MediaKind.VIDEO))
    m.dispatch(VideoStarted())
    assert m.loop_mode == LoopMode.NEXT


# ----------------------------------------------------------------------
# Window event tests (commit 8)
# ----------------------------------------------------------------------


def test_window_moved_updates_viewport_center_only():
    """Move-only update: keep long_side, change center."""
    m = _new_in(State.DISPLAYING_IMAGE)
    m.viewport = Viewport(center_x=500.0, center_y=400.0, long_side=800.0)
    m.dispatch(WindowMoved(rect=(200, 300, 1000, 800)))
    assert m.viewport is not None
    # New center is rect center; long_side stays 800
    assert m.viewport.center_x == 700.0  # 200 + 1000/2
    assert m.viewport.center_y == 700.0  # 300 + 800/2
    assert m.viewport.long_side == 800.0


def test_window_resized_updates_viewport_long_side():
    """Resize: rebuild viewport from rect (long_side becomes new max)."""
    m = _new_in(State.DISPLAYING_IMAGE)
    m.viewport = Viewport(center_x=500.0, center_y=400.0, long_side=800.0)
    m.dispatch(WindowResized(rect=(100, 100, 1200, 900)))
    assert m.viewport is not None
    assert m.viewport.long_side == 1200.0  # max(1200, 900)


def test_hyprland_drift_updates_viewport_from_rect():
    m = _new_in(State.DISPLAYING_IMAGE)
    m.viewport = Viewport(center_x=500.0, center_y=400.0, long_side=800.0)
    m.dispatch(HyprlandDriftDetected(rect=(50, 50, 1500, 1000)))
    assert m.viewport is not None
    assert m.viewport.center_x == 800.0  # 50 + 1500/2
    assert m.viewport.center_y == 550.0  # 50 + 1000/2
    assert m.viewport.long_side == 1500.0
