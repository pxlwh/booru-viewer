"""Toolbar icons drawn from inline SVG, tinted to the button's text colour.

The preview and popout toolbars used to be text glyphs (☆ ↓ ⊘ ⊗ ⧉). A glyph is
drawn by whatever font the active Qt style supplies, so its size and weight
changed with the theme: under AeroThemePlasma ⊘ and ⊗ came out large and ↓
tiny, next to a small ☆. An SVG has one shape under every style; only its colour
follows the theme, read from the button's palette each time the palette or
style changes (a custom.qss `color:` lands in the palette when Qt polishes the
widget, so themed buttons tint correctly too).

Drawn for this app on a 24 unit grid, stroke based, no third party icon set.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QPushButton

_STROKE = ('fill="none" stroke="currentColor" stroke-width="2" '
           'stroke-linecap="round" stroke-linejoin="round"')

_STAR = "12,2.8 14.8,8.6 21.2,9.5 16.6,14 17.7,20.4 12,17.4 6.3,20.4 7.4,14 2.8,9.5 9.2,8.6"
# floppy outline, its metal shutter and its label, as one evenodd path so the
# filled variant keeps shutter and label as cut outs
_FLOPPY_BODY = "M4 5a1 1 0 0 1 1-1h11.6l3.4 3.4V19a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z"
_FLOPPY_SHUTTER = "M8 4v4.5h7V4"
_FLOPPY_LABEL = "M7.5 20v-6h9v6"

SVG: dict[str, str] = {
    "star": f'<polygon points="{_STAR}" {_STROKE}/>',
    "star-filled": f'<polygon points="{_STAR}" fill="currentColor" stroke="currentColor" '
                   'stroke-width="2" stroke-linejoin="round"/>',
    "floppy": (f'<path d="{_FLOPPY_BODY}" {_STROKE}/>'
               f'<path d="{_FLOPPY_SHUTTER}" {_STROKE}/>'
               f'<path d="{_FLOPPY_LABEL}" {_STROKE}/>'),
    "floppy-filled": (f'<path fill-rule="evenodd" fill="currentColor" d="{_FLOPPY_BODY} '
                      'M8.5 5v3h6V5z M8.5 15.5v4h7v-4z"/>'
                      f'<path d="{_FLOPPY_BODY}" {_STROKE}/>'),
    # a price tag struck through: blacklist a tag
    "tag-off": (f'<path d="M3.5 12 9.5 5.5H20a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H9.5z" {_STROKE}/>'
                f'<circle cx="9" cy="12" r="1.2" fill="currentColor"/>'
                f'<path d="M5 20 19.5 4" {_STROKE}/>'),
    # an eye struck through: blacklist this post, i.e. hide it from results
    "eye-off": (f'<path d="M2.5 12s3.5-6.5 9.5-6.5 9.5 6.5 9.5 6.5-3.5 6.5-9.5 6.5S2.5 12 2.5 12z" {_STROKE}/>'
                f'<circle cx="12" cy="12" r="2.8" {_STROKE}/>'
                f'<path d="M4.5 20 19.5 4" {_STROKE}/>'),
    # a window with an arrow leaving it: open in the popout
    "popout": (f'<path d="M11 5H5a1 1 0 0 0-1 1v13a1 1 0 0 0 1 1h13a1 1 0 0 0 1-1v-6" {_STROKE}/>'
               f'<path d="M14 4h6v6M20 4l-8.5 8.5" {_STROKE}/>'),
}

ICON_PX = 16


def render(name: str, color: QColor, px: int = ICON_PX, dpr: float = 1.0) -> QPixmap:
    """Rasterise icon ``name`` at ``px`` logical pixels in ``color``."""
    body = SVG[name].replace("currentColor", color.name())
    doc = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{body}</svg>'
    renderer = QSvgRenderer(QByteArray(doc.encode()))
    pm = QPixmap(round(px * dpr), round(px * dpr))
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(dpr)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    return pm


class IconButton(QPushButton):
    """Square toolbar button showing one of ``SVG``, recoloured with the theme."""

    def __init__(self, icon: str, name: str, tip: str, size: int = 24) -> None:
        super().__init__()
        self.setObjectName(name)
        self.setFixedSize(size, size)
        # Matches QPushButton[iconBtn="true"] in the base QSS: zero padding so a
        # theme's button padding cannot squeeze the icon.
        self.setProperty("iconBtn", True)
        self.setToolTip(tip)
        self.setIconSize(QSize(ICON_PX, ICON_PX))
        self._icon_name = icon
        self._refresh()

    def set_icon(self, icon: str) -> None:
        if icon != self._icon_name:
            self._icon_name = icon
            self._refresh()

    def icon_name(self) -> str:
        return self._icon_name

    def _refresh(self) -> None:
        color = self.palette().color(QPalette.ColorRole.ButtonText)
        self.setIcon(QIcon(render(self._icon_name, color, ICON_PX, self.devicePixelRatioF())))

    def changeEvent(self, event) -> None:
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self._refresh()
        super().changeEvent(event)
