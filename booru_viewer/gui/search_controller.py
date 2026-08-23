"""Search orchestration, infinite scroll, tag building, and blacklist filtering."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .search_state import SearchState
from .site_selection import effective_site_id

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

    *seen_ids* holds ``(site_id, post_id)`` tuples — post ids are unique
    only within one booru, so the site has to be part of the key.
    Mutates *seen_ids* in place (adds surviving keys).
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
    deduped = []
    for p in posts:
        key = (p.site_id, p.id)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        deduped.append(p)
    posts = deduped
    n3 = len(posts)
    drops["dedup"] = n2 - n3
    return posts, drops


def should_backfill(collected_count: int, limit: int, last_batch_size: int) -> bool:
    """Return True if another backfill page should be fetched."""
    return collected_count < limit and last_batch_size >= limit


def interleave(batches: list[list], limit: int) -> list:
    """Round-robin across per-site batches, skipping exhausted ones.

    Round *k* takes index *k* from every batch still long enough to have
    one, in the order the batches were given (which is selector order).
    A batch that runs out drops out and the rest keep cycling — without
    that, three results from one site would cap the whole grid at three
    rounds.
    """
    if not batches or limit <= 0:
        return []
    out: list = []
    longest = max(len(b) for b in batches)
    for k in range(longest):
        for b in batches:
            if k < len(b):
                out.append(b[k])
                if len(out) >= limit:
                    return out
    return out


async def fetch_site_page(
    client,
    search_tags: str,
    page: int,
    limit: int,
    bl_tags: set,
    bl_posts: set,
    seen: set,
    backfill_delay: float = 0.3,
) -> tuple[list, int, bool, dict]:
    """Fetch one logical page from one site, backfilling short results.

    Fetches *page*, filters it, and while the filtered yield is short of
    *limit* and the API is still returning full batches, fetches up to
    nine more pages with *backfill_delay* between them. This is the one
    fetch loop behind both paged search and infinite scroll; it used to
    exist twice, drifting.

    *seen* may be shared between concurrently running calls: keys are
    ``(site_id, post_id)`` tuples so sites cannot collide, and
    `filter_posts` runs synchronously between awaits on one event loop.

    Returns ``(posts, last_page, api_exhausted, drops)`` — *last_page*
    is the last page actually consumed (backfill advances it),
    *api_exhausted* means the API returned a short batch (no more
    pages), *drops* counts removals as in `filter_posts`.
    """
    collected: list = []
    drops = {"bl_tags": 0, "bl_posts": 0, "dedup": 0}
    current_page = page
    last_page = page
    batch = await client.search(tags=search_tags, page=current_page, limit=limit)
    filtered, batch_drops = filter_posts(batch, bl_tags, bl_posts, seen)
    for k in drops:
        drops[k] += batch_drops[k]
    collected.extend(filtered)
    api_exhausted = len(batch) < limit
    for _ in range(9):
        if api_exhausted or len(collected) >= limit:
            break
        await asyncio.sleep(backfill_delay)
        current_page += 1
        batch = await client.search(tags=search_tags, page=current_page, limit=limit)
        last_page = current_page
        filtered, batch_drops = filter_posts(batch, bl_tags, bl_posts, seen)
        for k in drops:
            drops[k] += batch_drops[k]
        collected.extend(filtered)
        if len(batch) < limit:
            api_exhausted = True
    return collected, last_page, api_exhausted, drops


def partition_results(names: list[str], results: list) -> tuple[list, list[tuple[str, str]]]:
    """Split `asyncio.gather(..., return_exceptions=True)` output.

    Returns ``(successes, errors)`` where each error is a
    ``(name, message)`` pair for an entry that came back as an
    exception. Order is preserved within each list, so batch order
    still follows selector order.
    """
    successes: list = []
    errors: list[tuple[str, str]] = []
    for name, res in zip(names, results):
        if isinstance(res, BaseException):
            errors.append((name, str(res)))
        else:
            successes.append(res)
    return successes, errors


def build_tags_for_sites(
    tags: str,
    rating: str,
    api_types: list,
    min_score: int,
    media_filter: str,
) -> list[str]:
    """One search string per site — rating syntax differs per backend.

    danbooru wants ``rating:e`` where gelbooru wants ``rating:explicit``;
    building the string once from the first site's api_type would send
    one backend's syntax to all of them, and a gelbooru receiving
    ``rating:e`` returns wrong or empty results with no error.
    """
    return [
        build_search_tags(tags, rating, t, min_score, media_filter)
        for t in api_types
    ]


def format_search_status(
    n_posts: int,
    total_sites: int,
    errors: list,
    at_end: bool,
) -> str:
    """Status-bar line for a finished search page.

    With no errors this is the existing single-site message, byte for
    byte. Failed sites are named with their error text so a missing
    API key explains itself instead of reading as an empty result.
    """
    msg = f"{n_posts} results"
    if at_end:
        msg += " (end)"
    if errors:
        ok = max(total_sites - len(errors), 0)
        detail = "; ".join(f"{name}: {err}" for name, err in errors)
        msg = f"{msg} — showing {ok} of {total_sites} sites — {detail}"
    return msg


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
        # Cached lookup sets — rebuilt once per search, reused in
        # _drain_append_queue to avoid repeated DB queries and directory
        # listings on every infinite-scroll append.
        self._cached_names: set[str] | None = None
        self._bookmarked_ids: set[tuple[int, int]] | None = None
        self._saved_ids: set[tuple[int, int]] | None = None

    def reset(self) -> None:
        """Reset search state for a site change."""
        self._search.shown_post_ids.clear()
        self._search.page_cache.clear()
        self._cached_names = None
        self._bookmarked_ids = None
        self._saved_ids = None

    def invalidate_lookup_caches(self) -> None:
        """Clear cached bookmark/saved/cache-dir sets.

        Call after a bookmark or save operation so the next
        ``_drain_append_queue`` picks up the change.
        """
        self._bookmarked_ids = None
        self._saved_ids = None

    def clear_loading(self) -> None:
        self._loading = False

    # -- Search entry points --

    def on_search(self, tags: str) -> None:
        self._current_tags = tags
        self._app._page_spin.setValue(1)
        self._current_page = 1
        self._search = SearchState()
        self._cached_names = None
        self._bookmarked_ids = None
        self._saved_ids = None
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
                self._app._signals.search_done.emit(self._search.page_cache[self._current_page], [])
            else:
                self.do_search()

    def next_page(self) -> None:
        if self._loading:
            return
        self._current_page += 1
        if self._current_page in self._search.page_cache:
            self._app._signals.search_done.emit(self._search.page_cache[self._current_page], [])
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
        sites = self._app._selected_sites()
        if not sites:
            self._app._status.showMessage("No site selected")
            return
        self._loading = True
        self._app._page_label.setText(f"Page {self._current_page}")
        self._app._status.showMessage("Searching...")

        site_names = [s.name for s in sites]
        tags_by_site = build_tags_for_sites(
            self._current_tags,
            self._current_rating,
            [s.api_type for s in sites],
            self._min_score,
            self._app._media_filter.currentText(),
        )
        log.info(f"Search: sites={site_names} tags={tags_by_site} rating={self._current_rating}")
        page = self._current_page
        limit = self._app._db.get_setting_int("page_size") or 40

        bl_tags = set()
        if self._app._db.get_setting_bool("blacklist_enabled"):
            bl_tags = set(self._app._db.get_blacklisted_tags())
        bl_posts = self._app._db.get_blacklisted_posts()
        seen = self._search.shown_post_ids.copy()

        async def _search():
            clients = [self._app._client_for_site(s) for s in sites]
            try:
                results = await asyncio.gather(
                    *(
                        fetch_site_page(c, t, page, limit, bl_tags, bl_posts, seen)
                        for c, t in zip(clients, tags_by_site)
                    ),
                    return_exceptions=True,
                )
                oks, errors = partition_results(site_names, results)
                if errors and not oks:
                    self._app._signals.search_error.emit(
                        "; ".join(f"{n}: {m}" for n, m in errors)
                    )
                    return
                merged = interleave([r[0] for r in oks], limit)
                log.debug(
                    f"do_search: sites={site_names} limit={limit} kept={len(merged)} "
                    f"errors={[n for n, _ in errors]}"
                )
                self._app._signals.search_done.emit(merged, errors)
            except Exception as e:
                self._app._signals.search_error.emit(str(e))
            finally:
                for c in clients:
                    try:
                        await c.close()
                    except Exception:
                        pass

        self._app._run_async(_search)

    # -- Search results --

    def on_search_done(self, posts: list, errors: list | None = None) -> None:
        errors = errors or []
        self._app._page_label.setText(f"Page {self._current_page}")
        self._app._posts = posts
        ss = self._search
        ss.shown_post_ids.update((p.site_id, p.id) for p in posts)
        ss.page_cache[self._current_page] = posts
        if not self._infinite_scroll and len(ss.page_cache) > 10:
            oldest = min(ss.page_cache.keys())
            del ss.page_cache[oldest]
        limit = self._app._db.get_setting_int("page_size") or 40
        at_end = len(posts) < limit
        log.debug(f"on_search_done: displayed_count={len(posts)} limit={limit} at_end={at_end}")
        total_sites = len(self._app._selected_sites()) or 1
        self._app._status.showMessage(
            format_search_status(len(posts), total_sites, errors, at_end)
        )
        self._app._prev_page_btn.setVisible(self._current_page > 1)
        self._app._next_page_btn.setVisible(not at_end)
        thumbs = self._app._grid.set_posts(len(posts))
        self._app._grid.scroll_to_top()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(100, self.clear_loading)

        from ..core.cache import cached_path_for, cache_dir
        site_id = self._app._site_combo.currentData()

        self._saved_ids = self._app._db.get_saved_post_ids()

        self._bookmarked_ids = self._app._db.get_bookmarked_keys()

        _cd = cache_dir()
        self._cached_names = set()
        if _cd.exists():
            self._cached_names = {f.name for f in _cd.iterdir() if f.is_file()}

        for i, (post, thumb) in enumerate(zip(posts, thumbs)):
            if (effective_site_id(post, site_id), post.id) in self._bookmarked_ids:
                thumb.set_bookmarked(True)
            thumb.set_saved_locally((getattr(post, "site_id", None) or site_id or 0, post.id) in self._saved_ids)
            cached = cached_path_for(post.file_url)
            if cached.name in self._cached_names:
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
            self._app._media_ctrl.on_post_activated(idx)

        self._app._grid.setFocus()

        if self._app._db.get_setting("prefetch_mode") in ("Nearby", "Aggressive") and posts:
            self._app._media_ctrl.prefetch_adjacent(0)

        if self._infinite_scroll and posts:
            QTimer.singleShot(200, self.check_viewport_fill)

    # -- Infinite scroll --

    def on_reached_bottom(self) -> None:
        if not self._infinite_scroll or self._loading or self._search.infinite_exhausted:
            return
        sites = self._app._selected_sites()
        if not sites:
            return
        self._loading = True
        self._current_page += 1

        site_names = [s.name for s in sites]
        tags_by_site = build_tags_for_sites(
            self._current_tags,
            self._current_rating,
            [s.api_type for s in sites],
            self._min_score,
            self._app._media_filter.currentText(),
        )
        page = self._current_page
        limit = self._app._db.get_setting_int("page_size") or 40

        bl_tags = set()
        if self._app._db.get_setting_bool("blacklist_enabled"):
            bl_tags = set(self._app._db.get_blacklisted_tags())
        bl_posts = self._app._db.get_blacklisted_posts()
        seen = self._search.shown_post_ids.copy()

        async def _search():
            clients = [self._app._client_for_site(s) for s in sites]
            collected: list = []
            last_page = page
            api_exhausted = False
            try:
                results = await asyncio.gather(
                    *(
                        fetch_site_page(c, t, page, limit, bl_tags, bl_posts, seen)
                        for c, t in zip(clients, tags_by_site)
                    ),
                    return_exceptions=True,
                )
                oks, errors = partition_results(site_names, results)
                if oks:
                    collected = interleave([r[0] for r in oks], limit)
                    # One page number drives every site (paging model 1),
                    # but backfill may consume further on one site than
                    # another. Taking the furthest cursor would skip pages
                    # on the slower sites and lose their posts for good;
                    # the minimum only re-fetches pages whose posts dedup
                    # away against shown_post_ids. When a site errored,
                    # don't advance at all — its next chance is this page.
                    last_page = page if errors else min(r[1] for r in oks)
                    api_exhausted = not errors and all(r[2] for r in oks)
                for name, err in errors:
                    log.warning(f"Infinite scroll fetch failed for {name}: {err}")
            except Exception as e:
                log.warning(f"Infinite scroll fetch failed: {e}")
            finally:
                self._search.infinite_last_page = last_page
                self._search.infinite_api_exhausted = api_exhausted
                log.debug(
                    f"on_reached_bottom: sites={site_names} limit={limit} "
                    f"kept={len(collected)} api_exhausted={api_exhausted} last_page={last_page}"
                )
                self._app._signals.search_append.emit(collected)
                for c in clients:
                    try:
                        await c.close()
                    except Exception:
                        pass

        self._app._run_async(_search)

    def on_scroll_range_changed(self, _min: int, max_val: int) -> None:
        """Scrollbar range changed (resize/splitter) -- check if viewport needs filling."""
        if max_val == 0 and self._infinite_scroll and self._app._posts:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(100, self.check_viewport_fill)

    def check_viewport_fill(self) -> None:
        """If content doesn't fill the viewport, trigger infinite scroll."""
        if not self._infinite_scroll or self._loading or self._search.infinite_exhausted:
            return
        self._app._grid.widget().updateGeometry()
        from PySide6.QtWidgets import QApplication
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
                from PySide6.QtCore import QTimer
                QTimer.singleShot(100, self.check_viewport_fill)
            return
        if ss.infinite_last_page > self._current_page:
            self._current_page = ss.infinite_last_page
        ss.shown_post_ids.update((p.site_id, p.id) for p in posts)
        ss.append_queue.extend(posts)
        self._drain_append_queue()

    def _drain_append_queue(self) -> None:
        """Add all queued posts to the grid at once, thumbnails load async."""
        ss = self._search
        if not ss.append_queue:
            self._loading = False
            return

        from ..core.cache import cached_path_for

        # Reuse the lookup sets built in on_search_done. They stay valid
        # within an infinite-scroll session — bookmarks/saves don't change
        # during passive scrolling, and the cache directory only grows.
        site_id = self._app._site_combo.currentData()
        if self._saved_ids is None:
            self._saved_ids = self._app._db.get_saved_post_ids()
        if self._bookmarked_ids is None:
            self._bookmarked_ids = self._app._db.get_bookmarked_keys()
        if self._cached_names is None:
            from ..core.cache import cache_dir
            _cd = cache_dir()
            self._cached_names = set()
            if _cd.exists():
                self._cached_names = {f.name for f in _cd.iterdir() if f.is_file()}

        posts = ss.append_queue[:]
        ss.append_queue.clear()
        start_idx = len(self._app._posts)
        self._app._posts.extend(posts)
        thumbs = self._app._grid.append_posts(len(posts))

        for i, (post, thumb) in enumerate(zip(posts, thumbs)):
            idx = start_idx + i
            if (effective_site_id(post, site_id), post.id) in self._bookmarked_ids:
                thumb.set_bookmarked(True)
            thumb.set_saved_locally((getattr(post, "site_id", None) or site_id or 0, post.id) in self._saved_ids)
            cached = cached_path_for(post.file_url)
            if cached.name in self._cached_names:
                thumb._cached_path = str(cached)
            if post.preview_url:
                self.fetch_thumbnail(idx, post.preview_url)

        self._app._status.showMessage(f"{len(self._app._posts)} results")

        self._loading = False
        self._app._media_ctrl.auto_evict_cache()
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
        from PySide6.QtGui import QPixmap
        thumbs = self._app._grid._thumbs
        if 0 <= index < len(thumbs):
            pix = QPixmap(path)
            if not pix.isNull():
                thumbs[index].set_pixmap(pix, path)

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
                if self._app._popout_ctrl.window and self._app._popout_ctrl.window.isVisible():
                    self._app._popout_ctrl.window.stop_media()
                break

        for i in reversed(to_remove):
            self._app._posts.pop(i)

        thumbs = self._app._grid.set_posts(len(self._app._posts))
        site_id = self._app._site_combo.currentData()
        _saved_ids = self._app._db.get_saved_post_ids()

        for i, (post, thumb) in enumerate(zip(self._app._posts, thumbs)):
            sid = effective_site_id(post, site_id)
            if sid and self._app._db.is_bookmarked(sid, post.id):
                thumb.set_bookmarked(True)
            thumb.set_saved_locally((getattr(post, "site_id", None) or site_id or 0, post.id) in _saved_ids)
            from ..core.cache import cached_path_for as cpf
            cached = cpf(post.file_url)
            if cached.exists():
                thumb._cached_path = str(cached)
            if post.preview_url:
                self.fetch_thumbnail(i, post.preview_url)

        self._app._status.showMessage(f"{len(self._app._posts)} results — {len(to_remove)} removed")
