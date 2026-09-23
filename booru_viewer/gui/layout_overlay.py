"""Drag overlay for rearranging panels (model in panel_layout).

Panels never leave the main window. A drag only picks a zone on another
panel and main_window re-parents the widgets, so nothing depends on
global window coordinates (which Wayland clients never get).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from .panel_layout import PANEL_MIME, PANEL_TITLES, Rect, grip_rect, hit_zone, zone_rects


class LayoutOverlay(QWidget):
    """Over the main splitter: edit-mode grips, and the drop target.

    Parented to the splitter's parent, not the splitter itself: QSplitter
    adopts every child widget as a pane.
    """

    drag_requested = Signal(str)
    finished = Signal()

    def __init__(self, splitter: QWidget, panels: dict[str, QWidget], edit: bool) -> None:
        super().__init__(splitter.parentWidget())
        self._splitter = splitter
        self._panels = panels
        self._edit = edit
        self._hover: tuple[str, str] | None = None
        self._press: tuple[str, object] | None = None
        self.dragging: str | None = None
        self.dropped: tuple[str, str] | None = None
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        splitter.installEventFilter(self)
        self.setGeometry(splitter.geometry())
        self.raise_()
        self.show()
        if edit:
            self.setFocus()

    def eventFilter(self, obj, event) -> bool:
        if obj is self._splitter and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self.setGeometry(self._splitter.geometry())
        return False

    def panel_rects(self) -> dict[str, Rect]:
        out = {}
        for name, w in self._panels.items():
            if w.isVisible():
                tl = w.mapTo(self.parentWidget(), w.rect().topLeft()) - self.pos()
                out[name] = (tl.x(), tl.y(), w.width(), w.height())
        return out

    # edit-mode grips

    def _grip_at(self, pos) -> str | None:
        for name, rect in self.panel_rects().items():
            if QRect(*grip_rect(rect)).contains(pos):
                return name
        return None

    def mousePressEvent(self, event) -> None:
        name = self._grip_at(event.position().toPoint()) if self._edit else None
        self._press = (name, event.position().toPoint()) if name else None

    def mouseMoveEvent(self, event) -> None:
        if self._press and (
            event.position().toPoint() - self._press[1]
        ).manhattanLength() >= QApplication.startDragDistance():
            name = self._press[0]
            self._press = None
            self.drag_requested.emit(name)

    def mouseReleaseEvent(self, event) -> None:
        self._press = None

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._edit:
            self.finished.emit()
        else:
            super().keyPressEvent(event)

    # drop target

    def _hit(self, event) -> tuple[str, str] | None:
        p = event.position().toPoint()
        return hit_zone(self.panel_rects(), p.x(), p.y(), self.dragging)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(PANEL_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        hit = self._hit(event)
        if hit != self._hover:
            self._hover = hit
            self.update()
        if hit:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._hover = None
        self.update()

    def dropEvent(self, event) -> None:
        self.dropped = self._hit(event)
        self._hover = None
        self.update()
        if self.dropped:
            event.acceptProposedAction()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        pal = self.palette()
        accent = pal.color(QPalette.ColorRole.Highlight)
        rects = self.panel_rects()
        if self._edit:
            dim = QColor(pal.color(QPalette.ColorRole.Window))
            dim.setAlpha(150)
            painter.fillRect(self.rect(), dim)
        if self.dragging:
            for name, rect in rects.items():
                if name == self.dragging:
                    continue
                for zone, r in zone_rects(*rect).items():
                    fill = QColor(accent)
                    fill.setAlpha(140 if self._hover == (name, zone) else 40)
                    painter.fillRect(QRect(*r), fill)
        painter.setPen(accent)
        for rect in rects.values():
            painter.drawRect(QRect(*rect).adjusted(0, 0, -1, -1))
        if self._edit:
            for name, rect in rects.items():
                g = QRect(*grip_rect(rect))
                painter.fillRect(g, pal.color(QPalette.ColorRole.Button))
                painter.drawRect(g.adjusted(0, 0, -1, -1))
                painter.setPen(pal.color(QPalette.ColorRole.ButtonText))
                painter.drawText(g, Qt.AlignmentFlag.AlignCenter, f"≡ {PANEL_TITLES[name]}")
                painter.setPen(accent)
            painter.setPen(pal.color(QPalette.ColorRole.WindowText))
            painter.drawText(
                self.rect().adjusted(0, 0, 0, -12),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                "Drag panels to rearrange. Esc or Ctrl+E to finish.",
            )
        painter.end()
