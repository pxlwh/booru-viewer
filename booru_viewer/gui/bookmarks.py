"""Bookmarks browser widget with folder support."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QLabel,
    QComboBox,
    QMenu,
    QApplication,
    QInputDialog,
    QMessageBox,
)

from ..core.db import Database, Bookmark
from ..core.api.base import Post
from ..core.cache import download_thumbnail
from ..core.concurrency import run_on_app_loop
from .grid import ThumbnailGrid

log = logging.getLogger("booru")


class BookmarkThumbSignals(QObject):
    thumb_ready = Signal(int, str)
    save_done = Signal(int)  # post_id


class BookmarksView(QWidget):
    """Browse and search local bookmarks with folder support."""

    bookmark_selected = Signal(object)
    bookmark_activated = Signal(object)
    bookmarks_changed = Signal()  # emitted after bookmark add/remove/unsave
    open_in_browser_requested = Signal(int, int)  # (site_id, post_id)

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._bookmarks: list[Bookmark] = []
        self._signals = BookmarkThumbSignals()
        self._signals.thumb_ready.connect(self._on_thumb_ready, Qt.ConnectionType.QueuedConnection)
        self._signals.save_done.connect(self._on_save_done, Qt.ConnectionType.QueuedConnection)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Top bar: folder selector + search.
        # 4px right margin so the rightmost button doesn't sit flush
        # against the preview splitter handle.
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 4, 0)

        # Compact horizontal padding matches the rest of the app's narrow
        # toolbar buttons. Vertical padding (2px) and min-height (inherited
        # from the global QPushButton rule = 16px) give a total height of
        # 22px, lining up with the bundled themes' inputs/combos so the
        # whole toolbar row sits at one consistent height — and matches
        # what native Qt+Fusion produces with no QSS at all.
        _btn_style = "padding: 2px 6px;"

        self._folder_combo = QComboBox()
        self._folder_combo.setMinimumWidth(120)
        self._folder_combo.currentTextChanged.connect(lambda _: self.refresh())
        top.addWidget(self._folder_combo)

        manage_btn = QPushButton("+ Folder")
        manage_btn.setToolTip("New bookmark folder")
        manage_btn.setFixedWidth(75)
        manage_btn.setStyleSheet(_btn_style)
        manage_btn.clicked.connect(self._new_folder)
        top.addWidget(manage_btn)

        # Delete the currently-selected bookmark folder. Disabled when
        # the combo is on a virtual entry (All Bookmarks / Unfiled).
        # This only removes the DB row — bookmarks in that folder become
        # Unfiled (per remove_folder's UPDATE … SET folder = NULL). The
        # library filesystem is untouched: bookmark folders and library
        # folders are independent name spaces.
        self._delete_folder_btn = QPushButton("− Folder")
        self._delete_folder_btn.setToolTip("Delete the selected bookmark folder")
        self._delete_folder_btn.setFixedWidth(75)
        self._delete_folder_btn.setStyleSheet(_btn_style)
        self._delete_folder_btn.clicked.connect(self._delete_folder)
        top.addWidget(self._delete_folder_btn)
        self._folder_combo.currentTextChanged.connect(
            self._update_delete_folder_enabled
        )

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search bookmarks by tag")
        # Enter still triggers an immediate search.
        self._search_input.returnPressed.connect(self._do_search)
        # Live search via debounced timer: every keystroke restarts a
        # 150ms one-shot, when the user stops typing the search runs.
        # Cheap enough since each search is just one SQLite query.
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._do_search)
        self._search_input.textChanged.connect(
            lambda _: self._search_debounce.start()
        )
        top.addWidget(self._search_input, stretch=1)

        layout.addLayout(top)

        # Count label
        self._count_label = QLabel()
        layout.addWidget(self._count_label)

        # Grid
        self._grid = ThumbnailGrid()
        self._grid.post_selected.connect(self._on_selected)
        self._grid.post_activated.connect(self._on_activated)
        self._grid.context_requested.connect(self._on_context_menu)
        self._grid.multi_context_requested.connect(self._on_multi_context_menu)
        layout.addWidget(self._grid)

    def _refresh_folders(self) -> None:
        current = self._folder_combo.currentText()
        self._folder_combo.blockSignals(True)
        self._folder_combo.clear()
        self._folder_combo.addItem("All Bookmarks")
        self._folder_combo.addItem("Unfiled")
        for folder in self._db.get_folders():
            self._folder_combo.addItem(folder)
        # Restore selection
        idx = self._folder_combo.findText(current)
        if idx >= 0:
            self._folder_combo.setCurrentIndex(idx)
        self._folder_combo.blockSignals(False)
        self._update_delete_folder_enabled()

    def _update_delete_folder_enabled(self, *_args) -> None:
        """Enable the delete-folder button only on real folder rows."""
        text = self._folder_combo.currentText()
        self._delete_folder_btn.setEnabled(text not in ("", "All Bookmarks", "Unfiled"))

    def _delete_folder(self) -> None:
        """Delete the currently-selected bookmark folder.

        Bookmarks filed under it become Unfiled (remove_folder UPDATEs
        favorites.folder = NULL before DELETE FROM favorite_folders).
        Library files on disk are unaffected — bookmark folders and
        library folders are separate concepts after the decoupling.
        """
        name = self._folder_combo.currentText()
        if name in ("", "All Bookmarks", "Unfiled"):
            return
        reply = QMessageBox.question(
            self,
            "Delete Bookmark Folder",
            f"Delete bookmark folder '{name}'?\n\n"
            f"Bookmarks in this folder will become Unfiled. "
            f"Library files on disk are not affected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._db.remove_folder(name)
        # Drop back to All Bookmarks so the now-orphan filter doesn't
        # leave the combo on a missing row.
        self._folder_combo.setCurrentText("All Bookmarks")
        self.refresh()

    def refresh(self, search: str | None = None) -> None:
        self._refresh_folders()

        folder_text = self._folder_combo.currentText()
        folder_filter = None
        if folder_text == "Unfiled":
            folder_filter = ""  # sentinel for NULL folder
        elif folder_text not in ("All Bookmarks", ""):
            folder_filter = folder_text

        if folder_filter == "":
            # Get unfiled: folder IS NULL
            self._bookmarks = [
                f for f in self._db.get_bookmarks(search=search, limit=500)
                if f.folder is None
            ]
        elif folder_filter:
            self._bookmarks = self._db.get_bookmarks(search=search, folder=folder_filter, limit=500)
        else:
            self._bookmarks = self._db.get_bookmarks(search=search, limit=500)

        self._count_label.setText(f"{len(self._bookmarks)} bookmarks")
        thumbs = self._grid.set_posts(len(self._bookmarks))

        # Batch the "is this saved?" check via library_meta. One indexed
        # query gives us a set of every saved post_id, then per-thumb
        # membership is O(1). Format-agnostic — works for digit-stem
        # legacy files AND templated post-refactor saves, where the
        # old find_library_files(post_id)+digit-stem check silently
        # failed because the on-disk basename no longer matches the id.
        saved_ids = self._db.get_saved_post_ids()
        for i, (fav, thumb) in enumerate(zip(self._bookmarks, thumbs)):
            thumb.set_bookmarked(True)
            thumb.set_saved_locally(fav.post_id in saved_ids)
            # Set cached path for drag-and-drop and copy
            if fav.cached_path and Path(fav.cached_path).exists():
                thumb._cached_path = fav.cached_path
            if fav.preview_url:
                self._load_thumb_async(i, fav.preview_url)
            elif fav.cached_path and Path(fav.cached_path).exists():
                pix = QPixmap(fav.cached_path)
                if not pix.isNull():
                    thumb.set_pixmap(pix, fav.cached_path)

    def _load_thumb_async(self, index: int, url: str) -> None:
        # Schedule the download on the persistent event loop instead of
        # spawning a daemon thread that runs its own throwaway loop. This
        # is the fix for the loop-affinity bug where the cache module's
        # shared httpx client would get bound to the throwaway loop and
        # then fail every subsequent use from the persistent loop.
        async def _dl():
            try:
                path = await download_thumbnail(url)
                self._signals.thumb_ready.emit(index, str(path))
            except Exception as e:
                log.warning(f"Bookmark thumb {index} failed: {e}")
        run_on_app_loop(_dl())

    def _on_thumb_ready(self, index: int, path: str) -> None:
        thumbs = self._grid._thumbs
        if 0 <= index < len(thumbs):
            pix = QPixmap(path)
            if not pix.isNull():
                thumbs[index].set_pixmap(pix, path)

    def _on_save_done(self, post_id: int) -> None:
        """Light the saved-locally dot on the thumbnail for post_id."""
        for i, fav in enumerate(self._bookmarks):
            if fav.post_id == post_id and i < len(self._grid._thumbs):
                self._grid._thumbs[i].set_saved_locally(True)
                break

    def _do_search(self) -> None:
        text = self._search_input.text().strip()
        self.refresh(search=text if text else None)

    def _on_selected(self, index: int) -> None:
        if 0 <= index < len(self._bookmarks):
            self.bookmark_selected.emit(self._bookmarks[index])

    def _on_activated(self, index: int) -> None:
        if 0 <= index < len(self._bookmarks):
            self.bookmark_activated.emit(self._bookmarks[index])

    def _bookmark_to_post(self, fav: Bookmark) -> Post:
        """Adapt a Bookmark into a Post for the renderer / save flow.

        The unified save_post_file flow takes a Post (because it's
        called from the browse side too), so bookmarks borrow Post
        shape just for the duration of the save call. Bookmark already
        carries every field the renderer reads — this adapter is the
        one place to update if Post's field set drifts later.
        """
        return Post(
            id=fav.post_id,
            file_url=fav.file_url,
            preview_url=fav.preview_url,
            tags=fav.tags,
            score=fav.score or 0,
            rating=fav.rating,
            source=fav.source,
            tag_categories=fav.tag_categories or {},
        )

    def _save_bookmark_to_library(self, fav: Bookmark, folder: str | None) -> None:
        """Copy a bookmarked image into the library, optionally inside
        a subfolder, routing through the unified save_post_file flow.

        Fixes the latent v0.2.3 bug where bookmark→library copies
        wrote files but never registered library_meta rows — those
        files were on disk but invisible to Library tag-search."""
        from ..core.config import saved_dir, saved_folder_dir
        from ..core.library_save import save_post_file

        if not (fav.cached_path and Path(fav.cached_path).exists()):
            return
        try:
            dest_dir = saved_folder_dir(folder) if folder else saved_dir()
        except ValueError:
            return
        src = Path(fav.cached_path)
        post = self._bookmark_to_post(fav)

        async def _do():
            try:
                await save_post_file(src, post, dest_dir, self._db)
                self._signals.save_done.emit(fav.post_id)
            except Exception as e:
                log.warning(f"Bookmark→library save #{fav.post_id} failed: {e}")

        run_on_app_loop(_do())

    def _copy_to_library_unsorted(self, fav: Bookmark) -> None:
        """Copy a bookmarked image to the unsorted library folder."""
        self._save_bookmark_to_library(fav, None)

    def _copy_to_library(self, fav: Bookmark, folder: str) -> None:
        """Copy a bookmarked image to the named library subfolder."""
        self._save_bookmark_to_library(fav, folder)

    def _new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if ok and name.strip():
            try:
                self._db.add_folder(name.strip())
            except ValueError as e:
                QMessageBox.warning(self, "Invalid Folder Name", str(e))
                return
            self._refresh_folders()

    def _on_context_menu(self, index: int, pos) -> None:
        if index < 0 or index >= len(self._bookmarks):
            return
        fav = self._bookmarks[index]

        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        from .dialogs import save_file

        menu = QMenu(self)

        open_browser = menu.addAction("Open in Browser")
        open_default = menu.addAction("Open in Default App")
        menu.addSeparator()
        save_as = menu.addAction("Save As...")

        # Save to Library / Unsave — mutually exclusive based on
        # whether the post is already in the library.
        from ..core.config import library_folders
        save_lib_menu = None
        save_lib_unsorted = None
        save_lib_new = None
        save_lib_folders = {}
        unsave_lib = None
        if self._db.is_post_in_library(fav.post_id):
            unsave_lib = menu.addAction("Unsave from Library")
        else:
            save_lib_menu = menu.addMenu("Save to Library")
            save_lib_unsorted = save_lib_menu.addAction("Unfiled")
            save_lib_menu.addSeparator()
            for folder in library_folders():
                a = save_lib_menu.addAction(folder)
                save_lib_folders[id(a)] = folder
            save_lib_menu.addSeparator()
            save_lib_new = save_lib_menu.addAction("+ New Folder...")
        copy_file = menu.addAction("Copy File to Clipboard")
        copy_url = menu.addAction("Copy Image URL")
        copy_tags = menu.addAction("Copy Tags")

        # Move to folder submenu
        menu.addSeparator()
        move_menu = menu.addMenu("Move to Folder")
        move_none = move_menu.addAction("Unfiled")
        move_menu.addSeparator()
        folder_actions = {}
        for folder in self._db.get_folders():
            a = move_menu.addAction(folder)
            folder_actions[id(a)] = folder
        move_menu.addSeparator()
        move_new = move_menu.addAction("+ New Folder...")

        menu.addSeparator()
        remove_bookmark = menu.addAction("Remove Bookmark")

        action = menu.exec(pos)
        if not action:
            return

        if action == save_lib_unsorted:
            self._copy_to_library_unsorted(fav)
        elif action == save_lib_new:
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                try:
                    from ..core.config import saved_folder_dir
                    saved_folder_dir(name.strip())
                except ValueError as e:
                    QMessageBox.warning(self, "Invalid Folder Name", str(e))
                    return
                self._copy_to_library(fav, name.strip())
        elif id(action) in save_lib_folders:
            folder_name = save_lib_folders[id(action)]
            self._copy_to_library(fav, folder_name)
        elif action == open_browser:
            self.open_in_browser_requested.emit(fav.site_id, fav.post_id)
        elif action == open_default:
            if fav.cached_path and Path(fav.cached_path).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(fav.cached_path))
        elif action == save_as:
            if fav.cached_path and Path(fav.cached_path).exists():
                from ..core.config import render_filename_template
                from ..core.library_save import save_post_file
                src = Path(fav.cached_path)
                post = self._bookmark_to_post(fav)
                template = self._db.get_setting("library_filename_template")
                default_name = render_filename_template(template, post, src.suffix)
                dest = save_file(self, "Save Image", default_name, f"Images (*{src.suffix})")
                if dest:
                    dest_path = Path(dest)

                    async def _do_save_as():
                        try:
                            await save_post_file(
                                src, post, dest_path.parent, self._db,
                                explicit_name=dest_path.name,
                            )
                        except Exception as e:
                            log.warning(f"Bookmark Save As #{fav.post_id} failed: {e}")

                    run_on_app_loop(_do_save_as())
        elif action == unsave_lib:
            from ..core.cache import delete_from_library
            delete_from_library(fav.post_id, db=self._db)
            for i, f in enumerate(self._bookmarks):
                if f.post_id == fav.post_id and i < len(self._grid._thumbs):
                    self._grid._thumbs[i].set_saved_locally(False)
                    break
            self.bookmarks_changed.emit()
        elif action == copy_file:
            path = fav.cached_path
            if path and Path(path).exists():
                from PySide6.QtCore import QMimeData, QUrl
                from PySide6.QtGui import QPixmap
                mime = QMimeData()
                mime.setUrls([QUrl.fromLocalFile(str(Path(path).resolve()))])
                pix = QPixmap(path)
                if not pix.isNull():
                    mime.setImageData(pix.toImage())
                QApplication.clipboard().setMimeData(mime)
        elif action == copy_url:
            QApplication.clipboard().setText(fav.file_url)
        elif action == copy_tags:
            QApplication.clipboard().setText(fav.tags)
        elif action == move_none:
            self._db.move_bookmark_to_folder(fav.id, None)
            self.refresh()
        elif action == move_new:
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                try:
                    self._db.add_folder(name.strip())
                except ValueError as e:
                    QMessageBox.warning(self, "Invalid Folder Name", str(e))
                    return
                # Pure bookmark organization: file the bookmark, don't
                # touch the library filesystem. Save to Library is now a
                # separate, explicit action.
                self._db.move_bookmark_to_folder(fav.id, name.strip())
                self.refresh()
        elif id(action) in folder_actions:
            folder_name = folder_actions[id(action)]
            self._db.move_bookmark_to_folder(fav.id, folder_name)
            self.refresh()
        elif action == remove_bookmark:
            self._db.remove_bookmark(fav.site_id, fav.post_id)
            self.refresh()
            self.bookmarks_changed.emit()

    def _on_multi_context_menu(self, indices: list, pos) -> None:
        favs = [self._bookmarks[i] for i in indices if 0 <= i < len(self._bookmarks)]
        if not favs:
            return

        from ..core.config import library_folders

        menu = QMenu(self)

        any_unsaved = any(not self._db.is_post_in_library(f.post_id) for f in favs)
        any_saved = any(self._db.is_post_in_library(f.post_id) for f in favs)

        save_lib_menu = None
        save_lib_unsorted = None
        save_lib_new = None
        save_lib_folder_actions: dict[int, str] = {}
        unsave_all = None
        if any_unsaved:
            save_lib_menu = menu.addMenu(f"Save All ({len(favs)}) to Library")
            save_lib_unsorted = save_lib_menu.addAction("Unfiled")
            save_lib_menu.addSeparator()
            for folder in library_folders():
                a = save_lib_menu.addAction(folder)
                save_lib_folder_actions[id(a)] = folder
            save_lib_menu.addSeparator()
            save_lib_new = save_lib_menu.addAction("+ New Folder...")
        if any_saved:
            unsave_all = menu.addAction(f"Unsave All ({len(favs)}) from Library")
        menu.addSeparator()

        # Move to Folder is bookmark organization — reads from the DB.
        move_menu = menu.addMenu(f"Move All ({len(favs)}) to Folder")
        move_none = move_menu.addAction("Unfiled")
        move_menu.addSeparator()
        folder_actions = {}
        for folder in self._db.get_folders():
            a = move_menu.addAction(folder)
            folder_actions[id(a)] = folder

        menu.addSeparator()
        remove_all = menu.addAction(f"Remove All Bookmarks ({len(favs)})")

        action = menu.exec(pos)
        if not action:
            return

        def _save_all_into(folder_name: str | None) -> None:
            for fav in favs:
                if folder_name:
                    self._copy_to_library(fav, folder_name)
                else:
                    self._copy_to_library_unsorted(fav)

        if action == save_lib_unsorted:
            _save_all_into(None)
        elif action == save_lib_new:
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                try:
                    from ..core.config import saved_folder_dir
                    saved_folder_dir(name.strip())
                except ValueError as e:
                    QMessageBox.warning(self, "Invalid Folder Name", str(e))
                    return
                _save_all_into(name.strip())
        elif id(action) in save_lib_folder_actions:
            _save_all_into(save_lib_folder_actions[id(action)])
        elif action == unsave_all:
            from ..core.cache import delete_from_library
            unsaved_ids = set()
            for fav in favs:
                delete_from_library(fav.post_id, db=self._db)
                unsaved_ids.add(fav.post_id)
            for i, fav in enumerate(self._bookmarks):
                if fav.post_id in unsaved_ids and i < len(self._grid._thumbs):
                    self._grid._thumbs[i].set_saved_locally(False)
            self.bookmarks_changed.emit()
        elif action == move_none:
            for fav in favs:
                self._db.move_bookmark_to_folder(fav.id, None)
            self.refresh()
        elif id(action) in folder_actions:
            folder_name = folder_actions[id(action)]
            # Bookmark organization only — Save to Library is separate.
            for fav in favs:
                self._db.move_bookmark_to_folder(fav.id, folder_name)
            self.refresh()
        elif action == remove_all:
            for fav in favs:
                self._db.remove_bookmark(fav.site_id, fav.post_id)
            self.refresh()
            self.bookmarks_changed.emit()
