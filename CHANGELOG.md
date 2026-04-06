# Changelog

## 0.2.0

### New: mpv video backend
- Replaced Qt Multimedia (QMediaPlayer/QVideoWidget) with embedded mpv via `python-mpv`
- OpenGL render API (`MpvRenderContext`) for Wayland-native compositing — no XWayland needed
- Proper hardware-accelerated decoding (`hwdec=auto`)
- Reliable aspect ratio handling — portrait videos scale correctly
- Proper end-of-file detection via `eof-reached` property observer instead of fragile position-jump heuristic
- Frame-accurate seeking with `absolute+exact` and `relative+exact`
- `keep-open=yes` holds last frame on video end instead of flashing black
- Windows: bundle `mpv-2.dll` in PyInstaller build

### New: popout viewer (renamed from slideshow)
- Renamed "Slideshow" to "Popout" throughout UI
- Toolbar and video controls float over media with translucent background (`rgba(0,0,0,160)`)
- Auto-hide after 2 seconds of inactivity, reappear on mouse move
- Ctrl+H manual toggle
- Media fills entire window — no layout shift when UI appears/disappears
- Video controls only show for video posts, hidden for images/GIFs
- Smart F11 exit: window sizes to 60% of monitor, maintaining content aspect ratio
- Window auto-resizes to content aspect ratio on navigation (height adjusts, position stays)
- Window geometry and fullscreen state persisted to DB across sessions
- Hyprland-specific: uses `hyprctl resizewindowpixel` + `setprop keep_aspect_ratio` to lock window to content aspect ratio (works both floating and tiled)
- Default site setting in Settings > General

### New: preview toolbar
- Action bar above the preview panel: Bookmark, Save, BL Tag, BL Post, Popout
- Appears when a post is active, hidden when preview is cleared
- Save button opens folder picker menu (Unsorted / existing folders / + New Folder)
- Save/Unsave state shown on button text
- Bookmark/Unbookmark state shown on button text
- Per-tab button visibility: Library tab only shows Save + Popout
- All actions work from any tab (Browse, Bookmarks, Library)
- Blacklist tag and blacklist post show confirmation dialogs
- "Unsave from Library" only appears in context menu when post is saved

### New: media type filter
- Replaced "Animated" checkbox with dropdown: All / Animated / Video / GIF / Audio
- Each option appends the corresponding booru tag to the search query

### New: thumbnail cache limits
- Added "Max thumbnail cache" setting (default 500 MB)
- Auto-evicts oldest thumbnails when limit is reached

### Improved: state synchronization
- Saving/unsaving updates grid thumbnail dots instantly (browse, bookmarks, library)
- Unbookmarking refreshes the bookmarks tab immediately
- Saving from browse/bookmarks refreshes the library tab when async save completes
- Library items set `_current_post` on click so toolbar actions work correctly
- Preview toolbar tracks bookmark and save state across all tabs
- Tab switching clears grid selections to prevent cross-tab action conflicts
- Bookmark state updates after async bookmark completes (not before)

### Improved: infinite scroll
- Fixed missing posts when media type filters reduce results per page
- Local dedup set (`seen`) prevents cross-page duplicates within backfill without polluting `shown_post_ids`
- Page counter only advances when results are returned, not when filtering empties them
- Backfill loop increased to 10 max pages with 300ms delay between API calls (first call instant)

### Improved: pagination
- Status bar shows "(end)" when search returns fewer results than page size
- Prev/Next buttons hide when at page boundaries instead of just disabling
- Source URLs clickable in info panel, truncated at 60 chars for display

### Improved: video controls
- Seek step changed from 5s to ~3s for `,` and `.` keys
- `,` and `.` seek keys now work in the main preview panel, not just popout
- Translucent overlay style on video controls in both preview and popout
- Volume slider fixed at 60px to not compete with seek slider at small sizes

### New: API retry logic
- Single retry with backoff on HTTP 429 (rate limit) and 503 (service unavailable)
- Retries on request timeout
- Respects `Retry-After` header (capped at 5s)
- Applied to all API requests (search, get_post, autocomplete) across all four clients
- Downloads are not retried (large payloads, separate client)

### Refactor: SearchState dataclass
- Consolidated 8 scattered search state attributes into a single `SearchState` dataclass
- Eliminated all defensive `getattr`/`hasattr` patterns (8 instances)
- State resets cleanly on new search — no stale infinite scroll data

### Dependencies
- Added `python-mpv>=1.0`
- Removed dependency on `PySide6.QtMultimedia` and `PySide6.QtMultimediaWidgets`
