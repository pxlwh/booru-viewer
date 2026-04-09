"""mpv-backed video player widget with transport controls."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal, Property
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QStyle,
)

import mpv as mpvlib

from .mpv_gl import _MpvGLWidget


class _ClickSeekSlider(QSlider):
    """Slider that jumps to the clicked position instead of page-stepping."""
    clicked_position = Signal(int)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            val = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), int(event.position().x()), self.width()
            )
            self.setValue(val)
            self.clicked_position.emit(val)
        super().mousePressEvent(event)


# -- Video Player (mpv backend via OpenGL render API) --


class VideoPlayer(QWidget):
    """Video player with transport controls, powered by mpv."""

    play_next = Signal()       # emitted when video ends in "Next" mode
    media_ready = Signal()     # emitted when media is loaded and duration is known
    video_size = Signal(int, int)  # (width, height) emitted when video dimensions are known
    # Emitted whenever mpv fires its `playback-restart` event. This event
    # arrives once after each loadfile (when playback actually starts
    # producing frames) and once after each completed seek. The popout's
    # state machine adapter listens to this signal and dispatches either
    # VideoStarted or SeekCompleted depending on which state it's in
    # (LoadingVideo vs SeekingVideo). The pre-state-machine code did not
    # need this signal because it used a 500ms timestamp window to fake
    # a seek-done edge; the state machine refactor replaces that window
    # with this real event. Probe results in docs/POPOUT_REFACTOR_PLAN.md
    # confirm exactly one event per load and one per seek.
    playback_restart = Signal()

    # QSS-controllable letterbox / pillarbox color. mpv paints the area
    # around the video frame in this color instead of the default black,
    # so portrait videos in a landscape preview slot (or vice versa) blend
    # into the panel theme instead of sitting in a hard black box.
    # Set via `VideoPlayer { qproperty-letterboxColor: ${bg}; }` in a theme.
    # The class default below is just a fallback; __init__ replaces it
    # with the current palette's Window color so systems without a custom
    # QSS (e.g. Windows dark/light mode driven entirely by QPalette) get
    # a letterbox that automatically matches the OS background.
    _letterbox_color = QColor("#000000")

    def _get_letterbox_color(self): return self._letterbox_color
    def _set_letterbox_color(self, c):
        self._letterbox_color = QColor(c) if isinstance(c, str) else c
        self._apply_letterbox_color()
    letterboxColor = Property(QColor, _get_letterbox_color, _set_letterbox_color)

    def _apply_letterbox_color(self) -> None:
        """Push the current letterbox color into mpv. No-op if mpv hasn't
        been initialized yet — _ensure_mpv() calls this after creating the
        instance so a QSS-set property still takes effect on first use."""
        if self._mpv is None:
            return
        try:
            self._mpv['background'] = 'color'
            self._mpv['background-color'] = self._letterbox_color.name()
        except Exception:
            pass

    def __init__(self, parent: QWidget | None = None, embed_controls: bool = True) -> None:
        """
        embed_controls: When True (default), the transport controls bar is
        added to this VideoPlayer's own layout below the video — used by the
        popout window which then reparents the bar to its overlay layer.
        When False, the controls bar is constructed but never inserted into
        any layout, leaving the embedded preview a clean video surface with
        no transport controls visible. Use the popout for playback control.
        """
        super().__init__(parent)
        # Initialize the letterbox color from the current palette's Window
        # role so dark/light mode (or any system without a custom QSS)
        # gets a sensible default that matches the surrounding panel.
        # The QSS qproperty-letterboxColor on the bundled themes still
        # overrides this — Qt calls the setter during widget polish,
        # which happens AFTER __init__ when the widget is shown.
        from PySide6.QtGui import QPalette
        self._letterbox_color = self.palette().color(QPalette.ColorRole.Window)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Video surface — mpv renders via OpenGL render API
        self._gl_widget = _MpvGLWidget()
        layout.addWidget(self._gl_widget, stretch=1)

        # mpv reference (set by _ensure_mpv)
        self._mpv: mpvlib.MPV | None = None

        # Controls bar — in preview panel this sits in the layout normally;
        # in slideshow mode, FullscreenPreview reparents it as a floating overlay.
        self._controls_bar = QWidget(self)
        controls = QHBoxLayout(self._controls_bar)
        controls.setContentsMargins(4, 2, 4, 2)

        # Compact-padding override matches the top preview toolbar so the
        # bottom controls bar reads as part of the same panel rather than
        # as a stamped-in overlay. Bundled themes' default `padding: 5px 12px`
        # is too wide for short labels in narrow button slots.
        _ctrl_btn_style = "padding: 2px 6px;"

        self._play_btn = QPushButton("Play")
        self._play_btn.setMaximumWidth(65)
        self._play_btn.setStyleSheet(_ctrl_btn_style)
        self._play_btn.clicked.connect(self._toggle_play)
        controls.addWidget(self._play_btn)

        self._time_label = QLabel("0:00")
        self._time_label.setMaximumWidth(45)
        controls.addWidget(self._time_label)

        self._seek_slider = _ClickSeekSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.sliderMoved.connect(self._seek)
        self._seek_slider.clicked_position.connect(self._seek)
        controls.addWidget(self._seek_slider, stretch=1)

        self._duration_label = QLabel("0:00")
        self._duration_label.setMaximumWidth(45)
        controls.addWidget(self._duration_label)

        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(50)
        self._vol_slider.setFixedWidth(60)
        self._vol_slider.valueChanged.connect(self._set_volume)
        controls.addWidget(self._vol_slider)

        self._mute_btn = QPushButton("Mute")
        self._mute_btn.setMaximumWidth(80)
        self._mute_btn.setStyleSheet(_ctrl_btn_style)
        self._mute_btn.clicked.connect(self._toggle_mute)
        controls.addWidget(self._mute_btn)

        self._autoplay = True
        self._autoplay_btn = QPushButton("Auto")
        self._autoplay_btn.setMaximumWidth(70)
        self._autoplay_btn.setStyleSheet(_ctrl_btn_style)
        self._autoplay_btn.setCheckable(True)
        self._autoplay_btn.setChecked(True)
        self._autoplay_btn.setToolTip("Auto-play videos when selected")
        self._autoplay_btn.clicked.connect(self._toggle_autoplay)
        self._autoplay_btn.hide()
        controls.addWidget(self._autoplay_btn)

        self._loop_state = 0  # 0=Loop, 1=Once, 2=Next
        self._loop_btn = QPushButton("Loop")
        self._loop_btn.setMaximumWidth(60)
        self._loop_btn.setStyleSheet(_ctrl_btn_style)
        self._loop_btn.setToolTip("Loop: repeat / Once: stop at end / Next: advance")
        self._loop_btn.clicked.connect(self._cycle_loop)
        controls.addWidget(self._loop_btn)

        # NO styleSheet here. The popout (FullscreenPreview) re-applies its
        # own `_slideshow_controls` overlay styling after reparenting the
        # bar to its central widget — see FullscreenPreview.__init__ — so
        # the popout still gets the floating dark-translucent look. The
        # embedded preview leaves the bar unstyled so it inherits the
        # panel theme and visually matches the Bookmark/Save/BL Tag bar
        # at the top of the panel rather than looking like a stamped-in
        # overlay box.
        if embed_controls:
            layout.addWidget(self._controls_bar)

        self._eof_pending = False
        # Stale-eof suppression window. mpv emits `eof-reached=True`
        # whenever a file ends — including via `command('stop')` —
        # and the observer fires asynchronously on mpv's event thread.
        # When set_media swaps to a new file, the previous file's stop
        # generates an eof event that can race with `play_file`'s
        # `_eof_pending = False` reset and arrive AFTER it, sticking
        # the bool back to True. The next `_poll` then runs
        # `_handle_eof` and emits `play_next` in Loop=Next mode →
        # auto-advance past the post the user wanted → SKIP.
        #
        # Fix: ignore eof events for `_eof_ignore_window_secs` after
        # each `play_file` call. The race is single-digit ms, so
        # 250ms is comfortably wide for the suppression and narrow
        # enough not to mask a real EOF on the shortest possible
        # videos (booru video clips are always >= 1s).
        self._eof_ignore_until: float = 0.0
        self._eof_ignore_window_secs: float = 0.25

        # The legacy 500ms `_seek_pending_until` pin window that lived
        # here was removed after `609066c` switched the slider seek
        # to `'absolute+exact'`. With exact seek, mpv lands at the
        # click position rather than at a keyframe before it, so the
        # slider doesn't drag back through the missing time when
        # `_poll` resumes reading `time_pos` after the seek. The pin
        # was defense in depth for keyframe-rounding latency that no
        # longer exists.

        # Polling timer for position/duration/pause/eof state
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll)

        # Pending values from mpv observers (written from mpv thread)
        self._pending_duration: float | None = None
        self._media_ready_fired = False
        self._current_file: str | None = None
        # Last reported source video size — used to dedupe video-params
        # observer firings so widget-driven re-emissions don't trigger
        # repeated _fit_to_content calls (which would loop forever).
        self._last_video_size: tuple[int, int] | None = None
        # Pending mute state — survives the lazy mpv creation. The popout's
        # video player is constructed with no mpv attached (mpv is wired
        # in _ensure_mpv on first set_media), and main_window's open-popout
        # state sync writes is_muted before mpv exists. Without a Python-
        # side fallback the value would be lost — the setter would update
        # button text but the actual mpv instance (created later) would
        # spawn unmuted by default. _ensure_mpv replays this on creation.
        self._pending_mute: bool = False

    def _ensure_mpv(self) -> mpvlib.MPV:
        """Set up mpv callbacks on first use. MPV instance is pre-created."""
        if self._mpv is not None:
            return self._mpv
        self._mpv = self._gl_widget._mpv
        self._mpv['loop-file'] = 'inf'  # default to loop mode
        self._mpv.volume = self._vol_slider.value()
        self._mpv.mute = self._pending_mute
        self._mpv.observe_property('duration', self._on_duration_change)
        self._mpv.observe_property('eof-reached', self._on_eof_reached)
        self._mpv.observe_property('video-params', self._on_video_params)
        # Forward mpv's `playback-restart` event to the Qt-side signal so
        # the popout's state machine adapter can dispatch VideoStarted /
        # SeekCompleted events on the GUI thread. mpv's event_callback
        # decorator runs on mpv's event thread; emitting a Qt Signal is
        # thread-safe and the receiving slot runs on the connection's
        # target thread (typically the GUI main loop via the default
        # AutoConnection from the same-thread receiver).
        @self._mpv.event_callback('playback-restart')
        def _emit_playback_restart(_event):
            self.playback_restart.emit()
        self._pending_video_size: tuple[int, int] | None = None
        # Push any QSS-set letterbox color into mpv now that the instance
        # exists. The qproperty-letterboxColor setter is a no-op if mpv
        # hasn't been initialized yet, so we have to (re)apply on init.
        self._apply_letterbox_color()
        return self._mpv

    # -- Public API (used by app.py for state sync) --

    @property
    def volume(self) -> int:
        return self._vol_slider.value()

    @volume.setter
    def volume(self, val: int) -> None:
        self._vol_slider.setValue(val)

    @property
    def is_muted(self) -> bool:
        if self._mpv:
            return bool(self._mpv.mute)
        return self._pending_mute

    @is_muted.setter
    def is_muted(self, val: bool) -> None:
        self._pending_mute = val
        if self._mpv:
            self._mpv.mute = val
        self._mute_btn.setText("Unmute" if val else "Mute")

    @property
    def autoplay(self) -> bool:
        return self._autoplay

    @autoplay.setter
    def autoplay(self, val: bool) -> None:
        self._autoplay = val
        self._autoplay_btn.setChecked(val)
        self._autoplay_btn.setText("Autoplay" if val else "Manual")

    @property
    def loop_state(self) -> int:
        return self._loop_state

    @loop_state.setter
    def loop_state(self, val: int) -> None:
        self._loop_state = val
        labels = ["Loop", "Once", "Next"]
        self._loop_btn.setText(labels[val])
        self._autoplay_btn.setVisible(val == 2)
        self._apply_loop_to_mpv()

    def get_position_ms(self) -> int:
        if self._mpv and self._mpv.time_pos is not None:
            return int(self._mpv.time_pos * 1000)
        return 0

    def seek_to_ms(self, ms: int) -> None:
        if self._mpv:
            self._mpv.seek(ms / 1000.0, 'absolute+exact')

    def play_file(self, path: str, info: str = "") -> None:
        """Play a file from a local path OR a remote http(s) URL.

        URL playback is the fast path for uncached videos: rather than
        waiting for `download_image` to finish writing the entire file
        to disk before mpv touches it, the load flow hands mpv the
        remote URL and lets mpv stream + buffer + render the first
        frame in parallel with the cache-populating download. mpv's
        first frame typically lands in 1-2s instead of waiting for
        the full multi-MB transfer.

        For URL paths we set the `referrer` per-file option from the
        booru's hostname so CDNs that gate downloads on Referer don't
        reject mpv's request — same logic our own httpx client uses
        in `cache._referer_for`. python-mpv's `loadfile()` accepts
        per-file `**options` kwargs that become `--key=value` overrides
        for the duration of that file.
        """
        m = self._ensure_mpv()
        self._gl_widget.ensure_gl_init()
        self._current_file = path
        self._media_ready_fired = False
        self._pending_duration = None
        self._eof_pending = False
        # Open the stale-eof suppression window. Any eof-reached event
        # arriving from mpv's event thread within the next 250ms is
        # treated as belonging to the previous file's stop and
        # ignored — see the long comment at __init__'s
        # `_eof_ignore_until` definition for the race trace.
        import time as _time
        self._eof_ignore_until = _time.monotonic() + self._eof_ignore_window_secs
        self._last_video_size = None  # reset dedupe so new file fires a fit
        self._apply_loop_to_mpv()
        if path.startswith(("http://", "https://")):
            from urllib.parse import urlparse
            from ...core.cache import _referer_for
            referer = _referer_for(urlparse(path))
            m.loadfile(path, "replace", referrer=referer)
        else:
            m.loadfile(path)
        if self._autoplay:
            m.pause = False
        else:
            m.pause = True
        self._play_btn.setText("Pause" if not m.pause else "Play")
        self._poll_timer.start()

    def stop(self) -> None:
        self._poll_timer.stop()
        if self._mpv:
            self._mpv.command('stop')
        self._time_label.setText("0:00")
        self._duration_label.setText("0:00")
        self._seek_slider.setRange(0, 0)
        self._play_btn.setText("Play")

    def pause(self) -> None:
        if self._mpv:
            self._mpv.pause = True
            self._play_btn.setText("Play")

    def resume(self) -> None:
        if self._mpv:
            self._mpv.pause = False
            self._play_btn.setText("Pause")

    # -- Internal controls --

    def _toggle_play(self) -> None:
        if not self._mpv:
            return
        self._mpv.pause = not self._mpv.pause
        self._play_btn.setText("Play" if self._mpv.pause else "Pause")

    def _toggle_autoplay(self, checked: bool = True) -> None:
        self._autoplay = self._autoplay_btn.isChecked()
        self._autoplay_btn.setText("Autoplay" if self._autoplay else "Manual")

    def _cycle_loop(self) -> None:
        self.loop_state = (self._loop_state + 1) % 3

    def _apply_loop_to_mpv(self) -> None:
        if not self._mpv:
            return
        if self._loop_state == 0:  # Loop
            self._mpv['loop-file'] = 'inf'
        else:  # Once or Next
            self._mpv['loop-file'] = 'no'

    def _seek(self, pos: int) -> None:
        """Seek to position in milliseconds (from slider).

        Uses `'absolute+exact'` (frame-accurate seek) to match the
        existing `seek_to_ms` and `_seek_relative` methods. mpv
        decodes from the previous keyframe forward to the exact
        target position, costing 30-100ms more than keyframe-only
        seek but landing `time_pos` at the click position exactly.

        See `609066c` for the drag-back race fix that introduced
        this. The legacy 500ms `_seek_pending_until` pin window that
        used to wrap this call was removed after the exact-seek
        change made it redundant.
        """
        if self._mpv:
            self._mpv.seek(pos / 1000.0, 'absolute+exact')

    def _seek_relative(self, ms: int) -> None:
        if self._mpv:
            self._mpv.seek(ms / 1000.0, 'relative+exact')

    def _set_volume(self, val: int) -> None:
        if self._mpv:
            self._mpv.volume = val

    def _toggle_mute(self) -> None:
        if self._mpv:
            self._mpv.mute = not self._mpv.mute
            self._pending_mute = bool(self._mpv.mute)
            self._mute_btn.setText("Unmute" if self._mpv.mute else "Mute")

    # -- mpv callbacks (called from mpv thread) --

    def _on_video_params(self, _name: str, value) -> None:
        """Called from mpv thread when video dimensions become known."""
        if isinstance(value, dict) and value.get('w') and value.get('h'):
            new_size = (value['w'], value['h'])
            # mpv re-fires video-params on output-area changes too. Dedupe
            # against the source dimensions we last reported so resizing the
            # popout doesn't kick off a fit→resize→fit feedback loop.
            if new_size != self._last_video_size:
                self._last_video_size = new_size
                self._pending_video_size = new_size

    def _on_eof_reached(self, _name: str, value) -> None:
        """Called from mpv thread when eof-reached changes.

        Suppresses eof events that arrive within the post-play_file
        ignore window — those are stale events from the previous
        file's stop and would otherwise race the `_eof_pending=False`
        reset and trigger a spurious play_next auto-advance.
        """
        if value is True:
            import time as _time
            if _time.monotonic() < self._eof_ignore_until:
                # Stale eof from a previous file's stop. Drop it.
                return
            self._eof_pending = True

    def _on_duration_change(self, _name: str, value) -> None:
        if value is not None and value > 0:
            self._pending_duration = value

    # -- Main-thread polling --

    def _poll(self) -> None:
        if not self._mpv:
            return
        # Position. After the `609066c` exact-seek fix and the
        # subsequent removal of the `_seek_pending_until` pin window,
        # this is just a straight read-and-write — `mpv.time_pos`
        # equals the click position immediately after a slider seek
        # because mpv decodes from the previous keyframe forward to
        # the exact target before reporting it.
        pos = self._mpv.time_pos
        if pos is not None:
            pos_ms = int(pos * 1000)
            if not self._seek_slider.isSliderDown():
                self._seek_slider.setValue(pos_ms)
            self._time_label.setText(self._fmt(pos_ms))

        # Duration (from observer)
        dur = self._pending_duration
        if dur is not None:
            dur_ms = int(dur * 1000)
            if self._seek_slider.maximum() != dur_ms:
                self._seek_slider.setRange(0, dur_ms)
                self._duration_label.setText(self._fmt(dur_ms))
            if not self._media_ready_fired:
                self._media_ready_fired = True
                self.media_ready.emit()

        # Pause state
        paused = self._mpv.pause
        expected_text = "Play" if paused else "Pause"
        if self._play_btn.text() != expected_text:
            self._play_btn.setText(expected_text)

        # Video size (set by observer on mpv thread, emitted here on main thread)
        if self._pending_video_size is not None:
            w, h = self._pending_video_size
            self._pending_video_size = None
            self.video_size.emit(w, h)

        # EOF (set by observer on mpv thread, handled here on main thread)
        if self._eof_pending:
            self._handle_eof()

    def _handle_eof(self) -> None:
        """Handle end-of-file on the main thread."""
        if not self._eof_pending:
            return
        self._eof_pending = False
        if self._loop_state == 1:  # Once
            self.pause()
        elif self._loop_state == 2:  # Next
            self.pause()
            self.play_next.emit()

    @staticmethod
    def _fmt(ms: int) -> str:
        s = ms // 1000
        m = s // 60
        return f"{m}:{s % 60:02d}"

    def destroy(self, *args, **kwargs) -> None:
        self._poll_timer.stop()
        self._gl_widget.cleanup()
        self._mpv = None
        super().destroy(*args, **kwargs)
