# booru-viewer

Local desktop app for browsing, searching, and saving images from booru-style imageboards.

Qt6 GUI, cross-platform (Linux + Windows), fully themeable.

## Screenshots

**Windows 11 — Light Theme**

<picture><img src="screenshots/windows11-light.png" alt="Windows 11 — Light Theme" width="700"></picture>

**Windows 11 — Dark Theme (auto-detected)**

<picture><img src="screenshots/windows11-dark.png" alt="Windows 11 — Dark Theme" width="700"></picture>

**Windows 10 — Light Theme**

<picture><img src="screenshots/windows.png" alt="Windows 10 — Light Theme" width="700"></picture>

**Windows 10 — Dark Theme (auto-detected)**

<picture><img src="screenshots/windows-dark.png" alt="Windows 10 — Dark Theme" width="700"></picture>

**Linux — Styled via system Qt6 theme**

<picture><img src="screenshots/linux.png" alt="Linux — System Qt6 theme" width="700"></picture>

Supports custom styling via `custom.qss` — see [Theming](#theming).

## Features

### Browsing
- Supports **Danbooru, Gelbooru, Moebooru, and e621**
- Auto-detect site API type — just paste the URL
- Tag search with autocomplete, history dropdown, and saved searches
- Rating and score filtering (server-side `score:>=N`)
- Blacklisted tags (appended as negatives)
- Thumbnail grid with keyboard navigation

### Preview
- Image viewer with zoom (scroll wheel), pan (drag), and reset (middle click)
- GIF animation, Pixiv ugoira auto-conversion (zip to animated GIF)
- Video playback (MP4, WebM) with play/pause, seek, volume, mute, and seamless looping
- Info panel with post details, clickable tags, and filetype

### Slideshow Mode
- Right-click preview → "Slideshow Mode" for fullscreen viewing
- Arrow keys / `h`/`j`/`k`/`l` navigate posts (including during video playback)
- `,` / `.` seek 5 seconds in videos, `Space` toggles play/pause
- Toolbar with Favorite and Save/Unsave toggle buttons showing current state
- `F11` toggles fullscreen/windowed, `Ctrl+H` hides all UI
- Bidirectional sync — clicking posts in the main grid updates the slideshow
- Page boundary navigation — past the last/first post loads next/prev page

### Favorites & Library
- Favorite posts, organize into folders
- Save to library (unsorted or per-folder), drag-and-drop thumbnails as files
- Multi-select (Ctrl/Shift+Click, Ctrl+A) with bulk actions
- Bulk context menus in both Browse and Favorites tabs
- Unsave from Library available in grid, preview, and slideshow
- Import/export favorites as JSON

### Search
- Inline history dropdown inside the search bar
- Saved searches with management dialog
- Click empty search bar to open history
- Session cache mode clears history on exit (keeps saved searches)

## Install

### Linux

```sh
pip install -e .
booru-viewer
```

Or run directly: `python -m booru_viewer.main_gui`

### Windows

Download `booru-viewer.exe` from [Releases](https://git.pax.moe/pax/booru-viewer/releases).

For WebM video playback, install [VP9 Video Extensions](https://apps.microsoft.com/detail/9n4d0msmp0pt) from the Microsoft Store.

Windows 10 dark mode is automatically detected and applied.

### Dependencies

- Python 3.11+
- PySide6 (Qt6)
- httpx
- Pillow

## Keybinds

### Grid

| Key | Action |
|-----|--------|
| Arrow keys / `h`/`j`/`k`/`l` | Navigate grid |
| `Ctrl+A` | Select all |
| `Ctrl+Click` / `Shift+Click` | Multi-select |
| `Home` / `End` | Jump to first / last |
| Right click | Context menu |

### Preview

| Key | Action |
|-----|--------|
| Scroll wheel | Zoom |
| Middle click / `0` | Reset view |
| Left / Right | Previous / next post |
| `,` / `.` | Seek 5s back / forward (video) |
| `Space` | Play / pause (video) |
| Right click | Context menu (favorite, save, slideshow) |

### Slideshow

| Key | Action |
|-----|--------|
| Arrow keys / `h`/`j`/`k`/`l` | Navigate posts |
| `,` / `.` | Seek 5s (video) |
| `Space` | Play / pause (video) |
| `F11` | Toggle fullscreen / windowed |
| `Ctrl+H` | Hide / show UI |
| `Escape` / `Q` | Close slideshow |

### Global

| Key | Action |
|-----|--------|
| `Ctrl+P` | Privacy screen |
| `F11` | Toggle fullscreen |

## Adding Sites

File > Manage Sites. Enter a URL, click Auto-Detect, and save.

API credentials are optional — needed for Gelbooru and rate-limited sites.

## Theming

The app uses your OS native theme by default. To customize, copy a `.qss` file from the [`themes/`](themes/) folder to your data directory as `custom.qss`:

- **Linux**: `~/.local/share/booru-viewer/custom.qss`
- **Windows**: `%APPDATA%\booru-viewer\custom.qss`

A template is also available in Settings > Theme > Create from Template.

### Included Themes

<picture><img src="screenshots/themes/nord.png" alt="Nord" width="400"></picture> <picture><img src="screenshots/themes/catppuccin-mocha.png" alt="Catppuccin Mocha" width="400"></picture>

<picture><img src="screenshots/themes/gruvbox.png" alt="Gruvbox" width="400"></picture> <picture><img src="screenshots/themes/solarized-dark.png" alt="Solarized Dark" width="400"></picture>

<picture><img src="screenshots/themes/tokyo-night.png" alt="Tokyo Night" width="400"></picture> <picture><img src="screenshots/themes/everforest.png" alt="Everforest" width="400"></picture>

## Settings

- **General** — page size, thumbnail size, default rating/score, file dialog platform
- **Cache** — max cache size, auto-evict, clear cache on exit (session-only mode)
- **Blacklist** — tag blacklist with import/export
- **Paths** — data directory, cache, database locations
- **Theme** — custom.qss editor, template generator, CSS guide

## Data Locations

| | Linux | Windows |
|--|-------|---------|
| Database | `~/.local/share/booru-viewer/booru.db` | `%APPDATA%\booru-viewer\booru.db` |
| Cache | `~/.local/share/booru-viewer/cache/` | `%APPDATA%\booru-viewer\cache\` |
| Library | `~/.local/share/booru-viewer/saved/` | `%APPDATA%\booru-viewer\saved\` |
| Theme | `~/.local/share/booru-viewer/custom.qss` | `%APPDATA%\booru-viewer\custom.qss` |

## Privacy

booru-viewer makes **no connections** except to the booru sites you configure. There is no telemetry, analytics, update checking, or phoning home. All data stays local on your machine.

Every outgoing request is logged in the debug panel (View > Log) so you can verify this yourself — you will only see requests to the booru API endpoints and CDNs you chose to connect to.

## License

MIT
