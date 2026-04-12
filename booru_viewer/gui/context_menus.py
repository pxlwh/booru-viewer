"""Single-post and multi-select right-click context menus."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QApplication, QMenu

if TYPE_CHECKING:
    from .main_window import BooruApp


class ContextMenuHandler:
    """Builds and dispatches context menus for the thumbnail grid."""

    def __init__(self, app: BooruApp) -> None:
        self._app = app

    @staticmethod
    def _is_child_of_menu(action, menu) -> bool:
        parent = action.parent()
        while parent:
            if parent == menu:
                return True
            parent = getattr(parent, 'parent', lambda: None)()
        return False

    def show_single(self, index: int, pos) -> None:
        if index < 0 or index >= len(self._app._posts):
            return
        post = self._app._posts[index]
        menu = QMenu(self._app)

        open_browser = menu.addAction("Open in Browser")
        open_default = menu.addAction("Open in Default App")
        menu.addSeparator()
        save_as = menu.addAction("Save As...")

        from ..core.config import library_folders
        save_lib_menu = None
        save_lib_unsorted = None
        save_lib_new = None
        save_lib_folders = {}
        unsave_lib = None
        if self._app._post_actions.is_post_saved(post.id):
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
        copy_clipboard = menu.addAction("Copy File to Clipboard")
        copy_url = menu.addAction("Copy Image URL")
        copy_tags = menu.addAction("Copy Tags")
        menu.addSeparator()

        fav_action = None
        bm_folder_actions: dict[int, str] = {}
        bm_unfiled = None
        bm_new = None
        if self._app._post_actions.is_current_bookmarked(index):
            fav_action = menu.addAction("Remove Bookmark")
        else:
            fav_menu = menu.addMenu("Bookmark as")
            bm_unfiled = fav_menu.addAction("Unfiled")
            fav_menu.addSeparator()
            for folder in self._app._db.get_folders():
                a = fav_menu.addAction(folder)
                bm_folder_actions[id(a)] = folder
            fav_menu.addSeparator()
            bm_new = fav_menu.addAction("+ New Folder...")
        menu.addSeparator()
        bl_menu = menu.addMenu("Blacklist Tag")
        if post.tag_categories:
            for category, tags in post.tag_categories.items():
                cat_menu = bl_menu.addMenu(category)
                for tag in tags[:30]:
                    cat_menu.addAction(tag)
        else:
            for tag in post.tag_list[:30]:
                bl_menu.addAction(tag)
        bl_post_action = menu.addAction("Blacklist Post")

        action = menu.exec(pos)
        if not action:
            return

        if action == open_browser:
            self._app._open_in_browser(post)
        elif action == open_default:
            self._app._open_in_default(post)
        elif action == save_as:
            self._app._post_actions.save_as(post)
        elif action == save_lib_unsorted:
            self._app._post_actions.save_to_library(post, None)
        elif action == save_lib_new:
            from PySide6.QtWidgets import QInputDialog, QMessageBox
            name, ok = QInputDialog.getText(self._app, "New Folder", "Folder name:")
            if ok and name.strip():
                try:
                    from ..core.config import saved_folder_dir
                    saved_folder_dir(name.strip())
                except ValueError as e:
                    QMessageBox.warning(self._app, "Invalid Folder Name", str(e))
                    return
                self._app._post_actions.save_to_library(post, name.strip())
        elif id(action) in save_lib_folders:
            self._app._post_actions.save_to_library(post, save_lib_folders[id(action)])
        elif action == unsave_lib:
            self._app._post_actions.unsave_from_preview()
        elif action == copy_clipboard:
            self._app._copy_file_to_clipboard()
        elif action == copy_url:
            QApplication.clipboard().setText(post.file_url)
            self._app._status.showMessage("URL copied")
        elif action == copy_tags:
            QApplication.clipboard().setText(post.tags)
            self._app._status.showMessage("Tags copied")
        elif fav_action is not None and action == fav_action:
            self._app._post_actions.toggle_bookmark(index)
        elif bm_unfiled is not None and action == bm_unfiled:
            self._app._post_actions.toggle_bookmark(index, None)
        elif bm_new is not None and action == bm_new:
            from PySide6.QtWidgets import QInputDialog, QMessageBox
            name, ok = QInputDialog.getText(self._app, "New Bookmark Folder", "Folder name:")
            if ok and name.strip():
                try:
                    self._app._db.add_folder(name.strip())
                except ValueError as e:
                    QMessageBox.warning(self._app, "Invalid Folder Name", str(e))
                    return
                self._app._post_actions.toggle_bookmark(index, name.strip())
        elif id(action) in bm_folder_actions:
            self._app._post_actions.toggle_bookmark(index, bm_folder_actions[id(action)])
        elif self._is_child_of_menu(action, bl_menu):
            tag = action.text()
            self._app._db.add_blacklisted_tag(tag)
            self._app._db.set_setting("blacklist_enabled", "1")
            if self._app._preview._current_path and tag in post.tag_list:
                from ..core.cache import cached_path_for
                cp = str(cached_path_for(post.file_url))
                if cp == self._app._preview._current_path:
                    self._app._preview.clear()
                    if self._app._popout_ctrl.window and self._app._popout_ctrl.window.isVisible():
                        self._app._popout_ctrl.window.stop_media()
            self._app._status.showMessage(f"Blacklisted: {tag}")
            self._app._search_ctrl.remove_blacklisted_from_grid(tag=tag)
        elif action == bl_post_action:
            self._app._db.add_blacklisted_post(post.file_url)
            self._app._search_ctrl.remove_blacklisted_from_grid(post_url=post.file_url)
            self._app._status.showMessage(f"Post #{post.id} blacklisted")
            self._app._search_ctrl.do_search()

    def show_multi(self, indices: list, pos) -> None:
        posts = [self._app._posts[i] for i in indices if 0 <= i < len(self._app._posts)]
        if not posts:
            return
        count = len(posts)

        site_id = self._app._site_combo.currentData()
        any_bookmarked = bool(site_id) and any(self._app._db.is_bookmarked(site_id, p.id) for p in posts)
        any_unbookmarked = bool(site_id) and any(not self._app._db.is_bookmarked(site_id, p.id) for p in posts)
        any_saved = any(self._app._post_actions.is_post_saved(p.id) for p in posts)
        any_unsaved = any(not self._app._post_actions.is_post_saved(p.id) for p in posts)

        menu = QMenu(self._app)

        save_menu = None
        save_unsorted = None
        save_new = None
        save_folder_actions: dict[int, str] = {}
        if any_unsaved:
            from ..core.config import library_folders
            save_menu = menu.addMenu(f"Save All to Library ({count})")
            save_unsorted = save_menu.addAction("Unfiled")
            for folder in library_folders():
                a = save_menu.addAction(folder)
                save_folder_actions[id(a)] = folder
            save_menu.addSeparator()
            save_new = save_menu.addAction("+ New Folder...")

        unsave_lib_all = None
        if any_saved:
            unsave_lib_all = menu.addAction(f"Unsave All from Library ({count})")

        if (any_unsaved or any_saved) and (any_unbookmarked or any_bookmarked):
            menu.addSeparator()

        fav_all = None
        if any_unbookmarked:
            fav_all = menu.addAction(f"Bookmark All ({count})")

        unfav_all = None
        if any_bookmarked:
            unfav_all = menu.addAction(f"Remove All Bookmarks ({count})")

        if any_unsaved or any_saved or any_unbookmarked or any_bookmarked:
            menu.addSeparator()
        batch_dl = menu.addAction(f"Download All ({count})...")
        copy_urls = menu.addAction("Copy All URLs")

        action = menu.exec(pos)
        if not action:
            return

        if fav_all is not None and action == fav_all:
            self._app._post_actions.bulk_bookmark(indices, posts)
        elif save_unsorted is not None and action == save_unsorted:
            self._app._post_actions.bulk_save(indices, posts, None)
        elif save_new is not None and action == save_new:
            from PySide6.QtWidgets import QInputDialog, QMessageBox
            name, ok = QInputDialog.getText(self._app, "New Folder", "Folder name:")
            if ok and name.strip():
                try:
                    from ..core.config import saved_folder_dir
                    saved_folder_dir(name.strip())
                except ValueError as e:
                    QMessageBox.warning(self._app, "Invalid Folder Name", str(e))
                    return
                self._app._post_actions.bulk_save(indices, posts, name.strip())
        elif id(action) in save_folder_actions:
            self._app._post_actions.bulk_save(indices, posts, save_folder_actions[id(action)])
        elif unsave_lib_all is not None and action == unsave_lib_all:
            self._app._post_actions.bulk_unsave(indices, posts)
        elif action == batch_dl:
            from .dialogs import select_directory
            dest = select_directory(self._app, "Download to folder")
            if dest:
                self._app._post_actions.batch_download_posts(posts, dest)
        elif unfav_all is not None and action == unfav_all:
            if site_id:
                for post in posts:
                    self._app._db.remove_bookmark(site_id, post.id)
                for idx in indices:
                    if 0 <= idx < len(self._app._grid._thumbs):
                        self._app._grid._thumbs[idx].set_bookmarked(False)
                self._app._grid._clear_multi()
                self._app._status.showMessage(f"Removed {count} bookmarks")
                if self._app._stack.currentIndex() == 1:
                    self._app._bookmarks_view.refresh()
        elif action == copy_urls:
            urls = "\n".join(p.file_url for p in posts)
            QApplication.clipboard().setText(urls)
            self._app._status.showMessage(f"Copied {count} URLs")
