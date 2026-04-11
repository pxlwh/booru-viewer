# Changelog

## 0.2.5

Full UI overhaul (icon buttons, compact top bar, responsive video controls), popout resize-pivot anchor, layout flip, and the main_window.py controller decomposition.

## Changes since 0.2.4

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

## 0.2.4 (pre-release)

Library filename templates, tag category fetching for all backends, and a popout video streaming overhaul. 50+ commits since v0.2.3.

## Changes since v0.2.3

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
- Tag cache auto-prunes at 50k rows to prevent unbounded DB growth over months of browsing.
- Canonical category display order: Artist > Character > Copyright > Species > General > Meta > Lore (matches Danbooru/e621 inline order across all booru types).

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
- **Library tag search for templated filenames.** The tag search filter used `f.stem.isdigit()` to extract post_id — templated filenames were invisible to search. Now resolves post_id via `get_library_post_id_by_filename` with digit-stem fallback.
- **Library thumbnail lookup for templated filenames.** Thumbnails were saved by post_id but looked up by file stem. Templated files showed wrong or missing thumbnails. Now resolves post_id before thumbnail lookup.
- **Saved-dot indicator in primary search handler.** `_on_search_done` still used the old filesystem walk with `stem.isdigit()` — last surviving digit-stem callsite. Replaced with `get_saved_post_ids()` DB query.
- **Library delete meta cleanup.** Library tab single-delete and multi-delete called `.unlink()` directly, bypassing `delete_from_library`. Orphan `library_meta` rows leaked. Now resolves post_id and calls `remove_library_meta` after unlinking.
- **Partial cache compose.** `try_compose_from_cache` now populates `post.tag_categories` with whatever IS cached (for immediate partial display) but returns True only at 100% coverage. Prevents single cached tags from blocking the fetch path.

### UI

- Swapped Score and Media Type filter positions in the top toolbar. Dropdowns (Rating, Media Type) are now adjacent; Score sits between Media Type and Page.
- Tightened thumbnail spacing in the grid from 8px to 2px.
- Thumbnail size capped at 200px in Settings.

### Other

- README updated for v0.2.4, unused Windows screenshots dropped from the repo.
- New `docs/SAVE_AND_CATEGORIES.md` architecture reference with diagrams covering the unified save flow, CategoryFetcher dispatch, cache table, and per-booru resolution matrix.

---
