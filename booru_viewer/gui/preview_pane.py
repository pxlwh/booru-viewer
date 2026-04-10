"""Embedded preview pane: image + video, with toolbar and context menu."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QMouseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QStackedWidget,
    QPushButton, QMenu, QInputDialog,
)

from .media.constants import _is_video
from .media.image_viewer import ImageViewer
from .media.video_player import VideoPlayer


# -- Combined Preview (image + video) --

class ImagePreview(QWidget):
    """Combined media preview — auto-switches between image and video."""

    close_requested = Signal()
    open_in_default = Signal()
    open_in_browser = Signal()
    save_to_folder = Signal(str)
    unsave_requested = Signal()
    bookmark_requested = Signal()
    # Bookmark-as: emitted when the user picks a bookmark folder from
    # the toolbar's Bookmark button submenu. Empty string = Unfiled.
    # Mirrors save_to_folder's shape so app.py can route it the same way.
    bookmark_to_folder = Signal(str)
    blacklist_tag_requested = Signal(str)
    blacklist_post_requested = Signal()
    navigate = Signal(int)  # -1 = prev, +1 = next
    play_next_requested = Signal()  # video ended in "Next" mode (wrap-aware)
    fullscreen_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._folders_callback = None
        # Bookmark folders live in a separate name space (DB-backed); the
        # toolbar Bookmark-as submenu reads them via this callback so the
        # preview widget stays decoupled from the Database object.
        self._bookmark_folders_callback = None
        self._current_path: str | None = None
        self._current_post = None  # Post object, set by app.py
        self._current_site_id = None  # site_id for the current post
        self._is_saved = False  # tracks library save state for context menu
        self._is_bookmarked = False  # tracks bookmark state for the button submenu
        self._current_tags: dict[str, list[str]] = {}
        self._current_tag_list: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Action toolbar — above the media, in the layout.
        # 4px horizontal margins so the leftmost button (Bookmark) doesn't
        # sit flush against the preview splitter handle on the left.
        self._toolbar = QWidget()
        tb = QHBoxLayout(self._toolbar)
        tb.setContentsMargins(4, 1, 4, 1)
        tb.setSpacing(4)

        # Compact toolbar buttons. The bundled themes set
        # `QPushButton { padding: 5px 12px }` which eats 24px of horizontal
        # space — too much for these short labels in fixed-width slots.
        # Override with tighter padding inline so the labels (Unbookmark,
        # Unsave, BL Tag, BL Post, Popout) fit cleanly under any theme.
        # Same pattern as the search-bar score buttons in app.py and the
        # settings dialog spinbox +/- buttons.
        _tb_btn_style = "padding: 2px 6px;"

        self._bookmark_btn = QPushButton("Bookmark")
        self._bookmark_btn.setFixedWidth(100)
        self._bookmark_btn.setStyleSheet(_tb_btn_style)
        self._bookmark_btn.clicked.connect(self._on_bookmark_clicked)
        tb.addWidget(self._bookmark_btn)

        self._save_btn = QPushButton("Save")
        # 75 fits "Unsave" (6 chars) cleanly across every bundled theme.
        # The previous 60 was tight enough that some themes clipped the
        # last character on library files where the label flips to Unsave.
        self._save_btn.setFixedWidth(75)
        self._save_btn.setStyleSheet(_tb_btn_style)
        self._save_btn.clicked.connect(self._on_save_clicked)
        tb.addWidget(self._save_btn)

        self._bl_tag_btn = QPushButton("BL Tag")
        self._bl_tag_btn.setFixedWidth(60)
        self._bl_tag_btn.setStyleSheet(_tb_btn_style)
        self._bl_tag_btn.setToolTip("Blacklist a tag")
        self._bl_tag_btn.clicked.connect(self._show_bl_tag_menu)
        tb.addWidget(self._bl_tag_btn)

        self._bl_post_btn = QPushButton("BL Post")
        self._bl_post_btn.setFixedWidth(65)
        self._bl_post_btn.setStyleSheet(_tb_btn_style)
        self._bl_post_btn.setToolTip("Blacklist this post")
        self._bl_post_btn.clicked.connect(self.blacklist_post_requested)
        tb.addWidget(self._bl_post_btn)

        tb.addStretch()

        self._popout_btn = QPushButton("Popout")
        self._popout_btn.setFixedWidth(65)
        self._popout_btn.setStyleSheet(_tb_btn_style)
        self._popout_btn.setToolTip("Open in popout")
        self._popout_btn.clicked.connect(self.fullscreen_requested)
        tb.addWidget(self._popout_btn)

        self._toolbar.hide()  # shown when a post is active
        layout.addWidget(self._toolbar)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, stretch=1)

        # Image viewer (index 0)
        self._image_viewer = ImageViewer()
        self._image_viewer.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._image_viewer.close_requested.connect(self.close_requested)
        self._stack.addWidget(self._image_viewer)

        # Video player (index 1). embed_controls=False keeps the
        # transport controls bar out of the VideoPlayer's own layout —
        # we reparent it below the stack a few lines down so the controls
        # sit *under* the media rather than overlaying it.
        self._video_player = VideoPlayer(embed_controls=False)
        self._video_player.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._video_player.play_next.connect(self.play_next_requested)
        self._stack.addWidget(self._video_player)

        # Place the video controls bar in the preview panel's own layout,
        # underneath the stack. The bar exists as a child of VideoPlayer
        # but is not in any layout (because of embed_controls=False); we
        # adopt it here as a sibling of the stack so it lays out cleanly
        # below the media rather than floating on top of it. The popout
        # uses its own separate VideoPlayer instance and reparents that
        # instance's controls bar to its own central widget as an overlay.
        self._stack_video_controls = self._video_player._controls_bar
        self._stack_video_controls.setParent(self)
        layout.addWidget(self._stack_video_controls)
        # Only visible when the stack is showing the video player.
        self._stack_video_controls.hide()
        self._stack.currentChanged.connect(
            lambda idx: self._stack_video_controls.setVisible(idx == 1)
        )

        # Info label
        self._info_label = QLabel()
        self._info_label.setStyleSheet("padding: 2px 6px;")
        layout.addWidget(self._info_label)

        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def set_post_tags(self, tag_categories: dict[str, list[str]], tag_list: list[str]) -> None:
        self._current_tags = tag_categories
        self._current_tag_list = tag_list

    def _show_bl_tag_menu(self) -> None:
        menu = QMenu(self)
        if self._current_tags:
            for category, tags in self._current_tags.items():
                cat_menu = menu.addMenu(category)
                for tag in tags[:30]:
                    cat_menu.addAction(tag)
        else:
            for tag in self._current_tag_list[:30]:
                menu.addAction(tag)
        action = menu.exec(self._bl_tag_btn.mapToGlobal(self._bl_tag_btn.rect().bottomLeft()))
        if action:
            self.blacklist_tag_requested.emit(action.text())

    def _on_bookmark_clicked(self) -> None:
        """Toolbar Bookmark button — mirrors the browse-tab Bookmark-as
        submenu so the preview pane has the same one-click filing flow.

        When the post is already bookmarked, the button collapses to a
        flat unbookmark action (emits the same signal as before, the
        existing toggle in app.py handles the removal). When not yet
        bookmarked, a popup menu lets the user pick the destination
        bookmark folder — the chosen name is sent through bookmark_to_folder
        and app.py adds the folder + creates the bookmark.
        """
        if self._is_bookmarked:
            self.bookmark_requested.emit()
            return
        menu = QMenu(self)
        unfiled = menu.addAction("Unfiled")
        menu.addSeparator()
        folder_actions: dict[int, str] = {}
        if self._bookmark_folders_callback:
            for folder in self._bookmark_folders_callback():
                a = menu.addAction(folder)
                folder_actions[id(a)] = folder
        menu.addSeparator()
        new_action = menu.addAction("+ New Folder...")
        action = menu.exec(self._bookmark_btn.mapToGlobal(self._bookmark_btn.rect().bottomLeft()))
        if not action:
            return
        if action == unfiled:
            self.bookmark_to_folder.emit("")
        elif action == new_action:
            name, ok = QInputDialog.getText(self, "New Bookmark Folder", "Folder name:")
            if ok and name.strip():
                self.bookmark_to_folder.emit(name.strip())
        elif id(action) in folder_actions:
            self.bookmark_to_folder.emit(folder_actions[id(action)])

    def _on_save_clicked(self) -> None:
        if self._save_btn.text() == "Unsave":
            self.unsave_requested.emit()
            return
        menu = QMenu(self)
        unsorted = menu.addAction("Unfiled")
        menu.addSeparator()
        folder_actions = {}
        if self._folders_callback:
            for folder in self._folders_callback():
                a = menu.addAction(folder)
                folder_actions[id(a)] = folder
        menu.addSeparator()
        new_action = menu.addAction("+ New Folder...")
        action = menu.exec(self._save_btn.mapToGlobal(self._save_btn.rect().bottomLeft()))
        if not action:
            return
        if action == unsorted:
            self.save_to_folder.emit("")
        elif action == new_action:
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                self.save_to_folder.emit(name.strip())
        elif id(action) in folder_actions:
            self.save_to_folder.emit(folder_actions[id(action)])

    def update_bookmark_state(self, bookmarked: bool) -> None:
        self._is_bookmarked = bookmarked
        self._bookmark_btn.setText("Unbookmark" if bookmarked else "Bookmark")
        self._bookmark_btn.setFixedWidth(90 if bookmarked else 80)

    def update_save_state(self, saved: bool) -> None:
        self._is_saved = saved
        self._save_btn.setText("Unsave" if saved else "Save")



    # Keep these for compatibility with app.py accessing them
    @property
    def _pixmap(self):
        return self._image_viewer._pixmap

    @property
    def _info_text(self):
        return self._image_viewer._info_text

    def set_folders_callback(self, callback) -> None:
        self._folders_callback = callback

    def set_bookmark_folders_callback(self, callback) -> None:
        """Wire the bookmark folder list source. Called once from app.py
        with self._db.get_folders. Kept separate from set_folders_callback
        because library and bookmark folders are independent name spaces.
        """
        self._bookmark_folders_callback = callback

    def set_image(self, pixmap: QPixmap, info: str = "") -> None:
        self._video_player.stop()
        self._image_viewer.set_image(pixmap, info)
        self._stack.setCurrentIndex(0)
        self._info_label.setText(info)
        self._current_path = None
        self._toolbar.show()
        self._toolbar.raise_()

    def set_media(self, path: str, info: str = "") -> None:
        """Auto-detect and show image or video."""
        self._current_path = path
        ext = Path(path).suffix.lower()
        if _is_video(path):
            self._image_viewer.clear()
            self._video_player.stop()
            self._video_player.play_file(path, info)
            self._stack.setCurrentIndex(1)
            self._info_label.setText(info)
        elif ext == ".gif":
            self._video_player.stop()
            self._image_viewer.set_gif(path, info)
            self._stack.setCurrentIndex(0)
            self._info_label.setText(info)
        else:
            self._video_player.stop()
            pix = QPixmap(path)
            if not pix.isNull():
                self._image_viewer.set_image(pix, info)
            self._stack.setCurrentIndex(0)
            self._info_label.setText(info)
        self._toolbar.show()
        self._toolbar.raise_()

    def clear(self) -> None:
        self._video_player.stop()
        self._image_viewer.clear()
        self._info_label.setText("")
        self._current_path = None
        self._toolbar.hide()

    def _on_context_menu(self, pos) -> None:
        menu = QMenu(self)

        # Bookmark: unbookmark if already bookmarked, folder submenu if not
        fav_action = None
        bm_folder_actions = {}
        bm_new_action = None
        bm_unfiled = None
        if self._is_bookmarked:
            fav_action = menu.addAction("Unbookmark")
        else:
            bm_menu = menu.addMenu("Bookmark as")
            bm_unfiled = bm_menu.addAction("Unfiled")
            bm_menu.addSeparator()
            if self._bookmark_folders_callback:
                for folder in self._bookmark_folders_callback():
                    a = bm_menu.addAction(folder)
                    bm_folder_actions[id(a)] = folder
            bm_menu.addSeparator()
            bm_new_action = bm_menu.addAction("+ New Folder...")

        save_menu = menu.addMenu("Save to Library")
        save_unsorted = save_menu.addAction("Unfiled")
        save_menu.addSeparator()
        save_folder_actions = {}
        if self._folders_callback:
            for folder in self._folders_callback():
                a = save_menu.addAction(folder)
                save_folder_actions[id(a)] = folder
        save_menu.addSeparator()
        save_new = save_menu.addAction("+ New Folder...")

        unsave_action = None
        if self._is_saved:
            unsave_action = menu.addAction("Unsave from Library")

        menu.addSeparator()
        copy_image = menu.addAction("Copy File to Clipboard")
        open_action = menu.addAction("Open in Default App")
        browser_action = menu.addAction("Open in Browser")

        # Image-specific
        reset_action = None
        if self._stack.currentIndex() == 0:
            reset_action = menu.addAction("Reset View")

        popout_action = None
        if self._current_path:
            popout_action = menu.addAction("Popout")
        clear_action = menu.addAction("Clear Preview")

        action = menu.exec(self.mapToGlobal(pos))
        if not action:
            return
        if action == fav_action:
            self.bookmark_requested.emit()
        elif action == bm_unfiled:
            self.bookmark_to_folder.emit("")
        elif action == bm_new_action:
            name, ok = QInputDialog.getText(self, "New Bookmark Folder", "Folder name:")
            if ok and name.strip():
                self.bookmark_to_folder.emit(name.strip())
        elif id(action) in bm_folder_actions:
            self.bookmark_to_folder.emit(bm_folder_actions[id(action)])
        elif action == save_unsorted:
            self.save_to_folder.emit("")
        elif action == save_new:
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                self.save_to_folder.emit(name.strip())
        elif id(action) in save_folder_actions:
            self.save_to_folder.emit(save_folder_actions[id(action)])
        elif action == copy_image:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtGui import QPixmap as _QP
            pix = self._image_viewer._pixmap
            if pix and not pix.isNull():
                QApplication.clipboard().setPixmap(pix)
            elif self._current_path:
                pix = _QP(self._current_path)
                if not pix.isNull():
                    QApplication.clipboard().setPixmap(pix)
        elif action == open_action:
            self.open_in_default.emit()
        elif action == browser_action:
            self.open_in_browser.emit()
        elif action == reset_action:
            self._image_viewer._fit_to_view()
            self._image_viewer.update()
        elif action == unsave_action:
            self.unsave_requested.emit()
        elif action == popout_action:
            self.fullscreen_requested.emit()
        elif action == clear_action:
            self.close_requested.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            event.ignore()
        else:
            super().mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        # Horizontal tilt navigates between posts on either stack
        tilt = event.angleDelta().x()
        if tilt > 30:
            self.navigate.emit(-1)
            return
        if tilt < -30:
            self.navigate.emit(1)
            return
        if self._stack.currentIndex() == 1:
            delta = event.angleDelta().y()
            if delta:
                vol = max(0, min(100, self._video_player.volume + (5 if delta > 0 else -5)))
                self._video_player.volume = vol
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._stack.currentIndex() == 0:
            self._image_viewer.keyPressEvent(event)
        elif event.key() == Qt.Key.Key_Space:
            self._video_player._toggle_play()
        elif event.key() == Qt.Key.Key_Period:
            self._video_player._seek_relative(1800)
        elif event.key() == Qt.Key.Key_Comma:
            self._video_player._seek_relative(-1800)
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_H):
            self.navigate.emit(-1)
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_L):
            self.navigate.emit(1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
