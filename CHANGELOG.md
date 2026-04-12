# Changelog

## [Unreleased]

### Fixed
- Video stutter on network streams — `cache_pause_initial` was blocking first frame, reverted cache_pause changes and kept larger demuxer buffer
- Rubber band selection state getting stuck across interrupted drags
- LIKE wildcards in `search_library_meta` not being escaped
- Copy File to Clipboard broken in preview pane and popout; added Copy Image URL action
- Thumbnail cleanup and Post ID sort broken for templated filenames in library
- Save/unsave bookmark UX — no flash on toggle, correct dot indicators
- Autocomplete broken for multi-tag queries
- Search not resetting to page 1 on new query
- Fade animation cleanup crashing `FlowLayout.clear`
- Privacy toggle not preserving video pause state
- Bookmarks grid not refreshing on unsave
- `_cached_path` not set for streaming videos
- Standard icon column showing in QMessageBox dialogs
- Popout aspect lock for bookmarks now reads actual image dimensions instead of guessing

### Changed
- Uncached videos now download via httpx in parallel with mpv streaming — file is cached immediately for copy/paste without waiting for playback to finish
- Library video thumbnails use mpv instead of ffmpeg — drops the ffmpeg dependency entirely
- Save/Unsave from Library mutually exclusive in context menus, preview pane, and popout
- S key guard consistent with B/F behavior
- Tag count limits removed from info panel
- Ctrl+S and Ctrl+D menu shortcuts removed (conflict-prone)
- Thumbnail fade-in shortened from 200ms to 80ms
- Default demuxer buffer reduced to 50MiB; streaming URLs still get 150MiB
- Minimum width set on thumbnail grid
- Popout overlay hover zone enlarged
- Settings dialog gets an Apply button; thumbnail size and flip layout apply live
- Tab selection preserved on view switch
- Scroll delta accumulated for volume control and zoom (smoother with hi-res scroll wheels)
- Force Fusion widget style when no `custom.qss` is present

### Performance
- Thumbnails re-decoded from disk on size change instead of holding full pixmaps in memory
- Off-screen thumbnail pixmaps recycled (decoded on demand from cached path)
- Lookup sets cached across infinite scroll appends; invalidated on bookmark/save
- `auto_evict_cache` throttled to once per 30s
- Stale prefetch spirals cancelled on new click
- Single-pass directory walk in cache eviction functions
- GTK dialog platform detection cached instead of recreating Database per call

### Removed
- Dead code: `core/images.py`
- `TODO.md`
- Unused imports across `main_window`, `grid`, `settings`, `dialogs`, `sites`, `search_controller`

## v0.2.6

### Security: 2026-04-10 audit remediation

Closes 12 of the 16 findings from the read-only audit at `docs/SECURITY_AUDIT.md`. Two High, four Medium, four Low, and two Informational findings fixed; the four skipped Informational items are documented at the bottom. Each fix is its own commit on the `security/audit-2026-04-10` branch with an `Audit-Ref:` trailer.

- **#1 SSRF (High)**: every httpx client now installs an event hook that resolves the target host and rejects loopback, RFC1918, link-local (including the 169.254.169.254 cloud-metadata endpoint), CGNAT, unique-local v6, and multicast. Hook fires on every redirect hop, not just the initial request. **Behavior change:** user-configured boorus pointing at private/loopback addresses now fail with `blocked request target ...` instead of being probed. Test Connection on a local booru will be rejected.
- **#2 mpv (High)**: the embedded mpv instance is constructed with `ytdl=no`, `load_scripts=no`, and `demuxer_lavf_o=protocol_whitelist=file,http,https,tls,tcp`, plus `input_conf=/dev/null` on POSIX. Closes the yt-dlp delegation surface (CVE-prone extractors invoked on attacker-supplied URLs) and the `concat:`/`subfile:` local-file-read gadget via ffmpeg's lavf demuxer. **Behavior change:** any `file_url` whose host is only handled by yt-dlp (youtube.com, reddit.com, ...) no longer plays. Boorus do not legitimately serve such URLs, so in practice this only affects hostile responses.
- **#3 Credential logging (Medium)**: `login`, `api_key`, `user_id`, and `password_hash` are now stripped from URLs and params before any logging path emits them. Single redaction helper in `core/api/_safety.py`, called from the booru-base request hook and from each per-client `log.debug` line.
- **#4 DB + data dir permissions (Medium)**: on POSIX, `~/.local/share/booru-viewer/` is now `0o700` and `booru.db` (plus the `-wal`/`-shm` sidecars) is `0o600`. **Behavior change:** existing installs are tightened on next launch. Windows is unchanged — NTFS ACLs handle this separately.
- **#5 Lock leak (Medium)**: the per-URL coalesce lock table is capped at 4096 entries with LRU eviction. Eviction skips currently-held locks so a coroutine mid-`async with` can't be ripped out from under itself.
- **#6 HTML injection (Medium)**: `post.source` is escaped before insertion into the info-panel rich text. Non-http(s) sources (including `javascript:` and `data:`) render as plain escaped text without an `<a>` tag, so they can't become click targets.
- **#7 Windows reserved names (Low)**: `render_filename_template` now prefixes filenames whose stem matches a reserved Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`) with `_`, regardless of host platform. Cross-OS library copies stay safe.
- **#8 PIL bomb cap (Low)**: `Image.MAX_IMAGE_PIXELS=256M` moved from `core/cache.py` (where it was a side-effect of import order) to `core/__init__.py`, so any `booru_viewer.core.*` import installs the cap first.
- **#9 Dependency bounds (Low)**: upper bounds added to runtime deps in `pyproject.toml` (`httpx<1.0`, `Pillow<12.0`, `PySide6<7.0`, `python-mpv<2.0`). Lock-file generation deferred — see `TODO.md`.
- **#10 Early content validation (Low)**: `_do_download` now accumulates the first 16 bytes of the response and validates magic bytes before committing to writing the rest. A hostile server omitting Content-Type previously could burn up to `MAX_DOWNLOAD_BYTES` (500MB) of bandwidth before the post-download check rejected.
- **#14 Category fetcher body cap (Informational)**: HTML body the regex walks over in `CategoryFetcher.fetch_post` is truncated at 2MB. Defense in depth — the regex is linear-bounded but a multi-MB hostile body still pegs CPU.
- **#16 Logging hook gap (Informational)**: e621 and detect_site_type clients now install the `_log_request` hook so their requests appear in the connection log alongside the base client. Absorbed into the #1 wiring commits since both files were already being touched.

