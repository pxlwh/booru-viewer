"""Thumbnail grid widget for the Qt6 GUI."""

from __future__ import annotations

import logging

log = logging.getLogger("booru")

from PySide6.QtCore import Qt, Signal, QSize, QRect, QRectF, QMimeData, QUrl, QPoint, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QKeyEvent, QWheelEvent, QDrag, QMouseEvent
from PySide6.QtWidgets import (
    QWidget,
    QScrollArea,
    QRubberBand,
)

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
        self._source_path: str | None = None  # on-disk path, for re-scaling on size change
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
        self.setMouseTracking(True)

    def set_pixmap(self, pixmap: QPixmap, path: str | None = None) -> None:
        if path is not None:
            self._source_path = path
        self._pixmap = pixmap.scaled(
            THUMB_SIZE - 4, THUMB_SIZE - 4,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumb_opacity = 0.0
        anim = QPropertyAnimation(self, b"thumbOpacity")
        anim.setDuration(80)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: self._on_fade_done(anim))
        self._fade_anim = anim
        anim.start()

    def _on_fade_done(self, anim: QPropertyAnimation) -> None:
        """Clear the reference then schedule deletion."""
        if self._fade_anim is anim:
            self._fade_anim = None
        anim.deleteLater()

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

    def leaveEvent(self, event) -> None:
        if self._hover:
            self._hover = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()

    def mouseMoveEvent(self, event) -> None:
        # If the grid has a pending or active rubber band, forward the move
        grid = self._grid()
        if grid and (grid._rb_origin or grid._rb_pending_origin):
            vp_pos = self.mapTo(grid.viewport(), event.position().toPoint())
            if grid._rb_origin:
                grid._rb_drag(vp_pos)
                return
            if grid._maybe_start_rb(vp_pos):
                grid._rb_drag(vp_pos)
                return
            return
        # Update hover and cursor based on whether cursor is over the pixmap
        over = self._hit_pixmap(event.position().toPoint()) if self._pixmap else False
        if over != self._hover:
            self._hover = over
            self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)
            self.update()
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
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return

    def _hit_pixmap(self, pos) -> bool:
        """True if pos is within the drawn pixmap area."""
        if not self._pixmap:
            return False
        px = (self.width() - self._pixmap.width()) // 2
        py = (self.height() - self._pixmap.height()) // 2
        return QRect(px, py, self._pixmap.width(), self._pixmap.height()).contains(pos)

    def _grid(self):
        """Walk up to the ThumbnailGrid ancestor."""
        w = self.parentWidget()
        while w:
            if isinstance(w, ThumbnailGrid):
                return w
            w = w.parentWidget()
        return None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            if not self._hit_pixmap(pos):
                grid = self._grid()
                if grid:
                    grid.on_padding_click(self, pos)
                event.accept()
                return
            # Pixmap click — clear any stale rubber band state from a
            # previous interrupted drag before starting a new interaction.
            grid = self._grid()
            if grid:
                grid._clear_stale_rubber_band()
            self._drag_start = pos
            self.clicked.emit(self.index, event)
        elif event.button() == Qt.MouseButton.RightButton:
            self.right_clicked.emit(self.index, event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event) -> None:
        self._drag_start = None
        grid = self._grid()
        if grid:
            if grid._rb_origin:
                grid._rb_end()
            elif grid._rb_pending_origin is not None:
                # Click without drag — treat as deselect
                grid._rb_pending_origin = None
                grid.clear_selection()

    def mouseDoubleClickEvent(self, event) -> None:
        self._drag_start = None
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            if not self._hit_pixmap(pos):
                grid = self._grid()
                if grid:
                    grid.on_padding_click(self, pos)
                return
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
            if hasattr(w, '_fade_anim') and w._fade_anim is not None:
                w._fade_anim.stop()
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
        self._rb_pending_origin: QPoint | None = None  # press position, not yet confirmed as drag
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

    def _clear_stale_rubber_band(self) -> None:
        """Reset any leftover rubber band state before starting a new interaction.

        Rubber band state can get stuck if a drag is interrupted without
        a matching release event — Wayland focus steal, drag outside the
        window, tab switch mid-drag, etc. Every new mouse press calls this
        so the next interaction starts from a clean slate instead of
        reusing a stale origin (which would make the rubber band "not
        work" until the app is restarted).
        """
        if self._rubber_band is not None:
            self._rubber_band.hide()
        self._rb_origin = None
        self._rb_pending_origin = None

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

    def _start_rubber_band(self, pos: QPoint) -> None:
        """Start a rubber band selection and deselect."""
        self._rb_origin = pos
        if not self._rubber_band:
            self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        self._rubber_band.setGeometry(QRect(self._rb_origin, QSize()))
        self._rubber_band.show()
        self.clear_selection()

    def on_padding_click(self, thumb, local_pos) -> None:
        """Called directly by ThumbnailWidget when a click misses the pixmap."""
        self._clear_stale_rubber_band()
        vp_pos = thumb.mapTo(self.viewport(), local_pos)
        self._rb_pending_origin = vp_pos

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # Clicks on viewport/flow (gaps, space below thumbs) start rubber band
        if event.button() == Qt.MouseButton.LeftButton:
            self._clear_stale_rubber_band()
            child = self.childAt(event.position().toPoint())
            if child is self.widget() or child is self.viewport():
                self._rb_pending_origin = event.position().toPoint()
                return
        super().mousePressEvent(event)

    def _rb_drag(self, vp_pos: QPoint) -> None:
        """Update rubber band geometry and intersected thumb selection."""
        if not (self._rb_origin and self._rubber_band):
            return
        rb_rect = QRect(self._rb_origin, vp_pos).normalized()
        self._rubber_band.setGeometry(rb_rect)
        # rb_rect is in viewport coords; thumb.geometry() is in widget (content)
        # coords. Convert rb_rect to widget coords for the intersection test —
        # widget.mapFrom(viewport, (0,0)) gives the widget-coord of viewport's
        # origin, which is exactly the translation needed when scrolled.
        vp_offset = self.widget().mapFrom(self.viewport(), QPoint(0, 0))
        rb_widget = rb_rect.translated(vp_offset)
        self._clear_multi()
        for i, thumb in enumerate(self._thumbs):
            if rb_widget.intersects(thumb.geometry()):
                self._multi_selected.add(i)
                thumb.set_multi_selected(True)

    def _rb_end(self) -> None:
        """Hide the rubber band and clear origin."""
        if self._rubber_band:
            self._rubber_band.hide()
        self._rb_origin = None

    def _maybe_start_rb(self, vp_pos: QPoint) -> bool:
        """If a rubber band press is pending and we've moved past threshold, start it."""
        if self._rb_pending_origin is None:
            return False
        if (vp_pos - self._rb_pending_origin).manhattanLength() < 30:
            return False
        self._start_rubber_band(self._rb_pending_origin)
        self._rb_pending_origin = None
        return True

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if self._rb_origin and self._rubber_band:
            self._rb_drag(pos)
            return
        if self._maybe_start_rb(pos):
            self._rb_drag(pos)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._rb_origin and self._rubber_band:
            self._rb_end()
            return
        if self._rb_pending_origin is not None:
            # Click without drag — treat as deselect
            self._rb_pending_origin = None
            self.clear_selection()
            return
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        # Clear stuck hover states — Wayland doesn't always fire
        # leaveEvent on individual child widgets when the mouse
        # exits the scroll area quickly.
        for thumb in self._thumbs:
            if thumb._hover:
                thumb._hover = False
                thumb.update()
        super().leaveEvent(event)

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
        elif key == Qt.Key.Key_Escape:
            self.clear_selection()
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
        self._recycle_offscreen()

    def _recycle_offscreen(self) -> None:
        """Release decoded pixmaps for thumbnails far from the viewport.

        Thumbnails within the visible area plus a buffer zone keep their
        pixmaps.  Thumbnails outside that zone have their pixmap set to
        None to free decoded-image memory.  When they scroll back into
        view, the pixmap is re-decoded from the on-disk thumbnail cache
        via ``_source_path``.

        This caps decoded-thumbnail memory to roughly (visible + buffer)
        widgets instead of every widget ever created during infinite scroll.
        """
        if not self._thumbs:
            return
        step = THUMB_SIZE + THUMB_SPACING
        if step == 0:
            return
        cols = self._flow.columns
        vp_top = self.verticalScrollBar().value()
        vp_height = self.viewport().height()

        # Row range that's visible (0-based row indices)
        first_visible_row = max(0, (vp_top - THUMB_SPACING) // step)
        last_visible_row = (vp_top + vp_height) // step

        # Buffer: keep ±5 rows of decoded pixmaps beyond the viewport
        buffer_rows = 5
        keep_first = max(0, first_visible_row - buffer_rows)
        keep_last = last_visible_row + buffer_rows

        keep_start = keep_first * cols
        keep_end = min(len(self._thumbs), (keep_last + 1) * cols)

        for i, thumb in enumerate(self._thumbs):
            if keep_start <= i < keep_end:
                # Inside keep zone — restore if missing
                if thumb._pixmap is None and thumb._source_path:
                    pix = QPixmap(thumb._source_path)
                    if not pix.isNull():
                        thumb._pixmap = pix.scaled(
                            THUMB_SIZE - 4, THUMB_SIZE - 4,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                        thumb._thumb_opacity = 1.0
                        thumb.update()
            else:
                # Outside keep zone — release
                if thumb._pixmap is not None:
                    thumb._pixmap = None

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
