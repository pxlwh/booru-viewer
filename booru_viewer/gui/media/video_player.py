"""mpv-backed video player widget with transport controls."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, Property, QPoint
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter, QPen, QBrush, QPolygon, QPainterPath, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QStyle,
    QApplication,
)


def _paint_icon(shape: str, color: QColor, size: int = 16) -> QIcon:
    """Paint a media control icon using the given color."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    s = size

    if shape == "play":
        p.drawPolygon(QPolygon([QPoint(3, 2), QPoint(3, s - 2), QPoint(s - 2, s // 2)]))

    elif shape == "pause":
        w = max(2, s // 4)
        p.drawRect(2, 2, w, s - 4)
        p.drawRect(s - 2 - w, 2, w, s - 4)

    elif shape == "volume":
        # Speaker cone
        p.drawPolygon(QPolygon([
            QPoint(1, s // 2 - 2), QPoint(4, s // 2 - 2),
            QPoint(8, 2), QPoint(8, s - 2),
            QPoint(4, s // 2 + 2), QPoint(1, s // 2 + 2),
        ]))
        # Sound waves
        p.setPen(QPen(color, 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.arcMoveTo(8, 3, 6, s - 6, 45)
        path.arcTo(8, 3, 6, s - 6, 45, -90)
        p.drawPath(path)

    elif shape == "muted":
        p.drawPolygon(QPolygon([
            QPoint(1, s // 2 - 2), QPoint(4, s // 2 - 2),
            QPoint(8, 2), QPoint(8, s - 2),
            QPoint(4, s // 2 + 2), QPoint(1, s // 2 + 2),
        ]))
        p.setPen(QPen(color, 2))
        p.drawLine(10, 4, s - 2, s - 4)
        p.drawLine(10, s - 4, s - 2, 4)

    elif shape == "loop":
        p.setPen(QPen(color, 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.arcMoveTo(2, 2, s - 4, s - 4, 30)
        path.arcTo(2, 2, s - 4, s - 4, 30, 300)
        p.drawPath(path)
        # Arrowhead
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        end = path.currentPosition().toPoint()
        p.drawPolygon(QPolygon([
            end, QPoint(end.x() - 4, end.y() - 3), QPoint(end.x() + 1, end.y() - 4),
        ]))

    elif shape == "once":
        p.setPen(QPen(color, 1))
        f = QFont()
        f.setPixelSize(s - 2)
        f.setBold(True)
        p.setFont(f)
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "1\u00D7")

    elif shape == "next":
        p.drawPolygon(QPolygon([QPoint(2, 2), QPoint(2, s - 2), QPoint(s - 5, s // 2)]))
        p.drawRect(s - 4, 2, 2, s - 4)

    elif shape == "auto":
        mid = s // 2
        p.drawPolygon(QPolygon([QPoint(1, 3), QPoint(1, s - 3), QPoint(mid - 1, s // 2)]))
        p.drawPolygon(QPolygon([QPoint(mid, 3), QPoint(mid, s - 3), QPoint(s - 2, s // 2)]))

    p.end()
    return QIcon(pix)

import mpv as mpvlib

log = logging.getLogger(__name__)

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

        _btn_sz = 24
        _fg = self.palette().buttonText().color()

        def _icon_btn(shape: str, name: str, tip: str) -> QPushButton:
            btn = QPushButton()
            btn.setObjectName(name)
            btn.setIcon(_paint_icon(shape, _fg))
            btn.setFixedSize(_btn_sz, _btn_sz)
            btn.setToolTip(tip)
            return btn

        self._icon_fg = _fg
        self._play_icon = _paint_icon("play", _fg)
        self._pause_icon = _paint_icon("pause", _fg)

        self._play_btn = _icon_btn("play", "_ctrl_play", "Play / Pause (Space)")
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

        self._vol_icon = _paint_icon("volume", _fg)
        self._muted_icon = _paint_icon("muted", _fg)

        self._mute_btn = _icon_btn("volume", "_ctrl_mute", "Mute / Unmute")
        self._mute_btn.clicked.connect(self._toggle_mute)
        controls.addWidget(self._mute_btn)

        self._autoplay = True
        self._auto_icon = _paint_icon("auto", _fg)
        self._autoplay_btn = _icon_btn("auto", "_ctrl_autoplay", "Auto-play videos when selected")
        self._autoplay_btn.setCheckable(True)
        self._autoplay_btn.setChecked(True)
        self._autoplay_btn.clicked.connect(self._toggle_autoplay)
        self._autoplay_btn.hide()
        controls.addWidget(self._autoplay_btn)

        self._loop_icons = {
            0: _paint_icon("loop", _fg),
            1: _paint_icon("once", _fg),
            2: _paint_icon("next", _fg),
        }
        self._loop_state = 0  # 0=Loop, 1=Once, 2=Next
        self._loop_btn = _icon_btn("loop", "_ctrl_loop", "Loop / Once / Next")
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

        # Responsive hiding: watch controls bar resize and hide widgets
        # that don't fit at narrow widths.
        self._controls_bar.installEventFilter(self)

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

        # Stream-record state: mpv's stream-record option tees its
        # network stream into a .part file that gets promoted to the
        # real cache path on clean EOF. Eliminates the parallel httpx
        # download that used to race with mpv for the same bytes.
        self._stream_record_tmp: Path | None = None
        self._stream_record_target: Path | None = None
        self._seeked_during_record: bool = False
        self._loudnorm: bool = False

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
        # Apply audio normalization if enabled in settings.
        if self._loudnorm:
            self._mpv.af = "loudnorm"
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
        self._mute_btn.setIcon(self._muted_icon if val else self._vol_icon)

    @property
    def autoplay(self) -> bool:
        return self._autoplay

    @autoplay.setter
    def autoplay(self, val: bool) -> None:
        self._autoplay = val
        self._autoplay_btn.setChecked(val)
        self._autoplay_btn.setIcon(self._auto_icon if val else self._play_icon)
        self._autoplay_btn.setToolTip("Autoplay on" if val else "Autoplay off")

    @property
    def loop_state(self) -> int:
        return self._loop_state

    @loop_state.setter
    def loop_state(self, val: int) -> None:
        self._loop_state = val
        tips = ["Loop: repeat", "Once: stop at end", "Next: advance"]
        self._loop_btn.setIcon(self._loop_icons[val])
        self._loop_btn.setToolTip(tips[val])
        self._autoplay_btn.setVisible(val == 2)
        self._apply_loop_to_mpv()

    def get_position_ms(self) -> int:
        if self._mpv and self._mpv.time_pos is not None:
            return int(self._mpv.time_pos * 1000)
        return 0

    def seek_to_ms(self, ms: int) -> None:
        if self._mpv:
            self._mpv.seek(ms / 1000.0, 'absolute+exact')
            if self._stream_record_target is not None:
                self._seeked_during_record = True

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

        # Clean up any leftover .part from a previous play_file that
        # didn't finish (rapid clicks, popout closed mid-stream, etc).
        self._discard_stream_record()

        if path.startswith(("http://", "https://")):
            from urllib.parse import urlparse
            from ...core.cache import _referer_for, cached_path_for
            referer = _referer_for(urlparse(path))
            target = cached_path_for(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".part")
            m.loadfile(path, "replace",
                       referrer=referer,
                       stream_record=tmp.as_posix(),
                       demuxer_max_bytes="150MiB")
            self._stream_record_tmp = tmp
            self._stream_record_target = target
        else:
            m.loadfile(path)
        if self._autoplay:
            m.pause = False
        else:
            m.pause = True
        self._play_btn.setIcon(self._pause_icon if not m.pause else self._play_icon)
        self._poll_timer.start()

    def stop(self) -> None:
        self._discard_stream_record()
        self._poll_timer.stop()
        if self._mpv:
            self._mpv.command('stop')
        self._time_label.setText("0:00")
        self._duration_label.setText("0:00")
        self._seek_slider.setRange(0, 0)
        self._play_btn.setIcon(self._play_icon)

    def pause(self) -> None:
        if self._mpv:
            self._mpv.pause = True
            self._play_btn.setIcon(self._play_icon)

    def resume(self) -> None:
        if self._mpv:
            self._mpv.pause = False
            self._play_btn.setIcon(self._pause_icon)

    # -- Internal controls --

    def eventFilter(self, obj, event):
        if obj is self._controls_bar and event.type() == event.Type.Resize:
            self._apply_responsive_layout()
        return super().eventFilter(obj, event)

    def _apply_responsive_layout(self) -> None:
        """Hide/show control elements based on available width."""
        w = self._controls_bar.width()
        # Breakpoints — hide wider elements first
        show_volume = w >= 320
        show_duration = w >= 240
        show_time = w >= 200
        self._vol_slider.setVisible(show_volume)
        self._duration_label.setVisible(show_duration)
        self._time_label.setVisible(show_time)

    def _toggle_play(self) -> None:
        if not self._mpv:
            return
        # If paused at end-of-file (Once mode after playback), seek back
        # to the start so pressing play replays instead of doing nothing.
        if self._mpv.pause:
            try:
                pos = self._mpv.time_pos
                dur = self._mpv.duration
                if pos is not None and dur is not None and dur > 0 and pos >= dur - 0.5:
                    self._mpv.command('seek', 0, 'absolute+exact')
            except Exception:
                pass
        self._mpv.pause = not self._mpv.pause
        self._play_btn.setIcon(self._play_icon if self._mpv.pause else self._pause_icon)

    def _toggle_autoplay(self, checked: bool = True) -> None:
        self._autoplay = self._autoplay_btn.isChecked()
        self._autoplay_btn.setIcon(self._auto_icon if self._autoplay else self._play_icon)
        self._autoplay_btn.setToolTip("Autoplay on" if self._autoplay else "Autoplay off")

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
            if self._stream_record_target is not None:
                self._seeked_during_record = True

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
            self._mute_btn.setIcon(self._muted_icon if self._mpv.mute else self._vol_icon)

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
        expected_icon = self._play_icon if paused else self._pause_icon
        if self._play_btn.icon().cacheKey() != expected_icon.cacheKey():
            self._play_btn.setIcon(expected_icon)

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
        self._finalize_stream_record()
        if self._loop_state == 1:  # Once
            self.pause()
        elif self._loop_state == 2:  # Next
            self.pause()
            self.play_next.emit()

    # -- Stream-record helpers --

    def _discard_stream_record(self) -> None:
        """Remove any pending stream-record temp file without promoting."""
        tmp = self._stream_record_tmp
        self._stream_record_tmp = None
        self._stream_record_target = None
        self._seeked_during_record = False
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _finalize_stream_record(self) -> None:
        """Promote the stream-record .part file to its final cache path.

        Only promotes if: (a) there is a pending stream-record, (b) the
        user did not seek during playback (seeking invalidates the file
        because mpv may have skipped byte ranges), and (c) the .part
        file exists and is non-empty.
        """
        tmp = self._stream_record_tmp
        target = self._stream_record_target
        self._stream_record_tmp = None
        self._stream_record_target = None
        if tmp is None or target is None:
            return
        if self._seeked_during_record:
            log.debug("Stream-record discarded (seek during playback): %s", tmp.name)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        if not tmp.exists() or tmp.stat().st_size == 0:
            log.debug("Stream-record .part missing or empty: %s", tmp.name)
            return
        try:
            os.replace(tmp, target)
            log.debug("Stream-record promoted: %s -> %s", tmp.name, target.name)
        except OSError as e:
            log.warning("Stream-record promote failed: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

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
