"""Base stylesheet defaults, Qt-free so tests can import them.

Kept out of app_runtime on purpose: that module imports PySide6 at the
top, and the test suite runs without Qt installed (CI installs only
httpx, Pillow, pytest). Anything a test needs to read lives here.
"""

from __future__ import annotations

# Base popout overlay style — always loaded *before* the user QSS so the
# floating top toolbar (`#_slideshow_toolbar`) and bottom video controls
# (`#_slideshow_controls`) get a sane translucent-black-with-white-text
# look on themes that don't define their own overlay rules. Bundled themes
# in `themes/` redefine the same selectors with their @palette colors and
# win on tie (last rule of equal specificity wins in QSS), so anyone using
# a packaged theme keeps the themed overlay; anyone with a stripped-down
# custom.qss still gets a usable overlay instead of bare letterbox.
_BASE_POPOUT_OVERLAY_QSS = """
/* 24x24 glyph buttons (preview + popout toolbars) opt in via the
   iconBtn dynamic property. Theme QPushButton padding (2px 6px + 1px
   border) would leave a 10px content box and clip the glyph. An
   attribute selector outranks a bare type selector on QSS specificity,
   so this holds no matter what a custom.qss sets on QPushButton. The
   overlay containers get their own variant because themes style
   `QWidget#_slideshow_toolbar QPushButton` (ID + type, 1-0-1), which
   would beat the bare attribute rule (0-1-1); ID + attribute + type
   (1-1-1) wins. One font size for both toolbars so the glyphs match.
   Geometry is pinned here rather than left to setFixedSize: QSS
   min-*/max-* are written straight onto the widget's minimum/maximum
   size (content-box relative), so a theme's `min-height: 17px` on
   QPushButton, or a `min-width: 0` here, silently replaces the 24px
   minimum and the layout shrinks each button to its glyph width. */
QPushButton[iconBtn="true"],
QWidget#_slideshow_toolbar QPushButton[iconBtn="true"],
QWidget#_slideshow_controls QPushButton[iconBtn="true"],
QWidget#_preview_controls QPushButton[iconBtn="true"] {
    padding: 0;
    min-width: 22px;
    max-width: 22px;
    min-height: 22px;
    max-height: 22px;
    font-size: 14px;
    font-weight: normal;
}
QWidget#_slideshow_toolbar,
QWidget#_slideshow_controls {
    background: rgba(0, 0, 0, 160);
}
QWidget#_slideshow_toolbar *,
QWidget#_slideshow_controls * {
    background: transparent;
    color: white;
    border: none;
}
QWidget#_slideshow_toolbar QPushButton,
QWidget#_slideshow_controls QPushButton {
    background: transparent;
    color: white;
    border: 1px solid rgba(255, 255, 255, 80);
    padding: 2px 6px;
    font-size: 15px;
    font-weight: bold;
}
QWidget#_slideshow_toolbar QPushButton:hover,
QWidget#_slideshow_controls QPushButton:hover {
    background: rgba(255, 255, 255, 30);
}
QWidget#_slideshow_toolbar QSlider::groove:horizontal,
QWidget#_slideshow_controls QSlider::groove:horizontal {
    background: rgba(255, 255, 255, 40);
    height: 4px;
    border: none;
}
QWidget#_slideshow_toolbar QSlider::handle:horizontal,
QWidget#_slideshow_controls QSlider::handle:horizontal {
    background: white;
    width: 10px;
    margin: -4px 0;
    border: none;
}
QWidget#_slideshow_toolbar QSlider::sub-page:horizontal,
QWidget#_slideshow_controls QSlider::sub-page:horizontal {
    background: white;
}
QWidget#_slideshow_toolbar QLabel,
QWidget#_slideshow_controls QLabel {
    background: transparent;
    color: white;
}
/* Hide the standard icon column on every QMessageBox (question mark,
 * warning triangle, info circle) so confirm dialogs are text-only. */
QMessageBox QLabel#qt_msgboxex_icon_label {
    image: none;
    max-width: 0px;
    max-height: 0px;
    margin: 0px;
    padding: 0px;
}
"""
