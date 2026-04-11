"""Application entry point and Qt-style loading."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .main_window import BooruApp

log = logging.getLogger("booru")


def _apply_windows_dark_mode(app: QApplication) -> None:
    """Detect Windows dark mode and apply Fusion dark palette if needed."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        if value == 0:
            from PySide6.QtGui import QPalette, QColor
            app.setStyle("Fusion")
            palette = QPalette()
            palette.setColor(QPalette.ColorRole.Window, QColor(32, 32, 32))
            palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
            palette.setColor(QPalette.ColorRole.AlternateBase, QColor(38, 38, 38))
            palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(50, 50, 50))
            palette.setColor(QPalette.ColorRole.ToolTipText, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Button, QColor(51, 51, 51))
            palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
            palette.setColor(QPalette.ColorRole.Link, QColor(0, 120, 215))
            palette.setColor(QPalette.ColorRole.Highlight, QColor(0, 120, 215))
            palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Mid, QColor(51, 51, 51))
            palette.setColor(QPalette.ColorRole.Dark, QColor(25, 25, 25))
            palette.setColor(QPalette.ColorRole.Shadow, QColor(0, 0, 0))
            palette.setColor(QPalette.ColorRole.Light, QColor(60, 60, 60))
            palette.setColor(QPalette.ColorRole.Midlight, QColor(55, 55, 55))
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(127, 127, 127))
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(127, 127, 127))
            app.setPalette(palette)
            # Flatten Fusion's 3D look
            app.setStyleSheet(app.styleSheet() + """
                QPushButton {
                    border: 1px solid #555;
                    border-radius: 2px;
                    padding: 4px 12px;
                }
                QPushButton:hover { background-color: #444; }
                QPushButton:pressed { background-color: #333; }
                QComboBox {
                    border: 1px solid #555;
                    border-radius: 2px;
                    padding: 3px 6px;
                }
                QComboBox::drop-down {
                    border: none;
                }
                QSpinBox {
                    border: 1px solid #555;
                    border-radius: 2px;
                }
                QLineEdit, QTextEdit {
                    border: 1px solid #555;
                    border-radius: 2px;
                    padding: 3px;
                    color: #fff;
                    background-color: #191919;
                }
                QScrollBar:vertical {
                    background: #252525;
                    width: 12px;
                }
                QScrollBar::handle:vertical {
                    background: #555;
                    border-radius: 4px;
                    min-height: 20px;
                }
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                    height: 0;
                }
            """)
    except Exception as e:
        log.warning(f"Operation failed: {e}")