**Skipped (Wontfix), with reason:**
- **#11 64-bit hash truncation**: not exploitable in practice (audit's own words). Fix would change every cache path and require a migration.
- **#12 Referer leak through CDN redirects**: intentional — booru CDNs gate downloads on Referer matching. Documented; not fixed.
- **#13 hyprctl batch joining**: user is trusted in the threat model and Hyprland controls the field. Informational only.
- **#15 dead code in `core/images.py`**: code quality, not security. Out of scope under the no-refactor constraint. Logged in `TODO.md`.

## v0.2.5

Full UI overhaul (icon buttons, compact top bar, responsive video controls), popout resize-pivot anchor, layout flip, and the main_window.py controller decomposition.

### Refactor: main_window.py controller decomposition

`main_window.py` went from a 3,318-line god-class to a 1,164-line coordinator plus 7 controller modules. Every other subsystem in the codebase had already been decomposed (popout state machine, library save, category fetcher) — BooruApp was the last monolith. 11 commits, pure refactor, no behavior change. Design doc at `docs/MAIN_WINDOW_REFACTOR.md`.

- New `gui/window_state.py` (293 lines) — geometry persistence, Hyprland IPC, splitter savers.
- New `gui/privacy.py` (66 lines) — privacy overlay toggle + popout coordination.
- New `gui/search_controller.py` (572 lines) — search orchestration, infinite scroll, backfill, blacklist filtering, tag building, autocomplete, thumbnail fetching.
- New `gui/media_controller.py` (273 lines) — image/video loading, prefetch, download progress, video streaming fast-path, cache eviction.
- New `gui/popout_controller.py` (204 lines) — popout lifecycle (open/close), state sync, geometry persistence, navigation delegation.
- New `gui/post_actions.py` (561 lines) — bookmarks, save/library, batch download, unsave, bulk ops, blacklist actions from popout.
- New `gui/context_menus.py` (246 lines) — single-post and multi-select context menu building + dispatch.
- Controller-pattern: each takes `app: BooruApp` via constructor, accesses app internals as trusted collaborator via `self._app`. No mixins, no ABC, no dependency injection — just plain classes with one reference each. `TYPE_CHECKING` import for `BooruApp` avoids circular imports at runtime.
- Cleaned up 14 dead imports from `main_window.py`.
- The `_fullscreen_window` reference (52 sites across the codebase) was fully consolidated into `PopoutController.window`. No file outside `popout_controller.py` touches `_fullscreen_window` directly anymore.

### New: Phase 2 test suite (64 tests for extracted pure functions)

Each controller extraction also pulled decision-making code out into standalone module-level functions that take plain data in and return plain data out. Controllers call those functions; tests import them directly. Same structural forcing function as the popout state machine tests — the test files fail to collect if anyone adds a Qt import to a tested module.

- `tests/gui/test_search_controller.py` (24 tests): `build_search_tags` rating/score/media filter mapping per API type, `filter_posts` blacklist/dedup/seen-ids interaction, `should_backfill` termination conditions.
- `tests/gui/test_window_state.py` (16 tests): `parse_geometry` / `format_geometry` round-trip, `parse_splitter_sizes` validation edge cases, `build_hyprctl_restore_cmds` for every floating/tiled permutation including the no_anim priming path.
- `tests/gui/test_media_controller.py` (9 tests): `compute_prefetch_order` for Nearby (cardinals) and Aggressive (ring expansion) modes, including bounds, cap, and dedup invariants.
- `tests/gui/test_post_actions.py` (10 tests): `is_batch_message` progress-pattern detection, `is_in_library` path-containment check.
- `tests/gui/test_popout_controller.py` (3 tests): `build_video_sync_dict` shape.
- Total suite: **186 tests** (57 core + 65 popout state machine + 64 new controller pure functions), ~0.3s runtime, all import-pure.
- PySide6 imports in controller modules were made lazy (inside method bodies) so the Phase 2 tests can collect on CI, which only installs `httpx`, `Pillow`, and `pytest`.

### UI overhaul: icon buttons and responsive layout

Toolbar and video controls moved from fixed-width text buttons to 24x24 icon buttons. Preview toolbar uses Unicode symbols (☆/★ bookmark, ↓/✕ save, ⊘ blacklist tag, ⊗ blacklist post, ⧉ popout) — both the embedded preview and the popout toolbar share the same object names (`#_tb_bookmark`, `#_tb_save`, `#_tb_bl_tag`, `#_tb_bl_post`, `#_tb_popout`) so one QSS rule styles both. Video controls (play/pause, mute, loop, autoplay) render via QPainter using the palette's `buttonText` color so they match any theme automatically, with `1×` as bold text for the Once loop state.

- Responsive video controls bar: hides volume slider below 320px, duration label below 240px, current time label below 200px. Play/pause/seek/mute/loop always visible.
- Compact top bar: combos use `AdjustToContents`, 3px spacing, top/nav bars wrapped in `#_top_bar` / `#_nav_bar` named containers for theme targeting.
- Main window minimum size dropped from 900x600 to 740x400 — the hard floor was blocking Hyprland's keyboard resize mode on narrow floating windows.
- Preview pane minimum width dropped from 380 to 200.
- Info panel title + details use `QSizePolicy.Ignored` horizontally so long source URLs wrap within the splitter instead of pushing it wider.

### New: popout anchor setting (resize pivot)

