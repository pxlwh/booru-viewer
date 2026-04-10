# Changelog

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
