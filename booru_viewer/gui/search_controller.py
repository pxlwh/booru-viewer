"""Search orchestration, infinite scroll, tag building, and blacklist filtering."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from .search_state import SearchState

if TYPE_CHECKING:
    from .main_window import BooruApp

log = logging.getLogger("booru")


# -- Pure functions (tested in tests/gui/test_search_controller.py) --


def build_search_tags(
    tags: str,
    rating: str,
    api_type: str | None,
    min_score: int,
    media_filter: str,
) -> str:
    """Build the full search tag string from individual filter values."""
    parts = []
    if tags:
        parts.append(tags)

    if rating != "all" and api_type:
        if api_type == "danbooru":
            danbooru_map = {
                "general": "g", "sensitive": "s",
                "questionable": "q", "explicit": "e",
            }
            if rating in danbooru_map:
                parts.append(f"rating:{danbooru_map[rating]}")
        elif api_type == "gelbooru":
            gelbooru_map = {
                "general": "general", "sensitive": "sensitive",
                "questionable": "questionable", "explicit": "explicit",
            }
            if rating in gelbooru_map:
                parts.append(f"rating:{gelbooru_map[rating]}")
        elif api_type == "e621":
            e621_map = {
                "general": "s", "sensitive": "s",
                "questionable": "q", "explicit": "e",
            }
            if rating in e621_map:
                parts.append(f"rating:{e621_map[rating]}")
        else:
            moebooru_map = {
                "general": "safe", "sensitive": "safe",
                "questionable": "questionable", "explicit": "explicit",
            }
            if rating in moebooru_map:
                parts.append(f"rating:{moebooru_map[rating]}")

    if min_score > 0:
        parts.append(f"score:>={min_score}")

    if media_filter == "Animated":
        parts.append("animated")
    elif media_filter == "Video":
        parts.append("video")
    elif media_filter == "GIF":
        parts.append("animated_gif")
    elif media_filter == "Audio":
        parts.append("audio")

    return " ".join(parts)


def filter_posts(
    posts: list,
    bl_tags: set,
    bl_posts: set,
    seen_ids: set,
) -> tuple[list, dict]:
    """Filter posts by blacklisted tags/URLs and dedup against *seen_ids*.

    Mutates *seen_ids* in place (adds surviving post IDs).
    Returns ``(filtered_posts, drop_counts)`` where *drop_counts* has keys
    ``bl_tags``, ``bl_posts``, ``dedup``.
    """
    drops = {"bl_tags": 0, "bl_posts": 0, "dedup": 0}
    n0 = len(posts)
    if bl_tags:
        posts = [p for p in posts if not bl_tags.intersection(p.tag_list)]
    n1 = len(posts)
    drops["bl_tags"] = n0 - n1
    if bl_posts:
        posts = [p for p in posts if p.file_url not in bl_posts]
    n2 = len(posts)
    drops["bl_posts"] = n1 - n2
    posts = [p for p in posts if p.id not in seen_ids]
    n3 = len(posts)
    drops["dedup"] = n2 - n3
    seen_ids.update(p.id for p in posts)
    return posts, drops


def should_backfill(collected_count: int, limit: int, last_batch_size: int) -> bool:
    """Return True if another backfill page should be fetched."""
    return collected_count < limit and last_batch_size >= limit


# -- Controller --


class SearchController:
    """Owns search orchestration, pagination, infinite scroll, and blacklist."""

    def __init__(self, app: BooruApp) -> None:
        self._app = app
        self._current_page = 1
        self._current_tags = ""
        self._current_rating = "all"
        self._min_score = 0
        self._loading = False
        self._search = SearchState()
        self._last_scroll_page = 0
        self._infinite_scroll = app._db.get_setting_bool("infinite_scroll")

    def reset(self) -> None:
        """Reset search state for a site change."""
        self._search.shown_post_ids.clear()
        self._search.page_cache.clear()

    def clear_loading(self) -> None:
        self._loading = False

    # -- Search entry points --

    def on_search(self, tags: str) -> None:
        self._current_tags = tags
        self._current_page = self._app._page_spin.value()
        self._search = SearchState()
        self._min_score = self._app._score_spin.value()
        self._app._preview.clear()
        self._app._next_page_btn.setVisible(True)
        self._app._prev_page_btn.setVisible(False)
        self.do_search()

    def on_search_error(self, e: str) -> None:
        self._loading = False
        self._app._status.showMessage(f"Error: {e}")

    # -- Pagination --

    def prev_page(self) -> None:
        if self._current_page > 1:
            self._current_page -= 1
            if self._current_page in self._search.page_cache:
                self._app._signals.search_done.emit(self._search.page_cache[self._current_page])
            else:
                self.do_search()

    def next_page(self) -> None:
        if self._loading:
            return
        self._current_page += 1
        if self._current_page in self._search.page_cache:
            self._app._signals.search_done.emit(self._search.page_cache[self._current_page])
            return
        self.do_search()

    def on_nav_past_end(self) -> None:
        if self._infinite_scroll:
            return
        self._search.nav_page_turn = "first"
        self.next_page()

    def on_nav_before_start(self) -> None:
        if self._infinite_scroll:
            return
        if self._current_page > 1:
            self._search.nav_page_turn = "last"
            self.prev_page()

    def scroll_next_page(self) -> None:
        if self._loading:
            return
        self._current_page += 1
        self.do_search()

    def scroll_prev_page(self) -> None:
        if self._loading or self._current_page <= 1:
            return
        self._current_page -= 1
        self.do_search()

    # -- Tag building --

    def _build_search_tags(self) -> str:
        api_type = self._app._current_site.api_type if self._app._current_site else None
        return build_search_tags(
            self._current_tags,
            self._current_rating,
            api_type,
            self._min_score,
            self._app._media_filter.currentText(),
        )

    # -- Core search --

    def do_search(self) -> None:
        if not self._app._current_site:
            self._app._status.showMessage("No site selected")
            return
        self._loading = True
        self._app._page_label.setText(f"Page {self._current_page}")
        self._app._status.showMessage("Searching...")

        search_tags = self._build_search_tags()
        log.info(f"Search: tags='{search_tags}' rating={self._current_rating}")
        page = self._current_page
        limit = self._app._db.get_setting_int("page_size") or 40

        bl_tags = set()
        if self._app._db.get_setting_bool("blacklist_enabled"):
            bl_tags = set(self._app._db.get_blacklisted_tags())
        bl_posts = self._app._db.get_blacklisted_posts()
        shown_ids = self._search.shown_post_ids.copy()
        seen = shown_ids.copy()

        total_drops = {"bl_tags": 0, "bl_posts": 0, "dedup": 0}

        async def _search():
            client = self._app._make_client()
            try:
                collected = []
                raw_total = 0
                current_page = page
                batch = await client.search(tags=search_tags, page=current_page, limit=limit)
                raw_total += len(batch)
                filtered, batch_drops = filter_posts(batch, bl_tags, bl_posts, seen)
                for k in total_drops:
                    total_drops[k] += batch_drops[k]
                collected.extend(filtered)
                if should_backfill(len(collected), limit, len(batch)):
                    for _ in range(9):
                        await asyncio.sleep(0.3)
                        current_page += 1
                        batch = await client.search(tags=search_tags, page=current_page, limit=limit)
                        raw_total += len(batch)
                        filtered, batch_drops = filter_posts(batch, bl_tags, bl_posts, seen)
                        for k in total_drops:
                            total_drops[k] += batch_drops[k]
                        collected.extend(filtered)
                        log.debug(f"Backfill: page={current_page} batch={len(batch)} filtered={len(filtered)} total={len(collected)}/{limit}")
                        if not should_backfill(len(collected), limit, len(batch)):
                            break
                log.debug(
                    f"do_search: limit={limit} api_returned_total={raw_total} kept={len(collected[:limit])} "
                    f"drops_bl_tags={total_drops['bl_tags']} drops_bl_posts={total_drops['bl_posts']} drops_dedup={total_drops['dedup']} "
                    f"last_batch_size={len(batch)} api_short_signal={len(batch) < limit}"
                )
                self._app._signals.search_done.emit(collected[:limit])
            except Exception as e:
                self._app._signals.search_error.emit(str(e))
            finally:
                await client.close()

        self._app._run_async(_search)

    # -- Search results --

    def on_search_done(self, posts: list) -> None:
        self._app._page_label.setText(f"Page {self._current_page}")
        self._app._posts = posts
        ss = self._search
        ss.shown_post_ids.update(p.id for p in posts)
        ss.page_cache[self._current_page] = posts
        if not self._infinite_scroll and len(ss.page_cache) > 10:
            oldest = min(ss.page_cache.keys())
            del ss.page_cache[oldest]
        limit = self._app._db.get_setting_int("page_size") or 40
        at_end = len(posts) < limit
        log.debug(f"on_search_done: displayed_count={len(posts)} limit={limit} at_end={at_end}")
        if at_end:
            self._app._status.showMessage(f"{len(posts)} results (end)")
        else:
            self._app._status.showMessage(f"{len(posts)} results")
        self._app._prev_page_btn.setVisible(self._current_page > 1)
        self._app._next_page_btn.setVisible(not at_end)
        thumbs = self._app._grid.set_posts(len(posts))
        self._app._grid.scroll_to_top()
        QTimer.singleShot(100, self.clear_loading)

        from ..core.config import saved_dir
        from ..core.cache import cached_path_for, cache_dir
        site_id = self._app._site_combo.currentData()

        _saved_ids = self._app._db.get_saved_post_ids()

        _favs = self._app._db.get_bookmarks(site_id=site_id) if site_id else []
        _bookmarked_ids: set[int] = {f.post_id for f in _favs}

        _cd = cache_dir()
        _cached_names: set[str] = set()
        if _cd.exists():
            _cached_names = {f.name for f in _cd.iterdir() if f.is_file()}

        for i, (post, thumb) in enumerate(zip(posts, thumbs)):
            if post.id in _bookmarked_ids:
                thumb.set_bookmarked(True)
            thumb.set_saved_locally(post.id in _saved_ids)
            cached = cached_path_for(post.file_url)
            if cached.name in _cached_names:
                thumb._cached_path = str(cached)

            if post.preview_url:
                self.fetch_thumbnail(i, post.preview_url)

        turn = self._search.nav_page_turn
        if turn and posts:
            self._search.nav_page_turn = None
            if turn == "first":
                idx = 0
            else:
                idx = len(posts) - 1
            self._app._grid._select(idx)
            self._app._on_post_activated(idx)

        self._app._grid.setFocus()

        if self._app._db.get_setting("prefetch_mode") in ("Nearby", "Aggressive") and posts:
            self._app._prefetch_adjacent(0)

        if self._infinite_scroll and posts:
            QTimer.singleShot(200, self.check_viewport_fill)

    # -- Infinite scroll --

    def on_reached_bottom(self) -> None:
        if not self._infinite_scroll or self._loading or self._search.infinite_exhausted:
            return
        self._loading = True
        self._current_page += 1

        search_tags = self._build_search_tags()
        page = self._current_page
        limit = self._app._db.get_setting_int("page_size") or 40

        bl_tags = set()
        if self._app._db.get_setting_bool("blacklist_enabled"):
            bl_tags = set(self._app._db.get_blacklisted_tags())
        bl_posts = self._app._db.get_blacklisted_posts()
        shown_ids = self._search.shown_post_ids.copy()
        seen = shown_ids.copy()

        total_drops = {"bl_tags": 0, "bl_posts": 0, "dedup": 0}

        async def _search():
            client = self._app._make_client()
            collected = []
            raw_total = 0
            last_page = page
            api_exhausted = False
            try:
                current_page = page
                batch = await client.search(tags=search_tags, page=current_page, limit=limit)
                raw_total += len(batch)
                last_page = current_page
                filtered, batch_drops = filter_posts(batch, bl_tags, bl_posts, seen)
                for k in total_drops:
                    total_drops[k] += batch_drops[k]
                collected.extend(filtered)
                if len(batch) < limit:
                    api_exhausted = True
                elif len(collected) < limit:
                    for _ in range(9):
                        await asyncio.sleep(0.3)
                        current_page += 1
                        batch = await client.search(tags=search_tags, page=current_page, limit=limit)
                        raw_total += len(batch)
                        last_page = current_page
                        filtered, batch_drops = filter_posts(batch, bl_tags, bl_posts, seen)
                        for k in total_drops:
                            total_drops[k] += batch_drops[k]
                        collected.extend(filtered)
                        if len(batch) < limit:
                            api_exhausted = True
                            break
                        if len(collected) >= limit:
                            break
            except Exception as e:
                log.warning(f"Infinite scroll fetch failed: {e}")
            finally:
                self._search.infinite_last_page = last_page
                self._search.infinite_api_exhausted = api_exhausted
                log.debug(
                    f"on_reached_bottom: limit={limit} api_returned_total={raw_total} kept={len(collected[:limit])} "
                    f"drops_bl_tags={total_drops['bl_tags']} drops_bl_posts={total_drops['bl_posts']} drops_dedup={total_drops['dedup']} "
                    f"api_exhausted={api_exhausted} last_page={last_page}"
                )
                self._app._signals.search_append.emit(collected[:limit])
                await client.close()

        self._app._run_async(_search)

    def on_scroll_range_changed(self, _min: int, max_val: int) -> None:
        """Scrollbar range changed (resize/splitter) -- check if viewport needs filling."""
        if max_val == 0 and self._infinite_scroll and self._app._posts:
            QTimer.singleShot(100, self.check_viewport_fill)

    def check_viewport_fill(self) -> None:
        """If content doesn't fill the viewport, trigger infinite scroll."""
        if not self._infinite_scroll or self._loading or self._search.infinite_exhausted:
            return
        self._app._grid.widget().updateGeometry()
        QApplication.processEvents()
        sb = self._app._grid.verticalScrollBar()
        if sb.maximum() == 0 and self._app._posts:
            self.on_reached_bottom()

    def on_search_append(self, posts: list) -> None:
        """Queue posts and add them one at a time as thumbnails arrive."""
        ss = self._search

        if not posts:
            if ss.infinite_api_exhausted and ss.infinite_last_page > self._current_page:
                self._current_page = ss.infinite_last_page
            self._loading = False
            if ss.infinite_api_exhausted:
                ss.infinite_exhausted = True
                self._app._status.showMessage(f"{len(self._app._posts)} results (end)")
            else:
                QTimer.singleShot(100, self.check_viewport_fill)
            return
        if ss.infinite_last_page > self._current_page:
            self._current_page = ss.infinite_last_page
        ss.shown_post_ids.update(p.id for p in posts)
        ss.append_queue.extend(posts)
        self._drain_append_queue()

    def _drain_append_queue(self) -> None:
        """Add all queued posts to the grid at once, thumbnails load async."""
        ss = self._search
        if not ss.append_queue:
            self._loading = False
            return

        from ..core.cache import cached_path_for, cache_dir
        site_id = self._app._site_combo.currentData()
        _saved_ids = self._app._db.get_saved_post_ids()

        _favs = self._app._db.get_bookmarks(site_id=site_id) if site_id else []
        _bookmarked_ids: set[int] = {f.post_id for f in _favs}
        _cd = cache_dir()
        _cached_names: set[str] = set()
        if _cd.exists():
            _cached_names = {f.name for f in _cd.iterdir() if f.is_file()}

        posts = ss.append_queue[:]
        ss.append_queue.clear()
        start_idx = len(self._app._posts)
        self._app._posts.extend(posts)
        thumbs = self._app._grid.append_posts(len(posts))

        for i, (post, thumb) in enumerate(zip(posts, thumbs)):
            idx = start_idx + i
            if post.id in _bookmarked_ids:
                thumb.set_bookmarked(True)
            thumb.set_saved_locally(post.id in _saved_ids)
            cached = cached_path_for(post.file_url)
            if cached.name in _cached_names:
                thumb._cached_path = str(cached)
            if post.preview_url:
                self.fetch_thumbnail(idx, post.preview_url)

        self._app._status.showMessage(f"{len(self._app._posts)} results")

        self._loading = False
        self._app._auto_evict_cache()
        sb = self._app._grid.verticalScrollBar()
        from .grid import THUMB_SIZE, THUMB_SPACING
        threshold = THUMB_SIZE + THUMB_SPACING * 2
        if sb.maximum() == 0 or sb.value() >= sb.maximum() - threshold:
            self.on_reached_bottom()

    # -- Thumbnails --

    def fetch_thumbnail(self, index: int, url: str) -> None:
        from ..core.cache import download_thumbnail

        async def _download():
            try:
                path = await download_thumbnail(url)
                self._app._signals.thumb_done.emit(index, str(path))
            except Exception as e:
                log.warning(f"Thumb #{index} failed: {e}")
        self._app._run_async(_download)

    def on_thumb_done(self, index: int, path: str) -> None:
        thumbs = self._app._grid._thumbs
        if 0 <= index < len(thumbs):
            pix = QPixmap(path)
            if not pix.isNull():
                thumbs[index].set_pixmap(pix)

    # -- Autocomplete --

    def request_autocomplete(self, query: str) -> None:
        if not self._app._current_site or len(query) < 2:
            return

        async def _ac():
            client = self._app._make_client()
            try:
                results = await client.autocomplete(query)
                self._app._signals.autocomplete_done.emit(results)
            except Exception as e:
                log.warning(f"Operation failed: {e}")
            finally:
                await client.close()

        self._app._run_async(_ac)

    def on_autocomplete_done(self, suggestions: list) -> None:
        self._app._search_bar.set_suggestions(suggestions)

    # -- Blacklist removal --

    def remove_blacklisted_from_grid(self, tag: str = None, post_url: str = None) -> None:
        """Remove matching posts from the grid in-place without re-searching."""
        to_remove = []
        for i, post in enumerate(self._app._posts):
            if tag and tag in post.tag_list:
                to_remove.append(i)
            elif post_url and post.file_url == post_url:
                to_remove.append(i)

        if not to_remove:
            return

        from ..core.cache import cached_path_for
        for i in to_remove:
            cp = str(cached_path_for(self._app._posts[i].file_url))
            if cp == self._app._preview._current_path:
                self._app._preview.clear()
                if self._app._fullscreen_window and self._app._fullscreen_window.isVisible():
                    self._app._fullscreen_window.stop_media()
                break

        for i in reversed(to_remove):
            self._app._posts.pop(i)

        thumbs = self._app._grid.set_posts(len(self._app._posts))
        site_id = self._app._site_combo.currentData()
        _saved_ids = self._app._db.get_saved_post_ids()

        for i, (post, thumb) in enumerate(zip(self._app._posts, thumbs)):
            if site_id and self._app._db.is_bookmarked(site_id, post.id):
                thumb.set_bookmarked(True)
            thumb.set_saved_locally(post.id in _saved_ids)
            from ..core.cache import cached_path_for as cpf
            cached = cpf(post.file_url)
            if cached.exists():
                thumb._cached_path = str(cached)
            if post.preview_url:
                self.fetch_thumbnail(i, post.preview_url)

        self._app._status.showMessage(f"{len(self._app._posts)} results — {len(to_remove)} removed")
