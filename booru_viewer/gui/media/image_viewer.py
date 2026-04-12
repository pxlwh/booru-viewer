"""Zoom/pan image viewer used by both the embedded preview and the popout."""

from __future__ import annotations

from PySide6.QtCore import Qt, QPointF, Signal
from PySide6.QtGui import QPixmap, QPainter, QWheelEvent, QMouseEvent, QKeyEvent, QMovie
from PySide6.QtWidgets import QWidget


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
        self._zoom_scroll_accum = 0
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
        # No 1.0 cap — scale up to fill the available view, matching how
        # the video player fills its widget. In the popout the window is
        # already aspect-locked to the image's aspect, so scaling up
        # produces a clean fill with no letterbox. In the embedded
        # preview the user can drag the splitter past the image's native
        # size; letting it scale up there fills the pane the same way
        # the popout does.
        self._zoom = min(scale_w, scale_h)
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
        delta = event.angleDelta().y()
        if delta == 0:
            # Pure horizontal tilt — let parent handle (navigation)
            event.ignore()
            return
        self._zoom_scroll_accum += delta
        steps = self._zoom_scroll_accum // 120
        if not steps:
            return
        self._zoom_scroll_accum -= steps * 120
        mouse_pos = event.position()
        old_zoom = self._zoom
        factor = 1.15 ** steps
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
        if not self._pixmap:
            return
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw == 0 or ph == 0:
            return
        # Only re-fit if the user was at fit-to-view at the *previous*
        # size. If they had explicitly zoomed/panned, leave _zoom and
        # _offset alone — clobbering them on every resize (F11 toggle,
        # manual window drag, splitter move) loses their state. Use
        # event.oldSize() to compute the prior fit-to-view zoom and
        # compare to current _zoom; the 0.001 epsilon absorbs float
        # drift but is tighter than any wheel/key zoom step (±20%).
        old = event.oldSize()
        if old.isValid() and old.width() > 0 and old.height() > 0:
            old_fit = min(old.width() / pw, old.height() / ph)
            if abs(self._zoom - old_fit) < 0.001:
                self._fit_to_view()
        else:
            # First resize (no valid old size) — default to fit.
            self._fit_to_view()
        self.update()
