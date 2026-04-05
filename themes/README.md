# booru-viewer Theme Reference

Copy any `.qss` file from this folder to your data directory as `custom.qss`:

- **Linux**: `~/.local/share/booru-viewer/custom.qss`
- **Windows**: `%APPDATA%\booru-viewer\custom.qss`

Restart the app after changing themes.

## Included Themes

| Theme | File | Preview |
|-------|------|---------|
| Nord | [nord.qss](nord.qss) | <picture><img src="../screenshots/themes/nord.png" width="300"></picture> |
| Catppuccin Mocha | [catppuccin-mocha.qss](catppuccin-mocha.qss) | <picture><img src="../screenshots/themes/catppuccin-mocha.png" width="300"></picture> |
| Gruvbox | [gruvbox.qss](gruvbox.qss) | <picture><img src="../screenshots/themes/gruvbox.png" width="300"></picture> |
| Solarized Dark | [solarized-dark.qss](solarized-dark.qss) | <picture><img src="../screenshots/themes/solarized-dark.png" width="300"></picture> |
| Tokyo Night | [tokyo-night.qss](tokyo-night.qss) | <picture><img src="../screenshots/themes/tokyo-night.png" width="300"></picture> |
| Everforest | [everforest.qss](everforest.qss) | <picture><img src="../screenshots/themes/everforest.png" width="300"></picture> |

## Widget Targets

### Global

```css
QWidget {
    background-color: #282828;
    color: #ebdbb2;
    font-size: 13px;
    font-family: monospace;
    selection-background-color: #fe8019;  /* also sets grid selection highlight */
    selection-color: #282828;
}
```

### Buttons

```css
QPushButton {
    background-color: #333;
    color: #fff;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 5px 14px;
}
QPushButton:hover { background-color: #444; }
QPushButton:pressed { background-color: #555; }
QPushButton:checked { background-color: #0078d7; }  /* Browse/Favorites active tab */
```

### Text Inputs

```css
QLineEdit, QTextEdit {
    background-color: #1a1a1a;
    color: #fff;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 4px 8px;
}
QLineEdit:focus, QTextEdit:focus {
    border-color: #0078d7;
}
```

### Dropdowns

```css
QComboBox {
    background-color: #333;
    color: #fff;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 3px 6px;
}
QComboBox::drop-down {
    border: none;
    width: 20px;
}
QComboBox QAbstractItemView {
    background-color: #333;
    color: #fff;
    border: 1px solid #555;
    selection-background-color: #444;
}
```

### Spin Box (Score Filter)

```css
QSpinBox {
    background-color: #333;
    color: #fff;
    border: 1px solid #555;
    border-radius: 2px;
}
```

### Scrollbars

```css
QScrollBar:vertical {
    background: #1a1a1a;
    width: 10px;
    border: none;
}
QScrollBar::handle:vertical {
    background: #555;
    border-radius: 4px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover { background: #0078d7; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QScrollBar:horizontal {
    background: #1a1a1a;
    height: 10px;
}
QScrollBar::handle:horizontal {
    background: #555;
    border-radius: 4px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
```

### Menu Bar & Context Menus

```css
QMenuBar {
    background-color: #1a1a1a;
    color: #fff;
}
QMenuBar::item:selected { background-color: #333; }

QMenu {
    background-color: #1a1a1a;
    color: #fff;
    border: 1px solid #333;
}
QMenu::item:selected { background-color: #333; }
```

### Status Bar

```css
QStatusBar {
    background-color: #1a1a1a;
    color: #888;
}
```

### Splitter Handle

```css
QSplitter::handle {
    background: #555;
    width: 2px;
}
```

### Tab Bar (Settings Dialog)

```css
QTabBar::tab {
    background: #333;
    color: #fff;
    border: 1px solid #555;
    padding: 6px 16px;
}
QTabBar::tab:selected {
    background: #444;
    color: #0078d7;
}
```

### Video Player Controls

```css
QSlider::groove:horizontal {
    background: #333;
    height: 4px;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #0078d7;
    width: 12px;
    margin: -4px 0;
    border-radius: 6px;
}
```

### Progress Bar (Download)

```css
QProgressBar {
    background-color: #333;
    border: none;
}
QProgressBar::chunk {
    background-color: #0078d7;
}
```

### Tooltips

```css
QToolTip {
    background-color: #333;
    color: #fff;
    border: 1px solid #555;
    padding: 4px;
}
```

### Labels

```css
QLabel {
    background: transparent;  /* important: prevents opaque label backgrounds */
}
```

### Thumbnail Indicator Dots

```css
ThumbnailWidget {
    qproperty-savedColor: #22cc22;      /* green dot: saved to library */
    qproperty-favoritedColor: #ff4444;  /* red dot: favorited but not saved */
}
```

## States

| State | Description |
|-------|-------------|
| `:hover` | Mouse over |
| `:pressed` | Mouse down |
| `:focus` | Keyboard focus |
| `:checked` | Toggle buttons (Browse/Favorites) |
| `:selected` | Selected menu item or tab |
| `:disabled` | Grayed out |

## Notes

- `selection-background-color` on `QWidget` controls the **grid thumbnail selection highlight**
- Setting a custom QSS automatically switches to the Fusion Qt style for consistent rendering
- Tag category colors (Artist, Character, etc.) in the info panel are set in code, not via QSS
- Favorite/saved dots are QSS-controllable via `qproperty-savedColor` and `qproperty-favoritedColor` on `ThumbnailWidget`
- Use `QLabel { background: transparent; }` to prevent labels from getting opaque backgrounds
