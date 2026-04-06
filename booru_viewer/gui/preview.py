"""Full media preview — image viewer with zoom/pan and video player."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QPointF, Signal, QTimer
from PySide6.QtGui import QPixmap, QPainter, QWheelEvent, QMouseEvent, QKeyEvent, QMovie
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMainWindow,
    QStackedWidget, QPushButton, QSlider, QMenu, QInputDialog, QStyle,
)

import mpv as mpvlib

_log = logging.getLogger("booru")

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mkv", ".avi", ".mov")


def _is_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def _overlay_css(obj_name: str) -> str:
    """Generate overlay CSS scoped to a specific object name for specificity."""
    return f"""
    QWidget#{obj_name} * {{
        background: transparent;
        color: white;
        border: none;
    }}
    QWidget#{obj_name} QPushButton {{
        background: transparent;
        color: white;
        border: 1px solid rgba(255, 255, 255, 80);
        padding: 2px 6px;
    }}
    QWidget#{obj_name} QPushButton:hover {{
        background: rgba(255, 255, 255, 30);
    }}
    QWidget#{obj_name} QSlider::groove:horizontal {{
        background: rgba(255, 255, 255, 40);
        height: 4px;
    }}
    QWidget#{obj_name} QSlider::handle:horizontal {{
        background: white;
        width: 10px;
        margin: -4px 0;
    }}
    QWidget#{obj_name} QSlider::sub-page:horizontal {{
        background: rgba(255, 255, 255, 120);
    }}
    QWidget#{obj_name} QLabel {{
        background: transparent;
        color: white;
    }}
    """


class FullscreenPreview(QMainWindow):
    """Fullscreen media viewer with navigation — images, GIFs, and video."""

    navigate = Signal(int)  # direction: -1/+1 for left/right, -cols/+cols for up/down
    bookmark_requested = Signal()
    save_toggle_requested = Signal()  # save or unsave depending on state
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
        self._video.play_next.connect(lambda: self.navigate.emit(1))
        self._video.video_size.connect(self._on_video_size)
        self._stack.addWidget(self._video)

        self.setCentralWidget(central)

        # Floating toolbar — overlays on top of media, translucent
        self._toolbar = QWidget(central)
        toolbar = QHBoxLayout(self._toolbar)
        toolbar.setContentsMargins(8, 4, 8, 4)

        self._bookmark_btn = QPushButton("Bookmark")
        self._bookmark_btn.setMaximumWidth(80)
        self._bookmark_btn.clicked.connect(self.bookmark_requested)
        toolbar.addWidget(self._bookmark_btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.setMaximumWidth(70)
        self._save_btn.clicked.connect(self.save_toggle_requested)
        toolbar.addWidget(self._save_btn)
        self._is_saved = False

        self._bl_tag_btn = QPushButton("BL Tag")
        self._bl_tag_btn.setMaximumWidth(60)
        self._bl_tag_btn.setToolTip("Blacklist a tag")
        self._bl_tag_btn.clicked.connect(self._show_bl_tag_menu)
        toolbar.addWidget(self._bl_tag_btn)

        self._bl_post_btn = QPushButton("BL Post")
        self._bl_post_btn.setMaximumWidth(65)
        self._bl_post_btn.setToolTip("Blacklist this post")
        self._bl_post_btn.clicked.connect(self.blacklist_post_requested)
        toolbar.addWidget(self._bl_post_btn)

        if not show_actions:
            self._bookmark_btn.hide()
            self._save_btn.hide()
            self._bl_tag_btn.hide()
            self._bl_post_btn.hide()

        toolbar.addStretch()

        self._info_label = QLabel()  # kept for API compat but hidden in slideshow
        self._info_label.hide()

        self._toolbar.raise_()

        # Reparent video controls bar to central widget so it overlays properly
        self._video._controls_bar.setParent(central)
        self._toolbar.setStyleSheet("QWidget#_slideshow_toolbar { background: rgba(0,0,0,160); }" + _overlay_css("_slideshow_toolbar"))
        self._toolbar.setObjectName("_slideshow_toolbar")
        self._video._controls_bar.setStyleSheet("QWidget#_slideshow_controls { background: rgba(0,0,0,160); }" + _overlay_css("_slideshow_controls"))
        self._video._controls_bar.setObjectName("_slideshow_controls")
        self._video._controls_bar.raise_()
        self._toolbar.raise_()

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
        # Restore saved state or start fullscreen
        if FullscreenPreview._saved_geometry and not FullscreenPreview._saved_fullscreen:
            self.setGeometry(FullscreenPreview._saved_geometry)
            self.show()
        else:
            self.showFullScreen()

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
        self._bookmark_btn.setText("Unbookmark" if bookmarked else "Bookmark")
        self._bookmark_btn.setMaximumWidth(90 if bookmarked else 80)
        self._is_saved = saved
        self._save_btn.setText("Unsave" if saved else "Save")

    def set_media(self, path: str, info: str = "") -> None:
        self._info_label.setText(info)
        ext = Path(path).suffix.lower()
        if _is_video(path):
            self._viewer.clear()
            self._video.stop()
            self._video.play_file(path, info)
            self._stack.setCurrentIndex(1)
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
                    self._adjust_to_aspect(pix.width(), pix.height())
        self._show_overlay()

    def _on_video_size(self, w: int, h: int) -> None:
        if not self.isFullScreen() and w > 0 and h > 0:
            self._adjust_to_aspect(w, h)

    def _is_hypr_floating(self) -> bool | None:
        """Check if this window is floating in Hyprland. None if not on Hyprland."""
        win = self._hyprctl_get_window()
        if win is None:
            return None  # not Hyprland
        return bool(win.get("floating"))

    def _adjust_to_aspect(self, content_w: int, content_h: int) -> None:
        """Resize windowed popout height to match content aspect ratio. Position untouched."""
        if self.isFullScreen() or content_w <= 0 or content_h <= 0:
            return
        floating = self._is_hypr_floating()
        # On Hyprland tiled: skip resize entirely, just set the aspect lock prop
        if floating is False:
            self._hyprctl_resize(0, 0)  # only sets keep_aspect_ratio
            return
        aspect = content_w / content_h
        screen = self.screen()
        if screen:
            avail = screen.availableGeometry()
            max_w = avail.width()
            max_h = avail.height()
        else:
            max_w = max_h = 9999
        w = min(self.width(), max_w)
        new_h = int(w / aspect)
        if new_h > max_h:
            new_h = max_h
            w = int(new_h * aspect)
        _log.info(f"Aspect adjust: content={content_w}x{content_h} aspect={aspect:.2f} -> {w}x{new_h} (screen={max_w}x{max_h})")
        floating = self._is_hypr_floating()
        _log.info(f"Hyprland floating={floating}")
        self.resize(w, new_h)
        self._hyprctl_resize(w, new_h)

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
                self.close()
                return True
            elif key in (Qt.Key.Key_Left, Qt.Key.Key_H):
                self.navigate.emit(-1)
                return True
            elif key in (Qt.Key.Key_Right, Qt.Key.Key_L):
                self.navigate.emit(1)
                return True
            elif key in (Qt.Key.Key_Up, Qt.Key.Key_K):
                self.navigate.emit(-self._grid_cols)
                return True
            elif key in (Qt.Key.Key_Down, Qt.Key.Key_J):
                self.navigate.emit(self._grid_cols)
                return True
            elif key == Qt.Key.Key_F11:
                if self.isFullScreen():
                    self._exit_fullscreen()
                else:
                    self.showFullScreen()
                return True
            elif key == Qt.Key.Key_Space and self._stack.currentIndex() == 1:
                self._video._toggle_play()
                return True
            elif key == Qt.Key.Key_Period and self._stack.currentIndex() == 1:
                self._video._seek_relative(1800)
                return True
            elif key == Qt.Key.Key_Comma and self._stack.currentIndex() == 1:
                self._video._seek_relative(-1800)
                return True
        if event.type() == QEvent.Type.Wheel and self._stack.currentIndex() == 1 and self.isActiveWindow():
            delta = event.angleDelta().y()
            if delta:
                vol = max(0, min(100, self._video.volume + (5 if delta > 0 else -5)))
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

    def _hyprctl_get_window(self) -> dict | None:
        """Get the Hyprland window info for the popout window."""
        import os, subprocess, json
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return None
        try:
            result = subprocess.run(
                ["hyprctl", "clients", "-j"],
                capture_output=True, text=True, timeout=1,
            )
            for c in json.loads(result.stdout):
                if c.get("title") == self.windowTitle():
                    return c
        except Exception:
            pass
        return None

    def _hyprctl_resize(self, w: int, h: int) -> None:
        """Ask Hyprland to resize this window and lock aspect ratio. No-op on other WMs or tiled."""
        import os, subprocess
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return
        win = self._hyprctl_get_window()
        if not win:
            return
        addr = win.get("address")
        if not addr:
            return
        if not win.get("floating"):
            # Tiled — don't resize (fights the layout), just set aspect lock
            try:
                subprocess.Popen(
                    ["hyprctl", "dispatch", "setprop", f"address:{addr} keep_aspect_ratio 1"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                pass
            return
        try:
            subprocess.Popen(
                ["hyprctl", "--batch",
                 f"dispatch setprop address:{addr} keep_aspect_ratio 0"
                 f" ; dispatch resizewindowpixel exact {w} {h},address:{addr}"
                 f" ; dispatch setprop address:{addr} keep_aspect_ratio 1"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

    def _exit_fullscreen(self) -> None:
        """Leave fullscreen into an aspect-ratio-respecting window."""
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

        screen = self.screen()
        if content_w > 0 and content_h > 0 and screen:
            avail = screen.availableGeometry()
            max_w = int(avail.width() * 0.6)
            max_h = int(avail.height() * 0.6)
            aspect = content_w / content_h
            if max_w / max_h > aspect:
                win_h = max_h
                win_w = int(win_h * aspect)
            else:
                win_w = max_w
                win_h = int(win_w / aspect)
            x = avail.x() + (avail.width() - win_w) // 2
            y = avail.y() + (avail.height() - win_h) // 2
            geo = self.geometry()
            geo.setRect(x, y, win_w, win_h)
            FullscreenPreview._saved_geometry = geo
        FullscreenPreview._saved_fullscreen = False
        self.showNormal()
        if content_w > 0 and content_h > 0 and screen:
            self.resize(win_w, win_h)
            self._hyprctl_resize(win_w, win_h)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Position floating overlays
        w = self.centralWidget().width()
        h = self.centralWidget().height()
        tb_h = self._toolbar.sizeHint().height()
        self._toolbar.setGeometry(0, 0, w, tb_h)
        ctrl_h = self._video._controls_bar.sizeHint().height()
        self._video._controls_bar.setGeometry(0, h - ctrl_h, w, ctrl_h)

    def closeEvent(self, event) -> None:
        from PySide6.QtWidgets import QApplication
        # Save window state for next open
        FullscreenPreview._saved_fullscreen = self.isFullScreen()
        if not self.isFullScreen():
            FullscreenPreview._saved_geometry = self.geometry()
        QApplication.instance().removeEventFilter(self)
        self.closed.emit()
        self._video.stop()
        super().closeEvent(event)


# -- Image Viewer (zoom/pan) --

class ImageViewer(QWidget):
    """Zoomable, pannable image viewer."""

    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._movie: QMovie | None = None
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self._drag_start: QPointF | None = None
        self._drag_offset = QPointF(0, 0)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._info_text = ""

    def set_image(self, pixmap: QPixmap, info: str = "") -> None:
        self._stop_movie()
        self._pixmap = pixmap
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self._info_text = info
        self._fit_to_view()
        self.update()

    def set_gif(self, path: str, info: str = "") -> None:
        self._stop_movie()
        self._movie = QMovie(path)
        self._movie.frameChanged.connect(self._on_gif_frame)
        self._movie.start()
        self._info_text = info
        # Set initial pixmap from first frame
        self._pixmap = self._movie.currentPixmap()
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self._fit_to_view()
        self.update()

    def _on_gif_frame(self) -> None:
        if self._movie:
            self._pixmap = self._movie.currentPixmap()
            self.update()

    def _stop_movie(self) -> None:
        if self._movie:
            self._movie.stop()
            self._movie = None

    def clear(self) -> None:
        self._stop_movie()
        self._pixmap = None
        self._info_text = ""
        self.update()

    def _fit_to_view(self) -> None:
        if not self._pixmap:
            return
        vw, vh = self.width(), self.height()
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw == 0 or ph == 0:
            return
        scale_w = vw / pw
        scale_h = vh / ph
        self._zoom = min(scale_w, scale_h, 1.0)
        self._offset = QPointF(
            (vw - pw * self._zoom) / 2,
            (vh - ph * self._zoom) / 2,
        )

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(pal.ColorRole.Window))
        if self._pixmap:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.translate(self._offset)
            p.scale(self._zoom, self._zoom)
            p.drawPixmap(0, 0, self._pixmap)
            p.resetTransform()
        p.end()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._pixmap:
            return
        mouse_pos = event.position()
        old_zoom = self._zoom
        delta = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 1 / 1.15
        self._zoom = max(0.1, min(self._zoom * factor, 20.0))
        ratio = self._zoom / old_zoom
        self._offset = mouse_pos - ratio * (mouse_pos - self._offset)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._fit_to_view()
            self.update()
        elif event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position()
            self._drag_offset = QPointF(self._offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is not None:
            delta = event.position() - self._drag_start
            self._offset = self._drag_offset + delta
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_start = None
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
            self.close_requested.emit()
        elif event.key() == Qt.Key.Key_0:
            self._fit_to_view()
            self.update()
        elif event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._zoom = min(self._zoom * 1.2, 20.0)
            self.update()
        elif event.key() == Qt.Key.Key_Minus:
            self._zoom = max(self._zoom / 1.2, 0.1)
            self.update()
        else:
            event.ignore()

    def resizeEvent(self, event) -> None:
        if self._pixmap:
            self._fit_to_view()
            self.update()


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
        self._mpv = mpvlib.MPV(
            vo="libmpv",
            hwdec="auto",
            keep_open="yes",
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
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
            self._gl.makeCurrent()
            self._init_gl()

    def cleanup(self) -> None:
        if self._ctx:
            self._ctx.free()
            self._ctx = None
        if self._mpv:
            self._mpv.terminate()
            self._mpv = None


from PySide6.QtOpenGLWidgets import QOpenGLWidget as _QOpenGLWidget


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


class VideoPlayer(QWidget):
    """Video player with transport controls, powered by mpv."""

    play_next = Signal()       # emitted when video ends in "Next" mode
    media_ready = Signal()     # emitted when media is loaded and duration is known
    video_size = Signal(int, int)  # (width, height) emitted when video dimensions are known

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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

        self._play_btn = QPushButton("Play")
        self._play_btn.setMaximumWidth(65)
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
        self._mute_btn.clicked.connect(self._toggle_mute)
        controls.addWidget(self._mute_btn)

        self._autoplay = True
        self._autoplay_btn = QPushButton("Auto")
        self._autoplay_btn.setMaximumWidth(70)
        self._autoplay_btn.setCheckable(True)
        self._autoplay_btn.setChecked(True)
        self._autoplay_btn.setToolTip("Auto-play videos when selected")
        self._autoplay_btn.clicked.connect(self._toggle_autoplay)
        self._autoplay_btn.hide()
        controls.addWidget(self._autoplay_btn)

        self._loop_state = 0  # 0=Loop, 1=Once, 2=Next
        self._loop_btn = QPushButton("Loop")
        self._loop_btn.setMaximumWidth(55)
        self._loop_btn.setToolTip("Loop: repeat / Once: stop at end / Next: advance")
        self._loop_btn.clicked.connect(self._cycle_loop)
        controls.addWidget(self._loop_btn)

        # Style controls to match the translucent overlay look
        self._controls_bar.setObjectName("_preview_controls")
        self._controls_bar.setStyleSheet(
            "QWidget#_preview_controls { background: rgba(0,0,0,160); }" + _overlay_css("_preview_controls")
        )
        # Add to layout — in preview panel this is part of the normal layout.
        # FullscreenPreview reparents it to float as an overlay.
        layout.addWidget(self._controls_bar)

        self._eof_pending = False

        # Polling timer for position/duration/pause/eof state
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll)

        # Pending values from mpv observers (written from mpv thread)
        self._pending_duration: float | None = None
        self._media_ready_fired = False
        self._current_file: str | None = None

    def _ensure_mpv(self) -> mpvlib.MPV:
        """Set up mpv callbacks on first use. MPV instance is pre-created."""
        if self._mpv is not None:
            return self._mpv
        self._mpv = self._gl_widget._mpv
        self._mpv['loop-file'] = 'inf'  # default to loop mode
        self._mpv.volume = self._vol_slider.value()
        self._mpv.observe_property('duration', self._on_duration_change)
        self._mpv.observe_property('eof-reached', self._on_eof_reached)
        self._mpv.observe_property('video-params', self._on_video_params)
        self._pending_video_size: tuple[int, int] | None = None
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
        return False

    @is_muted.setter
    def is_muted(self, val: bool) -> None:
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
        m = self._ensure_mpv()
        self._gl_widget.ensure_gl_init()
        self._current_file = path
        self._media_ready_fired = False
        self._pending_duration = None
        self._eof_pending = False
        self._apply_loop_to_mpv()
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
        """Seek to position in milliseconds (from slider)."""
        if self._mpv:
            self._mpv.seek(pos / 1000.0, 'absolute')

    def _seek_relative(self, ms: int) -> None:
        if self._mpv:
            self._mpv.seek(ms / 1000.0, 'relative+exact')

    def _set_volume(self, val: int) -> None:
        if self._mpv:
            self._mpv.volume = val

    def _toggle_mute(self) -> None:
        if self._mpv:
            self._mpv.mute = not self._mpv.mute
            self._mute_btn.setText("Unmute" if self._mpv.mute else "Mute")

    # -- mpv callbacks (called from mpv thread) --

    def _on_video_params(self, _name: str, value) -> None:
        """Called from mpv thread when video dimensions become known."""
        if isinstance(value, dict) and value.get('w') and value.get('h'):
            self._pending_video_size = (value['w'], value['h'])

    def _on_eof_reached(self, _name: str, value) -> None:
        """Called from mpv thread when eof-reached changes."""
        if value is True:
            self._eof_pending = True

    def _on_duration_change(self, _name: str, value) -> None:
        if value is not None and value > 0:
            self._pending_duration = value

    # -- Main-thread polling --

    def _poll(self) -> None:
        if not self._mpv:
            return
        # Position
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


# -- Combined Preview (image + video) --

class ImagePreview(QWidget):
    """Combined media preview — auto-switches between image and video."""

    close_requested = Signal()
    open_in_default = Signal()
    open_in_browser = Signal()
    save_to_folder = Signal(str)
    unsave_requested = Signal()
    bookmark_requested = Signal()
    blacklist_tag_requested = Signal(str)
    blacklist_post_requested = Signal()
    navigate = Signal(int)  # -1 = prev, +1 = next
    fullscreen_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._folders_callback = None
        self._current_path: str | None = None
        self._current_post = None  # Post object, set by app.py
        self._current_site_id = None  # site_id for the current post
        self._is_saved = False  # tracks library save state for context menu
        self._current_tags: dict[str, list[str]] = {}
        self._current_tag_list: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Action toolbar — above the media, in the layout
        self._toolbar = QWidget()
        tb = QHBoxLayout(self._toolbar)
        tb.setContentsMargins(2, 1, 2, 1)
        tb.setSpacing(4)

        self._bookmark_btn = QPushButton("Bookmark")
        self._bookmark_btn.setFixedWidth(80)
        self._bookmark_btn.clicked.connect(self.bookmark_requested)
        tb.addWidget(self._bookmark_btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.setFixedWidth(50)
        self._save_btn.clicked.connect(self._on_save_clicked)
        tb.addWidget(self._save_btn)

        self._bl_tag_btn = QPushButton("BL Tag")
        self._bl_tag_btn.setFixedWidth(55)
        self._bl_tag_btn.setToolTip("Blacklist a tag")
        self._bl_tag_btn.clicked.connect(self._show_bl_tag_menu)
        tb.addWidget(self._bl_tag_btn)

        self._bl_post_btn = QPushButton("BL Post")
        self._bl_post_btn.setFixedWidth(60)
        self._bl_post_btn.setToolTip("Blacklist this post")
        self._bl_post_btn.clicked.connect(self.blacklist_post_requested)
        tb.addWidget(self._bl_post_btn)

        tb.addStretch()

        self._popout_btn = QPushButton("Popout")
        self._popout_btn.setFixedWidth(60)
        self._popout_btn.setToolTip("Open in popout")
        self._popout_btn.clicked.connect(self.fullscreen_requested)
        tb.addWidget(self._popout_btn)

        self._toolbar.hide()  # shown when a post is active
        layout.addWidget(self._toolbar)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, stretch=1)

        # Image viewer (index 0)
        self._image_viewer = ImageViewer()
        self._image_viewer.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._image_viewer.close_requested.connect(self.close_requested)
        self._stack.addWidget(self._image_viewer)

        # Video player (index 1)
        self._video_player = VideoPlayer()
        self._video_player.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._video_player.play_next.connect(lambda: self.navigate.emit(1))
        self._stack.addWidget(self._video_player)

        # Info label
        self._info_label = QLabel()
        self._info_label.setStyleSheet("padding: 2px 6px;")
        layout.addWidget(self._info_label)

        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

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

    def _on_save_clicked(self) -> None:
        if self._save_btn.text() == "Unsave":
            self.unsave_requested.emit()
            return
        menu = QMenu(self)
        unsorted = menu.addAction("Unsorted")
        menu.addSeparator()
        folder_actions = {}
        if self._folders_callback:
            for folder in self._folders_callback():
                a = menu.addAction(folder)
                folder_actions[id(a)] = folder
        menu.addSeparator()
        new_action = menu.addAction("+ New Folder...")
        action = menu.exec(self._save_btn.mapToGlobal(self._save_btn.rect().bottomLeft()))
        if not action:
            return
        if action == unsorted:
            self.save_to_folder.emit("")
        elif action == new_action:
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                self.save_to_folder.emit(name.strip())
        elif id(action) in folder_actions:
            self.save_to_folder.emit(folder_actions[id(action)])

    def update_bookmark_state(self, bookmarked: bool) -> None:
        self._bookmark_btn.setText("Unbookmark" if bookmarked else "Bookmark")
        self._bookmark_btn.setFixedWidth(90 if bookmarked else 80)

    def update_save_state(self, saved: bool) -> None:
        self._is_saved = saved
        self._save_btn.setText("Unsave" if saved else "Save")



    # Keep these for compatibility with app.py accessing them
    @property
    def _pixmap(self):
        return self._image_viewer._pixmap

    @property
    def _info_text(self):
        return self._image_viewer._info_text

    def set_folders_callback(self, callback) -> None:
        self._folders_callback = callback

    def set_image(self, pixmap: QPixmap, info: str = "") -> None:
        self._video_player.stop()
        self._image_viewer.set_image(pixmap, info)
        self._stack.setCurrentIndex(0)
        self._info_label.setText(info)
        self._current_path = None
        self._toolbar.show()
        self._toolbar.raise_()

    def set_media(self, path: str, info: str = "") -> None:
        """Auto-detect and show image or video."""
        self._current_path = path
        ext = Path(path).suffix.lower()
        if _is_video(path):
            self._image_viewer.clear()
            self._video_player.stop()
            self._video_player.play_file(path, info)
            self._stack.setCurrentIndex(1)
            self._info_label.setText(info)
        elif ext == ".gif":
            self._video_player.stop()
            self._image_viewer.set_gif(path, info)
            self._stack.setCurrentIndex(0)
            self._info_label.setText(info)
        else:
            self._video_player.stop()
            pix = QPixmap(path)
            if not pix.isNull():
                self._image_viewer.set_image(pix, info)
            self._stack.setCurrentIndex(0)
            self._info_label.setText(info)
        self._toolbar.show()
        self._toolbar.raise_()

    def clear(self) -> None:
        self._video_player.stop()
        self._image_viewer.clear()
        self._info_label.setText("")
        self._current_path = None
        self._toolbar.hide()

    def _on_context_menu(self, pos) -> None:
        menu = QMenu(self)
        fav_action = menu.addAction("Bookmark")

        save_menu = menu.addMenu("Save to Library")
        save_unsorted = save_menu.addAction("Unsorted")
        save_menu.addSeparator()
        save_folder_actions = {}
        if self._folders_callback:
            for folder in self._folders_callback():
                a = save_menu.addAction(folder)
                save_folder_actions[id(a)] = folder
        save_menu.addSeparator()
        save_new = save_menu.addAction("+ New Folder...")

        unsave_action = None
        if self._is_saved:
            unsave_action = menu.addAction("Unsave from Library")

        menu.addSeparator()
        copy_image = menu.addAction("Copy File to Clipboard")
        open_action = menu.addAction("Open in Default App")
        browser_action = menu.addAction("Open in Browser")

        # Image-specific
        reset_action = None
        if self._stack.currentIndex() == 0:
            reset_action = menu.addAction("Reset View")

        popout_action = None
        if self._current_path:
            popout_action = menu.addAction("Popout")
        clear_action = menu.addAction("Clear Preview")

        action = menu.exec(self.mapToGlobal(pos))
        if not action:
            return
        if action == fav_action:
            self.bookmark_requested.emit()
        elif action == save_unsorted:
            self.save_to_folder.emit("")
        elif action == save_new:
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                self.save_to_folder.emit(name.strip())
        elif id(action) in save_folder_actions:
            self.save_to_folder.emit(save_folder_actions[id(action)])
        elif action == copy_image:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtGui import QPixmap as _QP
            pix = self._image_viewer._pixmap
            if pix and not pix.isNull():
                QApplication.clipboard().setPixmap(pix)
            elif self._current_path:
                pix = _QP(self._current_path)
                if not pix.isNull():
                    QApplication.clipboard().setPixmap(pix)
        elif action == open_action:
            self.open_in_default.emit()
        elif action == browser_action:
            self.open_in_browser.emit()
        elif action == reset_action:
            self._image_viewer._fit_to_view()
            self._image_viewer.update()
        elif action == unsave_action:
            self.unsave_requested.emit()
        elif action == popout_action:
            self.fullscreen_requested.emit()
        elif action == clear_action:
            self.close_requested.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            event.ignore()
        else:
            super().mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        if self._stack.currentIndex() == 1:
            delta = event.angleDelta().y()
            vol = max(0, min(100, self._video_player.volume + (5 if delta > 0 else -5)))
            self._video_player.volume = vol
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._stack.currentIndex() == 0:
            self._image_viewer.keyPressEvent(event)
        elif event.key() == Qt.Key.Key_Space:
            self._video_player._toggle_play()
        elif event.key() == Qt.Key.Key_Period:
            self._video_player._seek_relative(1800)
        elif event.key() == Qt.Key.Key_Comma:
            self._video_player._seek_relative(-1800)
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_H):
            self.navigate.emit(-1)
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_L):
            self.navigate.emit(1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
