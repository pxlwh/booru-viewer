# booru-viewer

Local desktop application for browsing, searching, and favoriting images from booru-style imageboards.

Dual interface — **Qt6 GUI** and **Textual TUI**. Green-on-black theme.

## Features

- Tag-based search across multiple booru sites (Danbooru, Gelbooru, Moebooru)
- Auto-detect site API type — just paste the URL
- Thumbnail grid with full image preview (zoom/pan)
- Favorites system with local download cache and offline browsing
- Tag autocomplete
- Per-site API key support

## Install

```sh
pip install -e ".[all]"
```

Or install only what you need:

```sh
pip install -e ".[gui]"   # Qt6 GUI only
pip install -e ".[tui]"   # Textual TUI only
```

### Dependencies

- **GUI**: PySide6 (Qt6)
- **TUI**: Textual
- **Core**: httpx, Pillow, SQLite (stdlib)

## Usage

```sh
# Qt6 GUI
booru-gui

# Terminal TUI
booru-tui
```

### TUI Keybinds

| Key | Action |
|-----|--------|
| `/` | Focus search |
| `Enter` | Preview selected |
| `f` | Toggle favorite |
| `j`/`k` | Navigate down/up |
| `n`/`p` | Next/previous page |
| `1`/`2`/`3` | Browse / Favorites / Sites |
| `Escape` | Close preview |
| `q` | Quit |

### GUI Keybinds

| Key | Action |
|-----|--------|
| `F` | Toggle favorite on selected |
| `Ctrl+S` | Manage sites |
| `Ctrl+Q` | Quit |
| Scroll wheel | Zoom in preview |
| Right click | Close preview |
| `0` | Fit to view |
| `+`/`-` | Zoom in/out |

## Adding Sites

Open the site manager (GUI: `Ctrl+S`, or in the Sites tab). Enter a URL and click Auto-Detect — the app probes for Danbooru, Gelbooru, and Moebooru APIs automatically.

Or via Python:

```python
from booru_viewer.core.db import Database
db = Database()
db.add_site("Danbooru", "https://danbooru.donmai.us", "danbooru")
db.add_site("Gelbooru", "https://gelbooru.com", "gelbooru")
```

## Data

- Database: `~/.local/share/booru-viewer/booru.db`
- Image cache: `~/.local/share/booru-viewer/cache/`
- Thumbnails: `~/.local/share/booru-viewer/thumbnails/`

## License

MIT
