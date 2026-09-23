"""Rearrangeable main-window panels: layout model plus the drag overlay.

A layout is a list of columns, each a list of panel names stacked top
to bottom, stored as ``"results,preview/info"`` (``,`` between columns,
``/`` between stacked panels). Sizes are stored by name, not index:
column widths under the column's own spec, stacked heights under the
panel, so a width follows its panel through any reorder.

Panels never leave the main window. A drag only picks a zone on another
panel and main_window re-parents the widgets, so nothing depends on
global window coordinates (which Wayland clients never get).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import QApplication, QWidget

PANELS = ("results", "preview", "info")
PANEL_TITLES = {"results": "Results", "preview": "Preview", "info": "Info"}
PANEL_MIME = "application/x-booru-viewer-panel"

DEFAULT_WIDTH = {"results": 600, "preview": 500, "info": 250}
DEFAULT_HEIGHT = {"results": 500, "preview": 500, "info": 200}

Layout = list[list[str]]
Rect = tuple[int, int, int, int]


# -- Model (pure) --

def default_layout(flip: bool) -> Layout:
    """The pre-drag arrangement, honoring the retired flip_layout setting."""
    return [["preview", "info"], ["results"]] if flip else [["results"], ["preview", "info"]]


def parse_layout(s: str) -> Layout | None:
    """Parse a stored layout; None unless every panel appears exactly once."""
    if not s:
        return None
    layout = [col.split("/") for col in s.split(",")]
    names = [p for col in layout for p in col]
    if sorted(names) != sorted(PANELS):
        return None
    return layout


def format_layout(layout: Layout) -> str:
    return ",".join(column_key(col) for col in layout)


def column_key(col: list[str]) -> str:
    return "/".join(col)


def move_panel(layout: Layout, panel: str, target: str, zone: str) -> Layout:
    """Return *layout* with *panel* dropped on *zone* of *target*.

    left/right make *panel* its own column beside target's column;
    top/bottom stack it in target's column.
    """
    if panel == target:
        return layout
    cols = [[p for p in col if p != panel] for col in layout]
    cols = [col for col in cols if col]
    ci = next(i for i, col in enumerate(cols) if target in col)
    ri = cols[ci].index(target)
    if zone == "left":
        cols.insert(ci, [panel])
    elif zone == "right":
        cols.insert(ci + 1, [panel])
    elif zone == "top":
        cols[ci].insert(ri, panel)
    else:
        cols[ci].insert(ri + 1, panel)
    return cols


def parse_sizes(s: str) -> dict[str, int]:
    """``"results:600,preview/info:500"`` to a dict; bad entries are skipped."""
    out: dict[str, int] = {}
    for part in (s or "").split(","):
        key, _, val = part.partition(":")
        try:
            size = int(val)
        except ValueError:
            continue
        if key and size > 0:
            out[key] = size
    return out


def format_sizes(sizes: dict[str, int]) -> str:
    return ",".join(f"{k}:{v}" for k, v in sizes.items())


def legacy_sizes(flip: bool, main: str, right: str) -> tuple[dict[str, int], dict[str, int]]:
    """Seed name-keyed sizes from the pre-drag index-keyed settings.

    main_splitter_sizes was (grid, right column) in screen order and
    right_splitter_sizes was (preview, dl_progress, info). Both keys are
    left untouched so an older build still finds them.
    """
    widths: dict[str, int] = {}
    heights: dict[str, int] = {}
    m = [int(p) for p in main.split(",") if p.strip().isdigit()] if main else []
    if len(m) == 2:
        right_w, grid_w = (m[0], m[1]) if flip else (m[1], m[0])
        if grid_w > 0:
            widths["results"] = grid_w
        if right_w > 0:
            widths["preview/info"] = right_w
    r = [int(p) for p in right.split(",") if p.strip().isdigit()] if right else []
    if len(r) == 3:
        if r[0] > 0:
            heights["preview"] = r[0]
        if r[2] > 0:
            heights["info"] = r[2]
    return widths, heights


def zone_rects(x: int, y: int, w: int, h: int) -> dict[str, Rect]:
    """Drop zones inside one panel's rect; the centre is not a zone."""
    side, band = w // 4, h * 2 // 5
    return {
        "left": (x, y, side, h),
        "right": (x + w - side, y, side, h),
        "top": (x + side, y, w - 2 * side, band),
        "bottom": (x + side, y + h - band, w - 2 * side, band),
    }


def hit_zone(rects: dict[str, Rect], px: int, py: int, exclude: str | None) -> tuple[str, str] | None:
    """(panel, zone) under (px, py), skipping the panel being dragged."""
    for name, rect in rects.items():
        if name == exclude:
            continue
        for zone, (zx, zy, zw, zh) in zone_rects(*rect).items():
            if zx <= px < zx + zw and zy <= py < zy + zh:
                return name, zone
    return None


def grip_rect(rect: Rect) -> Rect:
    """Edit-mode grip label, centred near the top of a panel."""
    x, y, w, _ = rect
    gw = min(140, w - 8)
    return (x + (w - gw) // 2, y + 12, gw, 28)


# -- Overlay --

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