Combo in Settings > General. Controls which point of the popout window stays fixed across navigations as the aspect ratio changes: `Center` (default, pins window center), or one of the four corners (pins that corner, window grows/shrinks from the opposite corner). The user can still drag the window anywhere — the anchor only controls the resize direction, not the screen position. Works on all platforms; on Hyprland the hyprctl dispatch path is used, elsewhere Qt's `setGeometry` fallback handles the same math.

- `Viewport.center_x`/`center_y` repurposed as anchor point coordinates — in center mode it's the window center, in corner modes it's the pinned corner. New `anchor_point()` helper in `viewport.py` extracts the right point from a window rect based on mode.
- `_compute_window_rect` branches on anchor: center mode keeps the existing symmetric math, corner modes derive position from the anchor point + the new size.
- Hyprland monitor reserved-area handling: reads `reserved` from `hyprctl monitors -j` so window positioning respects Waybar's exclusive zone (Qt's `screen.availableGeometry()` doesn't see layer-shell reservations on Wayland).

### New: layout flip setting

Checkbox in Settings > General (restart required). Swaps the main splitter — preview+info panel on the left, grid on the right. Useful for left-handed workflows or multi-monitor setups where you want the preview closer to your other reference windows.

### New: thumbnail fade-in animation

Thumbnails animate from 0 to 1 opacity over 200ms (OutCubic easing) as they load. Uses a `QPropertyAnimation` on a `thumbOpacity` Qt Property applied in `paintEvent`. The animation is stored on the widget instance to prevent Python garbage collection before the Qt event loop runs it.

### New: B / F / S keyboard shortcuts

- `B` or `F` — toggle bookmark on the selected post (works in main grid and popout).
- `S` — toggle save to library (Unfiled). If already saved, unsaves. Works in main grid and popout.
- The popout gained a new `toggle_save_requested` signal that routes to a shared `PostActionsController.toggle_save_from_preview` so both paths use the same toggle logic.

### UX: grid click behavior

- Clicking empty grid space (blue area around thumbnails, cell padding outside the pixmap, or the 2px gaps between cells) deselects everything. Cell padding clicks work via a direct parent-walk from `ThumbnailWidget.mousePressEvent` to the grid — Qt event propagation through `QScrollArea` swallows events too aggressively to rely on.
- Rubber band drag selection now works from any empty space — not just the 2px gaps. 30px manhattan threshold gates activation so single clicks on padding just deselect without flashing a zero-size rubber band.
- Hover highlight only appears when the cursor is actually over the pixmap, not the cell padding. Uses the same `_hit_pixmap` hit-test as clicks. Cursor swaps between pointing-hand (over pixmap) and arrow (over padding) via `mouseMoveEvent` tracking.
- Clicking an already-showing post no longer restarts the video (fixes the click-to-drag case where the drag-start click was restarting mpv).
- Escape clears the grid selection.
- Stuck forbidden cursor after cancelled drag-and-drop is reset on mouse release. Stuck hover states on Wayland fast-exits are force-cleared in `ThumbnailGrid.leaveEvent`.

### Themes

All 12 bundled QSS themes were trimmed and regenerated:

- Removed 12 dead selector groups that the app never instantiates: `QRadioButton`, `QToolButton`, `QToolBar`, `QDockWidget`, `QTreeView`/`QTreeWidget`, `QTableView`/`QTableWidget`, `QHeaderView`, `QDoubleSpinBox`, `QPlainTextEdit`, `QFrame`.
- Popout overlay buttons now use `font-size: 15px; font-weight: bold` so the icon symbols read well against the translucent-black overlay.
- `themes/README.md` documents the new `#_tb_*` toolbar button object names and the popout overlay styling. Removed the old Nerd Font remapping note — QSS can't change button text, so that claim was incorrect.

## v0.2.4

Library filename templates, tag category fetching for all backends, and a popout video streaming overhaul. 50+ commits since v0.2.3.

### New: library filename templates

Save files with custom names instead of bare post IDs. Templates use `%id%`, `%artist%`, `%character%`, `%copyright%`, `%general%`, `%meta%`, `%species%`, `%md5%`, `%rating%`, `%score%`, `%ext%` tokens. Set in Settings > Paths.

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
- Tightened thumbnail spacing in the grid from 8px to 2px.
- Max thumbnail size at 200px.

## v0.2.3

A refactor + cleanup release. The two largest source files (`gui/app.py` 3608 lines + `gui/preview.py` 2273 lines) are gone, replaced by a module-per-concern layout. The popout viewer's internal state was rebuilt as an explicit state machine with the historical race bugs locked out structurally instead of by suppression windows. The slider drag-back race that no one had named is finally fixed. A handful of latent bugs got caught and resolved on the way through.

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
- **Re-export shim pattern.** Each move added a `from .new_location import MovedClass # re-export for refactor compat` line at the bottom of the old file so existing imports kept resolving the same class object during the migration. The final cleanup commit updated the importer call sites to canonical paths and deleted the now-empty `app.py` and `preview.py`.

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

## v0.2.2

A hardening + decoupling release. Bookmark folders and library folders are no longer the same thing under the hood, the `core/` layers get a defensive hardening pass, the async/DB layers get a real concurrency refactor, and the README finally articulates what this project is.

### Bookmarks ↔ Library decoupling

