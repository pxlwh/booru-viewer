# booru-viewer Theme Reference

Copy any `.qss` file from this folder to your data directory as `custom.qss`:

- **Linux**: `~/.local/share/booru-viewer/custom.qss`
- **Windows**: `%APPDATA%\booru-viewer\custom.qss`

Restart the app after changing themes.

## Recoloring a theme — `@palette` blocks and `${...}` vars

Qt's QSS dialect has no native variables, so booru-viewer adds a small
preprocessor that runs before the stylesheet is handed to Qt. Each
bundled theme starts with an `@palette` header block listing the colors
the rest of the file uses, and the body references them as `${name}`:

```css
/* @palette
   bg:             #1e1e2e
   accent:         #cba6f7
   text:           #cdd6f4
*/

QWidget {
    background-color: ${bg};
    color: ${text};
    selection-background-color: ${accent};
}
```

To recolor a theme, **edit the `@palette` block at the top — that's the
only place hex literals appear**. The body picks up the new values
automatically. Save and restart the app.

The preprocessor is opt-in: a `custom.qss` without an `@palette` block
loads as plain Qt-standard QSS, so existing hand-written themes still
work unchanged. Unknown `${name}` references are left in place verbatim
and a warning is logged so typos are visible.

### Available palette slots

The bundled themes define 17 standard color slots. You can add more in
your own `@palette` block (or remove ones you don't reference) — only
slots that the body actually uses need to be defined.

| Slot | Used for |
|---|---|
| `bg` | Window background, scroll area, menu bar |
| `bg_alt` | Alternate row stripes in lists/trees, disabled inputs |
| `bg_subtle` | Buttons and inputs at rest, dropdown panels, tooltips |
| `bg_hover` | Surfaces under cursor hover, scrollbar handles |
| `bg_active` | Surfaces while pressed, scrollbar handles on hover |
| `text` | Primary foreground text |
| `text_dim` | Secondary text — status bar, group titles, placeholders |
| `text_disabled` | Disabled control text |
| `border` | Subtle dividers between adjacent surfaces |
| `border_strong` | More visible borders, default focus rings |
| `accent` | Selection background, focused borders, checked buttons |
| `accent_text` | Foreground used on top of accent backgrounds |
| `accent_dim` | Softer accent variant for hover-on-accent surfaces |
| `link` | Hyperlinks (info panel source URL) |
| `danger` | Destructive action color (Clear All button etc.) |
| `success` | Positive action color (also Character tag default) |
| `warning` | Warning color (also Artist tag default) |
| `overlay_bg` | Translucent background for the popout's floating top toolbar and bottom transport controls. Should be `rgba(...)` so video shows through. |

## Included Themes

Each theme ships in two corner-radius variants:

- **`*-rounded.qss`** — 4px radius on buttons, inputs, dropdowns, scrollbar handles, group boxes, tabs etc. The "default" Fusion-style look.
- **`*-square.qss`** — every `border-radius:` declaration stripped *except* the one on `QRadioButton::indicator`, so radio buttons stay circular while everything else (buttons, inputs, scrollbars, tabs, group boxes, tooltips, progress bars, checkbox indicators) renders square.

Pick whichever matches your overall desktop aesthetic. Both variants share the same `@palette` block, so you can swap one for the other and your colors carry over.

| Theme | Rounded | Square |
|-------|---------|--------|
| Nord | [nord-rounded.qss](nord-rounded.qss) | [nord-square.qss](nord-square.qss) |
| Catppuccin Mocha | [catppuccin-mocha-rounded.qss](catppuccin-mocha-rounded.qss) | [catppuccin-mocha-square.qss](catppuccin-mocha-square.qss) |
| Gruvbox | [gruvbox-rounded.qss](gruvbox-rounded.qss) | [gruvbox-square.qss](gruvbox-square.qss) |
| Solarized Dark | [solarized-dark-rounded.qss](solarized-dark-rounded.qss) | [solarized-dark-square.qss](solarized-dark-square.qss) |
| Tokyo Night | [tokyo-night-rounded.qss](tokyo-night-rounded.qss) | [tokyo-night-square.qss](tokyo-night-square.qss) |
| Everforest | [everforest-rounded.qss](everforest-rounded.qss) | [everforest-square.qss](everforest-square.qss) |

<picture><img src="../screenshots/themes/nord.png" alt="Nord" width="400"></picture> <picture><img src="../screenshots/themes/catppuccin-mocha.png" alt="Catppuccin Mocha" width="400"></picture>

<picture><img src="../screenshots/themes/gruvbox.png" alt="Gruvbox" width="400"></picture> <picture><img src="../screenshots/themes/solarized-dark.png" alt="Solarized Dark" width="400"></picture>

<picture><img src="../screenshots/themes/tokyo-night.png" alt="Tokyo Night" width="400"></picture> <picture><img src="../screenshots/themes/everforest.png" alt="Everforest" width="400"></picture>

## Widget Targets

### Global

```css
QWidget {
    background-color: ${bg};
    color: ${text};
    font-size: 13px;
    font-family: monospace;
    selection-background-color: ${accent};  /* grid selection border + hover highlight */
    selection-color: ${accent_text};
}
```

### Buttons

```css
QPushButton {
    background-color: ${bg_subtle};
    color: ${text};
    border: 1px solid ${border};
    border-radius: 4px;
    padding: 5px 14px;
}
QPushButton:hover { background-color: ${bg_hover}; }
QPushButton:pressed { background-color: ${bg_active}; }
QPushButton:checked { background-color: ${accent}; }  /* Active tab (Browse/Bookmarks/Library), Autoplay, Loop toggles */
```

**Note:** Qt's QSS does not support the CSS `content` property, so you cannot replace button text (e.g. swap icon symbols) via stylesheet alone. The toolbar icon buttons use hardcoded Unicode symbols — to change which symbols appear, modify the Python source directly (see `preview_pane.py` and `popout/window.py`).

### Text Inputs

```css
QLineEdit, QTextEdit {
    background-color: ${bg};
    color: ${text};
    border: 1px solid ${border};
    border-radius: 4px;
    padding: 4px 8px;
}
QLineEdit:focus, QTextEdit:focus {
    border-color: ${accent};
}
```

### Dropdowns

```css
QComboBox {
    background-color: ${bg_subtle};
    color: ${text};
    border: 1px solid ${border};
    border-radius: 4px;
    padding: 3px 6px;
}
QComboBox::drop-down {
    border: none;
    width: 20px;
}
QComboBox QAbstractItemView {
    background-color: ${bg_subtle};
    color: ${text};
    border: 1px solid ${border};
    selection-background-color: ${bg_hover};
}
```

### Spin Box (Score Filter)

```css
QSpinBox {
    background-color: ${bg_subtle};
    color: ${text};
    border: 1px solid ${border};
    border-radius: 2px;
}
```

### Scrollbars

```css
QScrollBar:vertical {
    background: ${bg};
    width: 10px;
    border: none;
}
QScrollBar::handle:vertical {
    background: ${bg_hover};
    border-radius: 4px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover { background: ${bg_active}; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QScrollBar:horizontal {
    background: ${bg};
    height: 10px;
}
QScrollBar::handle:horizontal {
    background: ${bg_hover};
    border-radius: 4px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
```

### Menu Bar & Context Menus

```css
QMenuBar {
    background-color: ${bg};
    color: ${text};
}
QMenuBar::item:selected { background-color: ${bg_subtle}; }

QMenu {
    background-color: ${bg};
    color: ${text};
    border: 1px solid ${border};
}
QMenu::item:selected { background-color: ${bg_subtle}; }
```

### Status Bar

```css
QStatusBar {
    background-color: ${bg};
    color: ${text_dim};
}
```

### Splitter Handle

```css
QSplitter::handle {
    background: ${border};
    width: 2px;
}
```

### Tab Bar (Settings Dialog)

```css
QTabBar::tab {
    background: ${bg_subtle};
    color: ${text};
    border: 1px solid ${border};
    padding: 6px 16px;
}
QTabBar::tab:selected {
    background: ${bg_hover};
    color: ${accent};
}
```

### Video Player Controls

The preview panel's video controls bar uses a translucent overlay style by default (`rgba(0,0,0,160)` background, white text). This is styled internally and **overrides QSS** for the controls bar. The seek/volume sliders and buttons inside the controls bar use a built-in dark overlay theme.

To override the preview controls bar background in QSS:

```css
QWidget#_preview_controls {
    background: ${overlay_bg};
}
```

Standard slider styling still applies outside the controls bar:

```css
QSlider::groove:horizontal {
    background: ${bg_subtle};
    height: 4px;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: ${accent};
    width: 12px;
    margin: -4px 0;
    border-radius: 6px;
}
```

### Popout Overlay

The popout (fullscreen preview) toolbar and video controls float over the media with a translucent background and auto-hide after 2 seconds of no mouse activity. Mouse movement or Ctrl+H toggles them.

These overlays use internal styling that overrides QSS. To customize:

```css
/* Popout top toolbar */
QWidget#_slideshow_toolbar {
    background: ${overlay_bg};
}

/* Popout bottom video controls */
QWidget#_slideshow_controls {
    background: ${overlay_bg};
}
```

Buttons and labels inside both overlays inherit a white-on-transparent style. To override:

```css
QWidget#_slideshow_toolbar QPushButton {
    border: 1px solid ${border};
    color: ${text_dim};
}
QWidget#_slideshow_controls QPushButton {
    border: 1px solid ${border};
    color: ${text_dim};
}
```

### Preview & Popout Toolbar Icon Buttons

The preview and popout toolbars use 24x24 icon buttons with Unicode symbols. Each button has an object name for QSS targeting:

| Object Name | Symbol | Action |
|-------------|--------|--------|
| `#_tb_bookmark` | ☆ / ★ | Bookmark / Unbookmark |
| `#_tb_save` | ⤓ / ✕ | Save / Unsave |
| `#_tb_bl_tag` | ⊘ | Blacklist a tag |
| `#_tb_bl_post` | ⊗ | Blacklist this post |
| `#_tb_popout` | ⧉ | Open popout (preview only) |

```css
/* Style all toolbar icon buttons */
QPushButton#_tb_bookmark,
QPushButton#_tb_save,
QPushButton#_tb_bl_tag,
QPushButton#_tb_bl_post,
QPushButton#_tb_popout {
    background: transparent;
    border: 1px solid ${border};
    color: ${text};
    padding: 0px;
}

```

The same object names are used in both the preview pane and the popout overlay, so one rule targets both. The symbols themselves are hardcoded in Python — QSS can style the buttons but cannot change which symbol is displayed.

### Progress Bar (Download)

```css
QProgressBar {
    background-color: ${bg_subtle};
    border: none;
}
QProgressBar::chunk {
    background-color: ${accent};
}
```

### Tooltips

```css
QToolTip {
    background-color: ${bg_subtle};
    color: ${text};
    border: 1px solid ${border};
    padding: 4px;
}
```

### Labels

```css
QLabel {
    background: transparent;  /* important: prevents opaque label backgrounds */
}
```

### Rubber Band Selection

Click and drag on empty grid space to select multiple thumbnails. The rubber band uses the system's default `QRubberBand` style, which can be customized:

```css
QRubberBand {
    background: ${accent};  /* use rgba(...) variant for translucency */
    border: 1px solid ${accent};
}
```

### Library Count Label States

The library tab's count label switches between three visual states depending on what `refresh()` finds. The state is exposed as a Qt dynamic property `libraryCountState` so themes target it via attribute selectors:

```css
QLabel[libraryCountState="empty"] {
    color: ${text_dim};        /* dim text — search miss or empty folder */
}
QLabel[libraryCountState="error"] {
    color: ${danger};          /* danger color — directory unreachable */
    font-weight: bold;
}
```

The `normal` state (`N files`) inherits the panel's default text color — no rule needed.

### Thumbnail Indicators and Selection Colors

```css
ThumbnailWidget {
    qproperty-savedColor: #22cc22;        /* green dot: saved to library */
    qproperty-bookmarkedColor: #ffcc00;   /* yellow star: bookmarked */
    qproperty-selectionColor: #cba6f7;    /* selected cell border (3px) */
    qproperty-multiSelectColor: #b4befe;  /* multi-select fill + border */
    qproperty-hoverColor: #cba6f7;        /* hover border (1px) */
    qproperty-idleColor: #45475a;         /* idle 1px border */
}
```

All four selection colors default to your system palette (`Highlight` + a derived idle color from `Mid`) so a `custom.qss` without these qproperties still picks up the theme. Override any of them to retint individual cell states without touching the global palette.

### Info Panel Tag Categories

The tag list in the info panel groups tags by category and colors each
category. Defaults follow the booru convention (Danbooru, Gelbooru, etc.)
so the panel reads naturally to anyone coming from a booru site. Override
any of them via `qproperty-tag<Category>Color` on `InfoPanel`:

```css
InfoPanel {
    qproperty-tagArtistColor: #f2ac08;     /* default: orange */
    qproperty-tagCharacterColor: #00aa00;  /* default: green (booru convention) */
    qproperty-tagCopyrightColor: #cc00ff;  /* default: magenta */
    qproperty-tagSpeciesColor: #ee4444;    /* default: red */
    qproperty-tagMetaColor: #888888;       /* default: gray */
    qproperty-tagLoreColor: #888888;       /* default: gray */
}
```

The General category has no color override — its tags use the panel's
default text color so they fall in line with the rest of the theme.

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

- `selection-background-color` on `QWidget` controls the **grid thumbnail selection border** and **hover highlight** (lighter version auto-derived)
- Setting a custom QSS automatically switches to the Fusion Qt style for consistent rendering
- Tag category colors (Artist, Character, Copyright, Species, Meta, Lore) are QSS-controllable via `qproperty-tag<Category>Color` on `InfoPanel` — see the Info Panel Tag Categories section above
- Saved dot (green) and bookmark star (yellow) are QSS-controllable via `qproperty-savedColor` and `qproperty-bookmarkedColor` on `ThumbnailWidget`
- Use `QLabel { background: transparent; }` to prevent labels from getting opaque backgrounds