# Base popout overlay style — always loaded *before* the user QSS so the
# floating top toolbar (`#_slideshow_toolbar`) and bottom video controls
# (`#_slideshow_controls`) get a sane translucent-black-with-white-text
# look on themes that don't define their own overlay rules. Bundled themes
# in `themes/` redefine the same selectors with their @palette colors and
# win on tie (last rule of equal specificity wins in QSS), so anyone using
# a packaged theme keeps the themed overlay; anyone with a stripped-down
# custom.qss still gets a usable overlay instead of bare letterbox.
_BASE_POPOUT_OVERLAY_QSS = """
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


def _load_user_qss(path: Path) -> str:
    """Load a QSS file with optional @palette variable substitution.

    Qt's QSS dialect has no native variables, so we add a tiny preprocessor:

        /* @palette
           accent:        #cba6f7
           bg:            #1e1e2e
           text:          #cdd6f4
        */

        QWidget {
            background-color: ${bg};
            color: ${text};
            selection-background-color: ${accent};
        }

    The header comment block is parsed for `name: value` pairs and any
    `${name}` reference elsewhere in the file is substituted with the
    corresponding value before the QSS is handed to Qt. This lets users
    recolor a bundled theme by editing the palette block alone, without
    hunting through the body for every hex literal.

    Backward compatibility: a file without an @palette block is returned
    as-is, so plain hand-written Qt-standard QSS still loads unchanged.
    Unknown ${name} references are left in place verbatim and logged as
    warnings so typos are visible in the log.
    """
    import re
    text = path.read_text()
    palette_match = re.search(r'/\*\s*@palette\b(.*?)\*/', text, re.DOTALL)
    if not palette_match:
        return text

    palette: dict[str, str] = {}
    for raw_line in palette_match.group(1).splitlines():
        # Strip leading whitespace and any leading * from C-style continuation
        line = raw_line.strip().lstrip('*').strip()
        if not line or ':' not in line:
            continue
        key, value = line.split(':', 1)
        key = key.strip()
        value = value.strip().rstrip(';').strip()
        # Allow trailing comments on the same line
        if '/*' in value:
            value = value.split('/*', 1)[0].strip()
        if key and value:
            palette[key] = value

    refs = set(re.findall(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}', text))
    missing = refs - palette.keys()
    if missing:
        log.warning(
            f"QSS @palette: unknown vars {sorted(missing)} in {path.name} "
            f"— left in place verbatim, fix the @palette block to define them"
        )

    def replace(m):
        return palette.get(m.group(1), m.group(0))

    return re.sub(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}', replace, text)


def run() -> None:
    from ..core.config import data_dir

    app = QApplication(sys.argv)

    # Set a stable Wayland app_id so Hyprland and other compositors can
    # consistently identify our windows by class (not by title, which
    # changes when search terms appear in the title bar). Qt translates
    # setDesktopFileName into the xdg-shell app_id on Wayland.
    app.setApplicationName("booru-viewer")
    app.setDesktopFileName("booru-viewer")

    # mpv requires LC_NUMERIC=C — Qt resets the locale in QApplication(),
    # so we must restore it after Qt init but before creating any mpv instances.
    import locale
    locale.setlocale(locale.LC_NUMERIC, "C")

    # Apply dark mode on Windows 10+ if system is set to dark
    if sys.platform == "win32":
        _apply_windows_dark_mode(app)

    # Load user custom stylesheet if it exists
    custom_css = data_dir() / "custom.qss"
    if custom_css.exists():
        try:
            # Use Fusion style with arrow color fix
            from PySide6.QtWidgets import QProxyStyle
            from PySide6.QtGui import QPalette, QColor, QPainter as _P
            from PySide6.QtCore import QPoint as _QP

            import re
            # Run through the @palette preprocessor (see _load_user_qss
            # for the dialect). Plain Qt-standard QSS files without an
            # @palette block are returned unchanged.
            css_text = _load_user_qss(custom_css)

            # Extract text color for arrows
            m = re.search(r'QWidget\s*\{[^}]*?(?:^|\s)color\s*:\s*(#[0-9a-fA-F]{3,8})', css_text, re.MULTILINE)
            arrow_color = QColor(m.group(1)) if m else QColor(200, 200, 200)

            class _DarkArrowStyle(QProxyStyle):
                """Fusion proxy that draws visible arrows on dark themes."""
                def drawPrimitive(self, element, option, painter, widget=None):
                    if element in (self.PrimitiveElement.PE_IndicatorSpinUp,
                                   self.PrimitiveElement.PE_IndicatorSpinDown,
                                   self.PrimitiveElement.PE_IndicatorArrowDown,
                                   self.PrimitiveElement.PE_IndicatorArrowUp):
                        painter.save()
                        painter.setRenderHint(_P.RenderHint.Antialiasing)
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(arrow_color)
                        r = option.rect
                        cx, cy = r.center().x(), r.center().y()
                        s = min(r.width(), r.height()) // 3
                        from PySide6.QtGui import QPolygon
                        if element in (self.PrimitiveElement.PE_IndicatorSpinUp,
                                       self.PrimitiveElement.PE_IndicatorArrowUp):
                            painter.drawPolygon(QPolygon([
                                _QP(cx, cy - s), _QP(cx - s, cy + s), _QP(cx + s, cy + s)
                            ]))
                        else:
                            painter.drawPolygon(QPolygon([
                                _QP(cx - s, cy - s), _QP(cx + s, cy - s), _QP(cx, cy + s)
                            ]))
                        painter.restore()
                        return
                    super().drawPrimitive(element, option, painter, widget)

            app.setStyle(_DarkArrowStyle("Fusion"))
            # Prepend the base overlay defaults so even minimal custom.qss
            # files get a usable popout overlay. User rules with the same
            # selectors come last and win on tie.
            app.setStyleSheet(_BASE_POPOUT_OVERLAY_QSS + "\n" + css_text)

            # Extract selection color for grid highlight
            pal = app.palette()
            m = re.search(r'selection-background-color\s*:\s*(#[0-9a-fA-F]{3,8})', css_text)
            if m:
                pal.setColor(QPalette.ColorRole.Highlight, QColor(m.group(1)))
            app.setPalette(pal)
        except Exception as e:
            log.warning(f"Operation failed: {e}")
    else:
        # No custom.qss — force Fusion widgets so distro pyside6 builds linked
        # against system Qt don't pick up Breeze (or whatever the platform
        # theme plugin supplies) and diverge from the bundled-Qt look that
        # source-from-pip users get. The inherited palette is intentionally
        # left alone: KDE writes ~/.config/Trolltech.conf which every Qt app
        # reads, so KDE users still get their color scheme — just under
        # Fusion widgets instead of Breeze.
        app.setStyle("Fusion")
        # Install the popout overlay defaults so the floating toolbar/controls
        # have a sane background instead of bare letterbox color.
        app.setStyleSheet(_BASE_POPOUT_OVERLAY_QSS)

    # Set app icon (works in taskbar on all platforms)
    from PySide6.QtGui import QIcon
    # PyInstaller sets _MEIPASS for bundled data
    base_dir = Path(getattr(sys, '_MEIPASS', Path(__file__).parent.parent.parent))
    icon_path = base_dir / "icon.png"
    if not icon_path.exists():
        icon_path = Path(__file__).parent.parent.parent / "icon.png"
    if not icon_path.exists():
        icon_path = data_dir() / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = BooruApp()
    window.show()
    sys.exit(app.exec())
