# Changelog

## 0.2.4 (pre-release)

Library filename templates, tag category fetching for all backends, and a popout video streaming overhaul. 50+ commits since v0.2.3.

## Changes since v0.2.3

### New: library filename templates

Save files with custom names instead of bare post IDs. Templates use `%id%`, `%artist%`, `%character%`, `%copyright%`, `%general%`, `%meta%`, `%species%`, `%md5%`, `%rating%`, `%score%`, `%ext%` tokens. Set in Settings > Library.

- New `core/library_save.py` module with a single `save_post_file` entry point. All eight save sites (Save to Library, Save As, Bulk Save, Batch Download, and their bookmarks-tab equivalents) route through it.
- DB-backed `library_meta.filename` column tracks the rendered name per post. Non-breaking migration for existing databases.
- Sequential collision suffixes (`_1`, `_2`, `_3`) when multiple posts render to the same filename (e.g. same artist).
- Same-post idempotency via `get_library_post_id_by_filename` lookup. Re-saving a post that already exists under a different template returns the existing path.
- `find_library_files` and `delete_from_library` updated to match templated filenames alongside legacy digit-stem files.
- `is_post_in_library` / `get_saved_post_ids` DB helpers replace filesystem walks for saved-dot indicators. Format-agnostic.
- `reconcile_library_meta` cleans up orphan meta rows on startup.
- Saved-dot indicators fixed across all tabs for templated filenames.
- Library tab single-delete and multi-delete now clean up `library_meta` rows (was leaking orphan rows for templated files).
- Save As dialog default filename comes from the rendered template instead of the old hardcoded `post_` prefix.
- Batch downloads into library folders now register `library_meta` (was silently skipping it).
- Bookmark-to-library copies now register `library_meta` (was invisible to Library tag search).
- Cross-folder re-save is now copy, not move (the atomic rename was a workaround for not having a DB-backed filename column).

### New: tag category fetching

Tag categories (Artist, Character, Copyright, General, Meta, Species) now work across all four backends, not just Danbooru and e621.

- New `CategoryFetcher` module with two strategies: batch tag API (Gelbooru proper with auth) and per-post HTML scrape (Rule34, Safebooru.org, Moebooru sites).
- DB-backed `tag_types` cache table. Tags are fetched once per site and cached across sessions. `clear_tag_cache` in Settings wipes it.
- Batch API probe result persisted per site. First session probes once; subsequent sessions skip the probe.
- Background prefetch for Gelbooru batch API path only. search() fires `prefetch_batch` in the background when `_batch_api_works` is True, so the cache is warm before the user clicks.
- Danbooru and e621 `get_post` now populates `tag_categories` inline (latent bug: was returning empty categories on re-fetch).
- `categories_updated` signal re-renders the info panel when categories arrive asynchronously.
- `_categories_pending` flag on the info panel suppresses the flat-tag fallback flash when a fetch is in progress. Tags area stays empty until categories arrive and render in one pass.
- HTML parser two-pass rewrite: Pass 1 finds tag-type elements by class, Pass 2 extracts tag names from `tags=NAME` URL parameters in search links. Works on Rule34, Safebooru.org, and Moebooru.
- `save_post_file` ensures categories before template render so `%artist%` / `%character%` tokens resolve on Gelbooru-style sites.
- On-demand fetch model for Rule34 / Safebooru.org / Moebooru: ~200ms HTML scrape on first click, instant from cache on re-click.

### Improved: popout video streaming

Click-to-first-frame latency on uncached video posts with the popout open is roughly halved. Single HTTP connection per video instead of two.

