"""Main BooruApp window class."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QUrl, Property
from PySide6.QtGui import QPixmap, QAction, QKeySequence, QDesktopServices, QShortcut, QColor
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QStackedWidget,
    QComboBox,
    QLabel,
    QPushButton,
    QStatusBar,
    QSplitter,
    QMessageBox,
    QTextEdit,
    QMenu,
    QFileDialog,
    QSpinBox,
    QScrollArea,
    QProgressBar,
)

from dataclasses import dataclass, field

from ..core.db import Database, Site
from ..core.api.base import BooruClient, Post
from ..core.api.detect import client_for_type
from ..core.cache import download_image, download_thumbnail, cache_size_bytes, evict_oldest, evict_oldest_thumbnails
from ..core.config import MEDIA_EXTENSIONS

from .grid import ThumbnailGrid
from .preview_pane import ImagePreview
from .search import SearchBar
from .sites import SiteManagerDialog
from .bookmarks import BookmarksView
from .library import LibraryView
from .settings import SettingsDialog

# Imports added by the refactor: classes that used to live in app.py but
# now live in their canonical sibling modules. Originally these resolved
# through the app.py module namespace; main_window.py imports them
# explicitly so the same bare-name lookups inside BooruApp methods
# (`SearchState(...)`, `LogHandler(...)`, etc.) keep resolving the same
# class objects.
from .search_state import SearchState
from .log_handler import LogHandler
from .async_signals import AsyncSignals
from .info_panel import InfoPanel
from .window_state import WindowStateController
from .privacy import PrivacyController
from .search_controller import SearchController
from .media_controller import MediaController

log = logging.getLogger("booru")


# -- Main App --

class BooruApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("booru-viewer")
        self.setMinimumSize(900, 600)
        self.resize(1200, 800)

        self._db = Database()
        # Apply custom library directory if set
        lib_dir = self._db.get_setting("library_dir")
        if lib_dir:
            from ..core.config import set_library_dir
            set_library_dir(Path(lib_dir))
        # Apply saved thumbnail size
        saved_thumb = self._db.get_setting_int("thumbnail_size")
        if saved_thumb:
            import booru_viewer.gui.grid as grid_mod
            grid_mod.THUMB_SIZE = saved_thumb
        self._current_site: Site | None = None
        self._posts: list[Post] = []
        self._signals = AsyncSignals()

        self._async_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(target=self._async_loop.run_forever, daemon=True)
        self._async_thread.start()

        # Register the persistent loop as the process-wide app loop. Anything
        # that wants to schedule async work — `gui/sites.py`, `gui/bookmarks.py`,
        # any future helper — calls `core.concurrency.run_on_app_loop` which
        # uses this same loop. The whole point of PR2 is to never run async
        # code on a throwaway loop again.
        from ..core.concurrency import set_app_loop
        set_app_loop(self._async_loop)

        # Reset shared HTTP clients from previous session
        from ..core.api.base import BooruClient
        from ..core.api.e621 import E621Client
        BooruClient._shared_client = None
        E621Client._e621_client = None
        E621Client._e621_to_close = []
        import booru_viewer.core.cache as _cache_mod
        _cache_mod._shared_client = None

        self._setup_signals()
        self._setup_ui()
        self._setup_menu()
        self._load_sites()
        # One-shot orphan cleanup — must run after DB + library dir are
        # configured, before the library tab is first populated.
        orphans = self._db.reconcile_library_meta()
        if orphans:
            log.info("Reconciled %d orphan library_meta rows", orphans)
        # Debounced save for the main window state — fires from resizeEvent
        # (and from the splitter timer's flush on close). Uses the same
        # 300ms debounce pattern as the splitter saver.
        self._window_state = WindowStateController(self)
        self._privacy = PrivacyController(self)
        self._search_ctrl = SearchController(self)
        self._media_ctrl = MediaController(self)
        self._main_window_save_timer = QTimer(self)
        self._main_window_save_timer.setSingleShot(True)
        self._main_window_save_timer.setInterval(300)
        self._main_window_save_timer.timeout.connect(self._window_state.save_main_window_state)
        # Restore window state (geometry, floating) on the next event-loop
        # iteration — by then main.py has called show() and the window has
        # been registered with the compositor.
        QTimer.singleShot(0, self._window_state.restore_main_window_state)

    def _setup_signals(self) -> None:
        Q = Qt.ConnectionType.QueuedConnection
        s = self._signals
        s.search_done.connect(self._search_ctrl.on_search_done, Q)
        s.search_append.connect(self._search_ctrl.on_search_append, Q)
        s.search_error.connect(self._search_ctrl.on_search_error, Q)
        s.thumb_done.connect(self._search_ctrl.on_thumb_done, Q)
        s.image_done.connect(self._media_ctrl.on_image_done, Q)
        s.image_error.connect(self._on_image_error, Q)
        s.video_stream.connect(self._media_ctrl.on_video_stream, Q)
        s.bookmark_done.connect(self._on_bookmark_done, Q)
        s.bookmark_error.connect(self._on_bookmark_error, Q)
        s.autocomplete_done.connect(self._search_ctrl.on_autocomplete_done, Q)
        s.batch_progress.connect(self._on_batch_progress, Q)
        s.batch_done.connect(self._on_batch_done, Q)
        s.download_progress.connect(self._media_ctrl.on_download_progress, Q)
        s.prefetch_progress.connect(self._media_ctrl.on_prefetch_progress, Q)
        s.categories_updated.connect(self._on_categories_updated, Q)

    def _get_category_fetcher(self):
        """Return the CategoryFetcher for the active site, or None."""
        client = self._make_client()
        return client.category_fetcher if client else None

    def _ensure_post_categories_async(self, post) -> None:
        """Schedule an async ensure_categories for the post.

        No-op if the active client doesn't have a CategoryFetcher
        (Danbooru/e621 categorize inline, no fetcher needed).

        Sets _categories_pending on the info panel so it skips the
        flat-tag fallback render (avoids the flat→categorized
        re-layout hitch). The flag clears when categories arrive.
        """
        client = self._make_client()
        if client is None or client.category_fetcher is None:
            self._info_panel._categories_pending = False
            return
        self._info_panel._categories_pending = True
        fetcher = client.category_fetcher
        signals = self._signals

        async def _do():
            try:
                await fetcher.ensure_categories(post)
                if post.tag_categories:
                    signals.categories_updated.emit(post)
            except Exception as e:
                log.debug(f"ensure_categories failed: {e}")

        asyncio.run_coroutine_threadsafe(_do(), self._async_loop)

    def _on_categories_updated(self, post) -> None:
        """Background tag-category fill completed for a post.

        Re-render the info panel and preview pane if either is
        currently showing this post. The post object was mutated in
        place by the CategoryFetcher, so we just call the panel's
        set_post / set_post_tags again to pick up the new dict.
        """
        self._info_panel._categories_pending = False
        if not post or not post.tag_categories:
            return
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts) and self._posts[idx].id == post.id:
            self._info_panel.set_post(post)
            self._preview.set_post_tags(post.tag_categories, post.tag_list)

    def _on_image_error(self, e: str) -> None:
        self._dl_progress.hide()
        self._status.showMessage(f"Error: {e}")

    def _on_bookmark_error(self, e: str) -> None:
        self._status.showMessage(f"Error: {e}")

    def _run_async(self, coro_func, *args):
        future = asyncio.run_coroutine_threadsafe(coro_func(*args), self._async_loop)
        future.add_done_callback(self._on_async_done)

    @staticmethod
    def _on_async_done(future):
        try:
            future.result()
        except Exception as e:
            log.error(f"Async worker failed: {e}")

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Top bar: site selector + rating + search
        top = QHBoxLayout()

        self._site_combo = QComboBox()
        self._site_combo.setMinimumWidth(150)
        self._site_combo.currentIndexChanged.connect(self._on_site_changed)
        top.addWidget(self._site_combo)

        # Rating filter
        self._rating_combo = QComboBox()
        self._rating_combo.addItems(["All", "General", "Sensitive", "Questionable", "Explicit"])
        self._rating_combo.setMinimumWidth(100)
        self._rating_combo.currentTextChanged.connect(self._on_rating_changed)
        top.addWidget(self._rating_combo)

        # Media type filter
        self._media_filter = QComboBox()
        self._media_filter.addItems(["All", "Animated", "Video", "GIF", "Audio"])
        self._media_filter.setToolTip("Filter by media type")
        self._media_filter.setFixedWidth(90)
        top.addWidget(self._media_filter)

        # Score filter — type the value directly. Spinbox arrows hidden
        # since the field is small enough to type into and the +/- buttons
        # were just visual noise. setFixedHeight(23) overrides Qt's
        # QSpinBox sizeHint which still reserves vertical space for the
        # arrow buttons internally even when `setButtonSymbols(NoButtons)`
        # is set, leaving the spinbox 3px taller than the surrounding
        # combos/inputs/buttons in the top toolbar (26 vs 23).
        score_label = QLabel("Score≥")
        top.addWidget(score_label)
        self._score_spin = QSpinBox()
        self._score_spin.setRange(0, 99999)
        self._score_spin.setValue(0)
        self._score_spin.setFixedWidth(40)
        self._score_spin.setFixedHeight(23)
        self._score_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        top.addWidget(self._score_spin)

        page_label = QLabel("Page")
        top.addWidget(page_label)
        self._page_spin = QSpinBox()
        self._page_spin.setRange(1, 99999)
        self._page_spin.setValue(1)
        self._page_spin.setFixedWidth(40)
        self._page_spin.setFixedHeight(23)  # match the surrounding 23px row
        self._page_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        top.addWidget(self._page_spin)

        self._search_bar = SearchBar(db=self._db)
        self._search_bar.search_requested.connect(self._search_ctrl.on_search)
        self._search_bar.autocomplete_requested.connect(self._search_ctrl.request_autocomplete)
        top.addWidget(self._search_bar, stretch=1)

        layout.addLayout(top)

        # Nav bar
        nav = QHBoxLayout()
        self._browse_btn = QPushButton("Browse")
        self._browse_btn.setCheckable(True)
        self._browse_btn.setChecked(True)
        self._browse_btn.clicked.connect(lambda: self._switch_view(0))
        nav.addWidget(self._browse_btn)

        self._bookmark_btn = QPushButton("Bookmarks")
        self._bookmark_btn.setCheckable(True)
        self._bookmark_btn.clicked.connect(lambda: self._switch_view(1))
        nav.addWidget(self._bookmark_btn)

        self._library_btn = QPushButton("Library")
        self._library_btn.setCheckable(True)
        self._library_btn.setFixedWidth(80)
        self._library_btn.clicked.connect(lambda: self._switch_view(2))
        nav.addWidget(self._library_btn)

        layout.addLayout(nav)

        # Main content
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: stacked views
        self._stack = QStackedWidget()

        self._grid = ThumbnailGrid()
        self._grid.post_selected.connect(self._on_post_selected)
        self._grid.post_activated.connect(self._media_ctrl.on_post_activated)
        self._grid.context_requested.connect(self._on_context_menu)
        self._grid.multi_context_requested.connect(self._on_multi_context_menu)
        self._grid.nav_past_end.connect(self._search_ctrl.on_nav_past_end)
        self._grid.nav_before_start.connect(self._search_ctrl.on_nav_before_start)
        self._stack.addWidget(self._grid)

        self._bookmarks_view = BookmarksView(self._db)
        self._bookmarks_view.bookmark_selected.connect(self._on_bookmark_selected)
        self._bookmarks_view.bookmark_activated.connect(self._on_bookmark_activated)
        self._bookmarks_view.bookmarks_changed.connect(self._refresh_browse_saved_dots)
        self._bookmarks_view.open_in_browser_requested.connect(
            lambda site_id, post_id: self._open_post_id_in_browser(post_id, site_id=site_id)
        )
        self._stack.addWidget(self._bookmarks_view)

        self._library_view = LibraryView(db=self._db)
        self._library_view.file_selected.connect(self._on_library_selected)
        self._library_view.file_activated.connect(self._on_library_activated)
        self._library_view.files_deleted.connect(self._on_library_files_deleted)
        self._stack.addWidget(self._library_view)

        self._splitter.addWidget(self._stack)

        # Right: preview + info (vertical split)
        self._right_splitter = right = QSplitter(Qt.Orientation.Vertical)

        self._preview = ImagePreview()
        self._preview.close_requested.connect(self._close_preview)
        self._preview.open_in_default.connect(self._open_preview_in_default)
        self._preview.open_in_browser.connect(self._open_preview_in_browser)
        self._preview.bookmark_requested.connect(self._bookmark_from_preview)
        self._preview.bookmark_to_folder.connect(self._bookmark_to_folder_from_preview)
        self._preview.save_to_folder.connect(self._save_from_preview)
        self._preview.unsave_requested.connect(self._unsave_from_preview)
        self._preview.blacklist_tag_requested.connect(self._blacklist_tag_from_popout)
        self._preview.blacklist_post_requested.connect(self._blacklist_post_from_popout)
        self._preview.navigate.connect(self._navigate_preview)
        self._preview.play_next_requested.connect(self._on_video_end_next)
        self._preview.fullscreen_requested.connect(self._open_fullscreen_preview)
        # Library folders come from the filesystem (subdirs of saved_dir),
        # not the bookmark folders DB table — those are separate concepts.
        from ..core.config import library_folders
        self._preview.set_folders_callback(library_folders)
        # Bookmark folders feed the toolbar Bookmark-as submenu, sourced
        # from the DB so it stays in sync with the bookmarks tab combo.
        self._preview.set_bookmark_folders_callback(self._db.get_folders)
        self._fullscreen_window = None
        # Wide enough that the preview toolbar (Bookmark, Save, BL Tag,
        # BL Post, [stretch], Popout) has room to lay out all five buttons
        # at their fixed widths plus spacing without clipping the rightmost
        # one or compressing the row visually.
        self._preview.setMinimumWidth(380)
        right.addWidget(self._preview)

        self._dl_progress = QProgressBar()
        self._dl_progress.setMaximumHeight(6)
        self._dl_progress.setTextVisible(False)
        self._dl_progress.hide()
        right.addWidget(self._dl_progress)

        self._info_panel = InfoPanel()
        self._info_panel.tag_clicked.connect(self._on_tag_clicked)
        self._info_panel.setMinimumHeight(100)
        self._info_panel.hide()
        right.addWidget(self._info_panel)

        # Restore the right splitter sizes (preview / dl_progress / info)
        # from the persisted state. Falls back to the historic default if
        # nothing is saved or the saved string is malformed.
        saved_right = self._db.get_setting("right_splitter_sizes")
        right_applied = False
        if saved_right:
            try:
                parts = [int(p) for p in saved_right.split(",")]
                if len(parts) == 3 and all(p >= 0 for p in parts) and sum(parts) > 0:
                    right.setSizes(parts)
                    right_applied = True
            except ValueError:
                pass
        if not right_applied:
            right.setSizes([500, 0, 200])

        # Restore info panel visibility from the persisted state.
        if self._db.get_setting_bool("info_panel_visible"):
            self._info_panel.show()

        # Flag set during popout open/close so the splitter saver below
        # doesn't persist the temporary [0, 0, 1000] state the popout
        # uses to give the info panel the full right column.
        self._popout_active = False

        # Debounced saver for the right splitter (same pattern as main).
        self._right_splitter_save_timer = QTimer(self)
        self._right_splitter_save_timer.setSingleShot(True)
        self._right_splitter_save_timer.setInterval(300)
        self._right_splitter_save_timer.timeout.connect(self._window_state.save_right_splitter_sizes)
        right.splitterMoved.connect(
            lambda *_: self._right_splitter_save_timer.start()
        )

        self._splitter.addWidget(right)

        # Restore the persisted main-splitter sizes if present, otherwise
        # fall back to the historic default. The sizes are saved as a
        # comma-separated string in the settings table — same format as
        # slideshow_geometry to keep things consistent.
        saved_main_split = self._db.get_setting("main_splitter_sizes")
        applied = False
        if saved_main_split:
            try:
                parts = [int(p) for p in saved_main_split.split(",")]
                if len(parts) == 2 and all(p >= 0 for p in parts) and sum(parts) > 0:
                    self._splitter.setSizes(parts)
                    applied = True
            except ValueError:
                pass
        if not applied:
            self._splitter.setSizes([600, 500])
        # Debounced save on drag — splitterMoved fires hundreds of times
        # per second, so we restart a 300ms one-shot and save when it stops.
        self._main_splitter_save_timer = QTimer(self)
        self._main_splitter_save_timer.setSingleShot(True)
        self._main_splitter_save_timer.setInterval(300)
        self._main_splitter_save_timer.timeout.connect(self._window_state.save_main_splitter_sizes)
        self._splitter.splitterMoved.connect(
            lambda *_: self._main_splitter_save_timer.start()
        )
        layout.addWidget(self._splitter, stretch=1)

        # Bottom page nav (centered)
        self._bottom_nav = QWidget()
        bottom_nav = QHBoxLayout(self._bottom_nav)
        bottom_nav.setContentsMargins(0, 4, 0, 4)
        bottom_nav.addStretch()
        self._page_label = QLabel("Page 1")
        bottom_nav.addWidget(self._page_label)
        self._prev_page_btn = QPushButton("Prev")
        self._prev_page_btn.setFixedWidth(60)
        self._prev_page_btn.clicked.connect(self._search_ctrl.prev_page)
        bottom_nav.addWidget(self._prev_page_btn)
        self._next_page_btn = QPushButton("Next")
        self._next_page_btn.setFixedWidth(60)
        self._next_page_btn.clicked.connect(self._search_ctrl.next_page)
        bottom_nav.addWidget(self._next_page_btn)
        bottom_nav.addStretch()
        layout.addWidget(self._bottom_nav)

        # Infinite scroll (state lives on _search_ctrl, but UI visibility here)
        if self._search_ctrl._infinite_scroll:
            self._bottom_nav.hide()
        self._grid.reached_bottom.connect(self._search_ctrl.on_reached_bottom)
        self._grid.verticalScrollBar().rangeChanged.connect(self._search_ctrl.on_scroll_range_changed)

        # Log panel
        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setMaximumHeight(150)
        self._log_text.setStyleSheet("font-family: monospace; font-size: 11px;")
        self._log_text.hide()
        layout.addWidget(self._log_text)

        # Hook up logging
        self._log_handler = LogHandler(self._log_text)
        self._log_handler.setLevel(logging.DEBUG)
        logging.getLogger("booru").addHandler(self._log_handler)
        logging.getLogger("booru").setLevel(logging.DEBUG)

        # Status bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

        # Global shortcuts for preview navigation
        QShortcut(QKeySequence("Left"), self, lambda: self._navigate_preview(-1))
        QShortcut(QKeySequence("Right"), self, lambda: self._navigate_preview(1))
        QShortcut(QKeySequence("Ctrl+C"), self, self._copy_file_to_clipboard)

    def _setup_menu(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")

        sites_action = QAction("&Manage Sites...", self)
        sites_action.setShortcut(QKeySequence("Ctrl+S"))
        sites_action.triggered.connect(self._open_site_manager)
        file_menu.addAction(sites_action)

        settings_action = QAction("Se&ttings...", self)
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)

        file_menu.addSeparator()

        self._batch_action = QAction("Batch &Download Page...", self)
        self._batch_action.setShortcut(QKeySequence("Ctrl+D"))
        self._batch_action.triggered.connect(self._batch_download)
        file_menu.addAction(self._batch_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = menu.addMenu("&View")

        info_action = QAction("Toggle &Info Panel", self)
        info_action.setShortcut(QKeySequence("Ctrl+I"))
        info_action.triggered.connect(self._toggle_info)
        view_menu.addAction(info_action)

        log_action = QAction("Toggle &Log", self)
        log_action.setShortcut(QKeySequence("Ctrl+L"))
        log_action.triggered.connect(self._toggle_log)
        view_menu.addAction(log_action)

        view_menu.addSeparator()

        fullscreen_action = QAction("&Fullscreen", self)
        fullscreen_action.setShortcut(QKeySequence("F11"))
        fullscreen_action.triggered.connect(self._toggle_fullscreen)
        view_menu.addAction(fullscreen_action)

        privacy_action = QAction("&Privacy Screen", self)
        privacy_action.setShortcut(QKeySequence("Ctrl+P"))
        privacy_action.triggered.connect(self._privacy.toggle)
        view_menu.addAction(privacy_action)

    def _load_sites(self) -> None:
        self._site_combo.clear()
        for site in self._db.get_sites():
            self._site_combo.addItem(site.name, site.id)
        # Select default site if configured
        default_id = self._db.get_setting_int("default_site_id")
        if default_id:
            idx = self._site_combo.findData(default_id)
            if idx >= 0:
                self._site_combo.setCurrentIndex(idx)

    def _make_client(self) -> BooruClient | None:
        if not self._current_site:
            return None
        s = self._current_site
        return client_for_type(
            s.api_type, s.url, s.api_key, s.api_user,
            db=self._db, site_id=s.id,
        )

    def _on_site_changed(self, index: int) -> None:
        if index < 0:
            self._current_site = None
            return
        site_id = self._site_combo.currentData()
        sites = self._db.get_sites()
        site = next((s for s in sites if s.id == site_id), None)
        if not site:
            return
        self._current_site = site
        self._status.showMessage(f"Connected to {site.name}")
        # Reset browse state for the new site — stale page numbers
        # and results from the previous site shouldn't carry over.
        self._page_spin.setValue(1)
        self._posts.clear()
        self._grid.set_posts(0)
        self._preview.clear()
        self._search_ctrl.reset()

    def _on_rating_changed(self, text: str) -> None:
        self._search_ctrl._current_rating = text.lower()

    def _switch_view(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._browse_btn.setChecked(index == 0)
        self._bookmark_btn.setChecked(index == 1)
        self._library_btn.setChecked(index == 2)
        # Batch Download (Ctrl+D / File menu) only makes sense on browse —
        # bookmarks and library tabs already show local files, downloading
        # them again is meaningless. Disabling the QAction also disables
        # its keyboard shortcut.
        self._batch_action.setEnabled(index == 0)
        # Clear grid selections and current post to prevent cross-tab action conflicts
        # Preview media stays visible but actions are disabled until a new post is selected
        self._grid.clear_selection()
        self._bookmarks_view._grid.clear_selection()
        self._library_view._grid.clear_selection()
        self._preview._current_post = None
        self._preview._current_site_id = None
        is_library = index == 2
        self._preview.update_bookmark_state(False)
        # On the library tab the Save button is the only toolbar action
        # left visible (Bookmark / BL Tag / BL Post are hidden a few lines
        # down). Library files are saved by definition, so the button
        # should read "Unsave" the entire time the user is in that tab —
        # forcing the state to True here makes that true even before the
        # user clicks anything (the toolbar might already be showing old
        # media from the previous tab; this is fine because the same media
        # is also in the library if it was just saved).
        self._preview.update_save_state(is_library)
        # Show/hide preview toolbar buttons per tab
        self._preview._bookmark_btn.setVisible(not is_library)
        self._preview._bl_tag_btn.setVisible(not is_library)
        self._preview._bl_post_btn.setVisible(not is_library)
        if index == 1:
            self._bookmarks_view.refresh()
            self._bookmarks_view._grid.setFocus()
        elif index == 2:
            self._library_view.refresh()
        else:
            self._grid.setFocus()

    def _on_tag_clicked(self, tag: str) -> None:
        self._preview.clear()
        self._switch_view(0)
        self._search_bar.set_text(tag)
        self._search_ctrl.on_search(tag)

    # (Search methods moved to search_controller.py)

    # (_on_reached_bottom moved to search_controller.py)

    # (_scroll_next_page, _scroll_prev_page moved to search_controller.py)

    # (_build_search_tags through _on_autocomplete_done moved to search_controller.py)
    # -- Post selection / preview --

    def _on_post_selected(self, index: int) -> None:
        multi = self._grid.selected_indices
        if len(multi) > 1:
            self._status.showMessage(f"{len(multi)} posts selected")
            return
        if 0 <= index < len(self._posts):
            post = self._posts[index]
            self._status.showMessage(
                f"#{post.id}  {post.width}x{post.height}  score:{post.score}  [{post.rating}]  {Path(post.file_url.split('?')[0]).suffix.lstrip('.').upper() if post.file_url else ''}"
                + (f"  {post.created_at}" if post.created_at else "")
            )
            if self._info_panel.isVisible():
                # Signal the info panel whether a category fetch is
                # about to fire so it skips the flat-tag fallback
                # (avoids the flat→categorized re-layout flash).
                if not post.tag_categories:
                    client = self._make_client()
                    self._info_panel._categories_pending = (
                        client is not None and client.category_fetcher is not None
                    )
                else:
                    self._info_panel._categories_pending = False
                self._info_panel.set_post(post)
            self._media_ctrl.on_post_activated(index)


    def _update_fullscreen(self, path: str, info: str) -> None:
        """Sync the fullscreen window with the current preview media."""
        if self._fullscreen_window and self._fullscreen_window.isVisible():
            self._preview._video_player.stop()
            cp = self._preview._current_post
            w = cp.width if cp else 0
            h = cp.height if cp else 0
            self._fullscreen_window.set_media(path, info, width=w, height=h)
            show_full = self._stack.currentIndex() != 2
            self._fullscreen_window.set_toolbar_visibility(
                bookmark=show_full,
                save=True,
                bl_tag=show_full,
                bl_post=show_full,
            )
            self._update_fullscreen_state()

    def _update_fullscreen_state(self) -> None:
        """Update popout button states by mirroring the embedded preview."""
        if not self._fullscreen_window:
            return
        self._fullscreen_window.update_state(
            self._preview._is_bookmarked,
            self._preview._is_saved,
        )
        post = self._preview._current_post
        if post is not None:
            self._fullscreen_window.set_post_tags(
                post.tag_categories or {}, post.tag_list
            )

    def _show_library_post(self, path: str) -> None:
        # Read actual image dimensions so the popout can pre-fit and
        # set keep_aspect_ratio. library_meta doesn't store w/h, so
        # without this the popout gets 0/0 and skips the aspect lock.
        img_w, img_h = MediaController.image_dimensions(path)
        self._media_ctrl.set_preview_media(path, Path(path).name)
        self._set_library_info(path)
        # Build a Post from library metadata so toolbar actions work.
        # Templated filenames go through library_meta.filename;
        # legacy digit-stem files use int(stem).
        # width/height come from the file itself (library_meta doesn't
        # store them) so the popout can pre-fit and set keep_aspect_ratio.
        post_id = self._post_id_from_library_path(Path(path))
        if post_id is not None:
            from ..core.api.base import Post
            meta = self._db.get_library_meta(post_id) or {}
            post = Post(
                id=post_id, file_url=meta.get("file_url", ""),
                preview_url=None, tags=meta.get("tags", ""),
                score=meta.get("score", 0), rating=meta.get("rating"),
                source=meta.get("source"),
                tag_categories=meta.get("tag_categories", {}),
                width=img_w, height=img_h,
            )
            self._preview._current_post = post
            self._preview._current_site_id = self._site_combo.currentData()
            self._preview.update_save_state(True)
            self._preview.set_post_tags(post.tag_categories, post.tag_list)
        else:
            self._preview._current_post = None
            self._preview.update_save_state(True)
        # _update_fullscreen reads cp.width/cp.height from _current_post,
        # so it runs AFTER the Post is constructed with real dimensions.
        self._update_fullscreen(path, Path(path).name)

    def _on_bookmark_selected(self, fav) -> None:
        self._status.showMessage(f"Bookmark #{fav.post_id}")
        # Show bookmark tags in info panel
        from ..core.api.base import Post
        cats = fav.tag_categories or {}
        if not cats:
            meta = self._db.get_library_meta(fav.post_id)
            cats = meta.get("tag_categories", {}) if meta else {}
        p = Post(
            id=fav.post_id, file_url=fav.file_url or "",
            preview_url=fav.preview_url, tags=fav.tags or "",
            score=fav.score or 0, rating=fav.rating,
            source=fav.source, tag_categories=cats,
        )
        self._info_panel.set_post(p)
        self._on_bookmark_activated(fav)

    def _on_bookmark_activated(self, fav) -> None:
        from ..core.api.base import Post
        cats = fav.tag_categories or {}
        post = Post(
            id=fav.post_id, file_url=fav.file_url or "",
            preview_url=fav.preview_url, tags=fav.tags or "",
            score=fav.score or 0, rating=fav.rating,
            source=fav.source, tag_categories=cats,
        )
        self._preview._current_post = post
        self._preview._current_site_id = fav.site_id
        self._preview.set_post_tags(post.tag_categories, post.tag_list)
        self._preview.update_bookmark_state(
            bool(self._db.is_bookmarked(fav.site_id, post.id))
        )
        self._preview.update_save_state(self._is_post_saved(post.id))
        info = f"Bookmark #{fav.post_id}"

        # Try local cache first
        if fav.cached_path and Path(fav.cached_path).exists():
            self._media_ctrl.set_preview_media(fav.cached_path, info)
            self._update_fullscreen(fav.cached_path, info)
            return

        # Try saved library — walk by post id; the file may live in any
        # library folder regardless of which bookmark folder fav is in.
        # Pass db so templated filenames also match (without it, only
        # legacy digit-stem files would be found).
        from ..core.config import find_library_files
        for path in find_library_files(fav.post_id, db=self._db):
            self._media_ctrl.set_preview_media(str(path), info)
            self._update_fullscreen(str(path), info)
            return

        # Download it
        self._status.showMessage(f"Downloading #{fav.post_id}...")

        async def _dl():
            try:
                path = await download_image(fav.file_url)
                # Update cached_path in DB
                self._db.update_bookmark_cache_path(fav.id, str(path))
                info = f"Bookmark #{fav.post_id}"
                self._signals.image_done.emit(str(path), info)
            except Exception as e:
                self._signals.image_error.emit(str(e))

        self._run_async(_dl)

    def _open_preview_in_default(self) -> None:
        # The preview is shared across tabs but its right-click menu used
        # to read browse-tab grid/posts unconditionally and then fell back
        # to "open the most recently modified file in the cache", which on
        # bookmarks/library tabs opened a completely unrelated image.
        # Branch on the active tab and use the right source.
        stack_idx = self._stack.currentIndex()
        if stack_idx == 1:
            # Bookmarks: prefer the bookmark's stored cached_path, fall back
            # to deriving the hashed cache filename from file_url in case
            # the stored path was set on a different machine or is stale.
            favs = self._bookmarks_view._bookmarks
            idx = self._bookmarks_view._grid.selected_index
            if 0 <= idx < len(favs):
                fav = favs[idx]
                from ..core.cache import cached_path_for
                path = None
                if fav.cached_path and Path(fav.cached_path).exists():
                    path = Path(fav.cached_path)
                else:
                    derived = cached_path_for(fav.file_url)
                    if derived.exists():
                        path = derived
                if path is not None:
                    self._preview._video_player.pause()
                    if self._fullscreen_window and self._fullscreen_window.isVisible():
                        self._fullscreen_window.pause_media()
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
                else:
                    self._status.showMessage("Bookmark not cached — open it first to download")
            return
        if stack_idx == 2:
            # Library: the preview's current path IS the local library file.
            # Don't go through cached_path_for — library files live under
            # saved_dir, not the cache.
            current = self._preview._current_path
            if current and Path(current).exists():
                self._preview._video_player.pause()
                if self._fullscreen_window and self._fullscreen_window.isVisible():
                    self._fullscreen_window.pause_media()
                QDesktopServices.openUrl(QUrl.fromLocalFile(current))
            return
        # Browse: original path. Removed the "open random cache file"
        # fallback — better to do nothing than to open the wrong image.
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts):
            self._open_in_default(self._posts[idx])

    def _open_preview_in_browser(self) -> None:
        # Same shape as _open_preview_in_default: route per active tab so
        # bookmarks open the post page on the bookmark's source site, not
        # the search dropdown's currently-selected site.
        stack_idx = self._stack.currentIndex()
        if stack_idx == 1:
            favs = self._bookmarks_view._bookmarks
            idx = self._bookmarks_view._grid.selected_index
            if 0 <= idx < len(favs):
                fav = favs[idx]
                self._open_post_id_in_browser(fav.post_id, site_id=fav.site_id)
        elif stack_idx == 2:
            # Library files have no booru source URL — nothing to open.
            return
        else:
            idx = self._grid.selected_index
            if 0 <= idx < len(self._posts):
                self._open_in_browser(self._posts[idx])

    def _navigate_preview(self, direction: int, wrap: bool = False) -> None:
        """Navigate to prev/next post in the preview. direction: -1 or +1.

        wrap=True wraps to the start (or end) of the bookmarks/library lists
        when running off the edge — used for the video-end "Next" auto-advance
        on tabs that don't have pagination.
        """
        # Note on the missing explicit activate calls below: every
        # tab's `grid._select(idx)` already chains through to the
        # activation handler via the `post_selected` signal, which is
        # wired (per tab) to a slot that ultimately calls
        # `_on_post_activated` / `_on_bookmark_activated` /
        # `_show_library_post`. The previous version of this method
        # called the activate handler directly *after* `_select`,
        # which fired the activation TWICE per keyboard navigation.
        #
        # The second activation scheduled a second async `_load`,
        # which fired a second `set_media` → second `_video.stop()` →
        # second `play_file()` cycle. The two `play_file`'s 250ms
        # stale-eof ignore windows leave a brief un-armed gap between
        # them (between the new `_eof_pending = False` reset and the
        # new `_eof_ignore_until` set). An async `eof-reached=True`
        # event from one of the stops landing in that gap would stick
        # `_eof_pending = True`, get picked up by `_poll`'s
        # `_handle_eof`, fire `play_next` in Loop=Next mode, and
        # cause `_navigate_preview(1, wrap=True)` to advance ANOTHER
        # post. End result: pressing Right once sometimes advanced
        # two posts. Random skip bug, observed on keyboard nav.
        #
        # Stop calling the activation handlers directly. Trust the
        # signal chain.
        if self._stack.currentIndex() == 1:
            # Bookmarks view
            grid = self._bookmarks_view._grid
            favs = self._bookmarks_view._bookmarks
            idx = grid.selected_index + direction
            if 0 <= idx < len(favs):
                grid._select(idx)
            elif wrap and favs:
                idx = 0 if direction > 0 else len(favs) - 1
                grid._select(idx)
        elif self._stack.currentIndex() == 2:
            # Library view
            grid = self._library_view._grid
            files = self._library_view._files
            idx = grid.selected_index + direction
            if 0 <= idx < len(files):
                grid._select(idx)
            elif wrap and files:
                idx = 0 if direction > 0 else len(files) - 1
                grid._select(idx)
        else:
            idx = self._grid.selected_index + direction
            log.info(f"Navigate: direction={direction} current={self._grid.selected_index} next={idx} total={len(self._posts)}")
            if 0 <= idx < len(self._posts):
                self._grid._select(idx)
            elif idx >= len(self._posts) and direction > 0 and len(self._posts) > 0 and not self._search_ctrl._infinite_scroll:
                self._search_ctrl._search.nav_page_turn = "first"
                self._search_ctrl.next_page()
            elif idx < 0 and direction < 0 and self._search_ctrl._current_page > 1 and not self._search_ctrl._infinite_scroll:
                self._search_ctrl._search.nav_page_turn = "last"
                self._search_ctrl.prev_page()

    def _on_video_end_next(self) -> None:
        """Auto-advance from end of video in 'Next' mode.

        Wraps to start on bookmarks/library tabs (where there is no
        pagination), so a single video looping with Next mode keeps moving
        through the list indefinitely instead of stopping at the end. Browse
        tab keeps its existing page-turn behaviour.

        Same fix as `_navigate_fullscreen` — don't call
        `_update_fullscreen` here with the stale `_current_path`. The
        downstream sync paths inside `_navigate_preview` already
        handle the popout update with the correct new path. Calling
        it here would re-trigger the eof-reached race in mpv and
        cause auto-skip cascades through the playlist.
        """
        self._navigate_preview(1, wrap=True)

    def _is_post_saved(self, post_id: int) -> bool:
        """Check if a post is saved in the library (any folder).

        Goes through library_meta — format-agnostic, sees both
        digit-stem v0.2.3 files and templated post-refactor saves.
        Single indexed SELECT, no filesystem walk.
        """
        return self._db.is_post_in_library(post_id)

    def _get_preview_post(self):
        """Get the post currently shown in the preview, from grid or stored ref."""
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts):
            return self._posts[idx], idx
        if self._preview._current_post:
            return self._preview._current_post, -1
        return None, -1

    def _bookmark_from_preview(self) -> None:
        post, idx = self._get_preview_post()
        if not post:
            return
        site_id = self._preview._current_site_id or self._site_combo.currentData()
        if not site_id:
            return
        if idx >= 0:
            self._toggle_bookmark(idx)
        else:
            if self._db.is_bookmarked(site_id, post.id):
                self._db.remove_bookmark(site_id, post.id)
            else:
                from ..core.cache import cached_path_for
                cached = cached_path_for(post.file_url)
                self._db.add_bookmark(
                    site_id=site_id, post_id=post.id,
                    file_url=post.file_url, preview_url=post.preview_url or "",
                    tags=post.tags, rating=post.rating, score=post.score,
                    source=post.source, cached_path=str(cached) if cached.exists() else None,
                    tag_categories=post.tag_categories,
                )
        bookmarked = bool(self._db.is_bookmarked(site_id, post.id))
        self._preview.update_bookmark_state(bookmarked)
        self._update_fullscreen_state()
        # Refresh bookmarks tab if visible
        if self._stack.currentIndex() == 1:
            self._bookmarks_view.refresh()

    def _bookmark_to_folder_from_preview(self, folder: str) -> None:
        """Bookmark the current preview post into a specific bookmark folder.

        Triggered by the toolbar Bookmark-as submenu, which only shows
        when the post is not yet bookmarked — so this method only handles
        the create path, never the move/remove paths. Empty string means
        Unfiled. Brand-new folder names get added to the DB folder list
        first so the bookmarks tab combo immediately shows them.
        """
        post, idx = self._get_preview_post()
        if not post:
            return
        site_id = self._preview._current_site_id or self._site_combo.currentData()
        if not site_id:
            return
        target = folder if folder else None
        if target and target not in self._db.get_folders():
            try:
                self._db.add_folder(target)
            except ValueError as e:
                self._status.showMessage(f"Invalid folder name: {e}")
                return
        if idx >= 0:
            # In the grid — go through _toggle_bookmark so the grid
            # thumbnail's bookmark badge updates via _on_bookmark_done.
            self._toggle_bookmark(idx, target)
        else:
            # Preview-only post (e.g. opened from the bookmarks tab while
            # browse is empty). Inline the add — no grid index to update.
            from ..core.cache import cached_path_for
            cached = cached_path_for(post.file_url)
            self._db.add_bookmark(
                site_id=site_id, post_id=post.id,
                file_url=post.file_url, preview_url=post.preview_url or "",
                tags=post.tags, rating=post.rating, score=post.score,
                source=post.source,
                cached_path=str(cached) if cached.exists() else None,
                folder=target,
                tag_categories=post.tag_categories,
            )
            where = target or "Unfiled"
            self._status.showMessage(f"Bookmarked #{post.id} to {where}")
        self._preview.update_bookmark_state(True)
        self._update_fullscreen_state()
        # Refresh bookmarks tab if visible so the new entry appears.
        if self._stack.currentIndex() == 1:
            self._bookmarks_view.refresh()

    def _save_from_preview(self, folder: str) -> None:
        post, idx = self._get_preview_post()
        if post:
            target = folder if folder else None
            # _save_to_library calls saved_folder_dir() which mkdir's the
            # target directory itself — no need to register it in the
            # bookmark folders DB table (those are unrelated now).
            self._save_to_library(post, target)
            # State updates happen in _on_bookmark_done after async save completes

    def _unsave_from_preview(self) -> None:
        post, idx = self._get_preview_post()
        if not post:
            return
        # delete_from_library walks every library folder by post id and
        # deletes every match in one call — no folder hint needed. Pass
        # db so templated filenames also get unlinked AND the meta row
        # gets cleaned up.
        from ..core.cache import delete_from_library
        deleted = delete_from_library(post.id, db=self._db)
        if deleted:
            self._status.showMessage(f"Removed #{post.id} from library")
            self._preview.update_save_state(False)
            # Update browse grid thumbnail saved dot
            for i, p in enumerate(self._posts):
                if p.id == post.id and i < len(self._grid._thumbs):
                    self._grid._thumbs[i].set_saved_locally(False)
                    break
            # Update bookmarks grid thumbnail
            bm_grid = self._bookmarks_view._grid
            for i, fav in enumerate(self._bookmarks_view._bookmarks):
                if fav.post_id == post.id and i < len(bm_grid._thumbs):
                    bm_grid._thumbs[i].set_saved_locally(False)
                    break
            # Refresh library tab if visible
            if self._stack.currentIndex() == 2:
                self._library_view.refresh()
        else:
            self._status.showMessage(f"#{post.id} not in library")
        self._update_fullscreen_state()

    def _blacklist_tag_from_popout(self, tag: str) -> None:
        reply = QMessageBox.question(
            self, "Blacklist Tag",
            f"Blacklist tag \"{tag}\"?\nPosts with this tag will be hidden.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._db.add_blacklisted_tag(tag)
        self._db.set_setting("blacklist_enabled", "1")
        self._status.showMessage(f"Blacklisted: {tag}")
        self._search_ctrl.remove_blacklisted_from_grid(tag=tag)

    def _blacklist_post_from_popout(self) -> None:
        post, idx = self._get_preview_post()
        if post:
            reply = QMessageBox.question(
                self, "Blacklist Post",
                f"Blacklist post #{post.id}?\nThis post will be hidden from results.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            self._db.add_blacklisted_post(post.file_url)
            self._status.showMessage(f"Post #{post.id} blacklisted")
            self._search_ctrl.remove_blacklisted_from_grid(post_url=post.file_url)

    def _open_fullscreen_preview(self) -> None:
        path = self._preview._current_path
        if not path:
            return
        info = self._preview._info_label.text()
        # Grab video position before clearing
        video_pos = 0
        if self._preview._stack.currentIndex() == 1:
            video_pos = self._preview._video_player.get_position_ms()
        # Clear the main preview — popout takes over
        # Hide preview, expand info panel into the freed space.
        # Mark popout as active so the right splitter saver doesn't persist
        # this transient layout (which would lose the user's real preferred
        # sizes between sessions).
        self._popout_active = True
        self._info_was_visible = self._info_panel.isVisible()
        self._right_splitter_sizes = self._right_splitter.sizes()
        self._preview.clear()
        self._preview.hide()
        self._info_panel.show()
        self._right_splitter.setSizes([0, 0, 1000])
        self._preview._current_path = path
        # Populate info panel for the current post
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts):
            self._info_panel.set_post(self._posts[idx])
        from .popout.window import FullscreenPreview
        # Restore persisted window state
        saved_geo = self._db.get_setting("slideshow_geometry")
        saved_fs = self._db.get_setting_bool("slideshow_fullscreen")
        if saved_geo:
            parts = saved_geo.split(",")
            if len(parts) == 4:
                from PySide6.QtCore import QRect
                FullscreenPreview._saved_geometry = QRect(*[int(p) for p in parts])
                FullscreenPreview._saved_fullscreen = saved_fs
            else:
                FullscreenPreview._saved_geometry = None
                FullscreenPreview._saved_fullscreen = True
        else:
            FullscreenPreview._saved_fullscreen = True
        cols = self._grid._flow.columns
        show_actions = self._stack.currentIndex() != 2
        monitor = self._db.get_setting("slideshow_monitor")
        self._fullscreen_window = FullscreenPreview(grid_cols=cols, show_actions=show_actions, monitor=monitor, parent=self)
        self._fullscreen_window.navigate.connect(self._navigate_fullscreen)
        self._fullscreen_window.play_next_requested.connect(self._on_video_end_next)
        # Save signals are always wired — even in library mode, the
        # popout's Save button is the only toolbar action visible (acting
        # as Unsave for the file being viewed), and it has its own
        # Save-to-Library submenu shape that matches the embedded preview.
        from ..core.config import library_folders
        self._fullscreen_window.set_folders_callback(library_folders)
        self._fullscreen_window.save_to_folder.connect(self._save_from_preview)
        self._fullscreen_window.unsave_requested.connect(self._unsave_from_preview)
        if show_actions:
            self._fullscreen_window.bookmark_requested.connect(self._bookmark_from_preview)
            # Same Bookmark-as flow as the embedded preview — popout reuses
            # the existing handler since both signals carry just a folder
            # name and read the post from self._preview._current_post.
            self._fullscreen_window.set_bookmark_folders_callback(self._db.get_folders)
            self._fullscreen_window.bookmark_to_folder.connect(self._bookmark_to_folder_from_preview)
            self._fullscreen_window.blacklist_tag_requested.connect(self._blacklist_tag_from_popout)
            self._fullscreen_window.blacklist_post_requested.connect(self._blacklist_post_from_popout)
        self._fullscreen_window.open_in_default.connect(self._open_preview_in_default)
        self._fullscreen_window.open_in_browser.connect(self._open_preview_in_browser)
        self._fullscreen_window.closed.connect(self._on_fullscreen_closed)
        self._fullscreen_window.privacy_requested.connect(self._privacy.toggle)
        # Set post tags for BL Tag menu
        post = self._preview._current_post
        if post:
            self._fullscreen_window.set_post_tags(post.tag_categories, post.tag_list)
        # Sync video player state from preview to popout via the
        # popout's public sync_video_state method (replaces direct
        # popout._video.* attribute writes).
        pv = self._preview._video_player
        self._fullscreen_window.sync_video_state(
            volume=pv.volume,
            mute=pv.is_muted,
            autoplay=pv.autoplay,
            loop_state=pv.loop_state,
        )
        # Connect seek-after-load BEFORE set_media so we don't miss media_ready
        if video_pos > 0:
            self._fullscreen_window.connect_media_ready_once(
                lambda: self._fullscreen_window.seek_video_to(video_pos)
            )
        # Pre-fit dimensions for the popout video pre-fit optimization
        # — `post` is the same `self._preview._current_post` referenced
        # at line 2164 (set above), so reuse it without an extra read.
        pre_w = post.width if post else 0
        pre_h = post.height if post else 0
        self._fullscreen_window.set_media(path, info, width=pre_w, height=pre_h)
        # Always sync state — the save button is visible in both modes
        # (library mode = only Save shown, browse/bookmarks = full toolbar)
        # so its Unsave label needs to land before the user sees it.
        self._update_fullscreen_state()

    def _on_fullscreen_closed(self) -> None:
        # Persist popout window state to DB
        if self._fullscreen_window:
            from .popout.window import FullscreenPreview
            fs = FullscreenPreview._saved_fullscreen
            geo = FullscreenPreview._saved_geometry
            self._db.set_setting("slideshow_fullscreen", "1" if fs else "0")
            if geo:
                self._db.set_setting("slideshow_geometry", f"{geo.x()},{geo.y()},{geo.width()},{geo.height()}")
        # Restore preview and info panel visibility
        self._preview.show()
        if not getattr(self, '_info_was_visible', False):
            self._info_panel.hide()
        if hasattr(self, '_right_splitter_sizes'):
            self._right_splitter.setSizes(self._right_splitter_sizes)
        # Clear the popout-active flag now that the right splitter is back
        # in its real shape — future splitterMoved events should persist.
        self._popout_active = False
        # Sync video player state from popout back to preview via
        # the popout's public get_video_state method (replaces direct
        # popout._video.* attribute reads + popout._stack.currentIndex
        # check). The dict carries volume / mute / autoplay / loop_state
        # / position_ms in one read.
        video_pos = 0
        if self._fullscreen_window:
            vstate = self._fullscreen_window.get_video_state()
            pv = self._preview._video_player
            pv.volume = vstate["volume"]
            pv.is_muted = vstate["mute"]
            pv.autoplay = vstate["autoplay"]
            pv.loop_state = vstate["loop_state"]
            video_pos = vstate["position_ms"]
        # Restore preview with current media
        path = self._preview._current_path
        info = self._preview._info_label.text()
        self._fullscreen_window = None
        if path:
            # Connect seek-after-load BEFORE set_media so we don't miss media_ready
            if video_pos > 0:
                def _seek_preview():
                    self._preview._video_player.seek_to_ms(video_pos)
                    try:
                        self._preview._video_player.media_ready.disconnect(_seek_preview)
                    except RuntimeError:
                        pass
                self._preview._video_player.media_ready.connect(_seek_preview)
            self._preview.set_media(path, info)

    def _navigate_fullscreen(self, direction: int) -> None:
        # Just navigate. Do NOT call _update_fullscreen here with the
        # current_path even though earlier code did — for browse view,
        # _current_path still holds the PREVIOUS post's path at this
        # moment (the new post's path doesn't land until the async
        # _load completes and _on_image_done fires). Calling
        # _update_fullscreen with the stale path would re-load the
        # OLD video in the popout, which then races mpv's eof-reached
        # observer (mpv emits eof on the redundant `command('stop')`
        # the reload performs). If the observer fires after play_file's
        # _eof_pending=False reset, _handle_eof picks it up on the next
        # poll tick and emits play_next in Loop=Next mode — auto-
        # advancing past the ACTUAL next post the user wanted. Bug
        # observed empirically: keyboard nav in popout sometimes
        # skipped a post.
        #
        # The correct sync paths are already in place:
        #   - Browse: _navigate_preview → _on_post_activated → async
        #     _load → _on_image_done → _update_fullscreen(NEW_path)
        #   - Bookmarks: _navigate_preview → _on_bookmark_activated →
        #     _update_fullscreen(fav.cached_path) (sync, line 1683/1691)
        #   - Library: _navigate_preview → file_activated →
        #     _on_library_activated → _show_library_post →
        #     _update_fullscreen(path) (sync, line 1622)
        # Each downstream path uses the *correct* new path. The
        # additional call here was both redundant (bookmark/library)
        # and racy/buggy (browse).
        self._navigate_preview(direction)

    def _close_preview(self) -> None:
        self._preview.clear()

    # -- Context menu --

    def _on_context_menu(self, index: int, pos) -> None:
        if index < 0 or index >= len(self._posts):
            return
        post = self._posts[index]
        menu = QMenu(self)

        open_browser = menu.addAction("Open in Browser")
        open_default = menu.addAction("Open in Default App")
        menu.addSeparator()
        save_as = menu.addAction("Save As...")

        # Save to Library submenu — folders come from the library
        # filesystem, not the bookmark folder DB.
        from ..core.config import library_folders
        save_lib_menu = menu.addMenu("Save to Library")
        save_lib_unsorted = save_lib_menu.addAction("Unfiled")
        save_lib_menu.addSeparator()
        save_lib_folders = {}
        for folder in library_folders():
            a = save_lib_menu.addAction(folder)
            save_lib_folders[id(a)] = folder
        save_lib_menu.addSeparator()
        save_lib_new = save_lib_menu.addAction("+ New Folder...")

        unsave_lib = None
        if self._is_post_saved(post.id):
            unsave_lib = menu.addAction("Unsave from Library")
        copy_clipboard = menu.addAction("Copy File to Clipboard")
        copy_url = menu.addAction("Copy Image URL")
        copy_tags = menu.addAction("Copy Tags")
        menu.addSeparator()

        # Bookmark action: when not yet bookmarked, offer "Bookmark as"
        # with a submenu of bookmark folders so the user can file the
        # new bookmark in one click. Bookmark folders come from the DB
        # (separate name space from library folders). When already
        # bookmarked, the action collapses to a flat "Remove Bookmark"
        # — re-filing an existing bookmark belongs in the bookmarks tab
        # right-click menu's "Move to Folder" submenu.
        fav_action = None
        bm_folder_actions: dict[int, str] = {}
        bm_unfiled = None
        bm_new = None
        if self._is_current_bookmarked(index):
            fav_action = menu.addAction("Remove Bookmark")
        else:
            fav_menu = menu.addMenu("Bookmark as")
            bm_unfiled = fav_menu.addAction("Unfiled")
            fav_menu.addSeparator()
            for folder in self._db.get_folders():
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
            self._open_in_browser(post)
        elif action == open_default:
            self._open_in_default(post)
        elif action == save_as:
            self._save_as(post)
        elif action == save_lib_unsorted:
            self._save_to_library(post, None)
        elif action == save_lib_new:
            from PySide6.QtWidgets import QInputDialog, QMessageBox
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                # _save_to_library → saved_folder_dir() does the mkdir
                # and the path-traversal check; we surface the same error
                # message it would emit so a bad name is reported clearly.
                try:
                    from ..core.config import saved_folder_dir
                    saved_folder_dir(name.strip())
                except ValueError as e:
                    QMessageBox.warning(self, "Invalid Folder Name", str(e))
                    return
                self._save_to_library(post, name.strip())
        elif id(action) in save_lib_folders:
            self._save_to_library(post, save_lib_folders[id(action)])
        elif action == unsave_lib:
            self._preview._current_post = post
            self._unsave_from_preview()
        elif action == copy_clipboard:
            self._copy_file_to_clipboard()
        elif action == copy_url:
            QApplication.clipboard().setText(post.file_url)
            self._status.showMessage("URL copied")
        elif action == copy_tags:
            QApplication.clipboard().setText(post.tags)
            self._status.showMessage("Tags copied")
        elif fav_action is not None and action == fav_action:
            # Currently bookmarked → flat "Remove Bookmark" path.
            self._toggle_bookmark(index)
        elif bm_unfiled is not None and action == bm_unfiled:
            self._toggle_bookmark(index, None)
        elif bm_new is not None and action == bm_new:
            from PySide6.QtWidgets import QInputDialog, QMessageBox
            name, ok = QInputDialog.getText(self, "New Bookmark Folder", "Folder name:")
            if ok and name.strip():
                # Bookmark folders are DB-managed; add_folder validates
                # the name and is the same call the bookmarks tab uses.
                try:
                    self._db.add_folder(name.strip())
                except ValueError as e:
                    QMessageBox.warning(self, "Invalid Folder Name", str(e))
                    return
                self._toggle_bookmark(index, name.strip())
        elif id(action) in bm_folder_actions:
            self._toggle_bookmark(index, bm_folder_actions[id(action)])
        elif self._is_child_of_menu(action, bl_menu):
            tag = action.text()
            self._db.add_blacklisted_tag(tag)
            self._db.set_setting("blacklist_enabled", "1")
            # Clear preview if the previewed post has this tag
            if self._preview._current_path and tag in post.tag_list:
                from ..core.cache import cached_path_for
                cp = str(cached_path_for(post.file_url))
                if cp == self._preview._current_path:
                    self._preview.clear()
                    if self._fullscreen_window and self._fullscreen_window.isVisible():
                        self._fullscreen_window.stop_media()
            self._status.showMessage(f"Blacklisted: {tag}")
            self._search_ctrl.remove_blacklisted_from_grid(tag=tag)
        elif action == bl_post_action:
            self._db.add_blacklisted_post(post.file_url)
            self._search_ctrl.remove_blacklisted_from_grid(post_url=post.file_url)
            self._status.showMessage(f"Post #{post.id} blacklisted")
            self._search_ctrl.do_search()

    @staticmethod
    def _is_child_of_menu(action, menu) -> bool:
        parent = action.parent()
        while parent:
            if parent == menu:
                return True
            parent = getattr(parent, 'parent', lambda: None)()
        return False

    def _on_multi_context_menu(self, indices: list, pos) -> None:
        """Context menu for multi-selected posts.

        Library and bookmark actions are split into independent
        save/unsave and bookmark/remove-bookmark pairs (mirroring the
        single-post menu's separation), with symmetric conditional
        visibility: each action only appears when the selection actually
        contains posts the action would affect. Save All to Library
        appears only when at least one post is unsaved; Unsave All from
        Library only when at least one is saved; Bookmark All only when
        at least one is unbookmarked; Remove All Bookmarks only when at
        least one is bookmarked.
        """
        posts = [self._posts[i] for i in indices if 0 <= i < len(self._posts)]
        if not posts:
            return
        count = len(posts)

        site_id = self._site_combo.currentData()
        any_bookmarked = bool(site_id) and any(self._db.is_bookmarked(site_id, p.id) for p in posts)
        any_unbookmarked = bool(site_id) and any(not self._db.is_bookmarked(site_id, p.id) for p in posts)
        any_saved = any(self._is_post_saved(p.id) for p in posts)
        any_unsaved = any(not self._is_post_saved(p.id) for p in posts)

        menu = QMenu(self)

        # Library section
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

        # Bookmark section
        if (any_unsaved or any_saved) and (any_unbookmarked or any_bookmarked):
            menu.addSeparator()

        fav_all = None
        if any_unbookmarked:
            fav_all = menu.addAction(f"Bookmark All ({count})")

        unfav_all = None
        if any_bookmarked:
            unfav_all = menu.addAction(f"Remove All Bookmarks ({count})")

        # Always-shown actions
        if any_unsaved or any_saved or any_unbookmarked or any_bookmarked:
            menu.addSeparator()
        batch_dl = menu.addAction(f"Download All ({count})...")
        copy_urls = menu.addAction("Copy All URLs")

        action = menu.exec(pos)
        if not action:
            return

        if fav_all is not None and action == fav_all:
            self._bulk_bookmark(indices, posts)
        elif save_unsorted is not None and action == save_unsorted:
            self._bulk_save(indices, posts, None)
        elif save_new is not None and action == save_new:
            from PySide6.QtWidgets import QInputDialog, QMessageBox
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                try:
                    from ..core.config import saved_folder_dir
                    saved_folder_dir(name.strip())
                except ValueError as e:
                    QMessageBox.warning(self, "Invalid Folder Name", str(e))
                    return
                self._bulk_save(indices, posts, name.strip())
        elif id(action) in save_folder_actions:
            self._bulk_save(indices, posts, save_folder_actions[id(action)])
        elif unsave_lib_all is not None and action == unsave_lib_all:
            self._bulk_unsave(indices, posts)
        elif action == batch_dl:
            from .dialogs import select_directory
            dest = select_directory(self, "Download to folder")
            if dest:
                self._batch_download_posts(posts, dest)
        elif unfav_all is not None and action == unfav_all:
            if site_id:
                for post in posts:
                    self._db.remove_bookmark(site_id, post.id)
                for idx in indices:
                    if 0 <= idx < len(self._grid._thumbs):
                        self._grid._thumbs[idx].set_bookmarked(False)
                self._grid._clear_multi()
                self._status.showMessage(f"Removed {count} bookmarks")
                if self._stack.currentIndex() == 1:
                    self._bookmarks_view.refresh()
        elif action == copy_urls:
            urls = "\n".join(p.file_url for p in posts)
            QApplication.clipboard().setText(urls)
            self._status.showMessage(f"Copied {count} URLs")

    def _bulk_bookmark(self, indices: list[int], posts: list[Post]) -> None:
        site_id = self._site_combo.currentData()
        if not site_id:
            return
        self._status.showMessage(f"Bookmarking {len(posts)}...")

        async def _do():
            for i, (idx, post) in enumerate(zip(indices, posts)):
                if self._db.is_bookmarked(site_id, post.id):
                    continue
                try:
                    path = await download_image(post.file_url)
                    self._db.add_bookmark(
                        site_id=site_id, post_id=post.id,
                        file_url=post.file_url, preview_url=post.preview_url,
                        tags=post.tags, rating=post.rating, score=post.score,
                        source=post.source, cached_path=str(path),
                        tag_categories=post.tag_categories,
                    )
                    self._signals.bookmark_done.emit(idx, f"Bookmarked {i+1}/{len(posts)}")
                except Exception as e:
                    log.warning(f"Operation failed: {e}")
            self._signals.batch_done.emit(f"Bookmarked {len(posts)} posts")

        self._run_async(_do)

    def _bulk_save(self, indices: list[int], posts: list[Post], folder: str | None) -> None:
        """Bulk-save the selected posts into the library, optionally inside a subfolder.

        Each iteration routes through save_post_file with a shared
        in_flight set so template-collision-prone batches (e.g.
        %artist% on a page that has many posts by the same artist) get
        sequential _1, _2, _3 suffixes instead of clobbering each other.
        """
        from ..core.config import saved_dir, saved_folder_dir
        from ..core.library_save import save_post_file

        where = folder or "Unfiled"
        self._status.showMessage(f"Saving {len(posts)} to {where}...")
        try:
            dest_dir = saved_folder_dir(folder) if folder else saved_dir()
        except ValueError as e:
            self._status.showMessage(f"Invalid folder name: {e}")
            return

        in_flight: set[str] = set()

        async def _do():
            fetcher = self._get_category_fetcher()
            for i, (idx, post) in enumerate(zip(indices, posts)):
                try:
                    src = Path(await download_image(post.file_url))
                    await save_post_file(src, post, dest_dir, self._db, in_flight, category_fetcher=fetcher)
                    self._copy_library_thumb(post)
                    self._signals.bookmark_done.emit(idx, f"Saved {i+1}/{len(posts)} to {where}")
                except Exception as e:
                    log.warning(f"Bulk save #{post.id} failed: {e}")
            self._signals.batch_done.emit(f"Saved {len(posts)} to {where}")

        self._run_async(_do)

    def _bulk_unsave(self, indices: list[int], posts: list[Post]) -> None:
        """Bulk-remove selected posts from the library.

        Mirrors `_bulk_save` shape but synchronously — `delete_from_library`
        is a filesystem op, no httpx round-trip needed. Touches only the
        library (filesystem); bookmarks are a separate DB-backed concept
        and stay untouched. The grid's saved-locally dot clears for every
        selection slot regardless of whether the file was actually present
        — the user's intent is "make these not-saved", and a missing file
        is already not-saved.
        """
        from ..core.cache import delete_from_library
        for post in posts:
            delete_from_library(post.id, db=self._db)
        for idx in indices:
            if 0 <= idx < len(self._grid._thumbs):
                self._grid._thumbs[idx].set_saved_locally(False)
        self._grid._clear_multi()
        self._status.showMessage(f"Removed {len(posts)} from library")
        if self._stack.currentIndex() == 2:
            self._library_view.refresh()
        self._update_fullscreen_state()

    def _ensure_bookmarked(self, post: Post) -> None:
        """Bookmark a post if not already bookmarked."""
        site_id = self._site_combo.currentData()
        if not site_id or self._db.is_bookmarked(site_id, post.id):
            return

        async def _fav():
            try:
                path = await download_image(post.file_url)
                self._db.add_bookmark(
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

        self._run_async(_fav)

    def _batch_download_posts(self, posts: list, dest: str) -> None:
        """Multi-select Download All entry point. Delegates to
        _batch_download_to so the in_flight set, library_meta write,
        and saved-dots refresh share one implementation."""
        self._batch_download_to(posts, Path(dest))

    def _is_current_bookmarked(self, index: int) -> bool:
        site_id = self._site_combo.currentData()
        if not site_id or index < 0 or index >= len(self._posts):
            return False
        return self._db.is_bookmarked(site_id, self._posts[index].id)

    def _open_post_id_in_browser(self, post_id: int, site_id: int | None = None) -> None:
        """Open the post page in the system browser. site_id selects which
        site's URL/api scheme to use; defaults to the currently selected
        search site. Pass site_id explicitly when the post comes from a
        different source than the search dropdown (e.g. bookmarks)."""
        site = None
        if site_id is not None:
            sites = self._db.get_sites()
            site = next((s for s in sites if s.id == site_id), None)
        if site is None:
            site = self._current_site
        if not site:
            return
        base = site.url
        api = site.api_type
        if api == "danbooru" or api == "e621":
            url = f"{base}/posts/{post_id}"
        elif api == "gelbooru":
            url = f"{base}/index.php?page=post&s=view&id={post_id}"
        elif api == "moebooru":
            url = f"{base}/post/show/{post_id}"
        else:
            url = f"{base}/posts/{post_id}"
        QDesktopServices.openUrl(QUrl(url))

    def _open_in_browser(self, post: Post) -> None:
        self._open_post_id_in_browser(post.id)

    def _open_in_default(self, post: Post) -> None:
        from ..core.cache import cached_path_for, is_cached
        path = cached_path_for(post.file_url)
        if path.exists():
            # Pause any playing video before opening externally
            self._preview._video_player.pause()
            if self._fullscreen_window and self._fullscreen_window.isVisible():
                self._fullscreen_window.pause_media()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self._status.showMessage("Image not cached yet — double-click to download first")

    def _copy_library_thumb(self, post: Post) -> None:
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

    def _save_to_library(self, post: Post, folder: str | None) -> None:
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

        self._status.showMessage(f"Saving #{post.id} to library...")
        try:
            dest_dir = saved_folder_dir(folder) if folder else saved_dir()
        except ValueError as e:
            self._status.showMessage(f"Invalid folder name: {e}")
            return

        async def _save():
            try:
                src = Path(await download_image(post.file_url))
                await save_post_file(src, post, dest_dir, self._db, category_fetcher=self._get_category_fetcher())
                self._copy_library_thumb(post)
                where = folder or "Unfiled"
                self._signals.bookmark_done.emit(
                    self._grid.selected_index,
                    f"Saved #{post.id} to {where}",
                )
            except Exception as e:
                self._signals.bookmark_error.emit(str(e))

        self._run_async(_save)

    def _save_as(self, post: Post) -> None:
        """Open a Save As dialog for a single post and write the file
        through the unified save_post_file flow.

        The default name in the dialog comes from rendering the user's
        library_filename_template against the post; the user can edit
        before confirming. If the chosen destination ends up inside
        saved_dir(), save_post_file registers a library_meta row —
        a behavior change from v0.2.3 (where Save As never wrote meta
        regardless of destination)."""
        from ..core.cache import cached_path_for
        from ..core.config import render_filename_template
        from ..core.library_save import save_post_file
        from .dialogs import save_file

        src = cached_path_for(post.file_url)
        if not src.exists():
            self._status.showMessage("Image not cached — double-click to download first")
            return
        ext = src.suffix
        template = self._db.get_setting("library_filename_template")
        default_name = render_filename_template(template, post, ext)
        dest = save_file(self, "Save Image", default_name, f"Images (*{ext})")
        if not dest:
            return
        dest_path = Path(dest)

        async def _do_save():
            try:
                actual = await save_post_file(
                    src, post, dest_path.parent, self._db,
                    explicit_name=dest_path.name,
                    category_fetcher=self._get_category_fetcher(),
                )
                self._signals.bookmark_done.emit(
                    self._grid.selected_index,
                    f"Saved to {actual}",
                )
            except Exception as e:
                self._signals.bookmark_error.emit(f"Save failed: {e}")

        self._run_async(_do_save)

    # -- Batch download --

    def _batch_download_to(self, posts: list[Post], dest_dir: Path) -> None:
        """Download `posts` into `dest_dir`, routing each save through
        save_post_file with a shared in_flight set so collision-prone
        templates produce sequential _1, _2 suffixes within the batch.

        Stashes `dest_dir` on `self._batch_dest` so _on_batch_progress
        and _on_batch_done can decide whether the destination is inside
        the library and the saved-dots need refreshing. The library_meta
        write happens automatically inside save_post_file when dest_dir
        is inside saved_dir() — fixes the v0.2.3 latent bug where batch
        downloads into a library folder left files unregistered.
        """
        from ..core.library_save import save_post_file

        self._batch_dest = dest_dir
        self._status.showMessage(f"Downloading {len(posts)} images...")
        in_flight: set[str] = set()

        async def _batch():
            fetcher = self._get_category_fetcher()
            for i, post in enumerate(posts):
                try:
                    src = Path(await download_image(post.file_url))
                    await save_post_file(src, post, dest_dir, self._db, in_flight, category_fetcher=fetcher)
                    self._signals.batch_progress.emit(i + 1, len(posts), post.id)
                except Exception as e:
                    log.warning(f"Batch #{post.id} failed: {e}")
            self._signals.batch_done.emit(f"Downloaded {len(posts)} images to {dest_dir}")

        self._run_async(_batch)

    def _batch_download(self) -> None:
        if not self._posts:
            self._status.showMessage("No posts to download")
            return
        from .dialogs import select_directory
        dest = select_directory(self, "Download to folder")
        if not dest:
            return
        self._batch_download_to(list(self._posts), Path(dest))

    def _on_batch_progress(self, current: int, total: int, post_id: int) -> None:
        self._status.showMessage(f"Downloading {current}/{total}...")
        # Light the browse saved-dot for the just-finished post if the
        # batch destination is inside the library. Runs per-post on the
        # main thread (this is a Qt slot), so the dot appears as the
        # files land instead of all at once when the batch completes.
        dest = getattr(self, "_batch_dest", None)
        if dest is None:
            return
        from ..core.config import saved_dir
        if not dest.is_relative_to(saved_dir()):
            return
        for i, p in enumerate(self._posts):
            if p.id == post_id and i < len(self._grid._thumbs):
                self._grid._thumbs[i].set_saved_locally(True)
                break

    # -- Toggles --

    def _toggle_log(self) -> None:
        self._log_text.setVisible(not self._log_text.isVisible())

    def _toggle_info(self) -> None:
        new_visible = not self._info_panel.isVisible()
        self._info_panel.setVisible(new_visible)
        # Persist the user's intent so it survives the next launch.
        self._db.set_setting("info_panel_visible", "1" if new_visible else "0")
        if new_visible and 0 <= self._grid.selected_index < len(self._posts):
            self._info_panel.set_post(self._posts[self._grid.selected_index])

    def _open_site_manager(self) -> None:
        dlg = SiteManagerDialog(self._db, self)
        dlg.sites_changed.connect(self._load_sites)
        dlg.exec()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._db, self)
        dlg.settings_changed.connect(self._apply_settings)
        self._bookmarks_imported = False
        dlg.bookmarks_imported.connect(lambda: setattr(self, '_bookmarks_imported', True))
        dlg.exec()
        if self._bookmarks_imported:
            self._switch_view(1)
            self._bookmarks_view.refresh()

    def _apply_settings(self) -> None:
        """Re-read settings from DB and apply to UI."""
        rating = self._db.get_setting("default_rating")
        idx = self._rating_combo.findText(rating.capitalize() if rating != "all" else "All")
        if idx >= 0:
            self._rating_combo.setCurrentIndex(idx)
        self._score_spin.setValue(self._db.get_setting_int("default_score"))
        self._bookmarks_view.refresh()
        # Apply infinite scroll toggle live
        self._search_ctrl._infinite_scroll = self._db.get_setting_bool("infinite_scroll")
        self._bottom_nav.setVisible(not self._search_ctrl._infinite_scroll)
        # Apply library dir
        lib_dir = self._db.get_setting("library_dir")
        if lib_dir:
            from ..core.config import set_library_dir
            set_library_dir(Path(lib_dir))
        # Apply thumbnail size
        from .grid import THUMB_SIZE
        new_size = self._db.get_setting_int("thumbnail_size")
        if new_size and new_size != THUMB_SIZE:
            import booru_viewer.gui.grid as grid_mod
            grid_mod.THUMB_SIZE = new_size
        self._status.showMessage("Settings applied")

    # -- Fullscreen & Privacy --

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._privacy.resize_overlay()
        # Capture window state proactively so the saved value is always
        # fresh — closeEvent's hyprctl query can fail if the compositor has
        # already started unmapping. Debounced via the 300ms timer.
        if hasattr(self, '_main_window_save_timer'):
            self._main_window_save_timer.start()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        # moveEvent is unreliable on Wayland for floating windows but it
        # does fire on configure for some compositors — start the save
        # timer regardless. resizeEvent is the more reliable trigger.
        if hasattr(self, '_main_window_save_timer'):
            self._main_window_save_timer.start()

    # -- Keyboard shortcuts --

    def keyPressEvent(self, event) -> None:
        key = event.key()
        # Privacy screen always works
        if key == Qt.Key.Key_P and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self._privacy.toggle()
            return
        # If privacy is on, only allow toggling it off
        if self._privacy.is_active:
            return
        if key == Qt.Key.Key_F and self._posts:
            idx = self._grid.selected_index
            if 0 <= idx < len(self._posts):
                self._toggle_bookmark(idx)
                return
        elif key == Qt.Key.Key_I:
            self._toggle_info()
            return
        elif key == Qt.Key.Key_Space:
            if self._preview._stack.currentIndex() == 1 and self._preview.underMouse():
                self._preview._video_player._toggle_play()
                return
        elif key == Qt.Key.Key_Period:
            if self._preview._stack.currentIndex() == 1:
                self._preview._video_player._seek_relative(1800)
                return
        elif key == Qt.Key.Key_Comma:
            if self._preview._stack.currentIndex() == 1:
                self._preview._video_player._seek_relative(-1800)
                return
        super().keyPressEvent(event)

    def _copy_file_to_clipboard(self, path: str | None = None) -> None:
        """Copy a file to clipboard. Tries wl-copy on Wayland, Qt fallback."""
        if not path:
            path = self._preview._current_path
        if not path:
            idx = self._grid.selected_index
            if 0 <= idx < len(self._posts):
                from ..core.cache import cached_path_for
                cp = cached_path_for(self._posts[idx].file_url)
                if cp.exists():
                    path = str(cp)
        if not path or not Path(path).exists():
            log.debug(f"Copy failed: path={path} preview_path={self._preview._current_path}")
            self._status.showMessage("Nothing to copy")
            return
        log.debug(f"Copying: {path}")

        from PySide6.QtCore import QMimeData, QUrl
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(Path(path).resolve()))])
        # Also set image data for apps that prefer it
        pix = QPixmap(path)
        if not pix.isNull():
            mime.setImageData(pix.toImage())
        QApplication.clipboard().setMimeData(mime)
        self._status.showMessage(f"Copied to clipboard: {Path(path).name}")

    # -- Bookmarks --

    def _toggle_bookmark(self, index: int, folder: str | None = None) -> None:
        """Toggle the bookmark state of post at `index`.

        When `folder` is given and the post is not yet bookmarked, the
        new bookmark is filed under that bookmark folder. The folder
        arg is ignored when removing — bookmark folder membership is
        moot if the bookmark itself is going away.
        """
        post = self._posts[index]
        site_id = self._site_combo.currentData()
        if not site_id:
            return

        if self._db.is_bookmarked(site_id, post.id):
            self._db.remove_bookmark(site_id, post.id)
            self._status.showMessage(f"Unbookmarked #{post.id}")
            thumbs = self._grid._thumbs
            if 0 <= index < len(thumbs):
                thumbs[index].set_bookmarked(False)
        else:
            self._status.showMessage(f"Bookmarking #{post.id}...")

            async def _fav():
                try:
                    path = await download_image(post.file_url)
                    self._db.add_bookmark(
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
                    self._signals.bookmark_done.emit(index, f"Bookmarked #{post.id} to {where}")
                except Exception as e:
                    self._signals.bookmark_error.emit(str(e))

            self._run_async(_fav)

    def _on_bookmark_done(self, index: int, msg: str) -> None:
        self._status.showMessage(f"{len(self._posts)} results — {msg}")
        # Detect batch operations (e.g. "Saved 3/10 to Unfiled") — skip heavy updates
        is_batch = "/" in msg and any(c.isdigit() for c in msg.split("/")[0][-2:])
        thumbs = self._grid._thumbs
        if 0 <= index < len(thumbs):
            if "Saved" in msg:
                thumbs[index].set_saved_locally(True)
            if "Bookmarked" in msg:
                thumbs[index].set_bookmarked(True)
        if not is_batch:
            if "Bookmarked" in msg:
                self._preview.update_bookmark_state(True)
            if "Saved" in msg:
                self._preview.update_save_state(True)
                if self._stack.currentIndex() == 1:
                    bm_grid = self._bookmarks_view._grid
                    bm_idx = bm_grid.selected_index
                    if 0 <= bm_idx < len(bm_grid._thumbs):
                        bm_grid._thumbs[bm_idx].set_saved_locally(True)
                if self._stack.currentIndex() == 2:
                    self._library_view.refresh()
            self._update_fullscreen_state()

    def _on_library_files_deleted(self, post_ids: list) -> None:
        """Library deleted files — clear saved dots on browse grid."""
        for i, p in enumerate(self._posts):
            if p.id in post_ids and i < len(self._grid._thumbs):
                self._grid._thumbs[i].set_saved_locally(False)

    def _refresh_browse_saved_dots(self) -> None:
        """Bookmarks changed — rescan saved state for all visible browse grid posts."""
        for i, p in enumerate(self._posts):
            if i < len(self._grid._thumbs):
                self._grid._thumbs[i].set_saved_locally(self._is_post_saved(p.id))
                site_id = self._site_combo.currentData()
                self._grid._thumbs[i].set_bookmarked(
                    bool(site_id and self._db.is_bookmarked(site_id, p.id))
                )

    def _on_batch_done(self, msg: str) -> None:
        self._status.showMessage(msg)
        self._update_fullscreen_state()
        if self._stack.currentIndex() == 1:
            self._bookmarks_view.refresh()
        if self._stack.currentIndex() == 2:
            self._library_view.refresh()
        # Saved-dot updates happen incrementally in _on_batch_progress as
        # each file lands; this slot just clears the destination stash.
        self._batch_dest = None

    def closeEvent(self, event) -> None:
        # Flush any pending splitter / window-state saves (debounce timers
        # may still be running if the user moved/resized within the last
        # 300ms) and capture the final state. Both must run BEFORE
        # _db.close().
        if self._main_splitter_save_timer.isActive():
            self._main_splitter_save_timer.stop()
        if self._main_window_save_timer.isActive():
            self._main_window_save_timer.stop()
        if hasattr(self, '_right_splitter_save_timer') and self._right_splitter_save_timer.isActive():
            self._right_splitter_save_timer.stop()
        self._window_state.save_main_splitter_sizes()
        self._window_state.save_right_splitter_sizes()
        self._window_state.save_main_window_state()

        # Cleanly shut the shared httpx pools down BEFORE stopping the loop
        # so the connection pool / keepalive sockets / TLS state get released
        # instead of being abandoned mid-flight. Has to run on the loop the
        # clients were bound to.
        try:
            from ..core.api.base import BooruClient
            from ..core.api.e621 import E621Client
            from ..core.cache import aclose_shared_client

            async def _close_all():
                await BooruClient.aclose_shared()
                await E621Client.aclose_shared()
                await aclose_shared_client()

            fut = asyncio.run_coroutine_threadsafe(_close_all(), self._async_loop)
            fut.result(timeout=5)
        except Exception as e:
            log.warning(f"Shared httpx aclose failed: {e}")

        self._async_loop.call_soon_threadsafe(self._async_loop.stop)
        self._async_thread.join(timeout=2)
        if self._db.get_setting_bool("clear_cache_on_exit"):
            from ..core.cache import clear_cache
            clear_cache(clear_images=True, clear_thumbnails=True)
            self._db.clear_search_history()
        self._db.close()
        super().closeEvent(event)
