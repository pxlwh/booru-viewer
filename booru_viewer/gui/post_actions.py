"""Bookmark, save/library, batch download, and blacklist operations."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.cache import download_image

if TYPE_CHECKING:
    from .main_window import BooruApp

log = logging.getLogger("booru")


# Pure functions

def is_batch_message(msg: str) -> bool:
    """Detect batch progress messages like 'Saved 3/10 to Unfiled'."""
    return "/" in msg and any(c.isdigit() for c in msg.split("/")[0][-2:])

def is_in_library(path: Path, saved_root: Path) -> bool:
    """Check if path is inside the library root."""
    try:
        return path.is_relative_to(saved_root)
    except (TypeError, ValueError):
        return False


class PostActionsController:
    def __init__(self, app: BooruApp) -> None:
        self._app = app
        self._batch_dest: Path | None = None

    def on_bookmark_error(self, e: str) -> None:
        self._app._status.showMessage(f"Error: {e}")

    def is_post_saved(self, post_id: int) -> bool:
        return self._app._db.is_post_in_library(post_id)

    def _maybe_unbookmark(self, post) -> None:
        """Remove the bookmark for *post* if the unbookmark-on-save setting is on.

        Handles DB removal, grid thumbnail dot, preview state, bookmarks
        tab refresh, and popout sync in one place so every save path
        (single, bulk, Save As, batch download) can call it.
        """
        if not self._app._db.get_setting_bool("unbookmark_on_save"):
            return
        site_id = (
            self._app._preview._current_site_id
            or self._app._site_combo.currentData()
        )
        if not site_id or not self._app._db.is_bookmarked(site_id, post.id):
            return
        self._app._db.remove_bookmark(site_id, post.id)
        # Update grid thumbnail bookmark dot
        for i, p in enumerate(self._app._posts):
            if p.id == post.id and i < len(self._app._grid._thumbs):
                self._app._grid._thumbs[i].set_bookmarked(False)
                break
        # Update preview and popout
        if (self._app._preview._current_post
                and self._app._preview._current_post.id == post.id):
            self._app._preview.update_bookmark_state(False)
        self._app._popout_ctrl.update_state()
        # Refresh bookmarks tab if visible
        if self._app._stack.currentIndex() == 1:
            self._app._bookmarks_view.refresh()

    def get_preview_post(self):
        idx = self._app._grid.selected_index
        if 0 <= idx < len(self._app._posts):
            return self._app._posts[idx], idx
        if self._app._preview._current_post:
            return self._app._preview._current_post, -1
        return None, -1

    def bookmark_from_preview(self) -> None:
        post, idx = self.get_preview_post()
        if not post:
            return
        site_id = self._app._preview._current_site_id or self._app._site_combo.currentData()
        if not site_id:
            return
        if idx >= 0:
            self.toggle_bookmark(idx)
        else:
            if self._app._db.is_bookmarked(site_id, post.id):
                self._app._db.remove_bookmark(site_id, post.id)
            else:
                from ..core.cache import cached_path_for
                cached = cached_path_for(post.file_url)
                self._app._db.add_bookmark(
                    site_id=site_id, post_id=post.id,
                    file_url=post.file_url, preview_url=post.preview_url or "",
                    tags=post.tags, rating=post.rating, score=post.score,
                    source=post.source, cached_path=str(cached) if cached.exists() else None,
                    tag_categories=post.tag_categories,
                )
        bookmarked = bool(self._app._db.is_bookmarked(site_id, post.id))
        self._app._preview.update_bookmark_state(bookmarked)
        self._app._popout_ctrl.update_state()
        if self._app._stack.currentIndex() == 1:
            self._app._bookmarks_view.refresh()

    def bookmark_to_folder_from_preview(self, folder: str) -> None:
        """Bookmark the current preview post into a specific bookmark folder.

        Triggered by the toolbar Bookmark-as submenu, which only shows
        when the post is not yet bookmarked -- so this method only handles
        the create path, never the move/remove paths. Empty string means
        Unfiled. Brand-new folder names get added to the DB folder list
        first so the bookmarks tab combo immediately shows them.
        """
        post, idx = self.get_preview_post()
        if not post:
            return
        site_id = self._app._preview._current_site_id or self._app._site_combo.currentData()
        if not site_id:
            return
        target = folder if folder else None
        if target and target not in self._app._db.get_folders():
            try:
                self._app._db.add_folder(target)
            except ValueError as e:
                self._app._status.showMessage(f"Invalid folder name: {e}")
                return
        if idx >= 0:
            # In the grid -- go through toggle_bookmark so the grid
            # thumbnail's bookmark badge updates via on_bookmark_done.
            self.toggle_bookmark(idx, target)
        else:
            # Preview-only post (e.g. opened from the bookmarks tab while
            # browse is empty). Inline the add -- no grid index to update.
            from ..core.cache import cached_path_for
            cached = cached_path_for(post.file_url)
            self._app._db.add_bookmark(
                site_id=site_id, post_id=post.id,
                file_url=post.file_url, preview_url=post.preview_url or "",
                tags=post.tags, rating=post.rating, score=post.score,
                source=post.source,
                cached_path=str(cached) if cached.exists() else None,
                folder=target,
                tag_categories=post.tag_categories,
            )
            where = target or "Unfiled"
            self._app._status.showMessage(f"Bookmarked #{post.id} to {where}")
        self._app._preview.update_bookmark_state(True)
        self._app._popout_ctrl.update_state()
        # Refresh bookmarks tab if visible so the new entry appears.
        if self._app._stack.currentIndex() == 1:
            self._app._bookmarks_view.refresh()

    def save_from_preview(self, folder: str) -> None:
        post, idx = self.get_preview_post()
        if post:
            target = folder if folder else None
            self.save_to_library(post, target)

    def unsave_from_preview(self) -> None:
        post, idx = self.get_preview_post()
        if not post:
            return
        # delete_from_library walks every library folder by post id and
        # deletes every match in one call -- no folder hint needed. Pass
        # db so templated filenames also get unlinked AND the meta row
        # gets cleaned up.
        from ..core.cache import delete_from_library
        deleted = delete_from_library(post.id, db=self._app._db)
        if deleted:
            self._app._status.showMessage(f"Removed #{post.id} from library")
            self._app._preview.update_save_state(False)
            # Update browse grid thumbnail saved dot
            for i, p in enumerate(self._app._posts):
                if p.id == post.id and i < len(self._app._grid._thumbs):
                    self._app._grid._thumbs[i].set_saved_locally(False)
                    break
            # Update bookmarks grid thumbnail
            bm_grid = self._app._bookmarks_view._grid
            for i, fav in enumerate(self._app._bookmarks_view._bookmarks):
                if fav.post_id == post.id and i < len(bm_grid._thumbs):
                    bm_grid._thumbs[i].set_saved_locally(False)
                    break
            # Refresh library tab if visible
            if self._app._stack.currentIndex() == 2:
                self._app._library_view.refresh()
        else:
            self._app._status.showMessage(f"#{post.id} not in library")
        self._app._popout_ctrl.update_state()

    def blacklist_tag_from_popout(self, tag: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._app, "Blacklist Tag",
            f"Blacklist tag \"{tag}\"?\nPosts with this tag will be hidden.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._app._db.add_blacklisted_tag(tag)
        self._app._db.set_setting("blacklist_enabled", "1")
        self._app._status.showMessage(f"Blacklisted: {tag}")
        self._app._search_ctrl.remove_blacklisted_from_grid(tag=tag)

    def blacklist_post_from_popout(self) -> None:
        post, idx = self.get_preview_post()
        if post:
            from PySide6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self._app, "Blacklist Post",
                f"Blacklist post #{post.id}?\nThis post will be hidden from results.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            self._app._db.add_blacklisted_post(post.file_url)
            self._app._status.showMessage(f"Post #{post.id} blacklisted")
            self._app._search_ctrl.remove_blacklisted_from_grid(post_url=post.file_url)

    def toggle_bookmark(self, index: int, folder: str | None = None) -> None:
        """Toggle the bookmark state of post at `index`.

        When `folder` is given and the post is not yet bookmarked, the
        new bookmark is filed under that bookmark folder. The folder
        arg is ignored when removing -- bookmark folder membership is
        moot if the bookmark itself is going away.
        """
        post = self._app._posts[index]
        site_id = self._app._site_combo.currentData()
        if not site_id:
            return

        if self._app._db.is_bookmarked(site_id, post.id):
            self._app._db.remove_bookmark(site_id, post.id)
            self._app._status.showMessage(f"Unbookmarked #{post.id}")
            thumbs = self._app._grid._thumbs
            if 0 <= index < len(thumbs):
                thumbs[index].set_bookmarked(False)
        else:
            self._app._status.showMessage(f"Bookmarking #{post.id}...")

            async def _fav():
                try:
                    path = await download_image(post.file_url)
                    self._app._db.add_bookmark(
                        site_id=site_id,
                        post_id=post.id,
                        file_url=post.file_url,
                        preview_url=post.preview_url,
                        tags=post.tags,
                        rating=post.rating,
                        score=post.score,
                        source=post.source,
                        cached_path=str(path),
                        folder=folder,
                        tag_categories=post.tag_categories,
                    )
                    where = folder or "Unfiled"
                    self._app._signals.bookmark_done.emit(index, f"Bookmarked #{post.id} to {where}")
                except Exception as e:
                    self._app._signals.bookmark_error.emit(str(e))

            self._app._run_async(_fav)

    def bulk_bookmark(self, indices: list[int], posts: list) -> None:
        site_id = self._app._site_combo.currentData()
        if not site_id:
            return
        self._app._status.showMessage(f"Bookmarking {len(posts)}...")

        async def _do():
            for i, (idx, post) in enumerate(zip(indices, posts)):
                if self._app._db.is_bookmarked(site_id, post.id):
                    continue
                try:
                    path = await download_image(post.file_url)
                    self._app._db.add_bookmark(
                        site_id=site_id, post_id=post.id,
                        file_url=post.file_url, preview_url=post.preview_url,
                        tags=post.tags, rating=post.rating, score=post.score,
                        source=post.source, cached_path=str(path),
                        tag_categories=post.tag_categories,
                    )
                    self._app._signals.bookmark_done.emit(idx, f"Bookmarked {i+1}/{len(posts)}")
                except Exception as e:
                    log.warning(f"Operation failed: {e}")
            self._app._signals.batch_done.emit(f"Bookmarked {len(posts)} posts")

        self._app._run_async(_do)

    def bulk_save(self, indices: list[int], posts: list, folder: str | None) -> None:
        """Bulk-save the selected posts into the library, optionally inside a subfolder.

        Each iteration routes through save_post_file with a shared
        in_flight set so template-collision-prone batches (e.g.
        %artist% on a page that has many posts by the same artist) get
        sequential _1, _2, _3 suffixes instead of clobbering each other.
        """
        from ..core.config import saved_dir, saved_folder_dir
        from ..core.library_save import save_post_file

        where = folder or "Unfiled"
        self._app._status.showMessage(f"Saving {len(posts)} to {where}...")
        try:
            dest_dir = saved_folder_dir(folder) if folder else saved_dir()
        except ValueError as e:
            self._app._status.showMessage(f"Invalid folder name: {e}")
            return

        in_flight: set[str] = set()

        async def _do():
            fetcher = self._app._get_category_fetcher()
            for i, (idx, post) in enumerate(zip(indices, posts)):
                try:
                    src = Path(await download_image(post.file_url))
                    await save_post_file(src, post, dest_dir, self._app._db, in_flight, category_fetcher=fetcher)
                    self.copy_library_thumb(post)
                    self._app._signals.bookmark_done.emit(idx, f"Saved {i+1}/{len(posts)} to {where}")
                    self._maybe_unbookmark(post)
                except Exception as e:
                    log.warning(f"Bulk save #{post.id} failed: {e}")
            self._app._signals.batch_done.emit(f"Saved {len(posts)} to {where}")

        self._app._run_async(_do)

    def bulk_unsave(self, indices: list[int], posts: list) -> None:
        """Bulk-remove selected posts from the library.

        Mirrors `bulk_save` shape but synchronously -- `delete_from_library`
        is a filesystem op, no httpx round-trip needed. Touches only the
        library (filesystem); bookmarks are a separate DB-backed concept
        and stay untouched. The grid's saved-locally dot clears for every
        selection slot regardless of whether the file was actually present
        -- the user's intent is "make these not-saved", and a missing file
        is already not-saved.
        """
        from ..core.cache import delete_from_library
        for post in posts:
            delete_from_library(post.id, db=self._app._db)
        for idx in indices:
            if 0 <= idx < len(self._app._grid._thumbs):
                self._app._grid._thumbs[idx].set_saved_locally(False)
        self._app._grid._clear_multi()
        self._app._status.showMessage(f"Removed {len(posts)} from library")
        if self._app._stack.currentIndex() == 2:
            self._app._library_view.refresh()
        self._app._popout_ctrl.update_state()

    def ensure_bookmarked(self, post) -> None:
        """Bookmark a post if not already bookmarked."""
        site_id = self._app._site_combo.currentData()
        if not site_id or self._app._db.is_bookmarked(site_id, post.id):
            return

        async def _fav():
            try:
                path = await download_image(post.file_url)
                self._app._db.add_bookmark(
                    site_id=site_id,
                    post_id=post.id,
                    file_url=post.file_url,
                    preview_url=post.preview_url,
                    tags=post.tags,
                    rating=post.rating,
                    score=post.score,
                    source=post.source,
                    cached_path=str(path),
                )
            except Exception as e:
                log.warning(f"Operation failed: {e}")

        self._app._run_async(_fav)

    def batch_download_posts(self, posts: list, dest: str) -> None:
        """Multi-select Download All entry point. Delegates to
        batch_download_to so the in_flight set, library_meta write,
        and saved-dots refresh share one implementation."""
        self.batch_download_to(posts, Path(dest))

    def batch_download_to(self, posts: list, dest_dir: Path) -> None:
        """Download `posts` into `dest_dir`, routing each save through
        save_post_file with a shared in_flight set so collision-prone
        templates produce sequential _1, _2 suffixes within the batch.

        Stashes `dest_dir` on `self._batch_dest` so on_batch_progress
        and on_batch_done can decide whether the destination is inside
        the library and the saved-dots need refreshing. The library_meta
        write happens automatically inside save_post_file when dest_dir
        is inside saved_dir() -- fixes the v0.2.3 latent bug where batch
        downloads into a library folder left files unregistered.
        """
        from ..core.library_save import save_post_file

        self._batch_dest = dest_dir
        self._app._status.showMessage(f"Downloading {len(posts)} images...")
        in_flight: set[str] = set()

        async def _batch():
            fetcher = self._app._get_category_fetcher()
            for i, post in enumerate(posts):
                try:
                    src = Path(await download_image(post.file_url))
                    await save_post_file(src, post, dest_dir, self._app._db, in_flight, category_fetcher=fetcher)
                    self._app._signals.batch_progress.emit(i + 1, len(posts), post.id)
                    self._maybe_unbookmark(post)
                except Exception as e:
                    log.warning(f"Batch #{post.id} failed: {e}")
            self._app._signals.batch_done.emit(f"Downloaded {len(posts)} images to {dest_dir}")

        self._app._run_async(_batch)

    def batch_download(self) -> None:
        if not self._app._posts:
            self._app._status.showMessage("No posts to download")
            return
        from .dialogs import select_directory
        dest = select_directory(self._app, "Download to folder")
        if not dest:
            return
        self.batch_download_to(list(self._app._posts), Path(dest))

    def is_current_bookmarked(self, index: int) -> bool:
        site_id = self._app._site_combo.currentData()
        if not site_id or index < 0 or index >= len(self._app._posts):
            return False
        return self._app._db.is_bookmarked(site_id, self._app._posts[index].id)

    def copy_library_thumb(self, post) -> None:
        """Copy a post's browse thumbnail into the library thumbnail
        cache so the Library tab can paint it without re-downloading.
        No-op if there's no preview_url or the source thumb isn't cached."""
        if not post.preview_url:
            return
        from ..core.config import thumbnails_dir
        from ..core.cache import cached_path_for
        thumb_src = cached_path_for(post.preview_url, thumbnails_dir())
        if not thumb_src.exists():
            return
        lib_thumb_dir = thumbnails_dir() / "library"
        lib_thumb_dir.mkdir(parents=True, exist_ok=True)
        lib_thumb = lib_thumb_dir / f"{post.id}.jpg"
        if not lib_thumb.exists():
            import shutil
            shutil.copy2(thumb_src, lib_thumb)

    def save_to_library(self, post, folder: str | None) -> None:
        """Save a post into the library, optionally inside a subfolder.

        Routes through the unified save_post_file flow so the filename
        template, sequential collision suffixes, same-post idempotency,
        and library_meta write are all handled in one place. Re-saving
        the same post into the same folder is a no-op (idempotent);
        saving into a different folder produces a second copy without
        touching the first.
        """
        from ..core.config import saved_dir, saved_folder_dir
        from ..core.library_save import save_post_file

        self._app._status.showMessage(f"Saving #{post.id} to library...")
        try:
            dest_dir = saved_folder_dir(folder) if folder else saved_dir()
        except ValueError as e:
            self._app._status.showMessage(f"Invalid folder name: {e}")
            return

        async def _save():
            try:
                src = Path(await download_image(post.file_url))
                await save_post_file(src, post, dest_dir, self._app._db, category_fetcher=self._app._get_category_fetcher())
                self.copy_library_thumb(post)
                where = folder or "Unfiled"
                self._app._signals.bookmark_done.emit(
                    self._app._grid.selected_index,
                    f"Saved #{post.id} to {where}",
                )
                self._maybe_unbookmark(post)
            except Exception as e:
                self._app._signals.bookmark_error.emit(str(e))

        self._app._run_async(_save)

    def save_as(self, post) -> None:
        """Open a Save As dialog for a single post and write the file
        through the unified save_post_file flow.

        The default name in the dialog comes from rendering the user's
        library_filename_template against the post; the user can edit
        before confirming. If the chosen destination ends up inside
        saved_dir(), save_post_file registers a library_meta row --
        a behavior change from v0.2.3 (where Save As never wrote meta
        regardless of destination)."""
        from ..core.cache import cached_path_for
        from ..core.config import render_filename_template
        from ..core.library_save import save_post_file
        from .dialogs import save_file

        src = cached_path_for(post.file_url)
        if not src.exists():
            self._app._status.showMessage("Image not cached — double-click to download first")
            return
        ext = src.suffix
        template = self._app._db.get_setting("library_filename_template")
        default_name = render_filename_template(template, post, ext)
        dest = save_file(self._app, "Save Image", default_name, f"Images (*{ext})")
        if not dest:
            return
        dest_path = Path(dest)

        async def _do_save():
            try:
                actual = await save_post_file(
                    src, post, dest_path.parent, self._app._db,
                    explicit_name=dest_path.name,
                    category_fetcher=self._app._get_category_fetcher(),
                )
                self._app._signals.bookmark_done.emit(
                    self._app._grid.selected_index,
                    f"Saved to {actual}",
                )
                self._maybe_unbookmark(post)
            except Exception as e:
                self._app._signals.bookmark_error.emit(f"Save failed: {e}")

        self._app._run_async(_do_save)

    def on_bookmark_done(self, index: int, msg: str) -> None:
        self._app._status.showMessage(f"{len(self._app._posts)} results — {msg}")
        # Detect batch operations (e.g. "Saved 3/10 to Unfiled") -- skip heavy updates
        is_batch = is_batch_message(msg)
        thumbs = self._app._grid._thumbs
        if 0 <= index < len(thumbs):
            if "Saved" in msg:
                thumbs[index].set_saved_locally(True)
            if "Bookmarked" in msg:
                thumbs[index].set_bookmarked(True)
        if not is_batch:
            if "Bookmarked" in msg:
                self._app._preview.update_bookmark_state(True)
            if "Saved" in msg:
                self._app._preview.update_save_state(True)
                if self._app._stack.currentIndex() == 1:
                    bm_grid = self._app._bookmarks_view._grid
                    bm_idx = bm_grid.selected_index
                    if 0 <= bm_idx < len(bm_grid._thumbs):
                        bm_grid._thumbs[bm_idx].set_saved_locally(True)
                if self._app._stack.currentIndex() == 2:
                    self._app._library_view.refresh()
            self._app._popout_ctrl.update_state()

    def on_batch_progress(self, current: int, total: int, post_id: int) -> None:
        self._app._status.showMessage(f"Downloading {current}/{total}...")
        # Light the browse saved-dot for the just-finished post if the
        # batch destination is inside the library. Runs per-post on the
        # main thread (this is a Qt slot), so the dot appears as the
        # files land instead of all at once when the batch completes.
        dest = self._batch_dest
        if dest is None:
            return
        from ..core.config import saved_dir
        if not is_in_library(dest, saved_dir()):
            return
        for i, p in enumerate(self._app._posts):
            if p.id == post_id and i < len(self._app._grid._thumbs):
                self._app._grid._thumbs[i].set_saved_locally(True)
                break

    def on_batch_done(self, msg: str) -> None:
        self._app._status.showMessage(msg)
        self._app._popout_ctrl.update_state()
        if self._app._stack.currentIndex() == 1:
            self._app._bookmarks_view.refresh()
        if self._app._stack.currentIndex() == 2:
            self._app._library_view.refresh()
        # Saved-dot updates happen incrementally in on_batch_progress as
        # each file lands; this slot just clears the destination stash.
        self._batch_dest = None

    def on_library_files_deleted(self, post_ids: list) -> None:
        """Library deleted files -- clear saved dots on browse grid."""
        for i, p in enumerate(self._app._posts):
            if p.id in post_ids and i < len(self._app._grid._thumbs):
                self._app._grid._thumbs[i].set_saved_locally(False)

    def refresh_browse_saved_dots(self) -> None:
        """Bookmarks changed -- rescan saved state for all visible browse grid posts."""
        for i, p in enumerate(self._app._posts):
            if i < len(self._app._grid._thumbs):
                self._app._grid._thumbs[i].set_saved_locally(self.is_post_saved(p.id))
                site_id = self._app._site_combo.currentData()
                self._app._grid._thumbs[i].set_bookmarked(
                    bool(site_id and self._app._db.is_bookmarked(site_id, p.id))
                )