- **Stream-record.** mpv's `stream-record` per-file option tees the network stream to a `.part` temp file as it plays. On clean EOF the `.part` is promoted to the real cache path. The parallel httpx download that used to race with mpv for the same bytes is eliminated. Seeks during playback invalidate the recording (mpv may skip byte ranges); the `.part` is discarded on seek, stop, popout close, or rapid click.
- **Redundant stops removed.** `_on_video_stream` no longer stops the embedded preview's mpv when the popout is the visible target (was wasting ~50-100ms of synchronous `command('stop')` time). `_apply_load_video` no longer calls `stop()` before `play_file` (`loadfile("replace")` subsumes it).
- **Stack switch reordered.** `_apply_load_video` now switches to the video surface before calling `play_file`, so mpv's first frame lands on a visible widget instead of a cleared image viewer.
- **mpv network tuning.** `cache_pause=no` (stutter over pause for short clips), 50 MiB demuxer buffer cap, 20s read-ahead, 10s network timeout (down from ~60s).
- **Cache eviction safety.** `evict_oldest` skips `.part` files so eviction doesn't delete a temp file mpv is actively writing to.

### Bug fixes

- **Popout close preserves video position.** `closeEvent` now snapshots `position_ms` before dispatching `CloseRequested` (whose `StopMedia` effect destroys mpv's `time_pos`). The embedded preview resumes at the correct position instead of restarting from 0.
- **Library popout aspect lock for images.** Library items' Post objects were constructed without width/height, so the popout got 0/0 and `_fit_to_content` returned early without setting `keep_aspect_ratio`. Now reads actual pixel dimensions via `QPixmap` before constructing the Post.

### Other

- README updated, unused Windows screenshots dropped from the repo.

---

## 0.2.3

A refactor + cleanup release. The two largest source files (`gui/app.py` 3608 lines + `gui/preview.py` 2273 lines) are gone, replaced by a module-per-concern layout. The popout viewer's internal state was rebuilt as an explicit state machine with the historical race bugs locked out structurally instead of by suppression windows. The slider drag-back race that no one had named is finally fixed. A handful of latent bugs got caught and resolved on the way through.

## Changes since v0.2.2

### Structural refactor: gui/app.py + gui/preview.py split

The two largest source files were doing too much. `gui/app.py` was 3608 lines mixing async dispatch, signal wiring, tab switching, popout coordination, splitter persistence, context menus, bulk actions, batch download, fullscreen, privacy, and a dozen other concerns. `gui/preview.py` was 2273 lines holding the embedded preview, the popout, the image viewer, the video player, an OpenGL surface, and a click-to-seek slider. Both files had reached the point where almost every commit cited "the staging surface doesn't split cleanly" as the reason for bundling unrelated fixes.

This release pays that cost down with a structural carve into 12 module-per-concern files plus 2 oversize-by-design god-class files. 14 commits, every commit byte-identical except for relative-import depth corrections, app runnable at every commit boundary.

- **`gui/app.py` (3608 lines) gone.** Carved into:
  - `app_runtime.py`: `run()`, `_apply_windows_dark_mode()`, `_load_user_qss()` (`@palette` preprocessor), `_BASE_POPOUT_OVERLAY_QSS`. The QApplication setup, custom QSS load, icon resolution, BooruApp instantiation, and exec loop.
  - `main_window.py`: `BooruApp(QMainWindow)`, ~3200 lines. The class is one indivisible unit because every method shares instance attributes with every other method. Splitting it across files would have required either inheritance, composition, or method-as-attribute injection, and none of those were worth introducing for a refactor that was supposed to be a pure structural move with no logic changes.
  - `info_panel.py`: `InfoPanel(QWidget)` toggleable info panel.
  - `log_handler.py`: `LogHandler(logging.Handler, QObject)` Qt-aware logger adapter.
  - `async_signals.py`: `AsyncSignals(QObject)` signal hub for async worker results.
  - `search_state.py`: `SearchState` dataclass.
- **`gui/preview.py` (2273 lines) gone.** Carved into:
  - `preview_pane.py`: `ImagePreview(QWidget)` embedded preview pane.
  - `popout/window.py`: `FullscreenPreview(QMainWindow)` popout. Initially a single 1136-line file; further carved by the popout state machine refactor below.
  - `media/constants.py`: `VIDEO_EXTENSIONS`, `_is_video()`.
  - `media/image_viewer.py`: `ImageViewer(QWidget)` zoom/pan image viewer.
  - `media/mpv_gl.py`: `_MpvGLWidget` + `_MpvOpenGLSurface`.
  - `media/video_player.py`: `VideoPlayer(QWidget)` + `_ClickSeekSlider`.
  - `popout/viewport.py`: `Viewport(NamedTuple)` + `_DRIFT_TOLERANCE`.
- **Re-export shim pattern.** Each move added a `from .new_location import MovedClass  # re-export for refactor compat` line at the bottom of the old file so existing imports kept resolving the same class object during the migration. The final cleanup commit updated the importer call sites to canonical paths and deleted the now-empty `app.py` and `preview.py`.

### Bug fixes surfaced by the refactor

The refactor's "manually verify after every commit" rule exposed 10 latent bugs that had been lurking in the original god-files. Every one of these is a preexisting issue, not something the refactor caused.

- **Browse multi-select reshape.** Split library and bookmark actions into four distinct entries (Save All / Unsave All / Bookmark All / Remove All Bookmarks), each shown only when the selection actually contains posts the action would affect. The original combined action did both library and bookmark operations under a misleading bookmark-only label, with no way to bulk-unsave without also stripping bookmarks. The reshape resolves the actual need.
- **Infinite scroll page_size clamp.** One-character fix at `_on_reached_bottom`'s `search_append.emit` call site (`collected` becomes `collected[:limit]`) to mirror the non-infinite path's slice in `_do_search`. The backfill loop's `>=` break condition allowed the last full batch to push collected past the configured page size.
- **Batch download: incremental saved-dot updates and browse-tab-only gating.** Two-part fix. (1) Stash the chosen destination, light saved-dots incrementally as each file lands when the destination is inside `saved_dir()`. (2) Disable the Batch Download menu and Ctrl+D shortcut on the Bookmarks and Library tabs, where it didn't make sense.
- **F11 round-trip preserves zoom and position.** Two preservation bugs. (1) `ImageViewer.resizeEvent` no longer clobbers the user's explicit zoom and pan on F11 enter/exit; it uses `event.oldSize()` to detect whether the user was at fit-to-view at the previous size and only re-fits in that case. (2) The popout's F11 enter writes the current Hyprland window state directly into its viewport tracking so F11 exit lands at the actual pre-fullscreen position regardless of how the user got there (drag, drag+nav, drag+F11). The previous drift detection only fired during a fit and missed the "drag then F11 with no nav between" sequence.
- **Remove O keybind for Open in Default App.** Five-line block deleted from the main keypress handler. Right-click menu actions stay; only the keyboard shortcut is gone.
- **Privacy screen resumes video on un-hide.** `_toggle_privacy` now calls `resume()` on the active video player on the privacy-off branch, mirroring the existing `pause()` calls on the privacy-on branch. The popout's privacy overlay also moved from "hide the popout window" to "raise an in-place black overlay over the popout's central widget" because Wayland's hide → show round-trip drops window position when the compositor unmaps and remaps; an in-place overlay sidesteps the issue.
- **VideoPlayer mute state preservation.** When the popout opens, the embedded preview's mute state was synced into the popout's `VideoPlayer` before the popout's mpv instance was created (mpv is wired lazily on first `set_media`). The sync silently disappeared because the `is_muted` setter only forwarded to mpv if mpv existed. Now there's a `_pending_mute` field that the setter writes to unconditionally; `_ensure_mpv` replays it into the freshly-created mpv. Same pattern as the existing volume-from-slider replay.
- **Search count + end-of-results instrumentation.** `_do_search` and `_on_reached_bottom` now log per-filter drop counts (`bl_tags`, `bl_posts`, `dedup`), `api_returned`, `kept`, and the `at_end` decision at DEBUG level. Distinguishes "API ran out of posts" from "client-side filters trimmed the page" for the next reproduction. This is instrumentation, not a fix; the underlying intermittent end-of-results bug is still under investigation.

### Popout state machine refactor

In the past two weeks, five popout race fixes had landed (`baa910a`, `5a44593`, `7d19555`, `fda3b10`, `31d02d3`), each correct in isolation but fitting the same pattern: a perf round shifted timing, a latent race surfaced, a defensive layer was added. The pattern was emergent from the popout's signal-and-callback architecture, not from any one specific bug. Every defensive layer added a timestamp-based suppression window that the next race fix would have to navigate around.

This release rebuilds the popout's internal state as an explicit state machine. The 1136-line `FullscreenPreview` god-class became a thin Qt adapter on top of a pure-Python state machine, with the historical race fixes enforced structurally instead of by suppression windows. 16 commits.

The state machine has 6 states (`AwaitingContent`, `DisplayingImage`, `LoadingVideo`, `PlayingVideo`, `SeekingVideo`, `Closing`), 17 events, and 14 effects. The pure-Python core lives in `popout/state.py` and `popout/effects.py` and imports nothing from PySide6, mpv, or httpx. The Qt-side adapter in `popout/window.py` translates Qt events into state machine events and applies the returned effects to widgets; it never makes decisions about what to do.

The race fixes that were timestamp windows in the previous code are now structural transitions:

- **EOF race.** `VideoEofReached` is only legal in `PlayingVideo`. In every other state (most importantly `LoadingVideo`, where the stale-eof race lived), the event is dropped at the dispatch boundary without changing state or emitting effects. Replaces the 250ms `_eof_ignore_until` timestamp window that the previous code used to suppress stale eof events from a previous video's stop.
- **Double-load race.** `NavigateRequested` from a media-bearing state transitions to `AwaitingContent` once. A second `NavigateRequested` while still in `AwaitingContent` re-emits the navigate signal but does not re-stop or re-load. The state machine never produces two `LoadVideo` / `LoadImage` effects for the same navigation cycle, regardless of how many `NavigateRequested` events the eventFilter dispatches.
- **Persistent viewport.** The viewport (center + long_side) is a state machine field, only mutated by user-action events (`WindowMoved`, `WindowResized`, or `HyprlandDriftDetected`). Never overwritten by reading the previous fit's output. Replaces the per-nav drift accumulation that the previous "recompute viewport from current state" shortcut produced.
- **F11 round-trip.** Entering fullscreen snapshots the current viewport into a separate `pre_fullscreen_viewport` field. Exiting restores from the snapshot. The pre-fullscreen viewport is the captured value at the moment of entering, regardless of how the user got there.
- **Seek slider pin.** `SeekingVideo` state holds the user's click target. The slider rendering reads from the state machine: while in `SeekingVideo`, the displayed value is the click target; otherwise it's mpv's actual `time_pos`. `SeekCompleted` (from mpv's `playback-restart` event) transitions back to `PlayingVideo`. No timestamp window.
- **Pending mute.** The mute / volume / loop_mode values are state machine fields. `MuteToggleRequested` flips the field regardless of which state the machine is in. The `PlayingVideo` entry handler emits `[ApplyMute, ApplyVolume, ApplyLoopMode]` so the persistent values land in the freshly-loaded video on every load cycle.

