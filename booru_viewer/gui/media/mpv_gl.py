"""mpv OpenGL render context host widgets."""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtOpenGLWidgets import QOpenGLWidget as _QOpenGLWidget
from PySide6.QtWidgets import QWidget, QVBoxLayout

import mpv as mpvlib

log = logging.getLogger(__name__)


class _MpvGLWidget(QWidget):
    """OpenGL widget that hosts mpv rendering via the render API.

    Subclasses QOpenGLWidget so initializeGL/paintGL are dispatched
    correctly by Qt's C++ virtual method mechanism.
    Works on both X11 and Wayland.
    """

    _frame_ready = Signal()  # mpv thread → main thread repaint trigger

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._gl: _MpvOpenGLSurface = _MpvOpenGLSurface(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._gl)
        self._ctx: mpvlib.MpvRenderContext | None = None
        self._gl_inited = False
        self._proc_addr_fn = None
        self._frame_ready.connect(self._gl.update)
        # Create mpv eagerly on the main thread.
        #
        # `ao=pulse` is critical for Linux Discord screen-share audio
        # capture. Discord on Linux only enumerates audio clients via
        # the libpulse API; it does not see clients that talk to
        # PipeWire natively (which is mpv's default `ao=pipewire`).
        # Forcing the pulseaudio output here makes mpv go through
        # PipeWire's pulseaudio compatibility layer, which Discord
        # picks up the same way it picks up Firefox. Without this,
        # videos play locally but the audio is silently dropped from
        # any Discord screen share. See:
        #   https://github.com/mpv-player/mpv/issues/11100
        #   https://github.com/edisionnano/Screenshare-with-audio-on-Discord-with-Linux
        # On Windows mpv ignores `ao=pulse` and falls through to the
        # next entry, so listing `wasapi` second keeps Windows playback
        # working without a platform branch here.
        #
        # `audio_client_name` is the name mpv registers with the audio
        # backend. Sets `application.name` and friends so capture tools
        # group mpv's audio under the booru-viewer app identity instead
        # of the default "mpv Media Player".
        self._mpv = mpvlib.MPV(
            vo="libmpv",
            hwdec="auto",
            keep_open="yes",
            ao="pulse,wasapi,",
            audio_client_name="booru-viewer",
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            # Fast-load options: shave ~50-100ms off first-frame decode
            # for h264/hevc by skipping a few bitstream-correctness checks
            # (`vd-lavc-fast`) and the in-loop filter on non-keyframes
            # (`vd-lavc-skiploopfilter=nonkey`). The artifacts are only
            # visible on the first few frames before the decoder steady-
            # state catches up, and only on degraded sources. mpv
            # documents these as safe for "fast load" use cases like
            # ours where we want the first frame on screen ASAP and
            # don't care about a tiny quality dip during ramp-up.
            vd_lavc_fast="yes",
            vd_lavc_skiploopfilter="nonkey",
            # Network streaming tuning for the uncached-video fast path.
            # cache=yes is mpv's default for network sources but explicit
            # is clearer. cache_pause=no keeps playback running through
            # brief buffer underruns instead of pausing — for short booru
            # clips a momentary stutter beats a pause icon. demuxer caps
            # keep RAM bounded. network_timeout=10 replaces mpv's ~60s
            # default so stalled connections surface errors promptly.
            cache="yes",
            cache_pause="no",
            demuxer_max_bytes="50MiB",
            demuxer_readahead_secs="20",
            network_timeout="10",
        )
        # Wire up the GL surface's callbacks to us
        self._gl._owner = self

    def _init_gl(self) -> None:
        if self._gl_inited or self._mpv is None:
            return
        from PySide6.QtGui import QOpenGLContext
        ctx = QOpenGLContext.currentContext()
        if not ctx:
            return

        def _get_proc_address(_ctx, name):
            if isinstance(name, bytes):
                name_str = name
            else:
                name_str = name.encode('utf-8')
            addr = ctx.getProcAddress(name_str)
            if addr is not None:
                return int(addr)
            return 0

        self._proc_addr_fn = mpvlib.MpvGlGetProcAddressFn(_get_proc_address)
        self._ctx = mpvlib.MpvRenderContext(
            self._mpv, 'opengl',
            opengl_init_params={'get_proc_address': self._proc_addr_fn},
        )
        self._ctx.update_cb = self._on_mpv_frame
        self._gl_inited = True

    def _on_mpv_frame(self) -> None:
        """Called from mpv thread when a new frame is ready."""
        self._frame_ready.emit()

    def _paint_gl(self) -> None:
        if self._ctx is None:
            self._init_gl()
            if self._ctx is None:
                return
        ratio = self._gl.devicePixelRatioF()
        w = int(self._gl.width() * ratio)
        h = int(self._gl.height() * ratio)
        self._ctx.render(
            opengl_fbo={'w': w, 'h': h, 'fbo': self._gl.defaultFramebufferObject()},
            flip_y=True,
        )

    def ensure_gl_init(self) -> None:
        """Force GL context creation and render context setup.

        Needed when the widget is hidden (e.g. inside a QStackedWidget)
        but mpv needs a render context before loadfile().
        """
        if not self._gl_inited:
            log.debug("GL render context init (first-time for widget %s)", id(self))
            self._gl.makeCurrent()
            self._init_gl()

    def cleanup(self) -> None:
        if self._ctx:
            self._ctx.free()
            self._ctx = None
        if self._mpv:
            self._mpv.terminate()
            self._mpv = None


class _MpvOpenGLSurface(_QOpenGLWidget):
    """QOpenGLWidget subclass — delegates initializeGL/paintGL to _MpvGLWidget."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._owner: _MpvGLWidget | None = None

    def initializeGL(self) -> None:
        if self._owner:
            self._owner._init_gl()

    def paintGL(self) -> None:
        if self._owner:
            self._owner._paint_gl()
