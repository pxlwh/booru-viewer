"""Popout fullscreen media viewer window."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMenu, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from ..media.constants import _is_video
from ..media.image_viewer import ImageViewer
from ..media.video_player import VideoPlayer
from .state import (
    CloseRequested,
    ContentArrived,
    FullscreenToggled,
    LoopMode,
    LoopModeSet,
    MediaKind,
    MuteToggleRequested,
    NavigateRequested,
    Open,
    SeekCompleted,
    SeekRequested,
    State,
    StateMachine,
    TogglePlayRequested,
    VideoEofReached,
    VideoSizeKnown,
    VideoStarted,
    VolumeSet,
    WindowMoved,
    WindowResized,
)
from .viewport import Viewport, _DRIFT_TOLERANCE


# Adapter logger — separate from the popout's main `booru` logger so
# the dispatch trace can be filtered independently. Format: every
# dispatch call logs at DEBUG with the event name, state transition,
# and effect list. The user filters by `POPOUT_FSM` substring to see
# only the state machine activity during the manual sweep.
_fsm_log = logging.getLogger("booru.popout.adapter")


## Overlay styling for the popout's translucent toolbar / controls bar
## now lives in the bundled themes (themes/*.qss). The widgets get their
## object names set in code (FullscreenPreview / VideoPlayer) so theme QSS
## rules can target them via #_slideshow_toolbar / #_slideshow_controls /
## #_preview_controls. Users can override the look by editing the
## overlay_bg slot in their @palette block, or by adding more specific
## QSS rules in their custom.qss.


class FullscreenPreview(QMainWindow):
    """Fullscreen media viewer with navigation — images, GIFs, and video."""

    navigate = Signal(int)  # direction: -1/+1 for left/right, -cols/+cols for up/down
    play_next_requested = Signal()  # video ended in "Next" mode (wrap-aware)
    bookmark_requested = Signal()
    # Bookmark-as: emitted when the popout's Bookmark button submenu picks
    # a bookmark folder. Empty string = Unfiled. Mirrors ImagePreview's
    # signal so app.py routes both through _bookmark_to_folder_from_preview.
    bookmark_to_folder = Signal(str)
    # Save-to-library: same signal pair as ImagePreview so app.py reuses
    # _save_from_preview / _unsave_from_preview for both. Empty string =
    # Unfiled (root of saved_dir).
    save_to_folder = Signal(str)
    unsave_requested = Signal()
    blacklist_tag_requested = Signal(str)  # tag name
    blacklist_post_requested = Signal()
    privacy_requested = Signal()
    closed = Signal()

    def __init__(self, grid_cols: int = 3, show_actions: bool = True, monitor: str = "", parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("booru-viewer — Popout")
        self._grid_cols = grid_cols

        # Central widget — media fills the entire window
        central = QWidget()
        central.setLayout(QVBoxLayout())
        central.layout().setContentsMargins(0, 0, 0, 0)
        central.layout().setSpacing(0)

        # Media stack (fills entire window)
        self._stack = QStackedWidget()
        central.layout().addWidget(self._stack)

        self._viewer = ImageViewer()
        self._viewer.close_requested.connect(self.close)
        self._stack.addWidget(self._viewer)

        self._video = VideoPlayer()
        self._video.play_next.connect(self.play_next_requested)
        self._video.video_size.connect(self._on_video_size)
        self._stack.addWidget(self._video)

        self.setCentralWidget(central)

        # Floating toolbar — overlays on top of media, translucent.
        # Set the object name BEFORE the widget is polished by Qt so that
        # the bundled-theme `QWidget#_slideshow_toolbar` selector matches
        # on the very first style computation. Setting it later requires
        # an explicit unpolish/polish cycle, which we want to avoid.
        self._toolbar = QWidget(central)
        self._toolbar.setObjectName("_slideshow_toolbar")
        # Plain QWidget ignores QSS `background:` declarations unless this
        # attribute is set — without it the toolbar paints transparently
        # and the popout buttons sit on bare letterbox color.
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        toolbar = QHBoxLayout(self._toolbar)
        toolbar.setContentsMargins(8, 4, 8, 4)

        # Same compact-padding override as the embedded preview toolbar —
        # bundled themes' default `padding: 5px 12px` is too wide for these
        # short labels in narrow fixed slots.
        _tb_btn_style = "padding: 2px 6px;"

        # Bookmark folders for the popout's Bookmark-as submenu — wired
        # by app.py via set_bookmark_folders_callback after construction.
        self._bookmark_folders_callback = None
        self._is_bookmarked = False
        # Library folders for the popout's Save-to-Library submenu —
        # wired by app.py via set_folders_callback. Same shape as the
        # bookmark folders callback above; library and bookmark folders
        # are independent name spaces and need separate callbacks.
        self._folders_callback = None

        self._bookmark_btn = QPushButton("Bookmark")
        self._bookmark_btn.setMaximumWidth(90)
        self._bookmark_btn.setStyleSheet(_tb_btn_style)
        self._bookmark_btn.clicked.connect(self._on_bookmark_clicked)
        toolbar.addWidget(self._bookmark_btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.setMaximumWidth(70)
        self._save_btn.setStyleSheet(_tb_btn_style)
        self._save_btn.clicked.connect(self._on_save_clicked)
        toolbar.addWidget(self._save_btn)
        self._is_saved = False

        self._bl_tag_btn = QPushButton("BL Tag")
        self._bl_tag_btn.setMaximumWidth(65)
        self._bl_tag_btn.setStyleSheet(_tb_btn_style)
        self._bl_tag_btn.setToolTip("Blacklist a tag")
        self._bl_tag_btn.clicked.connect(self._show_bl_tag_menu)
        toolbar.addWidget(self._bl_tag_btn)

        self._bl_post_btn = QPushButton("BL Post")
        self._bl_post_btn.setMaximumWidth(70)
        self._bl_post_btn.setStyleSheet(_tb_btn_style)
        self._bl_post_btn.setToolTip("Blacklist this post")
        self._bl_post_btn.clicked.connect(self.blacklist_post_requested)
        toolbar.addWidget(self._bl_post_btn)

        if not show_actions:
            # Library mode: only the Save button stays — it acts as
            # Unsave for the file currently being viewed. Bookmark and
            # blacklist actions are meaningless on already-saved local
            # files (no site/post id to bookmark, no search to filter).
            self._bookmark_btn.hide()
            self._bl_tag_btn.hide()
            self._bl_post_btn.hide()

        toolbar.addStretch()

        self._info_label = QLabel()  # kept for API compat but hidden in slideshow
        self._info_label.hide()

        self._toolbar.raise_()

        # Reparent video controls bar to central widget so it overlays properly.
        # The translucent overlay styling (background, transparent buttons,
        # white-on-dark text) lives in the bundled themes — see the
        # `Popout overlay bars` section of any themes/*.qss. The object names
        # are what those rules target.
        #
        # The toolbar's object name is set above, in its constructor block,
        # so the first style poll picks it up. The controls bar was already
        # polished as a child of VideoPlayer before being reparented here,
        # so we have to force an unpolish/polish round-trip after setting
        # its object name to make Qt re-evaluate the style with the new
        # `#_slideshow_controls` selector.
        self._video._controls_bar.setParent(central)
        self._video._controls_bar.setObjectName("_slideshow_controls")
        # Same fix as the toolbar above — plain QWidget needs this attribute
        # for the QSS `background: ${overlay_bg}` rule to render.
        self._video._controls_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        cb_style = self._video._controls_bar.style()
        cb_style.unpolish(self._video._controls_bar)
        cb_style.polish(self._video._controls_bar)
        # Same trick on the toolbar — it might have been polished by the
        # central widget's parent before our object name took effect.
        tb_style = self._toolbar.style()
        tb_style.unpolish(self._toolbar)
        tb_style.polish(self._toolbar)
        self._video._controls_bar.raise_()
        self._toolbar.raise_()

        # Privacy overlay — black QWidget child of central, raised over
        # the media stack on privacy_hide. Lives inside the popout
        # itself instead of forcing main_window to hide() the popout
        # window — Wayland's hide→show round-trip drops position because
        # the compositor unmaps and remaps, and Hyprland may re-tile the
        # remap depending on window rules. Keeping the popout mapped
        # with an in-place overlay sidesteps both issues.
        self._privacy_overlay = QWidget(central)
        self._privacy_overlay.setStyleSheet("background: black;")
        self._privacy_overlay.hide()

        # Auto-hide timer for overlay UI
        self._ui_visible = True
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(2000)
        self._hide_timer.timeout.connect(self._hide_overlay)
        self._hide_timer.start()
        self.setMouseTracking(True)
        central.setMouseTracking(True)
        self._stack.setMouseTracking(True)

        from PySide6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)
        # Pick target monitor
        target_screen = None
        if monitor and monitor != "Same as app":
            for screen in QApplication.screens():
                label = f"{screen.name()} ({screen.size().width()}x{screen.size().height()})"
                if label == monitor:
                    target_screen = screen
                    break
        if not target_screen and parent and parent.screen():
            target_screen = parent.screen()
        if target_screen:
            self.setScreen(target_screen)
            self.setGeometry(target_screen.geometry())
        self._adjusting = False
        # Position-restore handshake: setGeometry below seeds Qt with the saved
        # size, but Hyprland ignores the position for child windows. The first
        # _fit_to_content call after show() picks up _pending_position_restore
        # and corrects the position via a hyprctl batch (no race with the
        # resize). After that first fit, navigation center-pins from whatever
        # position the user has dragged the window to.
        self._first_fit_pending = True
        self._pending_position_restore: tuple[int, int] | None = None
        self._pending_size: tuple[int, int] | None = None
        # Persistent viewport — the user's intent for popout center + size.
        # Seeded from `_pending_size` + `_pending_position_restore` on the
        # first fit after open or F11 exit. Updated only by user action
        # (external drag/resize detected via cur-vs-last-dispatched
        # comparison on Hyprland, or via moveEvent/resizeEvent on
        # non-Hyprland). Navigation between posts NEVER writes to it —
        # `_derive_viewport_for_fit` returns it unchanged unless drift
        # has exceeded `_DRIFT_TOLERANCE`. This is what stops the
        # sub-pixel accumulation that the recompute-from-current-state
        # shortcut couldn't avoid.
        self._viewport: Viewport | None = None
        # Last (x, y, w, h) we dispatched to Hyprland (or to setGeometry
        # on non-Hyprland). Used to detect external moves: if the next
        # nav reads a current rect that differs by more than
        # _DRIFT_TOLERANCE, the user moved or resized the window
        # externally and we adopt the new state as the viewport's intent.
        self._last_dispatched_rect: tuple[int, int, int, int] | None = None
        # Reentrancy guard — set to True around every dispatch so the
        # moveEvent/resizeEvent handlers (which fire on the non-Hyprland
        # Qt fallback path) skip viewport updates triggered by our own
        # programmatic geometry changes.
        self._applying_dispatch: bool = False
        # Last known windowed geometry — captured on entering fullscreen so
        # F11 → windowed can land back on the same spot. Seeded from saved
        # geometry when the popout opens windowed, so even an immediate
        # F11 → fullscreen → F11 has a sensible target.
        self._windowed_geometry = None
        # Restore saved state or start fullscreen
        if FullscreenPreview._saved_geometry and not FullscreenPreview._saved_fullscreen:
            self.setGeometry(FullscreenPreview._saved_geometry)
            self._pending_position_restore = (
                FullscreenPreview._saved_geometry.x(),
                FullscreenPreview._saved_geometry.y(),
            )
            self._pending_size = (
                FullscreenPreview._saved_geometry.width(),
                FullscreenPreview._saved_geometry.height(),
            )
            self._windowed_geometry = FullscreenPreview._saved_geometry
            self.show()
        else:
            self.showFullScreen()

        # ---- State machine adapter wiring (commit 14a) ----
        # Construct the pure-Python state machine and dispatch the
        # initial Open event with the cross-popout-session class state
        # the legacy code stashed above. The state machine runs in
        # PARALLEL with the legacy imperative code: every Qt event
        # handler / mpv signal / button click below dispatches a state
        # machine event AND continues to run the existing imperative
        # action. The state machine's returned effects are LOGGED at
        # DEBUG, not applied to widgets. The legacy path stays
        # authoritative through commit 14a; commit 14b switches the
        # authority to the dispatch path.
        #
        # The grid_cols field is used by the keyboard nav handlers
        # for the Up/Down ±cols stride.
        self._state_machine = StateMachine()
        self._state_machine.grid_cols = grid_cols
        saved_geo_tuple = None
        if FullscreenPreview._saved_geometry:
            sg = FullscreenPreview._saved_geometry
            saved_geo_tuple = (sg.x(), sg.y(), sg.width(), sg.height())
        self._fsm_dispatch(Open(
            saved_geo=saved_geo_tuple,
            saved_fullscreen=bool(FullscreenPreview._saved_fullscreen),
            monitor=monitor,
        ))

        # Wire VideoPlayer's playback_restart Signal (added in commit 1)
        # to the adapter's dispatch routing. mpv emits playback-restart
        # once after each loadfile and once after each completed seek;
        # the adapter distinguishes by checking the state machine's
        # current state at dispatch time.
        self._video.playback_restart.connect(self._on_video_playback_restart)
        # Wire video EOF (already connected to play_next_requested
        # signal above) — additionally dispatch VideoEofReached.
        self._video.play_next.connect(
            lambda: self._fsm_dispatch(VideoEofReached())
        )
        # Wire video size known.
        self._video.video_size.connect(
            lambda w, h: self._fsm_dispatch(VideoSizeKnown(width=w, height=h))
        )
        # Wire seek slider clicks → SeekRequested.
        self._video._seek_slider.clicked_position.connect(
            lambda v: self._fsm_dispatch(SeekRequested(target_ms=v))
        )
        # Wire mute button → MuteToggleRequested. Dispatch BEFORE the
        # legacy _toggle_mute runs (which mutates VideoPlayer state)
        # so the dispatch reflects the user-intent edge.
        self._video._mute_btn.clicked.connect(
            lambda: self._fsm_dispatch(MuteToggleRequested())
        )
        # Wire volume slider → VolumeSet.
        self._video._vol_slider.valueChanged.connect(
            lambda v: self._fsm_dispatch(VolumeSet(value=v))
        )
        # Wire loop button → LoopModeSet. Dispatched AFTER the legacy
        # cycle so the new value is what we send.
        self._video._loop_btn.clicked.connect(
            lambda: self._fsm_dispatch(
                LoopModeSet(mode=LoopMode(self._video.loop_state))
            )
        )

    def _fsm_dispatch(self, event) -> list:
        """Dispatch an event to the state machine and log the result.

        Adapter-internal helper. Centralizes the dispatch + log path
        so every wire-point is one line. Returns the effect list for
        callers that want to inspect it (commit 14a doesn't use the
        return value; commit 14b will pattern-match and apply).

        The hasattr guard handles edge cases where Qt events might
        fire during __init__ (e.g. resizeEvent on the first show())
        before the state machine has been constructed. After
        construction the guard is always True.
        """
        if not hasattr(self, "_state_machine"):
            return []
        old_state = self._state_machine.state
        effects = self._state_machine.dispatch(event)
        new_state = self._state_machine.state
        _fsm_log.debug(
            "POPOUT_FSM %s | %s -> %s | effects=%s",
            type(event).__name__,
            old_state.name,
            new_state.name,
            [type(e).__name__ for e in effects],
        )
        return effects

    def _on_video_playback_restart(self) -> None:
        """mpv `playback-restart` event arrived (via VideoPlayer's
        playback_restart Signal added in commit 1). Distinguish
        VideoStarted (after load) from SeekCompleted (after seek) by
        the state machine's current state.

        This is the ONE place the adapter peeks at state to choose an
        event type — it's a read, not a write, and it's the price of
        having a single mpv event mean two different things.
        """
        if not hasattr(self, "_state_machine"):
            return
        if self._state_machine.state == State.LOADING_VIDEO:
            self._fsm_dispatch(VideoStarted())
        elif self._state_machine.state == State.SEEKING_VIDEO:
            self._fsm_dispatch(SeekCompleted())
        # Other states: drop. The state machine's release-mode
        # legality check would also drop it; this saves the dispatch
        # round trip.

    _saved_geometry = None  # remembers window size/position across opens
    _saved_fullscreen = False
    _current_tags: dict[str, list[str]] = {}
    _current_tag_list: list[str] = []

    def set_post_tags(self, tag_categories: dict[str, list[str]], tag_list: list[str]) -> None:
        self._current_tags = tag_categories
        self._current_tag_list = tag_list

    def _show_bl_tag_menu(self) -> None:
        menu = QMenu(self)
        if self._current_tags:
            for category, tags in self._current_tags.items():
                cat_menu = menu.addMenu(category)
                for tag in tags[:30]:
                    cat_menu.addAction(tag)
        else:
            for tag in self._current_tag_list[:30]:
                menu.addAction(tag)
        action = menu.exec(self._bl_tag_btn.mapToGlobal(self._bl_tag_btn.rect().bottomLeft()))
        if action:
            self.blacklist_tag_requested.emit(action.text())

    def update_state(self, bookmarked: bool, saved: bool) -> None:
        self._is_bookmarked = bookmarked
        self._bookmark_btn.setText("Unbookmark" if bookmarked else "Bookmark")
        self._bookmark_btn.setMaximumWidth(90 if bookmarked else 80)
        self._is_saved = saved
        self._save_btn.setText("Unsave" if saved else "Save")

    def set_bookmark_folders_callback(self, callback) -> None:
        """Wire the bookmark folder list source. Called once from app.py
        right after the popout is constructed; matches the embedded
        ImagePreview's set_bookmark_folders_callback shape.
        """
        self._bookmark_folders_callback = callback

    def set_folders_callback(self, callback) -> None:
        """Wire the library folder list source. Called once from app.py
        right after the popout is constructed; matches the embedded
        ImagePreview's set_folders_callback shape.
        """
        self._folders_callback = callback

    def _on_save_clicked(self) -> None:
        """Popout Save button — same shape as the embedded preview's
        version. When already saved, emit unsave_requested for the existing
        unsave path. When not saved, pop a menu under the button with
        Unfiled / library folders / + New Folder, then emit the chosen
        name through save_to_folder. app.py reuses _save_from_preview /
        _unsave_from_preview to handle both signals.
        """
        if self._is_saved:
            self.unsave_requested.emit()
            return
        menu = QMenu(self)
        unfiled = menu.addAction("Unfiled")
        menu.addSeparator()
        folder_actions: dict[int, str] = {}
        if self._folders_callback:
            for folder in self._folders_callback():
                a = menu.addAction(folder)
                folder_actions[id(a)] = folder
        menu.addSeparator()
        new_action = menu.addAction("+ New Folder...")
        action = menu.exec(self._save_btn.mapToGlobal(self._save_btn.rect().bottomLeft()))
        if not action:
            return
        if action == unfiled:
            self.save_to_folder.emit("")
        elif action == new_action:
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                self.save_to_folder.emit(name.strip())
        elif id(action) in folder_actions:
            self.save_to_folder.emit(folder_actions[id(action)])

    def _on_bookmark_clicked(self) -> None:
        """Popout Bookmark button — same shape as the embedded preview's
        version. When already bookmarked, emits bookmark_requested for the
        existing toggle/remove path. When not bookmarked, pops a menu under
        the button with Unfiled / bookmark folders / + New Folder, then
        emits the chosen name through bookmark_to_folder.
        """
        if self._is_bookmarked:
            self.bookmark_requested.emit()
            return
        menu = QMenu(self)
        unfiled = menu.addAction("Unfiled")
        menu.addSeparator()
        folder_actions: dict[int, str] = {}
        if self._bookmark_folders_callback:
            for folder in self._bookmark_folders_callback():
                a = menu.addAction(folder)
                folder_actions[id(a)] = folder
        menu.addSeparator()
        new_action = menu.addAction("+ New Folder...")
        action = menu.exec(self._bookmark_btn.mapToGlobal(self._bookmark_btn.rect().bottomLeft()))
        if not action:
            return
        if action == unfiled:
            self.bookmark_to_folder.emit("")
        elif action == new_action:
            name, ok = QInputDialog.getText(self, "New Bookmark Folder", "Folder name:")
            if ok and name.strip():
                self.bookmark_to_folder.emit(name.strip())
        elif id(action) in folder_actions:
            self.bookmark_to_folder.emit(folder_actions[id(action)])

    def set_media(self, path: str, info: str = "", width: int = 0, height: int = 0) -> None:
        """Display `path` in the popout, info string above it.

        `width` and `height` are the *known* media dimensions from the
        post metadata (booru API), passed in by the caller when
        available. They're used to pre-fit the popout window for video
        files BEFORE mpv has loaded the file, so cached videos don't
        flash a wrong-shaped black surface while mpv decodes the first
        frame. mpv still fires `video_size` after demuxing and the
        second `_fit_to_content` call corrects the aspect if the
        encoded video-params differ from the API metadata (rare —
        anamorphic / weirdly cropped sources). Both fits use the
        persistent viewport's same `long_side` and the same center,
        so the second fit is a no-op in the common case and only
        produces a shape correction (no positional move) in the
        mismatch case.
        """
        self._info_label.setText(info)
        ext = Path(path).suffix.lower()

        # State machine dispatch (parallel — legacy code below stays
        # authoritative through commit 14a).
        if _is_video(path):
            kind = MediaKind.VIDEO
        elif ext == ".gif":
            kind = MediaKind.GIF
        else:
            kind = MediaKind.IMAGE
        # Detect streaming URL → set referer for the dispatch payload.
        # This matches the per-file referrer the legacy play_file path
        # already sets at media/video_player.py:343-347.
        referer = None
        if path.startswith(("http://", "https://")):
            try:
                from urllib.parse import urlparse
                from ...core.cache import _referer_for
                referer = _referer_for(urlparse(path))
            except Exception:
                pass
        self._fsm_dispatch(ContentArrived(
            path=path,
            info=info,
            kind=kind,
            width=width,
            height=height,
            referer=referer,
        ))

        if _is_video(path):
            self._viewer.clear()
            self._video.stop()
            self._video.play_file(path, info)
            self._stack.setCurrentIndex(1)
            # NOTE: pre-fit to API dimensions was tried here (option A
            # from the perf round) but caused a perceptible slowdown
            # in popout video clicks — the redundant second hyprctl
            # dispatch when mpv's video_size callback fired produced
            # a visible re-settle. The width/height params remain on
            # the signature so the streaming and update-fullscreen
            # call sites can keep passing them, but they're currently
            # ignored. Re-enable cautiously if you can prove the
            # second fit becomes a true no-op.
            _ = (width, height)  # accepted but unused for now
        else:
            self._video.stop()
            self._video._controls_bar.hide()
            if ext == ".gif":
                self._viewer.set_gif(path, info)
            else:
                pix = QPixmap(path)
                if not pix.isNull():
                    self._viewer.set_image(pix, info)
            self._stack.setCurrentIndex(0)
            # Adjust window to content aspect ratio
            if not self.isFullScreen():
                pix = self._viewer._pixmap
                if pix and not pix.isNull():
                    self._fit_to_content(pix.width(), pix.height())
        # Note: do NOT auto-show the overlay on every set_media. The
        # overlay should appear in response to user hover (handled in
        # eventFilter on mouse-move into the top/bottom edge zones),
        # not pop back up after every navigation. First popout open
        # already starts with _ui_visible = True and the auto-hide
        # timer running, so the user sees the controls for ~2s on
        # first open and then they stay hidden until hover.

    def _on_video_size(self, w: int, h: int) -> None:
        if not self.isFullScreen() and w > 0 and h > 0:
            self._fit_to_content(w, h)

    def _is_hypr_floating(self) -> bool | None:
        """Check if this window is floating in Hyprland. None if not on Hyprland."""
        win = self._hyprctl_get_window()
        if win is None:
            return None  # not Hyprland
        return bool(win.get("floating"))

    @staticmethod
    def _compute_window_rect(
        viewport: Viewport, content_aspect: float, screen
    ) -> tuple[int, int, int, int]:
        """Project a viewport onto a window rect for the given content aspect.

        Symmetric across portrait/landscape: a 9:16 portrait and a 16:9
        landscape with the same `long_side` have the same maximum edge
        length. Proportional clamp shrinks both edges by the same factor
        if either would exceed its 0.90-of-screen ceiling, preserving
        aspect exactly. Pure function — no side effects, no widget
        access, all inputs explicit so it's trivial to reason about.
        """
        if content_aspect >= 1.0:               # landscape or square
            w = viewport.long_side
            h = viewport.long_side / content_aspect
        else:                                   # portrait
            h = viewport.long_side
            w = viewport.long_side * content_aspect

        avail = screen.availableGeometry()
        cap_w = avail.width() * 0.90
        cap_h = avail.height() * 0.90
        scale = min(1.0, cap_w / w, cap_h / h)
        w *= scale
        h *= scale

        x = viewport.center_x - w / 2
        y = viewport.center_y - h / 2

        # Nudge onto screen if the projected rect would land off-edge.
        x = max(avail.x(), min(x, avail.right() - w))
        y = max(avail.y(), min(y, avail.bottom() - h))

        return (round(x), round(y), round(w), round(h))

    def _build_viewport_from_current(
        self, floating: bool | None, win: dict | None = None
    ) -> Viewport | None:
        """Build a viewport from the current window state, no caching.

        Used in two cases:
          1. First fit after open / F11 exit, when the persistent
             `_viewport` is None and we need a starting value (the
             `_pending_*` one-shots feed this path).
          2. The "user moved the window externally" detection branch
             in `_derive_viewport_for_fit`, when the cur-vs-last-dispatched
             comparison shows drift > _DRIFT_TOLERANCE.

        Returns None only if every source fails — Hyprland reports no
        window AND non-Hyprland Qt geometry is also invalid.
        """
        if floating is True:
            if win is None:
                win = self._hyprctl_get_window()
            if win and win.get("at") and win.get("size"):
                wx, wy = win["at"]
                ww, wh = win["size"]
                return Viewport(
                    center_x=wx + ww / 2,
                    center_y=wy + wh / 2,
                    long_side=float(max(ww, wh)),
                )
        if floating is None:
            rect = self.geometry()
            if rect.width() > 0 and rect.height() > 0:
                return Viewport(
                    center_x=rect.x() + rect.width() / 2,
                    center_y=rect.y() + rect.height() / 2,
                    long_side=float(max(rect.width(), rect.height())),
                )
        return None

    def _derive_viewport_for_fit(
        self, floating: bool | None, win: dict | None = None
    ) -> Viewport | None:
        """Return the persistent viewport, updating it only on user action.

        Three branches in priority order:

          1. **First fit after open or F11 exit**: the `_pending_*`
             one-shots are set. Seed `_viewport` from them and return.
             This is the only path that overwrites the persistent
             viewport unconditionally.

          2. **Persistent viewport exists and is in agreement with
             current window state**: return it unchanged. The compute
             never reads its own output as input — sub-pixel drift
             cannot accumulate here because we don't observe it.

          3. **Persistent viewport exists but current state differs by
             more than `_DRIFT_TOLERANCE`**: the user moved or resized
             the window externally (Super+drag in Hyprland, corner-resize,
             window manager intervention). Update the viewport from
             current state — the user's new physical position IS the
             new intent.

        Wayland external moves don't fire Qt's `moveEvent`, so branch 3
        is the only mechanism that captures Hyprland Super+drag. The
        `_last_dispatched_rect` cache is what makes branch 2 stable —
        without it, we'd have to read current state and compare to the
        viewport's projection (the same code path that drifts).

        `win` may be passed in by the caller to avoid an extra
        `_hyprctl_get_window()` subprocess call (~3ms saved).
        """
        # Branch 1: first fit after open or F11 exit
        if self._first_fit_pending and self._pending_size and self._pending_position_restore:
            pw, ph = self._pending_size
            px, py = self._pending_position_restore
            self._viewport = Viewport(
                center_x=px + pw / 2,
                center_y=py + ph / 2,
                long_side=float(max(pw, ph)),
            )
            return self._viewport

        # No persistent viewport yet AND no first-fit one-shots — defensive
        # fallback. Build from current state and stash for next call.
        if self._viewport is None:
            self._viewport = self._build_viewport_from_current(floating, win)
            return self._viewport

        # Branch 2/3: persistent viewport exists. Check whether the user
        # moved or resized the window externally since our last dispatch.
        if floating is True and self._last_dispatched_rect is not None:
            if win is None:
                win = self._hyprctl_get_window()
            if win and win.get("at") and win.get("size"):
                cur_x, cur_y = win["at"]
                cur_w, cur_h = win["size"]
                last_x, last_y, last_w, last_h = self._last_dispatched_rect
                drift = max(
                    abs(cur_x - last_x),
                    abs(cur_y - last_y),
                    abs(cur_w - last_w),
                    abs(cur_h - last_h),
                )
                if drift > _DRIFT_TOLERANCE:
                    # External move/resize detected. Adopt current as intent.
                    self._viewport = Viewport(
                        center_x=cur_x + cur_w / 2,
                        center_y=cur_y + cur_h / 2,
                        long_side=float(max(cur_w, cur_h)),
                    )

        return self._viewport

    def _fit_to_content(self, content_w: int, content_h: int, _retry: int = 0) -> None:
        """Size window to fit content. Viewport-based: long_side preserved across navs.

        Distinguishes "not on Hyprland" (Qt drives geometry, no aspect
        lock available) from "on Hyprland but the window isn't visible
        to hyprctl yet" (the very first call after a popout open races
        the wm:openWindow event — `hyprctl clients -j` returns no entry
        for our title for ~tens of ms). The latter case used to fall
        through to a plain Qt resize and skip the keep_aspect_ratio
        setprop entirely, so the *first* image popout always opened
        without aspect locking and only subsequent navigations got the
        right shape. Now we retry with a short backoff when on Hyprland
        and the window isn't found, capped so a real "not Hyprland"
        signal can't loop.

        Math is now viewport-based: a Viewport (center + long_side) is
        derived from current state, then projected onto a rect for the
        new content aspect via `_compute_window_rect`. This breaks the
        width-anchor ratchet that the previous version had — long_side
        is symmetric across portrait and landscape, so navigating
        P→L→P→L doesn't permanently shrink the landscape width.
        See the plan at ~/.claude/plans/ancient-growing-lantern.md
        for the full derivation.
        """
        if self.isFullScreen() or content_w <= 0 or content_h <= 0:
            return
        import os
        on_hypr = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
        # Cache the hyprctl window query — `_hyprctl_get_window()` is a
        # ~3ms subprocess.run call on the GUI thread, and the helpers
        # below would each fire it again if we didn't pass it down.
        # Threading the dict through cuts the per-fit subprocess count
        # from three to one, eliminating ~6ms of UI freeze per navigation.
        win = None
        if on_hypr:
            win = self._hyprctl_get_window()
            if win is None:
                if _retry < 5:
                    QTimer.singleShot(
                        40,
                        lambda: self._fit_to_content(content_w, content_h, _retry + 1),
                    )
                return
            floating = bool(win.get("floating"))
        else:
            floating = None
        if floating is False:
            self._hyprctl_resize(0, 0)  # tiled: just set keep_aspect_ratio
            return
        aspect = content_w / content_h
        screen = self.screen()
        if screen is None:
            return
        viewport = self._derive_viewport_for_fit(floating, win=win)
        if viewport is None:
            # No source for a viewport (Hyprland reported no window AND
            # Qt geometry is invalid). Bail without dispatching — clearing
            # the one-shots would lose the saved position; leaving them
            # set lets a subsequent fit retry.
            return
        x, y, w, h = self._compute_window_rect(viewport, aspect, screen)
        # Identical-rect skip. If the computed rect is exactly what
        # we last dispatched, the window is already in that state and
        # there's nothing for hyprctl (or setGeometry) to do. Skipping
        # saves one subprocess.Popen + Hyprland's processing of the
        # redundant resize/move dispatch — ~100-300ms of perceived
        # latency on cached video clicks where the new content has the
        # same aspect/long_side as the previous, which is common (back-
        # to-back videos from the same source, image→video with matching
        # aspect, re-clicking the same post). Doesn't apply on the very
        # first fit after open (last_dispatched_rect is None) and the
        # first dispatch always lands. Doesn't break drift detection
        # because the comparison branch in _derive_viewport_for_fit
        # already ran above and would have updated _viewport (and
        # therefore the computed rect) if Hyprland reported drift.
        if self._last_dispatched_rect == (x, y, w, h):
            self._first_fit_pending = False
            self._pending_position_restore = None
            self._pending_size = None
            return
        # Reentrancy guard: set before any dispatch so the
        # moveEvent/resizeEvent handlers (which fire on the non-Hyprland
        # Qt fallback path) don't update the persistent viewport from
        # our own programmatic geometry change.
        self._applying_dispatch = True
        try:
            if floating is True:
                # Hyprland: hyprctl is the sole authority. Calling self.resize()
                # here would race with the batch below and produce visible flashing
                # when the window also has to move.
                self._hyprctl_resize_and_move(w, h, x, y, win=win)
            else:
                # Non-Hyprland fallback: Qt drives geometry directly. Use
                # setGeometry with the computed top-left rather than resize()
                # so the window center stays put — Qt's resize() anchors
                # top-left and lets the bottom-right move, which causes the
                # popout center to drift toward the upper-left of the screen
                # over repeated navigations.
                self.setGeometry(QRect(x, y, w, h))
        finally:
            self._applying_dispatch = False
        # Cache the dispatched rect so the next nav can compare current
        # Hyprland state against it and detect external moves/resizes.
        # This is the persistent-viewport's link back to reality without
        # reading our own output every nav.
        self._last_dispatched_rect = (x, y, w, h)
        self._first_fit_pending = False
        self._pending_position_restore = None
        self._pending_size = None

    def _show_overlay(self) -> None:
        """Show toolbar and video controls, restart auto-hide timer."""
        if not self._ui_visible:
            self._toolbar.show()
            if self._stack.currentIndex() == 1:
                self._video._controls_bar.show()
            self._ui_visible = True
        self._hide_timer.start()

    def _hide_overlay(self) -> None:
        """Hide toolbar and video controls."""
        self._toolbar.hide()
        self._video._controls_bar.hide()
        self._ui_visible = False

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QLineEdit, QTextEdit, QSpinBox, QComboBox
        if event.type() == QEvent.Type.KeyPress:
            # Only intercept when slideshow is the active window
            if not self.isActiveWindow():
                return super().eventFilter(obj, event)
            # Don't intercept keys when typing in text inputs
            if isinstance(obj, (QLineEdit, QTextEdit, QSpinBox, QComboBox)):
                return super().eventFilter(obj, event)
            key = event.key()
            mods = event.modifiers()
            if key == Qt.Key.Key_P and mods & Qt.KeyboardModifier.ControlModifier:
                self.privacy_requested.emit()
                return True
            elif key == Qt.Key.Key_H and mods & Qt.KeyboardModifier.ControlModifier:
                if self._ui_visible:
                    self._hide_timer.stop()
                    self._hide_overlay()
                else:
                    self._show_overlay()
                return True
            elif key in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
                self._fsm_dispatch(CloseRequested())
                self.close()
                return True
            elif key in (Qt.Key.Key_Left, Qt.Key.Key_H):
                self._fsm_dispatch(NavigateRequested(direction=-1))
                self.navigate.emit(-1)
                return True
            elif key in (Qt.Key.Key_Right, Qt.Key.Key_L):
                self._fsm_dispatch(NavigateRequested(direction=1))
                self.navigate.emit(1)
                return True
            elif key in (Qt.Key.Key_Up, Qt.Key.Key_K):
                self._fsm_dispatch(NavigateRequested(direction=-self._grid_cols))
                self.navigate.emit(-self._grid_cols)
                return True
            elif key in (Qt.Key.Key_Down, Qt.Key.Key_J):
                self._fsm_dispatch(NavigateRequested(direction=self._grid_cols))
                self.navigate.emit(self._grid_cols)
                return True
            elif key == Qt.Key.Key_F11:
                self._fsm_dispatch(FullscreenToggled())
                if self.isFullScreen():
                    self._exit_fullscreen()
                else:
                    self._enter_fullscreen()
                return True
            elif key == Qt.Key.Key_Space and self._stack.currentIndex() == 1:
                self._fsm_dispatch(TogglePlayRequested())
                self._video._toggle_play()
                return True
            elif key == Qt.Key.Key_Period and self._stack.currentIndex() == 1:
                # +/- keys are seek-relative, NOT slider-pin seeks. The
                # state machine's SeekRequested is for slider-driven
                # seeks. The +/- keys go straight to mpv via the
                # legacy path; the dispatch path doesn't see them in
                # 14a (commit 14b will route them through SeekRequested
                # with a target_ms computed from current position).
                self._video._seek_relative(1800)
                return True
            elif key == Qt.Key.Key_Comma and self._stack.currentIndex() == 1:
                self._video._seek_relative(-1800)
                return True
        if event.type() == QEvent.Type.Wheel and self.isActiveWindow():
            # Horizontal tilt navigates between posts on either stack
            tilt = event.angleDelta().x()
            if tilt > 30:
                self._fsm_dispatch(NavigateRequested(direction=-1))
                self.navigate.emit(-1)
                return True
            if tilt < -30:
                self._fsm_dispatch(NavigateRequested(direction=1))
                self.navigate.emit(1)
                return True
            # Vertical wheel adjusts volume on the video stack only
            if self._stack.currentIndex() == 1:
                delta = event.angleDelta().y()
                if delta:
                    vol = max(0, min(100, self._video.volume + (5 if delta > 0 else -5)))
                    self._fsm_dispatch(VolumeSet(value=vol))
                    self._video.volume = vol
                    self._show_overlay()
                    return True
        if event.type() == QEvent.Type.MouseMove and self.isActiveWindow():
            # Map cursor position to window coordinates
            cursor_pos = self.mapFromGlobal(event.globalPosition().toPoint() if hasattr(event, 'globalPosition') else event.globalPos())
            y = cursor_pos.y()
            h = self.height()
            zone = 40  # px from top/bottom edge to trigger
            if y < zone:
                self._toolbar.show()
                self._hide_timer.start()
            elif y > h - zone and self._stack.currentIndex() == 1:
                self._video._controls_bar.show()
                self._hide_timer.start()
            self._ui_visible = self._toolbar.isVisible() or self._video._controls_bar.isVisible()
        return super().eventFilter(obj, event)

    # Hyprland helpers — moved to popout/hyprland.py in commit 13. These
    # methods are now thin shims around the module-level functions so
    # the existing call sites in this file (`_fit_to_content`,
    # `_enter_fullscreen`, `closeEvent`) keep working byte-for-byte.
    # Commit 14's adapter rewrite drops the shims and calls the
    # hyprland module directly.

    def _hyprctl_get_window(self) -> dict | None:
        """Shim → `popout.hyprland.get_window`."""
        from . import hyprland
        return hyprland.get_window(self.windowTitle())

    def _hyprctl_resize(self, w: int, h: int) -> None:
        """Shim → `popout.hyprland.resize`."""
        from . import hyprland
        hyprland.resize(self.windowTitle(), w, h)

    def _hyprctl_resize_and_move(
        self, w: int, h: int, x: int, y: int, win: dict | None = None
    ) -> None:
        """Shim → `popout.hyprland.resize_and_move`."""
        from . import hyprland
        hyprland.resize_and_move(self.windowTitle(), w, h, x, y, win=win)

    def privacy_hide(self) -> None:
        """Cover the popout's content with a black overlay for privacy.

        The popout window itself is NOT hidden — Wayland's hide→show
        round-trip drops position because the compositor unmaps and
        remaps the window, and Hyprland may re-tile the remapped window
        depending on its rules. Instead we raise an in-place black
        QWidget overlay over the central widget. The window stays
        mapped, position is preserved automatically, video is paused.
        """
        if self._stack.currentIndex() == 1:
            self._video.pause()
        central = self.centralWidget()
        if central is not None:
            self._privacy_overlay.setGeometry(0, 0, central.width(), central.height())
        self._privacy_overlay.raise_()
        self._privacy_overlay.show()

    def privacy_show(self) -> None:
        """Lift the black overlay and resume video. Counterpart to privacy_hide."""
        self._privacy_overlay.hide()
        if self._stack.currentIndex() == 1:
            self._video.resume()

    def _enter_fullscreen(self) -> None:
        """Enter fullscreen — capture windowed geometry first so F11 back can restore it.

        Also capture the current windowed state into the persistent
        `_viewport` so the F11-exit restore lands at the user's actual
        pre-F11 position, not at a stale viewport from before they last
        dragged the window. The drift detection in `_derive_viewport_for_fit`
        only fires when `_last_dispatched_rect` is set AND a fit is being
        computed — neither path catches the "user dragged the popout
        with Super+drag and then immediately pressed F11" sequence,
        because Hyprland Super+drag doesn't fire Qt's moveEvent and no
        nav has happened to trigger a fit. Capturing fresh into
        `_viewport` here makes the restore correct regardless.
        """
        from PySide6.QtCore import QRect
        win = self._hyprctl_get_window()
        if win and win.get("at") and win.get("size"):
            x, y = win["at"]
            w, h = win["size"]
            self._windowed_geometry = QRect(x, y, w, h)
            self._viewport = Viewport(
                center_x=x + w / 2,
                center_y=y + h / 2,
                long_side=float(max(w, h)),
            )
        else:
            self._windowed_geometry = self.frameGeometry()
            rect = self._windowed_geometry
            if rect.width() > 0 and rect.height() > 0:
                self._viewport = Viewport(
                    center_x=rect.x() + rect.width() / 2,
                    center_y=rect.y() + rect.height() / 2,
                    long_side=float(max(rect.width(), rect.height())),
                )
        self.showFullScreen()

    def _exit_fullscreen(self) -> None:
        """Leave fullscreen — let the persistent viewport drive the restore.

        With the Group B persistent viewport in place, F11 exit no longer
        needs to re-arm the `_first_fit_pending` one-shots. The viewport
        already holds the pre-fullscreen center + long_side from before
        the user pressed F11 — fullscreen entry doesn't write to it,
        and nothing during fullscreen does either (no `_fit_to_content`
        runs while `isFullScreen()` is True). So the next deferred fit
        after `showNormal()` reads the persistent viewport, computes the
        new windowed rect for the current content's aspect, and dispatches
        — landing at the pre-fullscreen CENTER with the new shape, which
        also fixes the legacy F11-walks-toward-saved-top-left bug 1f as a
        side effect of the Group B refactor.

        We still need to invalidate `_last_dispatched_rect` because the
        cached value is from the pre-fullscreen window, and after F11
        Hyprland may report a different position before the deferred fit
        catches up — we don't want the drift detector to think the user
        moved the window externally during fullscreen.
        """
        content_w, content_h = 0, 0
        if self._stack.currentIndex() == 1:
            mpv = self._video._mpv
            if mpv:
                try:
                    vp = mpv.video_params
                    if vp and vp.get('w') and vp.get('h'):
                        content_w, content_h = vp['w'], vp['h']
                except Exception:
                    pass
        else:
            pix = self._viewer._pixmap
            if pix and not pix.isNull():
                content_w, content_h = pix.width(), pix.height()
        FullscreenPreview._saved_fullscreen = False
        # Invalidate the cache so the next fit doesn't false-positive on
        # "user moved the window during fullscreen". The persistent
        # viewport stays as-is and will drive the restore.
        self._last_dispatched_rect = None
        self.showNormal()
        if content_w > 0 and content_h > 0:
            # Defer to next event-loop tick so Qt's showNormal() is processed
            # by Hyprland before our hyprctl batch fires. Without this defer
            # the two race and the window lands at top-left.
            QTimer.singleShot(0, lambda: self._fit_to_content(content_w, content_h))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Position floating overlays
        w = self.centralWidget().width()
        h = self.centralWidget().height()
        tb_h = self._toolbar.sizeHint().height()
        self._toolbar.setGeometry(0, 0, w, tb_h)
        ctrl_h = self._video._controls_bar.sizeHint().height()
        self._video._controls_bar.setGeometry(0, h - ctrl_h, w, ctrl_h)
        # Privacy overlay covers the entire central widget when active.
        if self._privacy_overlay.isVisible():
            self._privacy_overlay.setGeometry(0, 0, w, h)
        # Capture corner-resize into the persistent viewport so the
        # long_side the user chose survives subsequent navigations.
        #
        # GATED TO NON-HYPRLAND. On Wayland (Hyprland included), Qt
        # cannot know the window's absolute screen position — xdg-toplevel
        # doesn't expose it to clients — so `self.geometry()` returns
        # `QRect(0, 0, w, h)` regardless of where the compositor actually
        # placed the window. If we let this branch run on Hyprland, every
        # configure event from a hyprctl dispatch (or from the user's
        # Super+drag, or from `showNormal()` exiting fullscreen) would
        # corrupt the viewport center to ~(w/2, h/2) — a small positive
        # number far from the screen center — and the next dispatch
        # would project that bogus center, edge-nudge it, and land at
        # the top-left. Bug observed during the Group B viewport rollout.
        #
        # The `_applying_dispatch` guard catches the synchronous
        # non-Hyprland setGeometry path (where moveEvent fires inside
        # the try/finally block). It does NOT catch the async Hyprland
        # path because Popen returns instantly and the configure-event
        # → moveEvent round-trip happens later. The Hyprland gate
        # below is the actual fix; the `_applying_dispatch` guard
        # remains for the non-Hyprland path.
        #
        # On Hyprland, external drags/resizes are picked up by the
        # cur-vs-last-dispatched comparison in `_derive_viewport_for_fit`,
        # which reads `hyprctl clients -j` (the only reliable absolute
        # position source on Wayland).
        import os
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return
        if self._applying_dispatch or self.isFullScreen():
            return
        rect = self.geometry()
        if rect.width() > 0 and rect.height() > 0:
            self._viewport = Viewport(
                center_x=rect.x() + rect.width() / 2,
                center_y=rect.y() + rect.height() / 2,
                long_side=float(max(rect.width(), rect.height())),
            )
            # Parallel state machine dispatch for the same event.
            self._fsm_dispatch(WindowResized(rect=(
                rect.x(), rect.y(), rect.width(), rect.height(),
            )))

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        # Capture user drags into the persistent viewport on the
        # non-Hyprland Qt path.
        #
        # GATED TO NON-HYPRLAND for the same reason as resizeEvent —
        # `self.geometry()` is unreliable on Wayland. See the long
        # comment in resizeEvent above for the full diagnosis. On
        # Hyprland, drag detection happens via the cur-vs-last-dispatched
        # comparison in `_derive_viewport_for_fit` instead.
        import os
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return
        if self._applying_dispatch or self.isFullScreen():
            return
        if self._viewport is None:
            return
        rect = self.geometry()
        if rect.width() > 0 and rect.height() > 0:
            # Move-only update: keep the existing long_side, just
            # update the center to where the window now sits.
            self._viewport = Viewport(
                center_x=rect.x() + rect.width() / 2,
                center_y=rect.y() + rect.height() / 2,
                long_side=self._viewport.long_side,
            )
            # Parallel state machine dispatch for the same event.
            self._fsm_dispatch(WindowMoved(rect=(
                rect.x(), rect.y(), rect.width(), rect.height(),
            )))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Pre-warm the mpv GL render context as soon as the popout is
        # mapped, so the first video click doesn't pay for GL context
        # creation (~100-200ms one-time cost). The widget needs to be
        # visible for `makeCurrent()` to succeed, which is what showEvent
        # gives us. ensure_gl_init is idempotent — re-shows after a
        # close/reopen are cheap no-ops.
        try:
            self._video._gl_widget.ensure_gl_init()
        except Exception:
            # If GL pre-warm fails (driver weirdness, headless test),
            # play_file's lazy ensure_gl_init still runs as a fallback.
            pass

    def closeEvent(self, event) -> None:
        from PySide6.QtWidgets import QApplication
        # Parallel state machine dispatch — Closing is terminal in
        # the state machine, every subsequent dispatch will be a no-op.
        self._fsm_dispatch(CloseRequested())
        # Save window state for next open
        FullscreenPreview._saved_fullscreen = self.isFullScreen()
        if not self.isFullScreen():
            # On Hyprland, Qt doesn't know the real position — ask the WM
            win = self._hyprctl_get_window()
            if win and win.get("at") and win.get("size"):
                from PySide6.QtCore import QRect
                x, y = win["at"]
                w, h = win["size"]
                FullscreenPreview._saved_geometry = QRect(x, y, w, h)
            else:
                FullscreenPreview._saved_geometry = self.frameGeometry()
        QApplication.instance().removeEventFilter(self)
        self.closed.emit()
        self._video.stop()
        super().closeEvent(event)