The Qt adapter's interface to `main_window.py` was also cleaned up. Previously `main_window.py` reached into `_fullscreen_window._video.X`, `_fullscreen_window._stack.currentIndex()`, `_fullscreen_window._bookmark_btn.setVisible(...)`, and similar private-attribute access at ~25 sites. Those are gone. Nine new public methods on `FullscreenPreview` replace them: `is_video_active`, `set_toolbar_visibility`, `sync_video_state`, `get_video_state`, `seek_video_to`, `connect_media_ready_once`, `pause_media`, `force_mpv_pause`, `stop_media`. Existing methods (`set_media`, `update_state`, `set_post_tags`, `privacy_hide`, `privacy_show`) are preserved unchanged.

A new debug environment variable `BOORU_VIEWER_STRICT_STATE=1` raises an `InvalidTransition` exception on illegal (state, event) pairs in the state machine. Default release mode drops + logs at debug.

### Slider drag-back race fixed

The slider's `_seek` method used `mpv.seek(pos / 1000.0, 'absolute')` (keyframe-only seek). On videos with sparse keyframes (typical 1-5s GOP), mpv lands on the nearest keyframe at-or-before the click position, which is up to 5 seconds behind where the user actually clicked. The 500ms pin window from the earlier fix sweep papered over this for half a second, but afterwards the slider visibly dragged back to mpv's keyframe-rounded position and crawled forward.