- **Bookmark folders and library folders are now independent name spaces.** Used to share identity through `_db.get_folders()` — the same string was both a row in `favorite_folders` and a directory under `saved_dir`. The cross-bleed produced a duplicate-on-move bug and made "Save to Library" silently re-file the bookmark. Now they're two stores: bookmark folders are DB-backed labels for organizing your bookmark list, library folders are real subdirectories of `saved/` for organizing files on disk.
- **`library_folders()`** in `core.config` is the new source of truth for every Save-to-Library menu — reads filesystem subdirs of `saved_dir` directly.
- **`find_library_files(post_id)`** is the new "is this saved?" / delete primitive — walks the library shallowly by post id.
- **Move-aware Save to Library.** If the post is already in another library folder, atomic `Path.rename()` into the destination instead of re-copying from cache. Also fixes the duplicate-on-move bug.
- **Library tab right-click: Move to Folder submenu** for both single and multi-select, using `Path.rename` for atomic moves.
- **Bookmarks tab: − Folder button** next to + Folder for deleting the selected bookmark folder. DB-only, library filesystem untouched.
- **Browse tab right-click: "Bookmark as" submenu** when a post is not yet bookmarked (Unfiled / your bookmark folders / + New); flat "Remove Bookmark" when already bookmarked.
- **Embedded preview Bookmark button** got the same submenu shape via a new `bookmark_to_folder` signal + `set_bookmark_folders_callback`.
- **Popout Bookmark and Save buttons** both got the submenu treatment; works in both Browse and Bookmarks tab modes.
- **Popout in library mode** keeps the Save button visible as Unsave; the rest of the toolbar (Bookmark / BL Tag / BL Post) is hidden since they don't apply.
- **Popout state drift fixed.** `_update_fullscreen_state` now mirrors the embedded preview's `_is_bookmarked` / `_is_saved` instead of re-querying DB+filesystem, eliminating a state race during async bookmark adds.
- **"Unsorted" renamed to "Unfiled"** everywhere user-facing. Library Unfiled and bookmarks Unfiled now share one label.
- `favorite_folders` table preserved for backward compatibility — no migration required.

### Concurrency refactor

The earlier worker pattern of `threading.Thread + asyncio.run` was a real loop-affinity bug. The first throwaway loop a worker constructed would bind the shared httpx clients, and the next call from the persistent loop would fail with "Event loop is closed". This release routes everything through one loop and adds the locking and cleanup that should have been there from the start.

- **`core/concurrency.py`** is a new module: `set_app_loop()` / `get_app_loop()` / `run_on_app_loop()`. Every async piece of work in the GUI now schedules through one persistent loop, registered at startup by `BooruApp`.
- **`gui/sites.py` SiteDialog** Detect and Test buttons now route through `run_on_app_loop` instead of spawning a daemon thread. Results marshal back via Qt Signals with `QueuedConnection`. The dialog tracks in-flight futures and cancels them on close so a mid-detect dialog dismissal doesn't poke a destroyed QObject.
- **`gui/bookmarks.py` thumbnail loader** got the same swap. The existing `thumb_ready` signal already marshaled correctly.
- **Lazy-init lock on shared httpx clients.** `BooruClient._shared_client`, `E621Client._e621_client`, and `cache._shared_client` all use a fast-path / locked-slow-path lazy init. Concurrent first-callers can no longer both build a client and leak one.
- **`E621Client` UA-change leftover tracking.** When the User-Agent changes (api_user edit) and a new client is built, the old one is stashed in `_e621_to_close` and drained at shutdown instead of leaking.
- **`aclose_shared` on shutdown.** `BooruApp.closeEvent` now runs an `_close_all` coroutine via `run_coroutine_threadsafe(...).result(timeout=5)` before stopping the loop. Connection pools, keepalive sockets, and TLS state release cleanly instead of being abandoned.
- **`Database._write_lock` (RLock) + new `_write()` context manager.** Every write method now serializes through one lock so the asyncio thread and the Qt main thread can't interleave multi-statement writes. RLock so a writing method can call another writing method on the same thread without self-deadlocking. Reads stay lock-free under WAL.

### Defensive hardening

