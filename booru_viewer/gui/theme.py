"""Green-on-black Qt6 stylesheet."""

from ..core.config import GREEN, DARK_GREEN, DIM_GREEN, BG, BG_LIGHT, BG_LIGHTER, BORDER

STYLESHEET = f"""
QMainWindow, QDialog {{
    background-color: {BG};
    color: {GREEN};
}}

QWidget {{
    background-color: {BG};
    color: {GREEN};
    font-family: "Terminess Nerd Font Propo", "Hack Nerd Font", monospace;
    font-size: 13px;
}}

QMenuBar {{
    background-color: {BG};
    color: {GREEN};
    border-bottom: 1px solid {BORDER};
}}

QMenuBar::item:selected {{
    background-color: {BG_LIGHTER};
}}

QMenu {{
    background-color: {BG_LIGHT};
    color: {GREEN};
    border: 1px solid {BORDER};
}}

QMenu::item:selected {{
    background-color: {BG_LIGHTER};
}}

QLineEdit {{
    background-color: {BG_LIGHT};
    color: {GREEN};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 10px;
    selection-background-color: {DIM_GREEN};
    selection-color: {BG};
}}

QLineEdit:focus {{
    border-color: {GREEN};
}}

QPushButton {{
    background-color: {BG_LIGHT};
    color: {GREEN};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 16px;
    min-height: 28px;
}}

QPushButton:hover {{
    background-color: {BG_LIGHTER};
    border-color: {DIM_GREEN};
}}

QPushButton:pressed {{
    background-color: {DIM_GREEN};
    color: {BG};
}}

QComboBox {{
    background-color: {BG_LIGHT};
    color: {GREEN};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
}}

QComboBox:hover {{
    border-color: {DIM_GREEN};
}}

QComboBox QAbstractItemView {{
    background-color: {BG_LIGHT};
    color: {GREEN};
    border: 1px solid {BORDER};
    selection-background-color: {DIM_GREEN};
    selection-color: {BG};
}}

QComboBox::drop-down {{
    border: none;
    width: 20px;
}}

QScrollBar:vertical {{
    background: {BG};
    width: 10px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: {BORDER};
    min-height: 30px;
    border-radius: 5px;
}}

QScrollBar::handle:vertical:hover {{
    background: {DIM_GREEN};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background: {BG};
    height: 10px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background: {BORDER};
    min-width: 30px;
    border-radius: 5px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {DIM_GREEN};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

QLabel {{
    color: {GREEN};
}}

QStatusBar {{
    background-color: {BG};
    color: {DIM_GREEN};
    border-top: 1px solid {BORDER};
}}

QTabWidget::pane {{
    border: 1px solid {BORDER};
    background-color: {BG};
}}

QTabBar::tab {{
    background-color: {BG_LIGHT};
    color: {DIM_GREEN};
    border: 1px solid {BORDER};
    border-bottom: none;
    padding: 6px 16px;
    margin-right: 2px;
}}

QTabBar::tab:selected {{
    color: {GREEN};
    border-color: {GREEN};
    background-color: {BG};
}}

QTabBar::tab:hover {{
    color: {GREEN};
    background-color: {BG_LIGHTER};
}}

QListWidget {{
    background-color: {BG};
    color: {GREEN};
    border: 1px solid {BORDER};
    outline: none;
}}

QListWidget::item:selected {{
    background-color: {DIM_GREEN};
    color: {BG};
}}

QListWidget::item:hover {{
    background-color: {BG_LIGHTER};
}}

QDialogButtonBox QPushButton {{
    min-width: 80px;
}}

QToolTip {{
    background-color: {BG_LIGHT};
    color: {GREEN};
    border: 1px solid {BORDER};
    padding: 4px;
}}

QCompleter QAbstractItemView {{
    background-color: {BG_LIGHT};
    color: {GREEN};
    border: 1px solid {BORDER};
    selection-background-color: {DIM_GREEN};
    selection-color: {BG};
}}

QSplitter::handle {{
    background-color: {BORDER};
}}

QProgressBar {{
    background-color: {BG_LIGHT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    color: {GREEN};
}}

QProgressBar::chunk {{
    background-color: {DIM_GREEN};
    border-radius: 3px;
}}
"""