- **`'absolute' → 'absolute+exact'`** in `VideoPlayer._seek`. Aligns the slider with `seek_to_ms` and `_seek_relative`, which were already using exact seek. mpv decodes from the previous keyframe forward to the EXACT target position before reporting it via `time_pos`. Costs 30-100ms more per seek but lands at the exact click position. No more drag-back. Affects both the embedded preview and the popout because they share the `VideoPlayer` class.
- **Legacy 500ms pin window removed.** Now redundant after the exact-seek fix. The supporting fields (`_seek_target_ms`, `_seek_pending_until`, `_seek_pin_window_secs`) are gone, `_seek` is one line, `_poll`'s slider write is unconditional after the `isSliderDown()` check.

### Grid layout fix

The grid was collapsing by a column when switching to a post in some scenarios. Two compounding issues.

- **The flow layout's wrap loop was vulnerable to per-cell width drift.** Walked each thumb summing `widget.width() + THUMB_SPACING` and wrapped on `x + item_w > self.width()`. If `THUMB_SIZE` was changed at runtime via Settings, existing thumbs kept their old `setFixedSize` value while new ones from infinite-scroll backfill got the new value. Mixed widths break a width-summing wrap loop.
- **The `columns` property had an off-by-one** at column boundaries because it omitted the leading margin from `w // (THUMB_SIZE + THUMB_SPACING)`. A row that fits N thumbs needs `THUMB_SPACING + N * step` pixels, not `N * step`. The visible symptom was that keyboard Up/Down navigation step was off-by-one in the boundary range.
- **Fix.** The flow layout now computes column count once via `(width - THUMB_SPACING) // step` and positions thumbs by `(col, row)` index, with no per-widget `widget.width()` reads. The `columns` property uses the EXACT same formula so keyboard nav matches the visual layout at every window width. Affects all three tabs (Browse / Bookmarks / Library) since they all use the same `ThumbnailGrid`.

