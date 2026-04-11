"""Thumbnail grid widget for the Qt6 GUI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize, QRect, QRectF, QMimeData, QUrl, QPoint, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPixmap, QPainter, QPainterPath, QColor, QPen, QKeyEvent, QWheelEvent, QDrag, QMouseEvent
from PySide6.QtWidgets import (
    QWidget,
    QScrollArea,
    QMenu,
    QApplication,
    QRubberBand,
)

from ..core.api.base import Post

THUMB_SIZE = 180
THUMB_SPACING = 2
BORDER_WIDTH = 2


class ThumbnailWidget(QWidget):
    """Single clickable thumbnail cell."""

    clicked = Signal(int, object)  # index, QMouseEvent
    double_clicked = Signal(int)
    right_clicked = Signal(int, object)  # index, QPoint

    # QSS-controllable dot colors
    _saved_color = QColor("#22cc22")
    _bookmarked_color = QColor("#ffcc00")

    def _get_saved_color(self): return self._saved_color
    def _set_saved_color(self, c): self._saved_color = QColor(c) if isinstance(c, str) else c
    savedColor = Property(QColor, _get_saved_color, _set_saved_color)

    def _get_bookmarked_color(self): return self._bookmarked_color
    def _set_bookmarked_color(self, c): self._bookmarked_color = QColor(c) if isinstance(c, str) else c
    bookmarkedColor = Property(QColor, _get_bookmarked_color, _set_bookmarked_color)

    # QSS-controllable selection paint colors. Defaults are read from the
    # palette in __init__ so non-themed environments still pick up the
    # system Highlight color, but a custom.qss can override any of them
    # via `ThumbnailWidget { qproperty-selectionColor: ${accent}; }`.
    _selection_color = QColor("#3399ff")
    _multi_select_color = QColor("#226699")
    _hover_color = QColor("#66bbff")
    _idle_color = QColor("#444444")

    def _get_selection_color(self): return self._selection_color
    def _set_selection_color(self, c): self._selection_color = QColor(c) if isinstance(c, str) else c
    selectionColor = Property(QColor, _get_selection_color, _set_selection_color)

    def _get_multi_select_color(self): return self._multi_select_color
    def _set_multi_select_color(self, c): self._multi_select_color = QColor(c) if isinstance(c, str) else c
    multiSelectColor = Property(QColor, _get_multi_select_color, _set_multi_select_color)

    def _get_hover_color(self): return self._hover_color
    def _set_hover_color(self, c): self._hover_color = QColor(c) if isinstance(c, str) else c
    hoverColor = Property(QColor, _get_hover_color, _set_hover_color)

    def _get_idle_color(self): return self._idle_color
    def _set_idle_color(self, c): self._idle_color = QColor(c) if isinstance(c, str) else c
    idleColor = Property(QColor, _get_idle_color, _set_idle_color)

    # Thumbnail fade-in opacity (0.0 → 1.0 on pixmap arrival)
    def _get_thumb_opacity(self): return self._thumb_opacity
    def _set_thumb_opacity(self, v):
        self._thumb_opacity = v
        self.update()
    thumbOpacity = Property(float, _get_thumb_opacity, _set_thumb_opacity)

    def __init__(self, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self._pixmap: QPixmap | None = None
        self._selected = False
        self._multi_selected = False
        self._bookmarked = False
        self._saved_locally = False
        self._hover = False
        self._drag_start: QPoint | None = None
        self._cached_path: str | None = None
        self._prefetch_progress: float = -1  # -1 = not prefetching, 0-1 = progress
        self._thumb_opacity: float = 0.0
        # Seed selection colors from the palette so non-themed environments
        # (no custom.qss) automatically use the system highlight color.
        # The qproperty setters above override these later when the QSS is
        # polished, so any theme can repaint via `qproperty-selectionColor`.
        from PySide6.QtGui import QPalette
        pal = self.palette()
        self._selection_color = pal.color(QPalette.ColorRole.Highlight)
        self._multi_select_color = self._selection_color.darker(150)
        self._hover_color = self._selection_color.lighter(150)
        self._idle_color = pal.color(QPalette.ColorRole.Mid)
        self.setFixedSize(THUMB_SIZE, THUMB_SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap.scaled(
            THUMB_SIZE - 4, THUMB_SIZE - 4,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumb_opacity = 0.0
        self._fade_anim = QPropertyAnimation(self, b"thumbOpacity")
        self._fade_anim.setDuration(200)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.start()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

    def set_multi_selected(self, selected: bool) -> None:
        self._multi_selected = selected
        self.update()

    def set_bookmarked(self, bookmarked: bool) -> None:
        self._bookmarked = bookmarked
        self.update()

    def set_saved_locally(self, saved: bool) -> None:
        self._saved_locally = saved
        self.update()

    def set_prefetch_progress(self, progress: float) -> None:
        """Set prefetch progress: -1 = hide, 0.0-1.0 = progress."""
        self._prefetch_progress = progress
        self.update()

    def paintEvent(self, event) -> None:
        # Ensure QSS is applied so palette picks up custom colors
        self.ensurePolished()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        # State colors come from Qt Properties so QSS can override them.
        # Defaults were seeded from the palette in __init__.
        highlight = self._selection_color
        base = pal.color(pal.ColorRole.Base)
        mid = self._idle_color
        window = pal.color(pal.ColorRole.Window)

        # Fill entire cell with window color
        p.fillRect(self.rect(), window)

        # Content rect hugs the pixmap
        if self._pixmap:
            pw, ph = self._pixmap.width(), self._pixmap.height()
            cx = (self.width() - pw) // 2
            cy = (self.height() - ph) // 2
            content = QRect(cx - BORDER_WIDTH, cy - BORDER_WIDTH,
                            pw + BORDER_WIDTH * 2, ph + BORDER_WIDTH * 2)
        else:
            content = self.rect()

        # Background (content area only)
        if self._multi_selected:
            p.fillRect(content, self._multi_select_color.darker(200))
        elif self._hover:
            p.fillRect(content, window.lighter(130))

        # Border (content area only). Pen-width-aware geometry: a QPen
        # centered on a QRect's geometric edge spills half a pixel out on
        # each side, which on AA-on rendering blends with the cell
        # background and makes the border read as thinner than the pen
        # width. Inset by half the pen width into a QRectF so the full
        # pen width sits cleanly inside the content rect.
        # All four state colors are QSS-controllable Qt Properties on
        # ThumbnailWidget — see selectionColor, multiSelectColor,
        # hoverColor, idleColor at the top of this class.
        if self._selected:
            pen_width = 3
            pen_color = self._selection_color
        elif self._multi_selected:
            pen_width = 3
            pen_color = self._multi_select_color
        elif self._hover:
            pen_width = 1
            pen_color = self._hover_color
        else:
            pen_width = 1
            pen_color = self._idle_color
        half = pen_width / 2.0
        border_rect = QRectF(content).adjusted(half, half, -half, -half)

        # Draw the thumbnail FIRST so the selection border z-orders on top.
        # No clip path: the border is square and the pixmap is square, so
        # there's nothing to round and nothing to mismatch.
        if self._pixmap:
            x = (self.width() - self._pixmap.width()) // 2
            y = (self.height() - self._pixmap.height()) // 2
            if self._thumb_opacity < 1.0:
                p.setOpacity(self._thumb_opacity)
            p.drawPixmap(x, y, self._pixmap)
            if self._thumb_opacity < 1.0:
                p.setOpacity(1.0)

        # Border drawn AFTER the pixmap. Plain rectangle (no rounding) so
        # it lines up exactly with the pixmap's square edges — no corner
        # cut-off triangles where window color would peek through.
        pen = QPen(pen_color, pen_width)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(border_rect)

        # Indicators (top-right of content rect): bookmark on the left,
        # saved dot on the right. Both share a fixed-size box so
        # they're vertically and horizontally aligned. The right anchor
        # is fixed regardless of which indicators are visible, so the
        # rightmost slot stays in the same place whether the cell has
        # one indicator or two.
        from PySide6.QtGui import QFont
        slot_size = 9
        slot_gap = 2
        slot_y = content.top() + 3
        right_anchor = content.right() - 3

        # Build the row right-to-left so we can decrement x as we draw.
        # Right slot (drawn first): the saved-locally dot.
        # Left slot (drawn second): the bookmark star.
        draw_order: list[tuple[str, QColor]] = []
        if self._saved_locally:
            draw_order.append(('dot', self._saved_color))
        if self._bookmarked:
            draw_order.append(('star', self._bookmarked_color))

        x = right_anchor - slot_size
        for kind, color in draw_order:
            slot = QRect(x, slot_y, slot_size, slot_size)
            if kind == 'dot':
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
                # 1px inset so the circle doesn't kiss the slot edge —
                # makes it look slightly less stamped-on at small sizes.
                p.drawEllipse(slot.adjusted(1, 1, -1, -1))
            elif kind == 'star':
                p.setPen(color)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setFont(QFont(p.font().family(), 9))
                p.drawText(slot, int(Qt.AlignmentFlag.AlignCenter), "\u2605")
            x -= (slot_size + slot_gap)

        # Multi-select checkmark
        if self._multi_selected:
            cx, cy = content.left() + 4, content.top() + 4
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(highlight)
            p.drawEllipse(cx, cy, 12, 12)
            p.setPen(QPen(base, 2))
            p.drawLine(cx + 3, cy + 6, cx + 5, cy + 9)
            p.drawLine(cx + 5, cy + 9, cx + 10, cy + 3)

        # Prefetch progress bar
        if self._prefetch_progress >= 0:
            bar_h = 3
            bar_y = content.bottom() - bar_h - 1
            bar_w = int((content.width() - 8) * self._prefetch_progress)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(100, 100, 100, 120))
            p.drawRect(content.left() + 4, bar_y, content.width() - 8, bar_h)
            p.setBrush(highlight)
            p.drawRect(content.left() + 4, bar_y, bar_w, bar_h)

        p.end()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position().toPoint()
            self.clicked.emit(self.index, event)
        elif event.button() == Qt.MouseButton.RightButton:
            self.right_clicked.emit(self.index, event.globalPosition().toPoint())

    def mouseMoveEvent(self, event) -> None:
        if (self._drag_start and self._cached_path
                and (event.position().toPoint() - self._drag_start).manhattanLength() > 10):
            drag = QDrag(self)
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(self._cached_path)])
            drag.setMimeData(mime)
            if self._pixmap:
                drag.setPixmap(self._pixmap.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio))
            drag.exec(Qt.DropAction.CopyAction)
            self._drag_start = None
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_start = None

    def mouseDoubleClickEvent(self, event) -> None:
        self._drag_start = None
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self.index)


class FlowLayout(QWidget):
    """A widget that arranges children in a wrapping flow."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[QWidget] = []

    def add_widget(self, widget: QWidget) -> None:
        widget.setParent(self)
        self._items.append(widget)
        self._do_layout()

    def clear(self) -> None:
        for w in self._items:
            w.setParent(None)  # type: ignore
            w.deleteLater()
        self._items.clear()
        self.setMinimumHeight(0)

    def resizeEvent(self, event) -> None:
        self._do_layout()

    def _do_layout(self) -> None:
        """Position children in a deterministic grid.

        Uses the THUMB_SIZE / THUMB_SPACING constants instead of each
        widget's actual `width()` so the layout is independent of per-
        widget size variance. This matters because:

        1. ThumbnailWidget calls `setFixedSize(THUMB_SIZE, THUMB_SIZE)`
           in `__init__`, capturing the constant at construction time.
           If `THUMB_SIZE` is later mutated (`_apply_settings` writes
           `grid_mod.THUMB_SIZE = new_size` in main_window.py:2953),
           existing thumbs keep their old fixed size while new ones
           (e.g. from infinite-scroll backfill via `append_posts`) get
           the new one. Mixed widths break a width-summing wrap loop.

        2. The previous wrap loop walked each thumb summing
           `widget.width() + THUMB_SPACING` and wrapped on
           `x + item_w > self.width()`. At column boundaries
           (window width within a few pixels of `N * step + margin`)
           the boundary depends on every per-widget width, and any
           sub-pixel or mid-mutation drift could collapse the column
           count by 1.

        Now: compute the column count once from the container width
        and the constant step, then position thumbs by `(col, row)`
        index. The layout is a function of `self.width()` and the
        constants only — no per-widget reads.
        """
        if not self._items:
            return
        width = self.width() or 800
        step = THUMB_SIZE + THUMB_SPACING
        # Account for the leading THUMB_SPACING margin: a row that fits
        # N thumbs needs `THUMB_SPACING + N * step` pixels minimum, not
        # `N * step`. The previous formula `w // step` overcounted by 1
        # at the boundary (e.g. width=1135 returned 6 columns where the
        # actual fit is 5).
        cols = max(1, (width - THUMB_SPACING) // step)

        for i, widget in enumerate(self._items):
            col = i % cols
            row = i // cols
            x = THUMB_SPACING + col * step
            y = THUMB_SPACING + row * step
            widget.move(x, y)
            widget.show()

        rows = (len(self._items) + cols - 1) // cols
        self.setMinimumHeight(THUMB_SPACING + rows * step)

    @property
    def columns(self) -> int:
        """Same formula as `_do_layout`'s column count.

        Both must agree exactly so callers (e.g. main_window's
        keyboard Up/Down nav step) get the value the visual layout
        actually used. The previous version was off-by-one because it
        omitted the leading THUMB_SPACING from the calculation.
        """
        if not self._items:
            return 1
        # Use parent viewport width if inside a QScrollArea
        parent = self.parentWidget()
        if parent and hasattr(parent, 'viewport'):
            w = parent.viewport().width()
        else:
            w = self.width() or 800
        step = THUMB_SIZE + THUMB_SPACING
        return max(1, (w - THUMB_SPACING) // step)


class ThumbnailGrid(QScrollArea):
    """Scrollable grid of thumbnail widgets with keyboard nav, context menu, and multi-select."""

    post_selected = Signal(int)
    post_activated = Signal(int)
    context_requested = Signal(int, object)  # index, QPoint
    multi_context_requested = Signal(list, object)  # list[int], QPoint
    reached_bottom = Signal()  # emitted when scrolled to the bottom
    reached_top = Signal()     # emitted when scrolled to the top
    nav_past_end = Signal()    # nav past last post (keyboard or scroll tilt)
    nav_before_start = Signal()  # nav before first post (keyboard or scroll tilt)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._flow = FlowLayout()
        self.setWidget(self._flow)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._thumbs: list[ThumbnailWidget] = []
        self._selected_index = -1
        self._multi_selected: set[int] = set()
        self._last_click_index = -1  # for shift-click range
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.verticalScrollBar().valueChanged.connect(self._check_scroll_bottom)
        # Rubber band drag selection
        self._rubber_band: QRubberBand | None = None
        self._rb_origin: QPoint | None = None

    @property
    def selected_index(self) -> int:
        return self._selected_index

    @property
    def selected_indices(self) -> list[int]:
        """Return all multi-selected indices, or just the single selected one."""
        if self._multi_selected:
            return sorted(self._multi_selected)
        if self._selected_index >= 0:
            return [self._selected_index]
        return []

    def set_posts(self, count: int) -> list[ThumbnailWidget]:
        self._flow.clear()
        self._thumbs.clear()
        self._selected_index = -1
        self._multi_selected.clear()
        self._last_click_index = -1

        for i in range(count):
            thumb = ThumbnailWidget(i)
            thumb.clicked.connect(self._on_thumb_click)
            thumb.double_clicked.connect(self._on_thumb_double_click)
            thumb.right_clicked.connect(self._on_thumb_right_click)
            self._flow.add_widget(thumb)
            self._thumbs.append(thumb)

        return self._thumbs

    def append_posts(self, count: int) -> list[ThumbnailWidget]:
        """Add more thumbnails to the existing grid."""
        start = len(self._thumbs)
        new_thumbs = []
        for i in range(start, start + count):
            thumb = ThumbnailWidget(i)
            thumb.clicked.connect(self._on_thumb_click)
            thumb.double_clicked.connect(self._on_thumb_double_click)
            thumb.right_clicked.connect(self._on_thumb_right_click)
            self._flow.add_widget(thumb)
            self._thumbs.append(thumb)
            new_thumbs.append(thumb)
        return new_thumbs

    def _clear_multi(self) -> None:
        for idx in self._multi_selected:
            if 0 <= idx < len(self._thumbs):
                self._thumbs[idx].set_multi_selected(False)
        self._multi_selected.clear()

    def clear_selection(self) -> None:
        """Deselect everything."""
        self._clear_multi()
        if 0 <= self._selected_index < len(self._thumbs):
            self._thumbs[self._selected_index].set_selected(False)
        self._selected_index = -1

    def _select(self, index: int) -> None:
        if index < 0 or index >= len(self._thumbs):
            return
        self._clear_multi()
        if 0 <= self._selected_index < len(self._thumbs):
            self._thumbs[self._selected_index].set_selected(False)
        self._selected_index = index
        self._last_click_index = index
        self._thumbs[index].set_selected(True)
        self.ensureWidgetVisible(self._thumbs[index])
        self.post_selected.emit(index)

    def _toggle_multi(self, index: int) -> None:
        """Ctrl+click: toggle one item in/out of multi-selection."""
        # First ctrl+click: add the currently single-selected item too
        if not self._multi_selected and self._selected_index >= 0:
            self._multi_selected.add(self._selected_index)
            self._thumbs[self._selected_index].set_multi_selected(True)

        if index in self._multi_selected:
            self._multi_selected.discard(index)
            self._thumbs[index].set_multi_selected(False)
        else:
            self._multi_selected.add(index)
            self._thumbs[index].set_multi_selected(True)
        self._last_click_index = index

    def _range_select(self, index: int) -> None:
        """Shift+click: select range from last click to this one."""
        start = self._last_click_index if self._last_click_index >= 0 else 0
        lo, hi = min(start, index), max(start, index)
        self._clear_multi()
        for i in range(lo, hi + 1):
            self._multi_selected.add(i)
            self._thumbs[i].set_multi_selected(True)

    def _on_thumb_click(self, index: int, event) -> None:
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self._toggle_multi(index)
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            self._range_select(index)
        else:
            self._select(index)

    def _on_thumb_double_click(self, index: int) -> None:
        self._select(index)
        self.post_activated.emit(index)

    def _on_thumb_right_click(self, index: int, pos) -> None:
        if self._multi_selected and index in self._multi_selected:
            self.multi_context_requested.emit(sorted(self._multi_selected), pos)
        else:
            # Select visually but don't activate (no preview change)
            self._clear_multi()
            if 0 <= self._selected_index < len(self._thumbs):
                self._thumbs[self._selected_index].set_selected(False)
            self._selected_index = index
            self._thumbs[index].set_selected(True)
            self.ensureWidgetVisible(self._thumbs[index])
            self.context_requested.emit(index, pos)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            # Only start rubber band if click is on empty grid space (not a thumbnail)
            child = self.childAt(event.position().toPoint())
            if child is self.widget() or child is self.viewport():
                self._rb_origin = event.position().toPoint()
                if not self._rubber_band:
                    self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
                self._rubber_band.setGeometry(QRect(self._rb_origin, QSize()))
                self._rubber_band.show()
                # Click on empty space deselects everything
                self.clear_selection()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._rb_origin and self._rubber_band:
            rb_rect = QRect(self._rb_origin, event.position().toPoint()).normalized()
            self._rubber_band.setGeometry(rb_rect)
            # Select thumbnails that intersect the rubber band
            vp_offset = self.widget().mapFrom(self.viewport(), QPoint(0, 0))
            self._clear_multi()
            for i, thumb in enumerate(self._thumbs):
                thumb_rect = thumb.geometry().translated(vp_offset)
                if rb_rect.intersects(thumb_rect):
                    self._multi_selected.add(i)
                    thumb.set_multi_selected(True)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._rb_origin and self._rubber_band:
            self._rubber_band.hide()
            self._rb_origin = None
            return
        # Reset any stuck cursor from a cancelled drag-and-drop
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def select_all(self) -> None:
        self._clear_multi()
        for i in range(len(self._thumbs)):
            self._multi_selected.add(i)
            self._thumbs[i].set_multi_selected(True)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        cols = self._flow.columns
        idx = self._selected_index

        key = event.key()
        mods = event.modifiers()

        # Ctrl+A = select all
        if key == Qt.Key.Key_A and mods & Qt.KeyboardModifier.ControlModifier:
            self.select_all()
            return

        if key in (Qt.Key.Key_Right, Qt.Key.Key_L):
            self._nav_horizontal(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_H):
            self._nav_horizontal(-1)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_J):
            target = idx + cols
            if target >= len(self._thumbs):
                # If there are posts ahead in the last row, go to the last one
                if idx < len(self._thumbs) - 1:
                    self._select(len(self._thumbs) - 1)
                else:
                    self.nav_past_end.emit()
            else:
                self._select(target)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_K):
            target = idx - cols
            if target < 0:
                if idx > 0:
                    self._select(0)
                else:
                    self.nav_before_start.emit()
            else:
                self._select(target)
        elif key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            if 0 <= idx < len(self._thumbs):
                self.post_activated.emit(idx)
        elif key == Qt.Key.Key_Home:
            self._select(0)
        elif key == Qt.Key.Key_End:
            self._select(len(self._thumbs) - 1)
        else:
            super().keyPressEvent(event)

    def scroll_to_top(self) -> None:
        self.verticalScrollBar().setValue(0)

    def scroll_to_bottom(self) -> None:
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _check_scroll_bottom(self, value: int) -> None:
        sb = self.verticalScrollBar()
        # Trigger when within 3 rows of the bottom for early prefetch
        threshold = (THUMB_SIZE + THUMB_SPACING) * 3
        if sb.maximum() > 0 and value >= sb.maximum() - threshold:
            self.reached_bottom.emit()
        if value <= 0 and sb.maximum() > 0:
            self.reached_top.emit()

    def _nav_horizontal(self, direction: int) -> None:
        """Move selection one cell left (-1) or right (+1); emit edge signals at boundaries."""
        idx = self._selected_index
        target = idx + direction
        if target < 0:
            self.nav_before_start.emit()
        elif target >= len(self._thumbs):
            self.nav_past_end.emit()
        else:
            self._select(target)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().x()
        if delta > 30:
            self._nav_horizontal(-1)
        elif delta < -30:
            self._nav_horizontal(1)
        else:
            super().wheelEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._flow:
            self._flow.resize(self.viewport().size().width(), self._flow.minimumHeight())
