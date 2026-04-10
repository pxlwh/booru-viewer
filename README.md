# booru-viewer

A booru client for people who keep what they save and rice what they run.

Qt6 desktop app for Linux and Windows. Browse, search, and archive Danbooru, e621, Gelbooru, and Moebooru. Fully themeable.

## Screenshot

**Linux — Styled via system Qt6 theme**

<picture><img src="screenshots/linux.png" alt="Linux — System Qt6 theme" width="700"></picture>

Supports custom styling via `custom.qss` — see [Theming](#theming).

## Features

booru-viewer has three tabs that map to three commitment levels: **Browse** for live search against booru APIs, **Bookmarks** for posts you've starred for later, **Library** for files you've actually saved to disk.

### Browsing
- Supports **Danbooru, e621, Gelbooru, and Moebooru**
- Auto-detect site API type — just paste the URL
- Tag search with autocomplete, history dropdown, and saved searches
- Rating and score filtering (server-side `score:>=N`)
- **Media type filter** — dropdown: All / Animated / Video / GIF / Audio
- Blacklisted tags and posts (client-side filtering with backfill)
- Thumbnail grid with keyboard navigation, multi-select (Ctrl/Shift+Click, Ctrl+A), bulk context menus, and drag thumbnails out as files
- **Infinite scroll** — optional, auto-loads more posts at bottom
- **Start from page** — jump to any page number on search
- **Page cache** — prev/next loads from memory, no duplicates
- **Copy File to Clipboard** — Ctrl+C, works for images and videos

### Preview
- Image viewer with zoom (scroll wheel), pan (drag), and reset (middle click)
- GIF animation, Pixiv ugoira auto-conversion (zip to animated GIF)
- Animated PNG/WebP auto-conversion to GIF
- Video playback via mpv (MP4, WebM, MKV) with play/pause, seek, volume, mute, and seamless looping. Uncached videos stream directly from the CDN with single-connection cache population via mpv's stream-record.
- Info panel with post details, date, clickable tags color-coded by category (Artist, Character, Copyright, General, Meta, Species), and filetype
- **Preview toolbar** — Bookmark, Save, BL Tag, BL Post, and Popout buttons above the preview panel

### Popout Viewer
- Right-click preview → "Popout" or click the Popout button in the preview toolbar
- Arrow keys / `h`/`j`/`k`/`l` navigate posts (including during video playback)
- `,` / `.` seek 3 seconds in videos, `Space` toggles play/pause
- Floating overlay UI — toolbar and video controls auto-hide after 2 seconds, reappear on mouse move
- `F11` toggles fullscreen/windowed, `Ctrl+H` hides all UI, `Ctrl+P` privacy screen
- Window auto-sizes to content aspect ratio; state persisted across sessions
- Hyprland: `keep_aspect_ratio` prop locks window to content proportions
- Bidirectional sync — clicking posts in the main grid updates the popout
- Video position and player state synced between preview and popout

### Bookmarks
- **Bookmark** posts you might want later — lightweight pointers in the database, like clicking the star in your browser
- Group bookmarks into folders, separate from Library's folders
- Search bookmarks by tag
- Bulk save, unbookmark, or remove from the multi-select context menu
- Import/export bookmarks as JSON
- Unbookmark from grid, preview, or popout

### Library
- **Save** posts you want to keep — real files on disk in `saved/`, browsable in any file manager
- **Filename templates** — customize saved filenames with `%id%`, `%artist%`, `%character%`, `%copyright%`, `%md5%`, `%rating%`, `%score%` tokens. Default is post ID. Set in Settings > Paths.
- One-click promotion from bookmark to library when you decide to commit
- **Tag search across saved metadata** — type to filter by indexed tags, no filename conventions required
- On-disk folder organization with configurable library directory and folder sidebar — save unsorted or to a named subfolder
- Sort by date, name, or size
- Video thumbnail generation (ffmpeg if available, placeholder fallback)
- Unsave from grid, preview, and popout (only shown when post is saved)
- Unreachable directory detection

### Search
- Inline history dropdown inside the search bar
- Saved searches with management dialog
- Click empty search bar to open history
- Session cache mode clears history on exit (keeps saved searches)

## Install

### Windows

Download `booru-viewer-setup.exe` from [Releases](/releases) and run the installer. It installs to AppData with Start Menu and optional desktop shortcuts. To update, just run the new installer over the old one — your data in `%APPDATA%\booru-viewer\` is preserved.

Windows 10 dark mode is automatically detected and applied.

### Linux

Requires Python 3.11+ and pip. Most distros ship Python but you may need to install pip and the Qt6 system libraries.

**Arch / CachyOS:**
```sh
sudo pacman -S python python-pip qt6-base mpv ffmpeg
```

**Ubuntu / Debian (24.04+):**
```sh
sudo apt install python3 python3-pip python3-venv mpv libmpv-dev ffmpeg
```

**Fedora:**
```sh
sudo dnf install python3 python3-pip qt6-qtbase mpv mpv-libs-devel ffmpeg
```

Then clone and install:
```sh
git clone https://git.pax.moe/pax/booru-viewer.git
cd booru-viewer
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Run it:
```sh
booru-viewer
```

Or without installing: `python3 -m booru_viewer.main_gui`

**Desktop entry:** To add booru-viewer to your app launcher, create `~/.local/share/applications/booru-viewer.desktop`:
```ini
[Desktop Entry]
Name=booru-viewer
Exec=/path/to/booru-viewer/.venv/bin/booru-viewer
Icon=/path/to/booru-viewer/icon.png
Type=Application
Categories=Graphics;
```

### Hyprland integration

I daily-drive booru-viewer on Hyprland and I've baked in my own opinions on
how the app should behave there. By default, a handful of `hyprctl` dispatches
run at runtime to:

- Restore the main window's last floating mode + dimensions on launch
- Restore the popout's position, center-pin it around its content during
  navigation, and suppress F11 / fullscreen-transition flicker
- "Prime" Hyprland's per-window floating cache at startup so a mid-session
  toggle to floating uses your saved dimensions
- Lock the popout's aspect ratio to its content so you can't accidentally
  stretch mpv playback by dragging the popout corner

If you're a ricer with your own `windowrule`s targeting `class:^(booru-viewer)$`
and you'd rather the app keep its hands off your setup, there are two
independent opt-out env vars:

- **`BOORU_VIEWER_NO_HYPR_RULES=1`** — disables every in-code hyprctl dispatch
  *except* the popout's `keep_aspect_ratio` lock. Use this if you want app-side
  window management out of the way but you still want the popout to size itself
  to its content.
- **`BOORU_VIEWER_NO_POPOUT_ASPECT_LOCK=1`** — independently disables the popout's
  aspect ratio enforcement. Useful if you want to drag the popout to whatever
  shape you like (square, panoramic, monitor-aspect, whatever) and accept that
  mpv playback will letterbox or stretch to match.

For the full hands-off experience, set both:

```ini
[Desktop Entry]
Name=booru-viewer
Exec=env BOORU_VIEWER_NO_HYPR_RULES=1 BOORU_VIEWER_NO_POPOUT_ASPECT_LOCK=1 /path/to/booru-viewer/.venv/bin/booru-viewer
Icon=/path/to/booru-viewer/icon.png
Type=Application
Categories=Graphics;
```

Or for one-off launches from a shell:

```bash
BOORU_VIEWER_NO_HYPR_RULES=1 booru-viewer
```

### Dependencies

- Python 3.11+
- PySide6 (Qt6)
- httpx
- Pillow
- python-mpv
- mpv (system package on Linux, bundled DLL on Windows)

## Keybinds

### Grid

| Key | Action |
|-----|--------|
| Arrow keys / `h`/`j`/`k`/`l` | Navigate grid |
| `Ctrl+A` | Select all |
| `Ctrl+Click` / `Shift+Click` | Multi-select |
| `Home` / `End` | Jump to first / last |
| Scroll tilt left / right | Previous / next thumbnail (one cell) |
| `Ctrl+C` | Copy file to clipboard |
| Right click | Context menu |

### Preview

| Key | Action |
|-----|--------|
| Scroll wheel | Zoom (image) / volume (video) |
| Scroll tilt left / right | Previous / next post |
| Middle click / `0` | Reset view |
| Arrow keys / `h`/`j`/`k`/`l` | Navigate posts |
| `,` / `.` | Seek 3s back / forward (video) |
| `Space` | Play / pause (video, hover to activate) |
| Right click | Context menu (bookmark, save, popout) |

### Popout

| Key | Action |
|-----|--------|
| Arrow keys / `h`/`j`/`k`/`l` | Navigate posts |
| Scroll tilt left / right | Previous / next post |
| `,` / `.` | Seek 3s (video) |
| `Space` | Play / pause (video) |
| Scroll wheel | Volume up / down (video) |
| `F11` | Toggle fullscreen / windowed |
| `Ctrl+H` | Hide / show UI |
| `Ctrl+P` | Privacy screen |
| `Escape` / `Q` | Close popout |

### Global

| Key | Action |
|-----|--------|
| `Ctrl+P` | Privacy screen |
| `F11` | Toggle fullscreen |

## Adding Sites

File > Manage Sites. Enter a URL, click Auto-Detect, and save.

API credentials are optional — needed for Gelbooru and rate-limited sites.

### Tested Sites

- danbooru.donmai.us
- gelbooru.com
- rule34.xxx
- safebooru.donmai.us
- safebooru.org
- e621.net

## Theming

The app uses your OS native theme by default. To customize, copy a `.qss` file from the [`themes/`](themes/) folder to your data directory as `custom.qss`:

- **Linux**: `~/.local/share/booru-viewer/custom.qss`
- **Windows**: `%APPDATA%\booru-viewer\custom.qss`

A template is also available in Settings > Theme > Create from Template.

### Included Themes

Each theme ships in two variants: `*-rounded.qss` (4px corner radius) and `*-square.qss` (no corner radius except radio buttons). Same colors, different geometry.

<picture><img src="screenshots/themes/nord.png" alt="Nord" width="400"></picture> <picture><img src="screenshots/themes/catppuccin-mocha.png" alt="Catppuccin Mocha" width="400"></picture>

<picture><img src="screenshots/themes/gruvbox.png" alt="Gruvbox" width="400"></picture> <picture><img src="screenshots/themes/solarized-dark.png" alt="Solarized Dark" width="400"></picture>

<picture><img src="screenshots/themes/tokyo-night.png" alt="Tokyo Night" width="400"></picture> <picture><img src="screenshots/themes/everforest.png" alt="Everforest" width="400"></picture>

## Settings

- **General** — page size, thumbnail size (100-200px), default site, default rating/score, prefetch mode (Off / Nearby / Aggressive), infinite scroll, popout monitor, file dialog platform
- **Cache** — max cache size, max thumbnail cache, auto-evict, clear cache on exit (session-only mode)
- **Blacklist** — tag blacklist with toggle, post URL blacklist
- **Paths** — data directory, cache, database, configurable library directory, library filename template
- **Theme** — custom.qss editor, template generator, CSS guide
- **Network** — connection log showing all hosts contacted this session

## Data Locations

| | Linux | Windows |
|--|-------|---------|
| Database | `~/.local/share/booru-viewer/booru.db` | `%APPDATA%\booru-viewer\booru.db` |
| Cache | `~/.local/share/booru-viewer/cache/` | `%APPDATA%\booru-viewer\cache\` |
| Library | `~/.local/share/booru-viewer/saved/` | `%APPDATA%\booru-viewer\saved\` |
| Theme | `~/.local/share/booru-viewer/custom.qss` | `%APPDATA%\booru-viewer\custom.qss` |

To back up everything: copy `saved/` for the files themselves and `booru.db` for bookmarks, folders, and tag metadata. The two are independent — restoring one without the other still works. The `saved/` folder is browsable on its own in any file manager, and the database can be re-populated from the booru sites for any post IDs you still have on disk.

## Privacy

booru-viewer makes **no connections** except to the booru sites you configure. There is no telemetry, analytics, update checking, or phoning home. All data stays local on your machine.

Every outgoing request is logged in Settings > Network so you can verify this yourself — you will only see requests to the booru API endpoints and CDNs you chose to connect to.

## Support

If you find this useful, consider buying me a coffee:

[![Ko-fi](https://img.shields.io/badge/Support-Ko--fi-00ff00?style=for-the-badge&logo=ko-fi&logoColor=00ff00&labelColor=000000&color=006600)](https://ko-fi.com/paxmoe)

## License

MIT