### Other fixes

These two landed right after v0.2.2 was tagged but before the structural refactor started.

- **Popout video load performance.** mpv URL streaming for uncached videos via a new `video_stream` signal that hands the remote URL to mpv directly instead of waiting for the cache download to finish. mpv fast-load options `vd_lavc_fast` and `vd_lavc_skiploopfilter=nonkey`. GL pre-warm at popout open via a `showEvent` calling `ensure_gl_init` so the first video click doesn't pay for context creation. Identical-rect skip in `_fit_to_content` so back-to-back same-aspect navigation doesn't redundantly dispatch hyprctl. Plus three race-defense layers: pause-on-activate at the top of `_on_post_activated`, the 250ms stale-eof suppression window in VideoPlayer that the state machine refactor later subsumed, and removed redundant `_update_fullscreen` calls from `_navigate_fullscreen` and `_on_video_end_next` that were re-loading the previous post's path with a stale value.
- **Double-activation race fix in `_navigate_preview`.** Removed a redundant `_on_post_activated` call from all five view types (browse, bookmarks normal, bookmarks wrap-edge, library normal, library wrap-edge). `_select(idx)` already chains through `post_selected` which already calls `_on_post_activated`, so calling it explicitly again was a duplicate that fired the activation handler twice per keyboard nav.

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

### Changed: scroll tilt navigation
- Scroll tilt left/right now navigates between posts everywhere — grid, embedded preview, and popout — mirroring the L/R keys
- Grid: moves selection one cell, falls through to `nav_before_start` / `nav_past_end` at the edges
- Preview/popout: emits the existing `navigate` signal (±1)
- Vertical scroll still adjusts video volume on the video stack; tilt and vertical can no longer interfere
- Fixed: tilting over the image preview no longer zooms the image out (latent bug — `angleDelta().y() == 0` on pure tilt fell into the zoom-out branch)
- `page_forward` / `page_back` grid signals removed (only consumer was the old tilt handler)

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
