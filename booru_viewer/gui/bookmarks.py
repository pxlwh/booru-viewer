"""Bookmarks browser widget with folder support."""

from __future__ import annotations

import logging
import threading
import asyncio
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
from ..core.cache import download_thumbnail
from .grid import ThumbnailGrid

log = logging.getLogger("booru")


class BookmarkThumbSignals(QObject):
    thumb_ready = Signal(int, str)


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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Top bar: folder selector + search.
        # 4px right margin so the rightmost button doesn't sit flush
        # against the preview splitter handle.
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 4, 0)

        # Compact button padding matches the rest of the app's narrow
        # toolbar buttons (search bar score field, settings spinbox +/-,
        # preview toolbar). The bundled themes' default `padding: 5px 12px`
        # is too wide for short labels in fixed-width slots.
        # min-height 22px gives a total height of 30px (22 + 3+3 padding +
        # 1+1 border), matching the inputs/combos in the same row so the
        # whole toolbar lines up at one consistent height.
        _btn_style = "padding: 3px 6px; min-height: 22px;"

        self._folder_combo = QComboBox()
        self._folder_combo.setMinimumWidth(120)
        self._folder_combo.currentTextChanged.connect(lambda _: self.refresh())
        top.addWidget(self._folder_combo)

        manage_btn = QPushButton("+ Folder")
        manage_btn.setToolTip("New folder")
        manage_btn.setFixedWidth(75)
        manage_btn.setStyleSheet(_btn_style)
        manage_btn.clicked.connect(self._new_folder)
        top.addWidget(manage_btn)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search bookmarks by tag (live, Enter to commit)")
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

        from ..core.config import saved_dir, saved_folder_dir, MEDIA_EXTENSIONS
        for i, (fav, thumb) in enumerate(zip(self._bookmarks, thumbs)):
            thumb.set_bookmarked(True)
            # Check if saved to library
            saved = False
            if fav.folder:
                saved = any(
                    (saved_folder_dir(fav.folder) / f"{fav.post_id}{ext}").exists()
                    for ext in MEDIA_EXTENSIONS
                )
            else:
                saved = any(
                    (saved_dir() / f"{fav.post_id}{ext}").exists()
                    for ext in MEDIA_EXTENSIONS
                )
            thumb.set_saved_locally(saved)
            # Set cached path for drag-and-drop and copy
            if fav.cached_path and Path(fav.cached_path).exists():
                thumb._cached_path = fav.cached_path
            if fav.preview_url:
                self._load_thumb_async(i, fav.preview_url)
            elif fav.cached_path and Path(fav.cached_path).exists():
                pix = QPixmap(fav.cached_path)
                if not pix.isNull():
                    thumb.set_pixmap(pix)

    def _load_thumb_async(self, index: int, url: str) -> None:
        async def _dl():
            try:
                path = await download_thumbnail(url)
                self._signals.thumb_ready.emit(index, str(path))
            except Exception as e:
                log.warning(f"Bookmark thumb {index} failed: {e}")
        threading.Thread(target=lambda: asyncio.run(_dl()), daemon=True).start()

    def _on_thumb_ready(self, index: int, path: str) -> None:
        thumbs = self._grid._thumbs
        if 0 <= index < len(thumbs):
            pix = QPixmap(path)
            if not pix.isNull():
                thumbs[index].set_pixmap(pix)

    def _do_search(self) -> None:
        text = self._search_input.text().strip()
        self.refresh(search=text if text else None)

    def _on_selected(self, index: int) -> None:
        if 0 <= index < len(self._bookmarks):
            self.bookmark_selected.emit(self._bookmarks[index])

    def _on_activated(self, index: int) -> None:
        if 0 <= index < len(self._bookmarks):
            self.bookmark_activated.emit(self._bookmarks[index])

    def _copy_to_library_unsorted(self, fav: Bookmark) -> None:
        """Copy a bookmarked image to the unsorted library folder."""
        from ..core.config import saved_dir
        if fav.cached_path and Path(fav.cached_path).exists():
            import shutil
            src = Path(fav.cached_path)
            dest = saved_dir() / f"{fav.post_id}{src.suffix}"
            if not dest.exists():
                shutil.copy2(src, dest)

    def _copy_to_library(self, fav: Bookmark, folder: str) -> None:
        """Copy a bookmarked image to the library folder on disk."""
        from ..core.config import saved_folder_dir
        if fav.cached_path and Path(fav.cached_path).exists():
            import shutil
            src = Path(fav.cached_path)
            dest = saved_folder_dir(folder) / f"{fav.post_id}{src.suffix}"
            if not dest.exists():
                shutil.copy2(src, dest)

    def _new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if ok and name.strip():
            self._db.add_folder(name.strip())
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

        # Save to Library submenu
        save_lib_menu = menu.addMenu("Save to Library")
        save_lib_unsorted = save_lib_menu.addAction("Unsorted")
        save_lib_menu.addSeparator()
        save_lib_folders = {}
        for folder in self._db.get_folders():
            a = save_lib_menu.addAction(folder)
            save_lib_folders[id(a)] = folder
        save_lib_menu.addSeparator()
        save_lib_new = save_lib_menu.addAction("+ New Folder...")

        unsave_lib = None
        # Only show unsave if the post is saved locally
        from ..core.config import saved_dir, saved_folder_dir, MEDIA_EXTENSIONS
        _saved = False
        _sd = saved_dir()
        if _sd.exists():
            _saved = any((_sd / f"{fav.post_id}{ext}").exists() for ext in MEDIA_EXTENSIONS)
        if not _saved:
            for folder in self._db.get_folders():
                d = saved_folder_dir(folder)
                if d.exists() and any((d / f"{fav.post_id}{ext}").exists() for ext in MEDIA_EXTENSIONS):
                    _saved = True
                    break
        if _saved:
            unsave_lib = menu.addAction("Unsave from Library")
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
            self.refresh()
        elif action == save_lib_new:
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                self._db.add_folder(name.strip())
                self._copy_to_library(fav, name.strip())
                self._db.move_bookmark_to_folder(fav.id, name.strip())
                self.refresh()
        elif id(action) in save_lib_folders:
            folder_name = save_lib_folders[id(action)]
            self._copy_to_library(fav, folder_name)
            self.refresh()
        elif action == open_browser:
            self.open_in_browser_requested.emit(fav.site_id, fav.post_id)
        elif action == open_default:
            if fav.cached_path and Path(fav.cached_path).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(fav.cached_path))
        elif action == save_as:
            if fav.cached_path and Path(fav.cached_path).exists():
                src = Path(fav.cached_path)
                dest = save_file(self, "Save Image", f"post_{fav.post_id}{src.suffix}", f"Images (*{src.suffix})")
                if dest:
                    import shutil
                    shutil.copy2(src, dest)
        elif action == unsave_lib:
            from ..core.cache import delete_from_library
            if delete_from_library(fav.post_id, fav.folder):
                self.refresh()
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
                self._db.add_folder(name.strip())
                self._db.move_bookmark_to_folder(fav.id, name.strip())
                self._copy_to_library(fav, name.strip())
                self.refresh()
        elif id(action) in folder_actions:
            folder_name = folder_actions[id(action)]
            self._db.move_bookmark_to_folder(fav.id, folder_name)
            self._copy_to_library(fav, folder_name)
            self.refresh()
        elif action == remove_bookmark:
            self._db.remove_bookmark(fav.site_id, fav.post_id)
            self.refresh()
            self.bookmarks_changed.emit()

    def _on_multi_context_menu(self, indices: list, pos) -> None:
        favs = [self._bookmarks[i] for i in indices if 0 <= i < len(self._bookmarks)]
        if not favs:
            return

        menu = QMenu(self)
        save_all = menu.addAction(f"Save All ({len(favs)}) to Library")
        unsave_all = menu.addAction(f"Unsave All ({len(favs)}) from Library")
        menu.addSeparator()

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

        if action == save_all:
            for fav in favs:
                if fav.folder:
                    self._copy_to_library(fav, fav.folder)
                else:
                    self._copy_to_library_unsorted(fav)
            self.refresh()
        elif action == unsave_all:
            from ..core.cache import delete_from_library
            for fav in favs:
                delete_from_library(fav.post_id, fav.folder)
            self.refresh()
            self.bookmarks_changed.emit()
        elif action == move_none:
            for fav in favs:
                self._db.move_bookmark_to_folder(fav.id, None)
            self.refresh()
        elif id(action) in folder_actions:
            folder_name = folder_actions[id(action)]
            for fav in favs:
                self._db.move_bookmark_to_folder(fav.id, folder_name)
                self._copy_to_library(fav, folder_name)
            self.refresh()
        elif action == remove_all:
            for fav in favs:
                self._db.remove_bookmark(fav.site_id, fav.post_id)
            self.refresh()
            self.bookmarks_changed.emit()
