"""Image/video loading, prefetch, download progress, and cache eviction."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QPixmap

from ..core.cache import download_image, cache_size_bytes, evict_oldest, evict_oldest_thumbnails

if TYPE_CHECKING:
    from .main_window import BooruApp

log = logging.getLogger("booru")


# -- Pure functions (tested in tests/gui/test_media_controller.py) --


def compute_prefetch_order(
    index: int, total: int, columns: int, mode: str,
) -> list[int]:
    """Return an ordered list of indices to prefetch around *index*.

    *mode* is ``"Nearby"`` (4 cardinals) or ``"Aggressive"`` (ring expansion
    capped at ~3 rows radius).
    """
    if total == 0:
        return []

    if mode == "Nearby":
        order = []
        for offset in [1, -1, columns, -columns]:
            adj = index + offset
            if 0 <= adj < total:
                order.append(adj)
        return order

    # Aggressive: ring expansion
    max_radius = 3
    max_posts = columns * max_radius * 2 + columns
    seen = {index}
    order = []
    for dist in range(1, max_radius + 1):
        ring = set()
        for dy in (-dist, 0, dist):
            for dx in (-dist, 0, dist):
                if dy == 0 and dx == 0:
                    continue
                adj = index + dy * columns + dx
                if 0 <= adj < total and adj not in seen:
                    ring.add(adj)
        for adj in (index + dist, index - dist):
            if 0 <= adj < total and adj not in seen:
                ring.add(adj)
        for adj in sorted(ring):
            seen.add(adj)
            order.append(adj)
        if len(order) >= max_posts:
            break
    return order


# -- Controller --


class MediaController:
    """Owns image/video loading, prefetch, download progress, and cache eviction."""

    def __init__(self, app: BooruApp) -> None:
        self._app = app
        self._prefetch_pause = asyncio.Event()
        self._prefetch_pause.set()  # not paused

    # -- Post activation (media load) --

    def on_post_activated(self, index: int) -> None:
        if 0 <= index < len(self._app._posts):
            post = self._app._posts[index]
            log.info(f"Preview: #{post.id} -> {post.file_url}")
            try:
                if self._app._popout_ctrl.window:
                    self._app._popout_ctrl.window.force_mpv_pause()
                pmpv = self._app._preview._video_player._mpv
                if pmpv is not None:
                    pmpv.pause = True
            except Exception:
                pass
            self._app._preview._current_post = post
            self._app._preview._current_site_id = self._app._site_combo.currentData()
            self._app._preview.set_post_tags(post.tag_categories, post.tag_list)
            self._app._ensure_post_categories_async(post)
            site_id = self._app._preview._current_site_id
            self._app._preview.update_bookmark_state(
                bool(site_id and self._app._db.is_bookmarked(site_id, post.id))
            )
            self._app._preview.update_save_state(self._app._is_post_saved(post.id))
            self._app._status.showMessage(f"Loading #{post.id}...")
            preview_hidden = not (
                self._app._preview.isVisible() and self._app._preview.width() > 0
            )
            if preview_hidden:
                self._app._signals.prefetch_progress.emit(index, 0.0)
            else:
                self._app._dl_progress.show()
                self._app._dl_progress.setRange(0, 0)

            def _progress(downloaded, total):
                self._app._signals.download_progress.emit(downloaded, total)
                if preview_hidden and total > 0:
                    self._app._signals.prefetch_progress.emit(
                        index, downloaded / total
                    )

            info = (f"#{post.id}  {post.width}x{post.height}  score:{post.score}  [{post.rating}]  {Path(post.file_url.split('?')[0]).suffix.lstrip('.').upper() if post.file_url else ''}"
                    + (f"  {post.created_at}" if post.created_at else ""))

            from ..core.cache import is_cached
            from .media.constants import VIDEO_EXTENSIONS
            is_video = bool(
                post.file_url
                and Path(post.file_url.split('?')[0]).suffix.lower() in VIDEO_EXTENSIONS
            )
            streaming = is_video and post.file_url and not is_cached(post.file_url)
            if streaming:
                self._app._signals.video_stream.emit(
                    post.file_url, info, post.width, post.height
                )

            async def _load():
                self._prefetch_pause.clear()
                try:
                    if streaming:
                        return
                    path = await download_image(post.file_url, progress_callback=_progress)
                    self._app._signals.image_done.emit(str(path), info)
                except Exception as e:
                    log.error(f"Image download failed: {e}")
                    self._app._signals.image_error.emit(str(e))
                finally:
                    self._prefetch_pause.set()
                    if preview_hidden:
                        self._app._signals.prefetch_progress.emit(index, -1)

            self._app._run_async(_load)

            if self._app._db.get_setting("prefetch_mode") in ("Nearby", "Aggressive"):
                self.prefetch_adjacent(index)

    # -- Image/video result handlers --

    def on_image_done(self, path: str, info: str) -> None:
        self._app._dl_progress.hide()
        if self._app._popout_ctrl.window and self._app._popout_ctrl.window.isVisible():
            self._app._preview._info_label.setText(info)
            self._app._preview._current_path = path
        else:
            self.set_preview_media(path, info)
        self._app._status.showMessage(f"{len(self._app._posts)} results — Loaded")
        idx = self._app._grid.selected_index
        if 0 <= idx < len(self._app._grid._thumbs):
            self._app._grid._thumbs[idx]._cached_path = path
        self._app._popout_ctrl.update_media(path, info)
        self.auto_evict_cache()

    def on_video_stream(self, url: str, info: str, width: int, height: int) -> None:
        if self._app._popout_ctrl.window and self._app._popout_ctrl.window.isVisible():
            self._app._preview._info_label.setText(info)
            self._app._preview._current_path = url
            self._app._popout_ctrl.window.set_media(url, info, width=width, height=height)
            self._app._popout_ctrl.update_state()
        else:
            self._app._preview._video_player.stop()
            self._app._preview.set_media(url, info)
        self._app._status.showMessage(f"Streaming #{Path(url.split('?')[0]).name}...")

    def on_download_progress(self, downloaded: int, total: int) -> None:
        popout_open = bool(self._app._popout_ctrl.window and self._app._popout_ctrl.window.isVisible())
        if total > 0:
            if not popout_open:
                self._app._dl_progress.setRange(0, total)
                self._app._dl_progress.setValue(downloaded)
                self._app._dl_progress.show()
            mb = downloaded / (1024 * 1024)
            total_mb = total / (1024 * 1024)
            self._app._status.showMessage(f"Downloading... {mb:.1f}/{total_mb:.1f} MB")
            if downloaded >= total and not popout_open:
                self._app._dl_progress.hide()
        elif not popout_open:
            self._app._dl_progress.setRange(0, 0)
            self._app._dl_progress.show()

    def set_preview_media(self, path: str, info: str) -> None:
        """Set media on preview or just info if popout is open."""
        if self._app._popout_ctrl.window and self._app._popout_ctrl.window.isVisible():
            self._app._preview._info_label.setText(info)
            self._app._preview._current_path = path
        else:
            self._app._preview.set_media(path, info)

    # -- Prefetch --

    def on_prefetch_progress(self, index: int, progress: float) -> None:
        if 0 <= index < len(self._app._grid._thumbs):
            self._app._grid._thumbs[index].set_prefetch_progress(progress)

    def prefetch_adjacent(self, index: int) -> None:
        """Prefetch posts around the given index."""
        total = len(self._app._posts)
        if total == 0:
            return
        cols = self._app._grid._flow.columns
        mode = self._app._db.get_setting("prefetch_mode")
        order = compute_prefetch_order(index, total, cols, mode)

        async def _prefetch_spiral():
            for adj in order:
                await self._prefetch_pause.wait()
                if 0 <= adj < len(self._app._posts) and self._app._posts[adj].file_url:
                    self._app._signals.prefetch_progress.emit(adj, 0.0)
                    try:
                        def _progress(dl, total_bytes, idx=adj):
                            if total_bytes > 0:
                                self._app._signals.prefetch_progress.emit(idx, dl / total_bytes)
                        await download_image(self._app._posts[adj].file_url, progress_callback=_progress)
                    except Exception as e:
                        log.warning(f"Operation failed: {e}")
                    self._app._signals.prefetch_progress.emit(adj, -1)
                    await asyncio.sleep(0.2)
        self._app._run_async(_prefetch_spiral)

    # -- Cache eviction --

    def auto_evict_cache(self) -> None:
        if not self._app._db.get_setting_bool("auto_evict"):
            return
        max_mb = self._app._db.get_setting_int("max_cache_mb")
        if max_mb <= 0:
            return
        max_bytes = max_mb * 1024 * 1024
        current = cache_size_bytes(include_thumbnails=False)
        if current > max_bytes:
            protected = set()
            for fav in self._app._db.get_bookmarks(limit=999999):
                if fav.cached_path:
                    protected.add(fav.cached_path)
            evicted = evict_oldest(max_bytes, protected)
            if evicted:
                log.info(f"Auto-evicted {evicted} cached files")
        max_thumb_mb = self._app._db.get_setting_int("max_thumb_cache_mb") or 500
        max_thumb_bytes = max_thumb_mb * 1024 * 1024
        evicted_thumbs = evict_oldest_thumbnails(max_thumb_bytes)
        if evicted_thumbs:
            log.info(f"Auto-evicted {evicted_thumbs} thumbnails")

    # -- Utility --

    @staticmethod
    def image_dimensions(path: str) -> tuple[int, int]:
        """Read image width/height from a local file."""
        from .media.constants import _is_video
        if _is_video(path):
            return 0, 0
        try:
            pix = QPixmap(path)
            if not pix.isNull():
                return pix.width(), pix.height()
        except Exception:
            pass
        return 0, 0
