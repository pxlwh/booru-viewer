"""mpv OpenGL render context host widgets."""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Signal
from PySide6.QtOpenGLWidgets import QOpenGLWidget as _QOpenGLWidget
from PySide6.QtWidgets import QWidget, QVBoxLayout

import mpv as mpvlib

from ._mpv_options import build_mpv_kwargs

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
        # Options come from `build_mpv_kwargs` (see `_mpv_options.py`
        # for the full rationale). Summary: Discord screen-share audio
        # fix via `ao=pulse`, fast-load vd-lavc options, network cache
        # tuning for the uncached-video fast path, and the SECURITY
        # hardening from audit #2 (ytdl=no, load_scripts=no,
        # demuxer_lavf_o protocol whitelist, POSIX input_conf null).
        self._mpv = mpvlib.MPV(
            **build_mpv_kwargs(is_windows=sys.platform == "win32"),
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