- **DB transactions.** `delete_site`, `add_search_history`, `remove_folder`, `rename_folder`, and `_migrate` now wrap their multi-statement bodies in `with self.conn:` so a crash mid-method can't leave orphan rows.
- **`add_bookmark` lastrowid fix.** When `INSERT OR IGNORE` collides on `(site_id, post_id)`, `lastrowid` is stale; the method now re-`SELECT`s the existing id. Was returning `Bookmark(id=0)` silently, which then no-op'd `update_bookmark_cache_path` on the next bookmark.
- **LIKE wildcard escape.** `get_bookmarks` LIKE clauses now `ESCAPE '\\'` so user search literals stop acting as SQL wildcards (`cat_ear` no longer matches `catear`).
- **Path traversal guard on folder names.** New `_validate_folder_name` rejects `..`, path separators, and leading `.`/`~` at write time. `saved_folder_dir()` resolves the candidate and refuses anything that doesn't `relative_to` the saved-images base.
- **Download size cap and streaming.** `download_image` enforces a 500 MB hard cap against the advertised Content-Length and the running total inside the chunk loop (servers can lie). Payloads ≥ 50 MB stream to a tempfile and atomic `os.replace` instead of buffering in RAM.
- **Per-URL coalesce lock.** `defaultdict[str, asyncio.Lock]` keyed by URL hash so concurrent callers downloading the same URL don't race `write_bytes`.
- **`Image.MAX_IMAGE_PIXELS = 256M`** with `DecompressionBombError` handling in both PIL converters.
- **Ugoira zip-bomb caps.** Frame count and cumulative uncompressed size checked from `ZipInfo` headers before any decompression.
- **`_convert_animated_to_gif` failure cache.** Writes a `.convfailed` sentinel sibling on failure to break the re-decode-every-paint loop for malformed animated PNGs/WebPs.
- **`_is_valid_media` distinguishes IO errors from "definitely invalid".** Returns `True` (don't delete) on `OSError` so a transient EBUSY/permissions hiccup no longer triggers a delete + re-download loop.
- **Hostname suffix matching for Referer.** Was using substring `in` matching, which meant `imgblahgelbooru.attacker.com` falsely mapped to `gelbooru.com`. Now uses proper suffix check.
- **`_request` retries on `httpx.NetworkError` and `httpx.ConnectError`** in addition to `TimeoutException`. A single DNS hiccup or RST no longer blows up the whole search.
- **`test_connection` no longer echoes the response body** in error strings. It was a body-leak gadget when used via `detect_site_type`'s redirect-following client.
- **Exception logging across `detect`, `search`, and `autocomplete`** in every API client. Previously every failure was a silent `return []`; now every swallowed exception logs at WARNING with type, message, and (where relevant) the response body prefix.
- **`main_gui.py`** `file_dialog_platform` DB probe failure now prints to stderr instead of vanishing.
- **Folder name validation surfaced as `QMessageBox.warning`** in `gui/bookmarks.py` and `gui/app.py` instead of crashing when a user types something the validator rejects.

### Popout overlay fix

- **`WA_StyledBackground` set on `_slideshow_toolbar` and `_slideshow_controls`.** Plain `QWidget` parents silently ignore QSS `background:` declarations without this attribute, which is why the popout overlay strip was rendering fully transparent (buttons styled, but the bar behind them showing the letterbox color).
- **Base popout overlay style baked into the QSS loader.** `_BASE_POPOUT_OVERLAY_QSS` is prepended before the user's `custom.qss` so themes that don't define overlay rules still get a usable translucent black bar with white text. Bundled themes still override on the same selectors.

### Popout aspect-ratio handling

The popout viewer's aspect handling had been patch-thrashing for ~20 commits since 0.2.0. A cold-context audit mapped 13 distinct failure modes still live in the code; this release closes the four highest-impact ones.

- **Width-anchor ratchet broken.** The previous `_fit_to_content` was width-anchored: `start_w = self.width()` read the current window width and derived height from aspect, with a back-derive if height exceeded the cap. Width was the only stable reference, and because portrait content has aspect < 1 and the height cap (90% of screen) was tighter than the width cap (100%), every portrait visit ran the back-derive and permanently shrunk the window. Going P→L→P→L→P on a 1080p screen produced a visibly smaller landscape on each loop.
- **New `Viewport(center_x, center_y, long_side)` model.** Three numbers, no aspect. Aspect is recomputed from content on every nav. The new `_compute_window_rect(viewport, content_aspect, screen)` is a pure static method: symmetric across portrait/landscape (`long_side` becomes width for landscape and height for portrait), proportional clamp shrinks both edges by the same factor when either would exceed its 0.90 ceiling, no asymmetric clamp constants, no back-derive step.
- **Viewport derived per-call from existing state.** No persistent field, no `moveEvent`/`resizeEvent` hooks needed for the basic ratchet fix. Three priority sources: pending one-shots (first fit after open or F11 exit) → current Hyprland window position+size → current Qt geometry. The Hyprland-current source captures whatever the user has dragged the popout to, so the next nav respects manual resizes.
- **First-fit aspect-lock race fixed.** `_fit_to_content` used to call `_is_hypr_floating` which returned `None` for both "not Hyprland" and "Hyprland but the window isn't visible to hyprctl yet". The latter happens on the very first popout open because the `wm:openWindow` event hasn't been processed when `set_media` fires. The method then fell through to a plain Qt resize and skipped the `keep_aspect_ratio` setprop, so the first image always opened unlocked and only subsequent navigations got the right shape. Now inlines the env-var check, distinguishes the two `None` cases, and retries on Hyprland with a 40ms backoff (capped at 5 attempts / 200ms total) when the window isn't registered yet.
- **Non-Hyprland top-left drift fixed.** The Qt fallback branch used to call `self.resize(w, h)`, which anchors top-left and lets bottom-right drift. The popout center walked toward the upper-left of the screen across navigations on Qt-driven WMs. Now uses `self.setGeometry(QRect(x, y, w, h))` with the computed top-left so the center stays put.

### Image fill in popout and embedded preview

- **`ImageViewer._fit_to_view` no longer caps zoom at native pixel size.** Used `min(scale_w, scale_h, 1.0)` so a smaller image in a larger window centered with letterbox space around it. The `1.0` cap is gone — images scale up to fill the available view, matching how the video player fills its widget. Combined with the popout's `keep_aspect_ratio`, the window matches the image's aspect AND the image fills it cleanly. Tiled popouts with mismatched aspect still letterbox (intentional — the layout owns the window shape).

### Main app flash and popout resize speed

- **Suppress dl_progress widget when the popout is open.** The download progress bar at the bottom of the right splitter was unconditionally `show()`'d on every grid click, including when the popout was open and the right splitter had been collapsed to give the grid full width. The show/hide pulse forced a layout pass on the right splitter that briefly compressed the main grid before the download finished and `hide()` fired. Visible flash on every click in the main app, even when clicking the same post that was already loaded (because `download_image` still runs against the cache). Three callsites now skip the widget entirely when the popout is visible. The status bar still updates with `Loading #X...` so the user has feedback in the main window.
- **Cache `_hyprctl_get_window` across one fit call.** `_fit_to_content` used to call `hyprctl clients -j` three times per popout navigation: once at the top for the floating check, once inside `_derive_viewport_for_fit` for the position/size read, and once inside `_hyprctl_resize_and_move` for the address lookup. Each call is a ~3ms `subprocess.run` that blocks the Qt event loop, totalling ~9ms of UI freeze per nav. The two helpers now accept an optional `win=None` parameter; `_fit_to_content` fetches the window dict once and threads it down. Per-fit subprocess count drops from 3 to 1 (~6ms saved per navigation), making rapid clicking and aspect-flip transitions feel snappier.
- **Show download progress on the active thumbnail when the embedded preview is hidden.** After the dl_progress suppression above landed, the user lost all visible download feedback in the main app whenever the popout was open. `_on_post_activated` now decides per call whether to use the dl_progress widget at the bottom of the right splitter or fall back to drawing the download progress on the active thumbnail in the main grid via the existing prefetch-progress paint path (`set_prefetch_progress(0.0..1.0)` to fill, `set_prefetch_progress(-1)` to clear). The decision is captured at function entry as `preview_hidden = not (self._preview.isVisible() and self._preview.width() > 0)` and closed over by the `_progress` callback and the `_load` coroutine, so the indicator that starts on a download stays on the same target even if the user opens or closes the popout mid-download. Generalizes to any reason the preview is hidden, not just popout-open: a user who has dragged the main splitter to collapse the preview gets the thumbnail indicator now too.

### Popout overlay stays hidden across navigation

- **Stop auto-showing the popout overlay on every `set_media`.** `FullscreenPreview.set_media` ended with an unconditional `self._show_overlay()` call, which meant the floating toolbar and video controls bar popped back into view on every left/right/hjkl navigation between posts. Visually noisy and not what the overlay is for — it's supposed to be a hover-triggered surface, not a per-post popup. Removed the call. The overlay is still shown by `__init__` default state (`_ui_visible = True`, so the user sees it for ~2 seconds on first popout open and the auto-hide timer hides it after that), by `eventFilter` mouse-move-into-top/bottom-edge-zone (the intended hover trigger, unchanged), by volume scroll on the video stack (unchanged), and by `Ctrl+H` toggle (unchanged). After this, the only way the overlay appears mid-session is hover or `Ctrl+H` — navigation through posts no longer flashes it back into view.

### Discord screen-share audio capture

- **`ao=pulse` in the mpv constructor.** mpv defaults to `ao=pipewire` (native PipeWire audio output) on Linux. Discord's screen-share-with-audio capture on Linux only enumerates clients connected via the libpulse API; native PipeWire clients are invisible to it. Visible symptom: video plays locally fine but audio is silently dropped from any Discord screen share. Firefox works because Firefox uses libpulse to talk to PipeWire's pulseaudio compat layer. Setting `ao="pulse,wasapi,"` in the MPV constructor (comma-separated priority list, mpv tries each in order) routes mpv through the same pulseaudio compat layer Firefox uses. `pulse` works on Linux; `wasapi` is the Windows fallback; trailing empty falls through to mpv's compiled-in default. No platform branch needed — mpv silently skips audio outputs that aren't available. Verified by inspection: with the fix, mpv's sink-input has `module-stream-restore.id = "sink-input-by-application-name:booru-viewer"` (the pulse-protocol form, identical to Firefox) instead of `"sink-input-by-application-id:booru-viewer"` (the native-pipewire form). References: [mpv #11100](https://github.com/mpv-player/mpv/issues/11100), [edisionnano/Screenshare-with-audio-on-Discord-with-Linux](https://github.com/edisionnano/Screenshare-with-audio-on-Discord-with-Linux).
- **`audio_client_name="booru-viewer"` in the mpv constructor.** mpv now registers in pulseaudio/pipewire introspection as `booru-viewer` instead of the default "mpv Media Player". Sets `application.name`, `application.id`, `application.icon_name`, `node.name`, and `device.description` to `booru-viewer` so capture tools group mpv's audio under the same identity as the Qt application.

### Docs

- **README repositioning.** New "Why booru-viewer" section between Screenshots and Features that names ahoviewer, Grabber, and Hydrus, lays out the labor axis (who does the filing) and the desktop axis (Hyprland/Wayland targeting), and explains the bookmark/library two-tier model with the browser-bookmark analogy.
- **New tagline** that does positioning instead of category description.
- **Bookmarks and Library Features sections split** to remove the previous intertwining; each now describes its own folder concept clearly.
- **Backup recipe** in Data Locations explaining the `saved/` + `booru.db` split and the recovery path.
- **Theming section** notes that each bundled theme ships in `*-rounded.qss` and `*-square.qss` variants.

### Fixes & polish

- **Drop the unused "Size: WxH" line from the InfoPanel** — bookmarks and library never had width/height plumbed and the field just showed 0×0.
- **Tighter combo and button padding across all 12 bundled themes.** `QPushButton` padding 2px 8px → 2px 6px, `QComboBox` padding 2px 6px → 2px 4px, `QComboBox::drop-down` width 18px → 14px. Saves 8px non-text width per combo and 4px per button.
- **Library sort combo: new "Post ID" entry** with a numeric stem sort that handles non-digit stems gracefully. Fits in 75px instead of needing 90px after the padding tightening.
- **Score and page spinboxes 50px → 40px** in the top toolbar to recover horizontal space. The internal range (0–99999) is unchanged; values >9999 will visually clip at the right edge but the stored value is preserved.

## v0.2.1

A theme + persistence + ricer-friendliness release. The whole stylesheet system was rebuilt around a runtime preprocessor with `@palette` / `${name}` vars, every bundled theme was rewritten end-to-end, and 12 theme variants ship instead of 6. Lots of UI state now survives a restart, and Hyprland ricers get an explicit opt-out for the in-code window management.

This release does not ship a fresh Windows installer — the previous v0.2.0 installer remains the latest installable binary. Run from source to get 0.2.1, or wait for the next release.

### Theming System

- **`@palette` / `${name}` preprocessor** — themes start with a `/* @palette */` header block listing color slots, the body uses `${name}` placeholders that the app substitutes at load time. Edit the 17-slot palette block at the top of any theme to recolor the entire app — no hunting through hex literals.
- **All 6 bundled themes rewritten** with comprehensive Fusion-style QSS covering every widget the app uses, every state (hover, focus, disabled, checked), every control variant
- **Two corner-radius variants per theme** — `*-rounded.qss` (4px radius, default Fusion-style look) and `*-square.qss` (every border-radius stripped except radio buttons, which stay circular)
- **Native Fusion sizing** — themed widgets shrunk to match Qt+Fusion defaults, toolbar row height is now ~23px instead of 30px, matching what `no-custom.qss` renders
- **Bundled themes** — catppuccin-mocha, nord, gruvbox, solarized-dark, tokyo-night, everforest. 12 files total (6 themes × 2 variants)

### QSS-Targetable Surfaces

Many things hardcoded in Python paint code can now be overridden from a `custom.qss` without touching the source:

- **InfoPanel tag category colors** — `qproperty-tagArtistColor`, `tagCharacterColor`, `tagCopyrightColor`, `tagSpeciesColor`, `tagMetaColor`, `tagLoreColor`
- **ThumbnailWidget selection paint** — `qproperty-selectionColor`, `multiSelectColor`, `hoverColor`, `idleColor` (in addition to existing `savedColor` and `bookmarkedColor`)
- **VideoPlayer letterbox color** — `qproperty-letterboxColor`. mpv paints the area around the video frame in this color instead of hardcoded black. Defaults to `QPalette.Window` so KDE color schemes, qt6ct, Windows dark/light mode, and any system Qt theme automatically produce a matching letterbox
- **Popout overlay bars** — translucent background for the floating top toolbar and bottom controls bar via the `overlay_bg` palette slot
- **Library count label states** — `QLabel[libraryCountState="..."]` attribute selector distinguishes "N files" / "no items match" / "directory unreachable" with QSS-controlled colors instead of inline red

### Hyprland Integration

- **Two opt-out env vars** for users with their own windowrules:
  - `BOORU_VIEWER_NO_HYPR_RULES=1` — disables every in-code hyprctl dispatch except the popout's keep_aspect_ratio lock
  - `BOORU_VIEWER_NO_POPOUT_ASPECT_LOCK=1` — independently disables the popout's aspect ratio enforcement
- **Popout overlays themed** — top toolbar and bottom controls bar now look themed instead of hardcoded translucent black, respect the `@palette` `overlay_bg` slot
- **Popout video letterbox tracks the theme's bg color** via the new `qproperty-letterboxColor`
- **Wayland app_id** set via `setDesktopFileName("booru-viewer")` so compositors can target windows by class — `windowrule = float, class:^(booru-viewer)$` — instead of by the volatile window title

### State Persistence

- **Main window** — geometry, floating mode, tiled mode (Hyprland)
- **Splitter sizes** — main splitter (grid vs preview), right splitter (preview vs dl_progress vs info panel)
- **Info panel visibility**
- **Cache spinbox** auto-derived dialog min height (no more clipping when dragging the settings dialog small)
- **Popout window** position, dimensions, and F11 fullscreen state restored via Hyprland floating cache prime

### UX

- **Live debounced search** in bookmarks and library tabs — type to filter, press Enter to commit immediately. 150ms debounce on bookmarks (cheap SQLite), 250ms on library (filesystem scan)
- **Search button removed** from bookmarks toolbar (live search + Enter)
- **Score field +/- buttons removed** from main search bar — type the value directly
- **Embedded preview video controls** moved out of the overlay style and into the panel layout, sitting under the media instead of floating on top of it. Popout still uses the floating overlay
- **Next-mode loop wraps** to the start of the bookmarks/library list at the end of the last item instead of stopping
- **Splitter handle margins** — 4px breathing margin on either side so toolbar buttons don't sit flush against the splitter line

### Performance

- **Page-load thumbnails** pre-fetch bookmarks + cache state into set lookups instead of N synchronous SQLite queries per page
- **Animated PNG/WebP conversion** off-loaded to a worker thread via `asyncio.to_thread` so it doesn't block the asyncio event loop during downloads

### Fixes

- **Open in Browser/Default App** on the bookmarks tab now opens the bookmark's actual source post (was opening unrelated cached files)
- **Cache settings spinboxes** can no longer be vertically clipped at the dialog's minimum size; spinboxes use Python-side `setMinimumHeight()` to propagate floors up the layout chain
- **Settings dialog** uses side-by-side `+`/`-` buttons instead of QSpinBox's default vertical arrows for clearer interaction
- **Bookmarks tab BL Tag** refreshes correctly when navigating bookmarked posts (was caching stale tags from the first selection)
- **Popout F11 → windowed** restores its previous windowed position and dimensions
- **Popout flicker on F11** transitions eliminated via `no_anim` setprop + deferred fit + dedupe of mpv `video-params` events
- **Bookmark + saved indicator dots** in the thumbnail grid: bookmark star on left, saved dot on right, both vertically aligned in a fixed-size box
- **Selection border** on thumbnail cells redrawn pen-aware: square geometry (no rounded corner artifacts), even line width on all sides, no off-by-one anti-aliasing seams
- **Toolbar buttons in narrow slots** no longer clip text (Bookmark/Unbookmark, Save/Unsave, BL Tag, BL Post, Popout, + Folder, Refresh) — all bumped to fit "Unbookmark" comfortably under the bundled themes' button padding
- **Toolbar rows** on bookmarks/library/preview panels now sit at a uniform 23px height matching the inputs/combos in the same row
- **Score and Page spinbox heights** forced to 23px via `setFixedHeight` to work around QSpinBox reserving vertical space for arrow buttons even when `setButtonSymbols(NoButtons)` is set
- **Library Open in Default App** uses the actual file path instead of routing through `cached_path_for` (which would return a hash path that doesn't exist for library files)

### Cleanup

- Deleted unused `booru_viewer/gui/theme.py` (222 lines of legacy stylesheet template that was never imported)
- Deleted `GREEN`/`DARK_GREEN`/`DIM_GREEN`/`BG`/`BG_LIGHT` etc constants from `booru_viewer/core/config.py` (only `theme.py` used them)
- Removed dead missing-indicator code (`set_missing`, `_missing_color`, `missingColor` Qt Property, the unreachable `if not filepath.exists()` branch in `library.refresh`)
- Removed dead score `+`/`-` buttons code path

## v0.2.0

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

## v0.1.9

### New Features

- **Animated filter** — checkbox to only show animated/video posts (server-side `animated` tag)
- **Start from page** — page number field in top bar, jump to any page on search
- **Post date** — creation date shown in the info line
- **Prefetch modes** — Off / Nearby (4 cardinals) / Aggressive (3 row radius)
- **Animated PNG/WebP** — auto-converted to GIF for Qt playback

### Improvements

- Thumbnail selection/hover box hugs the actual image content
- Video controls locked to bottom of preview panel
- Score filter uses +/- buttons instead of spinbox arrows
- Cache eviction triggers after infinite scroll page drain
- Combobox dropdown styling fixed on Windows dark mode
- Saved thumbnail size applied on startup

### Fixes

- Infinite scroll no longer stops early from false exhaustion
- Infinite scroll triggers when viewport isn't full (initial load, splitter resize, window resize)
- Shared HTTP clients reset on startup (prevents stale event loop errors)
- Non-JSON API responses handled gracefully instead of crashing

## v0.1.8

### Windows Installer

- **Inno Setup installer** — proper Windows installer with Start Menu shortcut, optional desktop icon, and uninstaller
- **`--onedir` build** — instant startup, no temp extraction (was `--onefile`)
- **`optimize=2`** — stripped docstrings/asserts for smaller, faster bytecode
- **No UPX** — trades disk space for faster launch (no decompression overhead)
- **`noarchive`** — loose .pyc files, no zip decompression at startup

### Performance

- **Shared HTTP client for API calls** — single TLS handshake for all Danbooru/Gelbooru/Moebooru requests
- **E621 shared client** — separate pooled client (custom User-Agent required)
- **Site detection reuses shared client** — no extra TLS for auto-detect
- **Priority downloads** — clicking a post pauses prefetch, downloads at full speed, resumes after
- **Referer header per-request** — fixes Gelbooru CDN returning HTML captcha pages

### Infinite Scroll

- **Auto-fill viewport** — if first page doesn't fill the screen, auto-loads more
- **Auto-load after drain** — checks if still at bottom after staggered append finishes
- **Content-aware trigger** — fires when scrollbar max is 0 (no scroll needed)

### Library

- **Tag categories stored** — saved as JSON in both library_meta and bookmarks DB
- **Categorized tags in info panel** — Library and Bookmarks show Artist/Character/Copyright etc.
- **Tag search in Library** — search box filters by stored tags
- **Browse thumbnail copied on save** — Library tab shows thumbnails instantly
- **Unsave from Library** in bookmarks right-click menu

### Bugfixes

- **Clear preview on new search**
- **Fixed diagonal grid navigation** — viewport width used for column count
- **Fixed Gelbooru CDN** — Referer header passed per-request with shared client
- **Crash guards** — pop(0) on empty queue, bounds checks in API clients
- **Page cache capped** — 10 pages max in pagination mode
- **Missing DB migrations** — tag_categories column added to existing tables
- **Tag click switches to Browse** — clears preview and searches clicked tag

## v0.1.7

### Infinite Scroll

- **New mode** — toggle in Settings > General, applies live
- Auto-loads more posts when scrolling to bottom
- **Staggered loading** — posts appear one at a time as thumbnails arrive
- **Stops at end** — gracefully handles API exhaustion
- Arrow keys at bottom don't break the grid
- Loading locked during drain to prevent multi-page burst
- Triggered one row from bottom for seamless experience

### Page Cache & Deduplication

- Page results cached in memory — prev/next loads instantly
- Backfilled posts don't repeat on subsequent pages
- Page label updates on cached loads

### Prefetch

- **Ring expansion** — prefetches in all 8 directions (including diagonals)
- **Auto-start on search** — begins from top of page immediately
- **Re-centers on click** — restarts spiral from clicked post
- **Triggers on infinite scroll** — new appended posts prefetch automatically

### Clipboard

- **Copy File to Clipboard** — works in grid, preview, bookmarks, and library
- **Ctrl+C shortcut** — global shortcut via QShortcut
- **QMimeData** — uses same mechanism as drag-and-drop for universal compatibility
- Sets both file URL (for file managers) and image data (for Discord/image apps)
- Videos copy as file URIs

### Slideshow

- **Blacklist Tag button** — opens categorized tag menu
- **Blacklist Post button** — blacklists current post

### Blacklist

- **In-place removal** — blacklisting removes matching posts from grid without re-searching
- Preserves infinite scroll state
- Only clears preview when the blacklisted post is the one being viewed

### UI Polish

- **QProxyStyle dark arrows** — spinbox/combobox arrows visible on all dark QSS themes
- **Diagonal nav fix** — column count reads viewport width correctly
- **Status bar** — shows result count with action confirmations
- **Live settings** — infinite scroll, library dir, thumbnail size apply without restart

### Stability

- All silent exceptions logged
- Missing defaults added for fresh installs
- Git history cleaned

## v0.1.6

### Infinite Scroll

- **New mode** — toggle in Settings > General: "Infinite scroll (replaces page buttons)"
- Hides prev/next buttons, auto-loads more posts when scrolling to bottom
- Posts appended to grid, deduped, blacklist filtered
- Stops gracefully when API runs out of results (shows "end")
- Arrow keys at bottom don't nuke the grid — page turn disabled in infinite scroll
- Applies live — no restart needed

### Page Cache & Deduplication

- **Page results cached** — prev/next loads instantly from memory within a search session
- **Post deduplication** — backfilled posts don't repeat on subsequent pages
- **Page label updates** on cached page loads

### Prefetch

- **Ring expansion** — prefetches in all 8 directions (up, down, left, right, diagonals)
- **Auto-start on search** — begins prefetching from top of page immediately
- **Re-centers on click** — clicking a post restarts the spiral from that position
- **Triggers on infinite scroll** — new appended posts start prefetching automatically

### Slideshow

- **Blacklist Tag button** — opens categorized tag menu in slideshow toolbar
- **Blacklist Post button** — blacklists current post from slideshow toolbar
- **Blacklisting clears slideshow** — both preview and slideshow cleared when previewed post is blacklisted

### Copy to Clipboard

- **Ctrl+C** — copies preview image to clipboard (falls back to cached file)
- **Right-click grid** — "Copy Image to Clipboard" option
- **Right-click preview** — "Copy Image to Clipboard" always available

### Live Settings

- **Most settings apply instantly** — infinite scroll, library directory, thumbnail size, rating, score
- Removed "restart required" labels

### Bugfixes

- **Blacklisting doesn't clear unrelated preview** — only clears when the previewed post matches
- **Backfill confirmed working** — debug logging added
- **Status bar keeps result count** — shows "N results — Loaded" instead of just "Loaded"
- **Fixed README code block formatting** and added ffmpeg back to Linux deps
