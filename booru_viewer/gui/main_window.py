"""Main BooruApp window class."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QUrl
from PySide6.QtGui import QPixmap, QAction, QKeySequence, QDesktopServices, QShortcut
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
    QTextEdit,
    QSpinBox,
    QProgressBar,
)

from ..core.db import Database, Site
from ..core.api.base import BooruClient, Post
from ..core.api.detect import client_for_type
from ..core.cache import download_image

from .grid import ThumbnailGrid
from .preview_pane import ImagePreview
from .search import SearchBar
from .sites import SiteManagerDialog
from .bookmarks import BookmarksView
from .library import LibraryView
from .settings import SettingsDialog

from .log_handler import LogHandler
from .async_signals import AsyncSignals
from .info_panel import InfoPanel
from .window_state import WindowStateController
from .privacy import PrivacyController
from .search_controller import SearchController
from .media_controller import MediaController
from .popout_controller import PopoutController
from .post_actions import PostActionsController
from .context_menus import ContextMenuHandler

log = logging.getLogger("booru")


# -- Main App --

class BooruApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("booru-viewer")
        self.setMinimumSize(740, 400)
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

        # Controllers must be constructed before _setup_signals and
        # _setup_ui, which wire signals to controller methods.
        self._window_state = WindowStateController(self)
        self._privacy = PrivacyController(self)
        self._search_ctrl = SearchController(self)
        self._media_ctrl = MediaController(self)
        self._popout_ctrl = PopoutController(self)
        self._post_actions = PostActionsController(self)
        self._context = ContextMenuHandler(self)

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
        s.bookmark_done.connect(self._post_actions.on_bookmark_done, Q)
        s.bookmark_error.connect(self._post_actions.on_bookmark_error, Q)
        s.autocomplete_done.connect(self._search_ctrl.on_autocomplete_done, Q)
        s.batch_progress.connect(self._post_actions.on_batch_progress, Q)
        s.batch_done.connect(self._post_actions.on_batch_done, Q)
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
        _top_bar = QWidget()
        _top_bar.setObjectName("_top_bar")
        top = QHBoxLayout(_top_bar)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(3)

        self._site_combo = QComboBox()
        self._site_combo.setMinimumWidth(80)
        self._site_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._site_combo.currentIndexChanged.connect(self._on_site_changed)
        top.addWidget(self._site_combo)

        # Rating filter
        self._rating_combo = QComboBox()
        self._rating_combo.addItems(["All", "General", "Sensitive", "Questionable", "Explicit"])
        self._rating_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._rating_combo.currentTextChanged.connect(self._on_rating_changed)
        top.addWidget(self._rating_combo)

        # Media type filter
        self._media_filter = QComboBox()
        self._media_filter.addItems(["All", "Animated", "Video", "GIF", "Audio"])
        self._media_filter.setToolTip("Filter by media type")
        self._media_filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        top.addWidget(self._media_filter)

        # Score filter
        score_label = QLabel("Score\u2265")
        top.addWidget(score_label)
        self._score_spin = QSpinBox()
        self._score_spin.setRange(0, 99999)
        self._score_spin.setValue(0)
        self._score_spin.setFixedWidth(36)
        self._score_spin.setFixedHeight(21)
        self._score_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        top.addWidget(self._score_spin)

        page_label = QLabel("Page")
        top.addWidget(page_label)
        self._page_spin = QSpinBox()
        self._page_spin.setRange(1, 99999)
        self._page_spin.setValue(1)
        self._page_spin.setFixedWidth(36)
        self._page_spin.setFixedHeight(21)
        self._page_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        top.addWidget(self._page_spin)

        self._search_bar = SearchBar(db=self._db)
        self._search_bar.search_requested.connect(self._search_ctrl.on_search)
        self._search_bar.autocomplete_requested.connect(self._search_ctrl.request_autocomplete)
        top.addWidget(self._search_bar, stretch=1)

        layout.addWidget(_top_bar)

        # Nav bar
        _nav_bar = QWidget()
        _nav_bar.setObjectName("_nav_bar")
        nav = QHBoxLayout(_nav_bar)
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(3)

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
        self._library_btn.clicked.connect(lambda: self._switch_view(2))
        nav.addWidget(self._library_btn)

        layout.addWidget(_nav_bar)

        # Main content
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: stacked views
        self._stack = QStackedWidget()

        self._grid = ThumbnailGrid()
        self._grid.post_selected.connect(self._on_post_selected)
        self._grid.post_activated.connect(self._media_ctrl.on_post_activated)
        self._grid.context_requested.connect(self._context.show_single)
        self._grid.multi_context_requested.connect(self._context.show_multi)
        self._grid.nav_past_end.connect(self._search_ctrl.on_nav_past_end)
        self._grid.nav_before_start.connect(self._search_ctrl.on_nav_before_start)
        self._stack.addWidget(self._grid)

        self._bookmarks_view = BookmarksView(self._db)
        self._bookmarks_view.bookmark_selected.connect(self._on_bookmark_selected)
        self._bookmarks_view.bookmark_activated.connect(self._on_bookmark_activated)
        self._bookmarks_view.bookmarks_changed.connect(self._post_actions.refresh_browse_saved_dots)
        self._bookmarks_view.open_in_browser_requested.connect(
            lambda site_id, post_id: self._open_post_id_in_browser(post_id, site_id=site_id)
        )
        self._stack.addWidget(self._bookmarks_view)

        self._library_view = LibraryView(db=self._db)
        self._library_view.file_selected.connect(self._on_library_selected)
        self._library_view.file_activated.connect(self._on_library_activated)
        self._library_view.files_deleted.connect(self._post_actions.on_library_files_deleted)
        self._stack.addWidget(self._library_view)

        self._splitter.addWidget(self._stack)

        # Right: preview + info (vertical split)
        self._right_splitter = right = QSplitter(Qt.Orientation.Vertical)

        self._preview = ImagePreview()
        self._preview.close_requested.connect(self._close_preview)
        self._preview.open_in_default.connect(self._open_preview_in_default)
        self._preview.open_in_browser.connect(self._open_preview_in_browser)
        self._preview.bookmark_requested.connect(self._post_actions.bookmark_from_preview)
        self._preview.bookmark_to_folder.connect(self._post_actions.bookmark_to_folder_from_preview)
        self._preview.save_to_folder.connect(self._post_actions.save_from_preview)
        self._preview.unsave_requested.connect(self._post_actions.unsave_from_preview)
        self._preview.blacklist_tag_requested.connect(self._post_actions.blacklist_tag_from_popout)
        self._preview.blacklist_post_requested.connect(self._post_actions.blacklist_post_from_popout)
        self._preview.navigate.connect(self._navigate_preview)
        self._preview.play_next_requested.connect(self._on_video_end_next)
        self._preview.fullscreen_requested.connect(self._popout_ctrl.open)
        # Library folders come from the filesystem (subdirs of saved_dir),
        # not the bookmark folders DB table — those are separate concepts.
        from ..core.config import library_folders
        self._preview.set_folders_callback(library_folders)
        # Bookmark folders feed the toolbar Bookmark-as submenu, sourced
        # from the DB so it stays in sync with the bookmarks tab combo.
        self._preview.set_bookmark_folders_callback(self._db.get_folders)
        # Wide enough that the preview toolbar (Bookmark, Save, BL Tag,
        # BL Post, [stretch], Popout) has room to lay out all five buttons
        # at their fixed widths plus spacing without clipping the rightmost
        # one or compressing the row visually.
        self._preview.setMinimumWidth(200)
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

        # Debounced saver for the right splitter (same pattern as main).
        self._right_splitter_save_timer = QTimer(self)
        self._right_splitter_save_timer.setSingleShot(True)
        self._right_splitter_save_timer.setInterval(300)
        self._right_splitter_save_timer.timeout.connect(self._window_state.save_right_splitter_sizes)
        right.splitterMoved.connect(
            lambda *_: self._right_splitter_save_timer.start()
        )

        self._splitter.addWidget(right)

        # Flip layout: preview on the left, grid on the right
        if self._db.get_setting_bool("flip_layout"):
            self._splitter.insertWidget(0, right)

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
        self._batch_action.triggered.connect(self._post_actions.batch_download)
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
            # Skip media reload if already showing this post (avoids
            # restarting video when clicking to drag an already-selected cell)
            already_showing = (
                self._preview._current_post is not None
                and self._preview._current_post.id == post.id
            )
            if self._info_panel.isVisible() and not already_showing:
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
            if not already_showing:
                self._media_ctrl.on_post_activated(index)


    def _post_id_from_library_path(self, path: Path) -> int | None:
        """Resolve a library file path back to its post_id."""
        pid = self._db.get_library_post_id_by_filename(path.name)
        if pid is not None:
            return pid
        if path.stem.isdigit():
            return int(path.stem)
        return None

    def _set_library_info(self, path: str) -> None:
        """Update info panel with library metadata for the given file."""
        post_id = self._post_id_from_library_path(Path(path))
        if post_id is None:
            return
        meta = self._db.get_library_meta(post_id)
        if meta:
            from ..core.api.base import Post
            p = Post(
                id=post_id, file_url=meta.get("file_url", ""),
                preview_url=None, tags=meta.get("tags", ""),
                score=meta.get("score", 0), rating=meta.get("rating"),
                source=meta.get("source"), tag_categories=meta.get("tag_categories", {}),
            )
            self._info_panel.set_post(p)
            info = f"#{p.id}  score:{p.score}  [{p.rating}]  {Path(path).suffix.lstrip('.').upper()}" + (f"  {p.created_at}" if p.created_at else "")
            self._status.showMessage(info)

    def _on_library_selected(self, path: str) -> None:
        self._show_library_post(path)

    def _on_library_activated(self, path: str) -> None:
        self._show_library_post(path)

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
        self._popout_ctrl.update_media(path, Path(path).name)

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
        self._preview.update_save_state(self._post_actions.is_post_saved(post.id))
        info = f"Bookmark #{fav.post_id}"

        # Try local cache first
        if fav.cached_path and Path(fav.cached_path).exists():
            self._media_ctrl.set_preview_media(fav.cached_path, info)
            self._popout_ctrl.update_media(fav.cached_path, info)
            return

        # Try saved library — walk by post id; the file may live in any
        # library folder regardless of which bookmark folder fav is in.
        # Pass db so templated filenames also match (without it, only
        # legacy digit-stem files would be found).
        from ..core.config import find_library_files
        for path in find_library_files(fav.post_id, db=self._db):
            self._media_ctrl.set_preview_media(str(path), info)
            self._popout_ctrl.update_media(str(path), info)
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
                    if self._popout_ctrl.window and self._popout_ctrl.window.isVisible():
                        self._popout_ctrl.window.pause_media()
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
                if self._popout_ctrl.window and self._popout_ctrl.window.isVisible():
                    self._popout_ctrl.window.pause_media()
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

    def _close_preview(self) -> None:
        self._preview.clear()

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
        from ..core.cache import cached_path_for
        path = cached_path_for(post.file_url)
        if path.exists():
            # Pause any playing video before opening externally
            self._preview._video_player.pause()
            if self._popout_ctrl.window and self._popout_ctrl.window.isVisible():
                self._popout_ctrl.window.pause_media()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self._status.showMessage("Image not cached yet — double-click to download first")

    # -- Batch download --

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
        if key in (Qt.Key.Key_F, Qt.Key.Key_B) and self._posts:
            idx = self._grid.selected_index
            if 0 <= idx < len(self._posts):
                self._post_actions.toggle_bookmark(idx)
                return
        if key == Qt.Key.Key_S and self._posts:
            idx = self._grid.selected_index
            if 0 <= idx < len(self._posts):
                self._post_actions.toggle_save_from_preview()
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
