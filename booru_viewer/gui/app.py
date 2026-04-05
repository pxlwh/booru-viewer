"""Main Qt6 application window."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QUrl
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
    QMessageBox,
    QTextEdit,
    QMenu,
    QFileDialog,
    QSpinBox,
    QScrollArea,
    QProgressBar,
)

from ..core.db import Database, Site
from ..core.api.base import BooruClient, Post
from ..core.api.detect import client_for_type
from ..core.cache import download_image, download_thumbnail, cache_size_bytes, evict_oldest
from ..core.config import MEDIA_EXTENSIONS

from .grid import ThumbnailGrid
from .preview import ImagePreview
from .search import SearchBar
from .sites import SiteManagerDialog
from .bookmarks import BookmarksView
from .library import LibraryView
from .settings import SettingsDialog

log = logging.getLogger("booru")


class LogHandler(logging.Handler, QObject):
    """Logging handler that emits to a QTextEdit."""

    log_signal = Signal(str)

    def __init__(self, widget: QTextEdit) -> None:
        logging.Handler.__init__(self)
        QObject.__init__(self)
        self._widget = widget
        self.log_signal.connect(self._append)
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self.log_signal.emit(msg)

    def _append(self, msg: str) -> None:
        self._widget.append(msg)
        sb = self._widget.verticalScrollBar()
        sb.setValue(sb.maximum())


class AsyncSignals(QObject):
    """Signals for async worker results."""
    search_done = Signal(list)
    search_append = Signal(list)
    search_error = Signal(str)
    thumb_done = Signal(int, str)
    image_done = Signal(str, str)
    image_error = Signal(str)
    bookmark_done = Signal(int, str)
    bookmark_error = Signal(str)
    autocomplete_done = Signal(list)
    batch_progress = Signal(int, int)      # current, total
    batch_done = Signal(str)
    download_progress = Signal(int, int)  # bytes_downloaded, total_bytes
    prefetch_progress = Signal(int, float)  # index, progress (0-1 or -1 to hide)


# -- Info Panel --

class InfoPanel(QWidget):
    """Toggleable panel showing post details."""

    tag_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self._title = QLabel("No post selected")
        self._title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._title)

        self._details = QLabel()
        self._details.setWordWrap(True)
        self._details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._details)

        self._tags_label = QLabel("Tags:")
        self._tags_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        layout.addWidget(self._tags_label)

        self._tags_scroll = QScrollArea()
        self._tags_scroll.setWidgetResizable(True)
        self._tags_scroll.setStyleSheet("QScrollArea { border: none; }")
        self._tags_widget = QWidget()
        self._tags_flow = QVBoxLayout(self._tags_widget)
        self._tags_flow.setContentsMargins(0, 0, 0, 0)
        self._tags_flow.setSpacing(2)
        self._tags_scroll.setWidget(self._tags_widget)
        layout.addWidget(self._tags_scroll, stretch=1)

    def set_post(self, post: Post) -> None:
        log.debug(f"InfoPanel: tag_categories={list(post.tag_categories.keys()) if post.tag_categories else 'empty'}")
        self._title.setText(f"Post #{post.id}")
        filetype = Path(post.file_url.split("?")[0]).suffix.lstrip(".").upper() if post.file_url else "unknown"
        self._details.setText(
            f"Size: {post.width}x{post.height}\n"
            f"Score: {post.score}\n"
            f"Rating: {post.rating or 'unknown'}\n"
            f"Filetype: {filetype}\n"
            f"Source: {post.source or 'none'}"
        )
        # Clear old tags
        while self._tags_flow.count():
            item = self._tags_flow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Tag category colors
        _CAT_COLORS = {
            "Artist": "#f2ac08",
            "Character": "#0a0",
            "Copyright": "#c0f",
            "Species": "#e44",
            "General": "",
            "Meta": "#888",
            "Lore": "#888",
        }

        if post.tag_categories:
            # Display tags grouped by category
            for category, tags in post.tag_categories.items():
                color = _CAT_COLORS.get(category, "")
                header = QLabel(f"{category}:")
                header.setStyleSheet(
                    f"font-weight: bold; margin-top: 6px; margin-bottom: 2px;"
                    + (f" color: {color};" if color else "")
                )
                self._tags_flow.addWidget(header)
                for tag in tags[:50]:
                    btn = QPushButton(tag)
                    btn.setFlat(True)
                    btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    style = "QPushButton { text-align: left; padding: 1px 4px; border: none;"
                    if color:
                        style += f" color: {color};"
                    style += " }"
                    btn.setStyleSheet(style)
                    btn.clicked.connect(lambda checked, t=tag: self.tag_clicked.emit(t))
                    self._tags_flow.addWidget(btn)
        else:
            # Fallback: flat tag list (Gelbooru, Moebooru)
            for tag in post.tag_list[:100]:
                btn = QPushButton(tag)
                btn.setFlat(True)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setStyleSheet(
                    "QPushButton { text-align: left; padding: 1px 4px; border: none; }"
                )
                btn.clicked.connect(lambda checked, t=tag: self.tag_clicked.emit(t))
                self._tags_flow.addWidget(btn)
        self._tags_flow.addStretch()

    def clear(self) -> None:
        self._title.setText("No post selected")
        self._details.setText("")
        while self._tags_flow.count():
            item = self._tags_flow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()



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
        self._current_site: Site | None = None
        self._posts: list[Post] = []
        self._current_page = 1
        self._current_tags = ""
        self._current_rating = "all"
        self._min_score = 0
        self._loading = False
        self._last_scroll_page = 0
        self._prefetch_pause = asyncio.Event()
        self._prefetch_pause.set()  # not paused
        self._signals = AsyncSignals()

        self._async_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(target=self._async_loop.run_forever, daemon=True)
        self._async_thread.start()

        # Reset shared HTTP clients from previous session
        from ..core.cache import _get_shared_client
        from ..core.api.base import BooruClient
        BooruClient._shared_client = None
        import booru_viewer.core.cache as _cache_mod
        _cache_mod._shared_client = None

        self._setup_signals()
        self._setup_ui()
        self._setup_menu()
        self._load_sites()

    def _setup_signals(self) -> None:
        Q = Qt.ConnectionType.QueuedConnection
        s = self._signals
        s.search_done.connect(self._on_search_done, Q)
        s.search_append.connect(self._on_search_append, Q)
        s.search_error.connect(self._on_search_error, Q)
        s.thumb_done.connect(self._on_thumb_done, Q)
        s.image_done.connect(self._on_image_done, Q)
        s.image_error.connect(self._on_image_error, Q)
        s.bookmark_done.connect(self._on_bookmark_done, Q)
        s.bookmark_error.connect(self._on_bookmark_error, Q)
        s.autocomplete_done.connect(self._on_autocomplete_done, Q)
        s.batch_progress.connect(self._on_batch_progress, Q)
        s.batch_done.connect(lambda m: self._status.showMessage(m), Q)
        s.download_progress.connect(self._on_download_progress, Q)
        s.prefetch_progress.connect(self._on_prefetch_progress, Q)

    def _on_prefetch_progress(self, index: int, progress: float) -> None:
        if 0 <= index < len(self._grid._thumbs):
            self._grid._thumbs[index].set_prefetch_progress(progress)

    def _clear_loading(self) -> None:
        self._loading = False

    def _on_search_error(self, e: str) -> None:
        self._loading = False
        self._status.showMessage(f"Error: {e}")

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

        # Score filter
        score_label = QLabel("Score≥")
        top.addWidget(score_label)
        self._score_spin = QSpinBox()
        self._score_spin.setRange(0, 99999)
        self._score_spin.setValue(0)
        self._score_spin.setFixedWidth(50)
        self._score_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        top.addWidget(self._score_spin)
        score_down = QPushButton("-")
        score_down.setFixedWidth(25)
        score_down.clicked.connect(lambda: self._score_spin.setValue(max(0, self._score_spin.value() - 1)))
        top.addWidget(score_down)
        score_up = QPushButton("+")
        score_up.setFixedWidth(25)
        score_up.clicked.connect(lambda: self._score_spin.setValue(self._score_spin.value() + 1))
        top.addWidget(score_up)

        self._search_bar = SearchBar(db=self._db)
        self._search_bar.search_requested.connect(self._on_search)
        self._search_bar.autocomplete_requested.connect(self._request_autocomplete)
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
        self._grid.post_activated.connect(self._on_post_activated)
        self._grid.context_requested.connect(self._on_context_menu)
        self._grid.multi_context_requested.connect(self._on_multi_context_menu)
        self._grid.nav_past_end.connect(self._on_nav_past_end)
        self._grid.nav_before_start.connect(self._on_nav_before_start)
        self._grid.page_forward.connect(self._next_page)
        self._grid.page_back.connect(self._prev_page)
        self._stack.addWidget(self._grid)

        self._bookmarks_view = BookmarksView(self._db)
        self._bookmarks_view.bookmark_selected.connect(self._on_bookmark_selected)
        self._bookmarks_view.bookmark_activated.connect(self._on_bookmark_activated)
        self._stack.addWidget(self._bookmarks_view)

        self._library_view = LibraryView(db=self._db)
        self._library_view.file_selected.connect(self._on_library_selected)
        self._library_view.file_activated.connect(self._on_library_activated)
        self._stack.addWidget(self._library_view)

        self._splitter.addWidget(self._stack)

        # Right: preview + info (vertical split)
        self._right_splitter = right = QSplitter(Qt.Orientation.Vertical)

        self._preview = ImagePreview()
        self._preview.close_requested.connect(self._close_preview)
        self._preview.open_in_default.connect(self._open_preview_in_default)
        self._preview.open_in_browser.connect(self._open_preview_in_browser)
        self._preview.bookmark_requested.connect(self._bookmark_from_preview)
        self._preview.save_to_folder.connect(self._save_from_preview)
        self._preview.unsave_requested.connect(self._unsave_from_preview)
        self._preview.navigate.connect(self._navigate_preview)
        self._preview.fullscreen_requested.connect(self._open_fullscreen_preview)
        self._preview.set_folders_callback(self._db.get_folders)
        self._fullscreen_window = None
        self._preview.setMinimumWidth(300)
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

        right.setSizes([500, 0, 200])
        self._splitter.addWidget(right)

        self._splitter.setSizes([600, 500])
        layout.addWidget(self._splitter, stretch=1)

        # Bottom page nav (centered)
        self._bottom_nav = QWidget()
        bottom_nav = QHBoxLayout(self._bottom_nav)
        bottom_nav.setContentsMargins(0, 4, 0, 4)
        bottom_nav.addStretch()
        self._page_label = QLabel("Page 1")
        bottom_nav.addWidget(self._page_label)
        bottom_prev = QPushButton("Prev")
        bottom_prev.setFixedWidth(60)
        bottom_prev.clicked.connect(self._prev_page)
        bottom_nav.addWidget(bottom_prev)
        bottom_next = QPushButton("Next")
        bottom_next.setFixedWidth(60)
        bottom_next.clicked.connect(self._next_page)
        bottom_nav.addWidget(bottom_next)
        bottom_nav.addStretch()
        layout.addWidget(self._bottom_nav)

        # Infinite scroll
        self._infinite_scroll = self._db.get_setting_bool("infinite_scroll")
        if self._infinite_scroll:
            self._bottom_nav.hide()
        self._grid.reached_bottom.connect(self._on_reached_bottom)

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

        batch_action = QAction("Batch &Download Page...", self)
        batch_action.setShortcut(QKeySequence("Ctrl+D"))
        batch_action.triggered.connect(self._batch_download)
        file_menu.addAction(batch_action)

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
        privacy_action.triggered.connect(self._toggle_privacy)
        view_menu.addAction(privacy_action)

    def _load_sites(self) -> None:
        self._site_combo.clear()
        for site in self._db.get_sites():
            self._site_combo.addItem(site.name, site.id)

    def _make_client(self) -> BooruClient | None:
        if not self._current_site:
            return None
        s = self._current_site
        return client_for_type(s.api_type, s.url, s.api_key, s.api_user)

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

    def _on_rating_changed(self, text: str) -> None:
        self._current_rating = text.lower()

    def _switch_view(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._browse_btn.setChecked(index == 0)
        self._bookmark_btn.setChecked(index == 1)
        self._library_btn.setChecked(index == 2)
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
        self._on_search(tag)

    # -- Search --

    def _on_search(self, tags: str) -> None:
        self._current_tags = tags
        self._current_page = 1
        self._shown_post_ids = set()
        self._page_cache = {}
        self._infinite_exhausted = False
        self._min_score = self._score_spin.value()
        self._preview.clear()
        self._do_search()

    def _prev_page(self) -> None:
        if self._current_page > 1:
            self._current_page -= 1
            if self._current_page in self._page_cache:
                self._signals.search_done.emit(self._page_cache[self._current_page])
            else:
                self._do_search()

    def _next_page(self) -> None:
        if self._loading:
            return
        self._current_page += 1
        if self._current_page in getattr(self, '_page_cache', {}):
            self._signals.search_done.emit(self._page_cache[self._current_page])
            return
        self._do_search()

    def _on_nav_past_end(self) -> None:
        if self._infinite_scroll:
            return  # infinite scroll handles this via reached_bottom
        self._nav_page_turn = "first"
        self._next_page()

    def _on_nav_before_start(self) -> None:
        if self._infinite_scroll:
            return
        if self._current_page > 1:
            self._nav_page_turn = "last"
            self._prev_page()

    def _on_reached_bottom(self) -> None:
        if not self._infinite_scroll or self._loading or getattr(self, '_infinite_exhausted', False):
            return
        self._loading = True
        self._current_page += 1

        search_tags = self._build_search_tags()
        page = self._current_page
        limit = self._db.get_setting_int("page_size") or 40

        bl_tags = set()
        if self._db.get_setting_bool("blacklist_enabled"):
            bl_tags = set(self._db.get_blacklisted_tags())
        bl_posts = self._db.get_blacklisted_posts()
        shown_ids = getattr(self, '_shown_post_ids', set()).copy()

        def _filter(posts):
            if bl_tags:
                posts = [p for p in posts if not bl_tags.intersection(p.tag_list)]
            if bl_posts:
                posts = [p for p in posts if p.file_url not in bl_posts]
            posts = [p for p in posts if p.id not in shown_ids]
            return posts

        async def _search():
            client = self._make_client()
            try:
                collected = []
                current_page = page
                for _ in range(5):
                    batch = await client.search(tags=search_tags, page=current_page, limit=limit)
                    filtered = _filter(batch)
                    collected.extend(filtered)
                    if len(collected) >= limit or len(batch) < limit:
                        break
                    current_page += 1
                self._signals.search_append.emit(collected[:limit])
            except Exception as e:
                log.warning(f"Operation failed: {e}")
            finally:
                await client.close()

        self._run_async(_search)

    def _scroll_next_page(self) -> None:
        if self._loading:
            return
        self._current_page += 1
        self._do_search()

    def _scroll_prev_page(self) -> None:
        if self._loading or self._current_page <= 1:
            return
        self._current_page -= 1
        self._do_search()

    def _build_search_tags(self) -> str:
        """Build tag string with rating filter and negative tags."""
        parts = []
        if self._current_tags:
            parts.append(self._current_tags)

        # Rating filter — site-specific syntax
        # Danbooru/Gelbooru: 4-tier (general, sensitive, questionable, explicit)
        # Moebooru/e621: 3-tier (safe, questionable, explicit)
        rating = self._current_rating
        if rating != "all" and self._current_site:
            api = self._current_site.api_type
            if api == "danbooru":
                # Danbooru accepts both full words and single letters
                danbooru_map = {
                    "general": "g", "sensitive": "s",
                    "questionable": "q", "explicit": "e",
                }
                if rating in danbooru_map:
                    parts.append(f"rating:{danbooru_map[rating]}")
            elif api == "gelbooru":
                # Gelbooru requires full words, no abbreviations
                gelbooru_map = {
                    "general": "general", "sensitive": "sensitive",
                    "questionable": "questionable", "explicit": "explicit",
                }
                if rating in gelbooru_map:
                    parts.append(f"rating:{gelbooru_map[rating]}")
            elif api == "e621":
                # e621: 3-tier (s/q/e), accepts both full words and letters
                e621_map = {
                    "general": "s", "sensitive": "s",
                    "questionable": "q", "explicit": "e",
                }
                if rating in e621_map:
                    parts.append(f"rating:{e621_map[rating]}")
            else:
                # Moebooru (yande.re, konachan) — 3-tier, full words work
                # "general" and "sensitive" don't exist, map to "safe"
                moebooru_map = {
                    "general": "safe", "sensitive": "safe",
                    "questionable": "questionable", "explicit": "explicit",
                }
                if rating in moebooru_map:
                    parts.append(f"rating:{moebooru_map[rating]}")

        # Score filter
        if self._min_score > 0:
            parts.append(f"score:>={self._min_score}")

        return " ".join(parts)

    def _do_search(self) -> None:
        if not self._current_site:
            self._status.showMessage("No site selected")
            return
        self._loading = True
        self._page_label.setText(f"Page {self._current_page}")
        self._status.showMessage("Searching...")

        search_tags = self._build_search_tags()
        log.info(f"Search: tags='{search_tags}' rating={self._current_rating}")
        page = self._current_page
        limit = self._db.get_setting_int("page_size") or 40

        # Gather blacklist filters once on the main thread
        bl_tags = set()
        if self._db.get_setting_bool("blacklist_enabled"):
            bl_tags = set(self._db.get_blacklisted_tags())
        bl_posts = self._db.get_blacklisted_posts()
        shown_ids = getattr(self, '_shown_post_ids', set()).copy()

        def _filter(posts):
            if bl_tags:
                posts = [p for p in posts if not bl_tags.intersection(p.tag_list)]
            if bl_posts:
                posts = [p for p in posts if p.file_url not in bl_posts]
            # Skip posts already shown on previous pages
            posts = [p for p in posts if p.id not in shown_ids]
            return posts

        async def _search():
            client = self._make_client()
            try:
                collected = []
                current_page = page
                max_pages = 5  # safety cap to avoid infinite fetching
                for _ in range(max_pages):
                    batch = await client.search(tags=search_tags, page=current_page, limit=limit)
                    filtered = _filter(batch)
                    collected.extend(filtered)
                    log.debug(f"Backfill: page={current_page} batch={len(batch)} filtered={len(filtered)} total={len(collected)}/{limit}")
                    if len(collected) >= limit or len(batch) < limit:
                        break
                    current_page += 1
                self._signals.search_done.emit(collected[:limit])
            except Exception as e:
                self._signals.search_error.emit(str(e))
            finally:
                await client.close()

        self._run_async(_search)

    def _on_search_done(self, posts: list) -> None:
        self._page_label.setText(f"Page {self._current_page}")
        self._posts = posts
        # Cache page results and track shown IDs
        if not hasattr(self, '_shown_post_ids'):
            self._shown_post_ids = set()
        if not hasattr(self, '_page_cache'):
            self._page_cache = {}
        self._shown_post_ids.update(p.id for p in posts)
        self._page_cache[self._current_page] = posts
        # Cap page cache in pagination mode (infinite scroll needs all pages)
        if not self._infinite_scroll and len(self._page_cache) > 10:
            oldest = min(self._page_cache.keys())
            del self._page_cache[oldest]
        self._status.showMessage(f"{len(posts)} results")
        thumbs = self._grid.set_posts(len(posts))
        self._grid.scroll_to_top()
        # Clear loading after a brief delay so scroll signals don't re-trigger
        QTimer.singleShot(100, self._clear_loading)

        from ..core.config import saved_dir, saved_folder_dir
        site_id = self._site_combo.currentData()

        # Pre-scan saved directories once instead of per-post exists() calls
        _sd = saved_dir()
        _saved_ids: set[int] = set()
        if _sd.exists():
            _saved_ids = {int(f.stem) for f in _sd.iterdir() if f.is_file() and f.stem.isdigit()}
        _folder_saved: dict[str, set[int]] = {}
        for folder in self._db.get_folders():
            d = saved_folder_dir(folder)
            if d.exists():
                _folder_saved[folder] = {int(f.stem) for f in d.iterdir() if f.is_file() and f.stem.isdigit()}

        # Pre-fetch bookmarks for the site once (used for folder checks)
        _favs = self._db.get_bookmarks(site_id=site_id) if site_id else []

        for i, (post, thumb) in enumerate(zip(posts, thumbs)):
            # Bookmark status (DB)
            if site_id and self._db.is_bookmarked(site_id, post.id):
                thumb.set_bookmarked(True)
            # Saved status (filesystem) — independent of bookmark
            saved = post.id in _saved_ids
            if not saved:
                for folder_name, folder_ids in _folder_saved.items():
                    if post.id in folder_ids:
                        saved = True
                        break
            thumb.set_saved_locally(saved)
            # Set drag path from cache
            from ..core.cache import cached_path_for
            cached = cached_path_for(post.file_url)
            if cached.exists():
                thumb._cached_path = str(cached)

            if post.preview_url:
                self._fetch_thumbnail(i, post.preview_url)

        # Auto-select first/last post if page turn was triggered by navigation
        turn = getattr(self, "_nav_page_turn", None)
        if turn and posts:
            self._nav_page_turn = None
            if turn == "first":
                idx = 0
            else:
                idx = len(posts) - 1
            self._grid._select(idx)
            self._on_post_activated(idx)

        self._grid.setFocus()

        # Start prefetching from top of page
        if self._db.get_setting_bool("prefetch_adjacent") and posts:
            self._prefetch_adjacent(0)

        # Infinite scroll: if first page doesn't fill viewport, load more
        if self._infinite_scroll and posts:
            QTimer.singleShot(200, self._check_viewport_fill)

    def _check_viewport_fill(self) -> None:
        """If content doesn't fill the viewport, trigger infinite scroll."""
        if not self._infinite_scroll or self._loading or getattr(self, '_infinite_exhausted', False):
            return
        sb = self._grid.verticalScrollBar()
        if sb.maximum() == 0:
            self._on_reached_bottom()

    def _on_search_append(self, posts: list) -> None:
        """Queue posts and add them one at a time as thumbnails arrive."""
        if not posts:
            self._loading = False
            self._infinite_exhausted = True
            self._status.showMessage(f"{len(self._posts)} results (end)")
            return
        self._shown_post_ids.update(p.id for p in posts)

        if not hasattr(self, '_append_queue'):
            self._append_queue = []
        self._append_queue.extend(posts)
        self._drain_append_queue()

    def _drain_append_queue(self) -> None:
        """Add queued posts to the grid one at a time with thumbnail fetch."""
        if not getattr(self, '_append_queue', None) or len(self._append_queue) == 0:
            self._loading = False
            return

        from ..core.config import saved_dir
        from ..core.cache import cached_path_for
        site_id = self._site_combo.currentData()
        _sd = saved_dir()
        _saved_ids: set[int] = set()
        if _sd.exists():
            _saved_ids = {int(f.stem) for f in _sd.iterdir() if f.is_file() and f.stem.isdigit()}

        post = self._append_queue.pop(0)
        idx = len(self._posts)
        self._posts.append(post)
        thumbs = self._grid.append_posts(1)
        thumb = thumbs[0]

        if site_id and self._db.is_bookmarked(site_id, post.id):
            thumb.set_bookmarked(True)
        thumb.set_saved_locally(post.id in _saved_ids)
        cached = cached_path_for(post.file_url)
        if cached.exists():
            thumb._cached_path = str(cached)
        if post.preview_url:
            self._fetch_thumbnail(idx, post.preview_url)

        self._status.showMessage(f"{len(self._posts)} results")

        # Schedule next post
        if self._append_queue:
            QTimer.singleShot(50, self._drain_append_queue)
        else:
            # All done — unlock loading and prefetch
            self._loading = False
            if self._db.get_setting_bool("prefetch_adjacent"):
                self._prefetch_adjacent(idx)
            # Check if still at bottom or content doesn't fill viewport
            sb = self._grid.verticalScrollBar()
            from .grid import THUMB_SIZE, THUMB_SPACING
            threshold = THUMB_SIZE + THUMB_SPACING * 2
            if sb.maximum() == 0 or sb.value() >= sb.maximum() - threshold:
                self._on_reached_bottom()

    def _fetch_thumbnail(self, index: int, url: str) -> None:
        async def _download():
            try:
                path = await download_thumbnail(url)
                self._signals.thumb_done.emit(index, str(path))
            except Exception as e:
                log.warning(f"Thumb #{index} failed: {e}")
        self._run_async(_download)

    def _on_thumb_done(self, index: int, path: str) -> None:
        thumbs = self._grid._thumbs
        if 0 <= index < len(thumbs):
            pix = QPixmap(path)
            if not pix.isNull():
                thumbs[index].set_pixmap(pix)

    # -- Autocomplete --

    def _request_autocomplete(self, query: str) -> None:
        if not self._current_site or len(query) < 2:
            return

        async def _ac():
            client = self._make_client()
            try:
                results = await client.autocomplete(query)
                self._signals.autocomplete_done.emit(results)
            except Exception as e:
                log.warning(f"Operation failed: {e}")
            finally:
                await client.close()

        self._run_async(_ac)

    def _on_autocomplete_done(self, suggestions: list) -> None:
        self._search_bar.set_suggestions(suggestions)

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
            )
            if self._info_panel.isVisible():
                self._info_panel.set_post(post)
            self._on_post_activated(index)

    def _on_post_activated(self, index: int) -> None:
        if 0 <= index < len(self._posts):
            post = self._posts[index]
            log.info(f"Preview: #{post.id} -> {post.file_url}")
            self._status.showMessage(f"Loading #{post.id}...")
            self._dl_progress.show()
            self._dl_progress.setRange(0, 0)

            def _progress(downloaded, total):
                self._signals.download_progress.emit(downloaded, total)

            async def _load():
                self._prefetch_pause.clear()  # pause prefetch
                try:
                    path = await download_image(post.file_url, progress_callback=_progress)
                    info = f"#{post.id}  {post.width}x{post.height}  score:{post.score}  [{post.rating}]  {Path(post.file_url.split('?')[0]).suffix.lstrip('.').upper() if post.file_url else ''}"
                    self._signals.image_done.emit(str(path), info)
                except Exception as e:
                    log.error(f"Image download failed: {e}")
                    self._signals.image_error.emit(str(e))
                finally:
                    self._prefetch_pause.set()  # resume prefetch

            self._run_async(_load)

            # Prefetch adjacent posts
            if self._db.get_setting_bool("prefetch_adjacent"):
                self._prefetch_adjacent(index)

    def _prefetch_adjacent(self, index: int) -> None:
        """Prefetch outward from clicked post in all directions, covering the whole page."""
        total = len(self._posts)
        if total == 0:
            return
        cols = self._grid._flow.columns

        # Build ring order: at each distance, grab all 8 directions
        seen = {index}
        order = []
        for dist in range(1, total):
            ring = set()
            for dy in (-dist, 0, dist):
                for dx in (-dist, 0, dist):
                    if dy == 0 and dx == 0:
                        continue
                    adj = index + dy * cols + dx
                    if 0 <= adj < total and adj not in seen:
                        ring.add(adj)
            # Also add pure linear neighbors for non-grid nav
            for adj in (index + dist, index - dist):
                if 0 <= adj < total and adj not in seen:
                    ring.add(adj)
            for adj in sorted(ring):
                seen.add(adj)
                order.append(adj)
            if len(order) >= total - 1:
                break

        async def _prefetch_spiral():
            for adj in order:
                await self._prefetch_pause.wait()  # yield to active downloads
                if 0 <= adj < len(self._posts) and self._posts[adj].file_url:
                    self._signals.prefetch_progress.emit(adj, 0.0)
                    try:
                        def _progress(dl, total_bytes, idx=adj):
                            if total_bytes > 0:
                                self._signals.prefetch_progress.emit(idx, dl / total_bytes)
                        await download_image(self._posts[adj].file_url, progress_callback=_progress)
                    except Exception as e:
                        log.warning(f"Operation failed: {e}")
                    self._signals.prefetch_progress.emit(adj, -1)
                    await asyncio.sleep(0.2)  # gentle pacing
        self._run_async(_prefetch_spiral)

    def _on_download_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            self._dl_progress.setRange(0, total)
            self._dl_progress.setValue(downloaded)
            self._dl_progress.show()
            mb = downloaded / (1024 * 1024)
            total_mb = total / (1024 * 1024)
            self._status.showMessage(f"Downloading... {mb:.1f}/{total_mb:.1f} MB")
        else:
            self._dl_progress.setRange(0, 0)  # indeterminate
            self._dl_progress.show()

    def _set_preview_media(self, path: str, info: str) -> None:
        """Set media on preview or just info if slideshow is open."""
        if self._fullscreen_window and self._fullscreen_window.isVisible():
            self._preview._info_label.setText(info)
            self._preview._current_path = path
        else:
            self._preview.set_media(path, info)

    def _update_fullscreen(self, path: str, info: str) -> None:
        """Sync the fullscreen window with the current preview media."""
        if self._fullscreen_window and self._fullscreen_window.isVisible():
            self._preview._video_player.stop()
            self._fullscreen_window.set_media(path, info)
            # Show/hide action buttons based on current tab
            show = self._stack.currentIndex() != 2
            self._fullscreen_window._bookmark_btn.setVisible(show)
            self._fullscreen_window._save_btn.setVisible(show)
            self._fullscreen_window._bl_tag_btn.setVisible(show)
            self._fullscreen_window._bl_post_btn.setVisible(show)
            if show:
                self._update_fullscreen_state()

    def _update_fullscreen_state(self) -> None:
        """Update slideshow button states for the current post."""
        if not self._fullscreen_window:
            return
        from ..core.config import saved_dir, saved_folder_dir, MEDIA_EXTENSIONS
        site_id = self._site_combo.currentData()

        if self._stack.currentIndex() == 1:
            # Bookmarks view
            grid = self._bookmarks_view._grid
            favs = self._bookmarks_view._bookmarks
            idx = grid.selected_index
            if 0 <= idx < len(favs):
                fav = favs[idx]
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
                self._fullscreen_window.update_state(True, saved)
            else:
                self._fullscreen_window.update_state(False, False)
        else:
            idx = self._grid.selected_index
            if 0 <= idx < len(self._posts) and site_id:
                post = self._posts[idx]
                bookmarked = self._db.is_bookmarked(site_id, post.id)
                saved = any(
                    (saved_dir() / f"{post.id}{ext}").exists()
                    for ext in MEDIA_EXTENSIONS
                )
                if not saved:
                    for folder in self._db.get_folders():
                        saved = any(
                            (saved_folder_dir(folder) / f"{post.id}{ext}").exists()
                            for ext in MEDIA_EXTENSIONS
                        )
                        if saved:
                            break
                self._fullscreen_window.update_state(bookmarked, saved)
                self._fullscreen_window.set_post_tags(post.tag_categories, post.tag_list)
            else:
                self._fullscreen_window.update_state(False, False)

    def _on_image_done(self, path: str, info: str) -> None:
        self._dl_progress.hide()
        if self._fullscreen_window and self._fullscreen_window.isVisible():
            # Slideshow is open — only show there, keep preview clear
            self._preview._info_label.setText(info)
            self._preview._current_path = path
        else:
            self._set_preview_media(path, info)
        self._status.showMessage(f"{len(self._posts)} results — Loaded")
        # Update drag path on the selected thumbnail
        idx = self._grid.selected_index
        if 0 <= idx < len(self._grid._thumbs):
            self._grid._thumbs[idx]._cached_path = path
        self._update_fullscreen(path, info)
        # Auto-evict if over cache limit
        self._auto_evict_cache()

    def _auto_evict_cache(self) -> None:
        if not self._db.get_setting_bool("auto_evict"):
            return
        max_mb = self._db.get_setting_int("max_cache_mb")
        if max_mb <= 0:
            return
        max_bytes = max_mb * 1024 * 1024
        current = cache_size_bytes(include_thumbnails=False)
        if current > max_bytes:
            protected = set()
            for fav in self._db.get_bookmarks(limit=999999):
                if fav.cached_path:
                    protected.add(fav.cached_path)
            evicted = evict_oldest(max_bytes, protected)
            if evicted:
                log.info(f"Auto-evicted {evicted} cached files")

    def _set_library_info(self, path: str) -> None:
        """Update info panel with library metadata for the given file."""
        stem = Path(path).stem
        if not stem.isdigit():
            return
        meta = self._db.get_library_meta(int(stem))
        if meta:
            from ..core.api.base import Post
            p = Post(
                id=int(stem), file_url=meta.get("file_url", ""),
                preview_url=None, tags=meta.get("tags", ""),
                score=meta.get("score", 0), rating=meta.get("rating"),
                source=meta.get("source"), tag_categories=meta.get("tag_categories", {}),
            )
            self._info_panel.set_post(p)
            info = f"#{p.id}  score:{p.score}  [{p.rating}]  {Path(path).suffix.lstrip('.').upper()}"
            self._status.showMessage(info)

    def _on_library_selected(self, path: str) -> None:
        self._set_preview_media(path, Path(path).name)
        self._update_fullscreen(path, Path(path).name)
        self._set_library_info(path)

    def _on_library_activated(self, path: str) -> None:
        self._set_preview_media(path, Path(path).name)
        self._update_fullscreen(path, Path(path).name)
        self._set_library_info(path)

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
        info = f"Bookmark #{fav.post_id}"

        # Try local cache first
        if fav.cached_path and Path(fav.cached_path).exists():
            self._set_preview_media(fav.cached_path, info)
            self._update_fullscreen(fav.cached_path, info)
            return

        # Try saved library
        from ..core.config import saved_dir, saved_folder_dir
        search_dirs = [saved_dir()]
        if fav.folder:
            search_dirs.insert(0, saved_folder_dir(fav.folder))
        for d in search_dirs:
            for ext in MEDIA_EXTENSIONS:
                path = d / f"{fav.post_id}{ext}"
                if path.exists():
                    self._set_preview_media(str(path), info)
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
        # Try the currently selected post first
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts):
            self._open_in_default(self._posts[idx])
            return
        # Fall back to finding any cached image that matches the preview
        from ..core.cache import cache_dir
        from PySide6.QtGui import QDesktopServices
        # Open the most recently modified file in cache
        cache = cache_dir()
        files = sorted(cache.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files:
            if f.is_file() and f.suffix in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(f)))
                return

    def _open_preview_in_browser(self) -> None:
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts):
            self._open_in_browser(self._posts[idx])

    def _navigate_preview(self, direction: int) -> None:
        """Navigate to prev/next post in the preview. direction: -1 or +1."""
        if self._stack.currentIndex() == 1:
            # Bookmarks view
            grid = self._bookmarks_view._grid
            favs = self._bookmarks_view._bookmarks
            idx = grid.selected_index + direction
            if 0 <= idx < len(favs):
                grid._select(idx)
                self._on_bookmark_activated(favs[idx])
        elif self._stack.currentIndex() == 2:
            # Library view
            grid = self._library_view._grid
            files = self._library_view._files
            idx = grid.selected_index + direction
            if 0 <= idx < len(files):
                grid._select(idx)
                self._library_view.file_activated.emit(str(files[idx]))
        else:
            idx = self._grid.selected_index + direction
            log.info(f"Navigate: direction={direction} current={self._grid.selected_index} next={idx} total={len(self._posts)}")
            if 0 <= idx < len(self._posts):
                self._grid._select(idx)
                self._on_post_activated(idx)
            elif idx >= len(self._posts) and direction > 0 and len(self._posts) > 0 and not self._infinite_scroll:
                self._nav_page_turn = "first"
                self._next_page()
            elif idx < 0 and direction < 0 and self._current_page > 1 and not self._infinite_scroll:
                self._nav_page_turn = "last"
                self._prev_page()

    def _bookmark_from_preview(self) -> None:
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts):
            self._toggle_bookmark(idx)
            self._update_fullscreen_state()

    def _save_from_preview(self, folder: str) -> None:
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts):
            target = folder if folder else None
            if folder and folder not in self._db.get_folders():
                self._db.add_folder(folder)
            self._save_to_library(self._posts[idx], target)
            self._update_fullscreen_state()

    def _unsave_from_preview(self) -> None:
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts):
            post = self._posts[idx]
            from ..core.cache import delete_from_library
            site_id = self._site_combo.currentData()
            folder = None
            if site_id:
                favs = self._db.get_bookmarks(site_id=site_id)
                for f in favs:
                    if f.post_id == post.id and f.folder:
                        folder = f.folder
                        break
            if delete_from_library(post.id, folder):
                self._status.showMessage(f"Removed #{post.id} from library")
                if 0 <= idx < len(self._grid._thumbs):
                    self._grid._thumbs[idx].set_saved_locally(False)
            else:
                self._status.showMessage(f"#{post.id} not in library")
            self._update_fullscreen_state()

    def _save_toggle_from_slideshow(self) -> None:
        if self._fullscreen_window and self._fullscreen_window._is_saved:
            self._unsave_from_preview()
        else:
            self._save_from_preview("")

    def _blacklist_tag_from_slideshow(self, tag: str) -> None:
        self._db.add_blacklisted_tag(tag)
        self._db.set_setting("blacklist_enabled", "1")
        self._status.showMessage(f"Blacklisted: {tag}")
        self._remove_blacklisted_from_grid(tag=tag)

    def _blacklist_post_from_slideshow(self) -> None:
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts):
            post = self._posts[idx]
            self._db.add_blacklisted_post(post.file_url)
            self._status.showMessage(f"Post #{post.id} blacklisted")
            self._remove_blacklisted_from_grid(post_url=post.file_url)

    def _open_fullscreen_preview(self) -> None:
        path = self._preview._current_path
        if not path:
            return
        info = self._preview._info_label.text()
        # Grab video position before clearing
        video_pos = 0
        if self._preview._stack.currentIndex() == 1:
            video_pos = self._preview._video_player._player.position()
        # Clear the main preview — slideshow takes over
        # Hide preview, expand info panel into the freed space
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
        from .preview import FullscreenPreview
        cols = self._grid._flow.columns
        show_actions = self._stack.currentIndex() != 2
        monitor = self._db.get_setting("slideshow_monitor")
        self._fullscreen_window = FullscreenPreview(grid_cols=cols, show_actions=show_actions, monitor=monitor, parent=self)
        self._fullscreen_window.navigate.connect(self._navigate_fullscreen)
        if show_actions:
            self._fullscreen_window.bookmark_requested.connect(self._bookmark_from_preview)
            self._fullscreen_window.save_toggle_requested.connect(self._save_toggle_from_slideshow)
            self._fullscreen_window.blacklist_tag_requested.connect(self._blacklist_tag_from_slideshow)
            self._fullscreen_window.blacklist_post_requested.connect(self._blacklist_post_from_slideshow)
        self._fullscreen_window.closed.connect(self._on_fullscreen_closed)
        self._fullscreen_window.privacy_requested.connect(self._toggle_privacy)
        # Sync video player state from preview to slideshow
        pv = self._preview._video_player
        sv = self._fullscreen_window._video
        sv._audio.setMuted(pv._audio.isMuted())
        sv._audio.setVolume(pv._audio.volume())
        sv._vol_slider.setValue(pv._vol_slider.value())
        sv._mute_btn.setText(pv._mute_btn.text())
        sv._autoplay = pv._autoplay
        sv._autoplay_btn.setChecked(pv._autoplay_btn.isChecked())
        sv._autoplay_btn.setText(pv._autoplay_btn.text())
        sv._autoplay_btn.setVisible(pv._autoplay_btn.isVisible())
        sv._loop_state = pv._loop_state
        sv._loop_btn.setText(pv._loop_btn.text())
        self._fullscreen_window.set_media(path, info)
        # Seek to the position from the preview after media loads
        if video_pos > 0 and self._fullscreen_window._stack.currentIndex() == 1:
            def _seek_when_ready(status):
                from PySide6.QtMultimedia import QMediaPlayer
                if status == QMediaPlayer.MediaStatus.BufferedMedia or status == QMediaPlayer.MediaStatus.LoadedMedia:
                    self._fullscreen_window._video._player.setPosition(video_pos)
                    try:
                        self._fullscreen_window._video._player.mediaStatusChanged.disconnect(_seek_when_ready)
                    except RuntimeError:
                        pass
            self._fullscreen_window._video._player.mediaStatusChanged.connect(_seek_when_ready)
        if show_actions:
            self._update_fullscreen_state()

    def _on_fullscreen_closed(self) -> None:
        # Restore preview and info panel visibility
        self._preview.show()
        if not getattr(self, '_info_was_visible', False):
            self._info_panel.hide()
        if hasattr(self, '_right_splitter_sizes'):
            self._right_splitter.setSizes(self._right_splitter_sizes)
        # Sync video player state from slideshow back to preview
        if self._fullscreen_window:
            sv = self._fullscreen_window._video
            pv = self._preview._video_player
            pv._audio.setMuted(sv._audio.isMuted())
            pv._audio.setVolume(sv._audio.volume())
            pv._vol_slider.setValue(sv._vol_slider.value())
            pv._mute_btn.setText(sv._mute_btn.text())
            pv._autoplay = sv._autoplay
            pv._autoplay_btn.setChecked(sv._autoplay_btn.isChecked())
            pv._autoplay_btn.setText(sv._autoplay_btn.text())
            pv._autoplay_btn.setVisible(sv._autoplay_btn.isVisible())
            pv._loop_state = sv._loop_state
            pv._loop_btn.setText(sv._loop_btn.text())
        # Grab video position before cleanup
        video_pos = 0
        if self._fullscreen_window and self._fullscreen_window._stack.currentIndex() == 1:
            video_pos = self._fullscreen_window._video._player.position()
        # Restore preview with current media
        path = self._preview._current_path
        info = self._preview._info_label.text()
        self._fullscreen_window = None
        if path:
            self._preview.set_media(path, info)
            # Seek preview to slideshow position
            if video_pos > 0 and self._preview._stack.currentIndex() == 1:
                def _seek_preview(status):
                    from PySide6.QtMultimedia import QMediaPlayer
                    if status in (QMediaPlayer.MediaStatus.BufferedMedia, QMediaPlayer.MediaStatus.LoadedMedia):
                        self._preview._video_player._player.setPosition(video_pos)
                        try:
                            self._preview._video_player._player.mediaStatusChanged.disconnect(_seek_preview)
                        except RuntimeError:
                            pass
                self._preview._video_player._player.mediaStatusChanged.connect(_seek_preview)

    def _navigate_fullscreen(self, direction: int) -> None:
        self._navigate_preview(direction)
        # For synchronous loads (cached/bookmarks), update immediately
        if self._preview._current_path:
            self._update_fullscreen(
                self._preview._current_path,
                self._preview._info_label.text(),
            )

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

        unsave_lib = menu.addAction("Unsave from Library")
        copy_clipboard = menu.addAction("Copy File to Clipboard")
        copy_url = menu.addAction("Copy Image URL")
        copy_tags = menu.addAction("Copy Tags")
        menu.addSeparator()
        fav_action = menu.addAction("Remove Bookmark" if self._is_current_bookmarked(index) else "Bookmark")
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
            from PySide6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                self._db.add_folder(name.strip())
                self._save_to_library(post, name.strip())
        elif id(action) in save_lib_folders:
            self._save_to_library(post, save_lib_folders[id(action)])
        elif action == unsave_lib:
            from ..core.cache import delete_from_library
            site_id = self._site_combo.currentData()
            folder = None
            if site_id:
                favs = self._db.get_bookmarks(site_id=site_id)
                for f in favs:
                    if f.post_id == post.id and f.folder:
                        folder = f.folder
                        break
            if delete_from_library(post.id, folder):
                self._status.showMessage(f"Removed #{post.id} from library")
                if 0 <= index < len(self._grid._thumbs):
                    self._grid._thumbs[index].set_saved_locally(False)
            else:
                self._status.showMessage(f"#{post.id} not in library")
        elif action == copy_clipboard:
            self._copy_file_to_clipboard()
        elif action == copy_url:
            QApplication.clipboard().setText(post.file_url)
            self._status.showMessage("URL copied")
        elif action == copy_tags:
            QApplication.clipboard().setText(post.tags)
            self._status.showMessage("Tags copied")
        elif action == fav_action:
            self._toggle_bookmark(index)
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
                        self._fullscreen_window._viewer.clear()
                        self._fullscreen_window._video.stop()
            self._status.showMessage(f"Blacklisted: {tag}")
            self._remove_blacklisted_from_grid(tag=tag)
        elif action == bl_post_action:
            self._db.add_blacklisted_post(post.file_url)
            self._remove_blacklisted_from_grid(post_url=post.file_url)
            self._status.showMessage(f"Post #{post.id} blacklisted")
            self._do_search()

    def _remove_blacklisted_from_grid(self, tag: str = None, post_url: str = None) -> None:
        """Remove matching posts from the grid in-place without re-searching."""
        to_remove = []
        for i, post in enumerate(self._posts):
            if tag and tag in post.tag_list:
                to_remove.append(i)
            elif post_url and post.file_url == post_url:
                to_remove.append(i)

        if not to_remove:
            return

        # Check if previewed post is being removed
        from ..core.cache import cached_path_for
        for i in to_remove:
            cp = str(cached_path_for(self._posts[i].file_url))
            if cp == self._preview._current_path:
                self._preview.clear()
                if self._fullscreen_window and self._fullscreen_window.isVisible():
                    self._fullscreen_window._viewer.clear()
                    self._fullscreen_window._video.stop()
                break

        # Remove from posts list (reverse order to keep indices valid)
        for i in reversed(to_remove):
            self._posts.pop(i)

        # Rebuild grid with remaining posts
        thumbs = self._grid.set_posts(len(self._posts))
        from ..core.config import saved_dir
        site_id = self._site_combo.currentData()
        _sd = saved_dir()
        _saved_ids = {int(f.stem) for f in _sd.iterdir() if f.is_file() and f.stem.isdigit()} if _sd.exists() else set()

        for i, (post, thumb) in enumerate(zip(self._posts, thumbs)):
            if site_id and self._db.is_bookmarked(site_id, post.id):
                thumb.set_bookmarked(True)
            thumb.set_saved_locally(post.id in _saved_ids)
            from ..core.cache import cached_path_for as cpf
            cached = cpf(post.file_url)
            if cached.exists():
                thumb._cached_path = str(cached)
            if post.preview_url:
                self._fetch_thumbnail(i, post.preview_url)

        self._status.showMessage(f"{len(self._posts)} results — {len(to_remove)} removed")

    @staticmethod
    def _is_child_of_menu(action, menu) -> bool:
        parent = action.parent()
        while parent:
            if parent == menu:
                return True
            parent = getattr(parent, 'parent', lambda: None)()
        return False

    def _on_multi_context_menu(self, indices: list, pos) -> None:
        """Context menu for multi-selected posts."""
        posts = [self._posts[i] for i in indices if 0 <= i < len(self._posts)]
        if not posts:
            return
        count = len(posts)

        menu = QMenu(self)
        fav_all = menu.addAction(f"Bookmark All ({count})")

        save_menu = menu.addMenu(f"Save All to Library ({count})")
        save_unsorted = save_menu.addAction("Unsorted")
        save_folder_actions = {}
        for folder in self._db.get_folders():
            a = save_menu.addAction(folder)
            save_folder_actions[id(a)] = folder
        save_menu.addSeparator()
        save_new = save_menu.addAction("+ New Folder...")

        menu.addSeparator()
        unfav_all = menu.addAction(f"Remove All Bookmarks ({count})")
        menu.addSeparator()
        batch_dl = menu.addAction(f"Download All ({count})...")
        copy_urls = menu.addAction("Copy All URLs")

        action = menu.exec(pos)
        if not action:
            return

        if action == fav_all:
            self._bulk_bookmark(indices, posts)
        elif action == save_unsorted:
            self._bulk_save(indices, posts, None)
        elif action == save_new:
            from PySide6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
            if ok and name.strip():
                self._db.add_folder(name.strip())
                self._bulk_save(indices, posts, name.strip())
        elif id(action) in save_folder_actions:
            self._bulk_save(indices, posts, save_folder_actions[id(action)])
        elif action == batch_dl:
            from .dialogs import select_directory
            dest = select_directory(self, "Download to folder")
            if dest:
                self._batch_download_posts(posts, dest)
        elif action == unfav_all:
            site_id = self._site_combo.currentData()
            if site_id:
                from ..core.cache import delete_from_library
                from ..core.config import saved_dir, saved_folder_dir
                for post in posts:
                    # Delete from unsorted library
                    delete_from_library(post.id, None)
                    # Delete from all folders
                    for folder in self._db.get_folders():
                        delete_from_library(post.id, folder)
                    self._db.remove_bookmark(site_id, post.id)
                for idx in indices:
                    if 0 <= idx < len(self._grid._thumbs):
                        self._grid._thumbs[idx].set_bookmarked(False)
                        self._grid._thumbs[idx].set_saved_locally(False)
                self._grid._clear_multi()
                self._status.showMessage(f"Removed {count} bookmarks")
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
        site_id = self._site_combo.currentData()
        where = folder or "Unsorted"
        self._status.showMessage(f"Saving {len(posts)} to {where}...")

        async def _do():
            from ..core.config import saved_dir, saved_folder_dir
            import shutil
            for i, (idx, post) in enumerate(zip(indices, posts)):
                try:
                    path = await download_image(post.file_url)
                    ext = Path(path).suffix
                    dest_dir = saved_folder_dir(folder) if folder else saved_dir()
                    dest = dest_dir / f"{post.id}{ext}"
                    if not dest.exists():
                        shutil.copy2(path, dest)
                    if site_id and not self._db.is_bookmarked(site_id, post.id):
                        self._db.add_bookmark(
                            site_id=site_id, post_id=post.id,
                            file_url=post.file_url, preview_url=post.preview_url,
                            tags=post.tags, rating=post.rating, score=post.score,
                            source=post.source, cached_path=str(path), folder=folder,
                        )
                    self._signals.bookmark_done.emit(idx, f"Saved {i+1}/{len(posts)} to {where}")
                except Exception as e:
                    log.warning(f"Operation failed: {e}")
            self._signals.batch_done.emit(f"Saved {len(posts)} to {where}")

        self._run_async(_do)

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
        async def _batch():
            for i, post in enumerate(posts):
                try:
                    path = await download_image(post.file_url)
                    ext = Path(path).suffix
                    target = Path(dest) / f"{post.id}{ext}"
                    if not target.exists():
                        import shutil
                        shutil.copy2(path, target)
                    self._signals.batch_progress.emit(i + 1, len(posts))
                except Exception as e:
                    log.warning(f"Batch #{post.id} failed: {e}")
            self._signals.batch_done.emit(f"Downloaded {len(posts)} images to {dest}")

        self._run_async(_batch)

    def _is_current_bookmarked(self, index: int) -> bool:
        site_id = self._site_combo.currentData()
        if not site_id or index < 0 or index >= len(self._posts):
            return False
        return self._db.is_bookmarked(site_id, self._posts[index].id)

    def _open_in_browser(self, post: Post) -> None:
        if self._current_site:
            base = self._current_site.url
            api = self._current_site.api_type
            if api == "danbooru" or api == "e621":
                url = f"{base}/posts/{post.id}"
            elif api == "gelbooru":
                url = f"{base}/index.php?page=post&s=view&id={post.id}"
            elif api == "moebooru":
                url = f"{base}/post/show/{post.id}"
            else:
                url = f"{base}/posts/{post.id}"
            QDesktopServices.openUrl(QUrl(url))

    def _open_in_default(self, post: Post) -> None:
        from ..core.cache import cached_path_for, is_cached
        path = cached_path_for(post.file_url)
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self._status.showMessage("Image not cached yet — double-click to download first")

    def _save_to_library(self, post: Post, folder: str | None) -> None:
        """Download and save image to the library folder structure."""
        from ..core.config import saved_dir, saved_folder_dir

        self._status.showMessage(f"Saving #{post.id} to library...")

        async def _save():
            try:
                path = await download_image(post.file_url)
                ext = Path(path).suffix
                if folder:
                    dest_dir = saved_folder_dir(folder)
                else:
                    dest_dir = saved_dir()
                dest = dest_dir / f"{post.id}{ext}"
                if not dest.exists():
                    import shutil
                    shutil.copy2(path, dest)

                # Copy browse thumbnail to library thumbnail cache
                if post.preview_url:
                    from ..core.config import thumbnails_dir
                    from ..core.cache import cached_path_for as _cpf
                    thumb_src = _cpf(post.preview_url, thumbnails_dir())
                    if thumb_src.exists():
                        lib_thumb_dir = thumbnails_dir() / "library"
                        lib_thumb_dir.mkdir(parents=True, exist_ok=True)
                        lib_thumb = lib_thumb_dir / f"{post.id}.jpg"
                        if not lib_thumb.exists():
                            import shutil as _sh
                            _sh.copy2(thumb_src, lib_thumb)

                # Store metadata for library search
                self._db.save_library_meta(
                    post_id=post.id, tags=post.tags,
                    tag_categories=post.tag_categories,
                    score=post.score, rating=post.rating,
                    source=post.source, file_url=post.file_url,
                )

                where = folder or "Unsorted"
                self._signals.bookmark_done.emit(
                    self._grid.selected_index,
                    f"Saved #{post.id} to {where}"
                )
            except Exception as e:
                self._signals.bookmark_error.emit(str(e))

        self._run_async(_save)

    def _save_as(self, post: Post) -> None:
        from ..core.cache import cached_path_for
        from .dialogs import save_file
        src = cached_path_for(post.file_url)
        if not src.exists():
            self._status.showMessage("Image not cached — double-click to download first")
            return
        ext = src.suffix
        dest = save_file(self, "Save Image", f"post_{post.id}{ext}", f"Images (*{ext})")
        if dest:
            import shutil
            shutil.copy2(src, dest)
            self._status.showMessage(f"Saved to {dest}")

    # -- Batch download --

    def _batch_download(self) -> None:
        if not self._posts:
            self._status.showMessage("No posts to download")
            return
        from .dialogs import select_directory
        dest = select_directory(self, "Download to folder")
        if not dest:
            return

        posts = list(self._posts)
        self._status.showMessage(f"Downloading {len(posts)} images...")

        async def _batch():
            for i, post in enumerate(posts):
                try:
                    path = await download_image(post.file_url)
                    ext = Path(path).suffix
                    target = Path(dest) / f"{post.id}{ext}"
                    if not target.exists():
                        import shutil
                        shutil.copy2(path, target)
                    self._signals.batch_progress.emit(i + 1, len(posts))
                except Exception as e:
                    log.warning(f"Batch #{post.id} failed: {e}")
            self._signals.batch_done.emit(f"Downloaded {len(posts)} images to {dest}")

        self._run_async(_batch)

    def _on_batch_progress(self, current: int, total: int) -> None:
        self._status.showMessage(f"Downloading {current}/{total}...")

    # -- Toggles --

    def _toggle_log(self) -> None:
        self._log_text.setVisible(not self._log_text.isVisible())

    def _toggle_info(self) -> None:
        self._info_panel.setVisible(not self._info_panel.isVisible())
        if self._info_panel.isVisible() and 0 <= self._grid.selected_index < len(self._posts):
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
        self._infinite_scroll = self._db.get_setting_bool("infinite_scroll")
        self._bottom_nav.setVisible(not self._infinite_scroll)
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

    # -- Privacy screen --

    def _toggle_privacy(self) -> None:
        if not hasattr(self, '_privacy_on'):
            self._privacy_on = False
            self._privacy_overlay = QWidget(self)
            self._privacy_overlay.setStyleSheet("background: black;")
            self._privacy_overlay.hide()

        self._privacy_on = not self._privacy_on
        if self._privacy_on:
            self._privacy_overlay.setGeometry(self.rect())
            self._privacy_overlay.raise_()
            self._privacy_overlay.show()
            self.setWindowTitle("booru-viewer")
            # Pause preview video
            if self._preview._stack.currentIndex() == 1:
                self._preview._video_player._player.pause()
            # Hide and pause slideshow
            if self._fullscreen_window and self._fullscreen_window.isVisible():
                if self._fullscreen_window._stack.currentIndex() == 1:
                    self._fullscreen_window._video._player.pause()
                self._fullscreen_window.hide()
        else:
            self._privacy_overlay.hide()
            if self._fullscreen_window:
                self._fullscreen_window.show()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, '_privacy_overlay') and self._privacy_on:
            self._privacy_overlay.setGeometry(self.rect())

    # -- Keyboard shortcuts --

    def keyPressEvent(self, event) -> None:
        key = event.key()
        # Privacy screen always works
        if key == Qt.Key.Key_P and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self._toggle_privacy()
            return
        # If privacy is on, only allow toggling it off
        if hasattr(self, '_privacy_on') and self._privacy_on:
            return
        if key == Qt.Key.Key_F and self._posts:
            idx = self._grid.selected_index
            if 0 <= idx < len(self._posts):
                self._toggle_bookmark(idx)
                return
        elif key == Qt.Key.Key_I:
            self._toggle_info()
            return
        elif key == Qt.Key.Key_O and self._posts:
            idx = self._grid.selected_index
            if 0 <= idx < len(self._posts):
                self._open_in_default(self._posts[idx])
                return
        elif key == Qt.Key.Key_Space:
            if self._preview._stack.currentIndex() == 1 and self._preview.underMouse():
                self._preview._video_player._toggle_play()
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

    def _toggle_bookmark(self, index: int) -> None:
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
                        tag_categories=post.tag_categories,
                    )
                    self._signals.bookmark_done.emit(index, f"Bookmarked #{post.id}")
                except Exception as e:
                    self._signals.bookmark_error.emit(str(e))

            self._run_async(_fav)

    def _on_bookmark_done(self, index: int, msg: str) -> None:
        self._status.showMessage(f"{len(self._posts)} results — {msg}")
        thumbs = self._grid._thumbs
        if 0 <= index < len(thumbs):
            if "Saved" in msg:
                thumbs[index].set_saved_locally(True)
            if "Bookmarked" in msg:
                thumbs[index].set_bookmarked(True)
        self._update_fullscreen_state()

    def closeEvent(self, event) -> None:
        self._async_loop.call_soon_threadsafe(self._async_loop.stop)
        self._async_thread.join(timeout=2)
        if self._db.get_setting_bool("clear_cache_on_exit"):
            from ..core.cache import clear_cache
            clear_cache(clear_images=True, clear_thumbnails=True)
            self._db.clear_search_history()
        self._db.close()
        super().closeEvent(event)


def _apply_windows_dark_mode(app: QApplication) -> None:
    """Detect Windows dark mode and apply Fusion dark palette if needed."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        if value == 0:
            from PySide6.QtGui import QPalette, QColor
            app.setStyle("Fusion")
            palette = QPalette()
            palette.setColor(QPalette.ColorRole.Window, QColor(32, 32, 32))
            palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
            palette.setColor(QPalette.ColorRole.AlternateBase, QColor(38, 38, 38))
            palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(50, 50, 50))
            palette.setColor(QPalette.ColorRole.ToolTipText, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Button, QColor(51, 51, 51))
            palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
            palette.setColor(QPalette.ColorRole.Link, QColor(0, 120, 215))
            palette.setColor(QPalette.ColorRole.Highlight, QColor(0, 120, 215))
            palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Mid, QColor(51, 51, 51))
            palette.setColor(QPalette.ColorRole.Dark, QColor(25, 25, 25))
            palette.setColor(QPalette.ColorRole.Shadow, QColor(0, 0, 0))
            palette.setColor(QPalette.ColorRole.Light, QColor(60, 60, 60))
            palette.setColor(QPalette.ColorRole.Midlight, QColor(55, 55, 55))
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(127, 127, 127))
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(127, 127, 127))
            app.setPalette(palette)
            # Flatten Fusion's 3D look
            app.setStyleSheet(app.styleSheet() + """
                QPushButton {
                    border: 1px solid #555;
                    border-radius: 2px;
                    padding: 4px 12px;
                }
                QPushButton:hover { background-color: #444; }
                QPushButton:pressed { background-color: #333; }
                QComboBox {
                    border: 1px solid #555;
                    border-radius: 2px;
                    padding: 3px 6px;
                }
                QSpinBox {
                    border: 1px solid #555;
                    border-radius: 2px;
                }
                QLineEdit, QTextEdit {
                    border: 1px solid #555;
                    border-radius: 2px;
                    padding: 3px;
                    color: #fff;
                    background-color: #191919;
                }
                QScrollBar:vertical {
                    background: #252525;
                    width: 12px;
                }
                QScrollBar::handle:vertical {
                    background: #555;
                    border-radius: 4px;
                    min-height: 20px;
                }
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                    height: 0;
                }
            """)
    except Exception as e:
        log.warning(f"Operation failed: {e}")


def run() -> None:
    from ..core.config import data_dir

    app = QApplication(sys.argv)

    # Apply dark mode on Windows 10+ if system is set to dark
    if sys.platform == "win32":
        _apply_windows_dark_mode(app)

    # Load user custom stylesheet if it exists
    custom_css = data_dir() / "custom.qss"
    if custom_css.exists():
        try:
            # Use Fusion style with arrow color fix
            from PySide6.QtWidgets import QProxyStyle
            from PySide6.QtGui import QPalette, QColor, QPainter as _P
            from PySide6.QtCore import QPoint as _QP

            import re
            css_text = custom_css.read_text()

            # Extract text color for arrows
            m = re.search(r'QWidget\s*\{[^}]*?(?:^|\s)color\s*:\s*(#[0-9a-fA-F]{3,8})', css_text, re.MULTILINE)
            arrow_color = QColor(m.group(1)) if m else QColor(200, 200, 200)

            class _DarkArrowStyle(QProxyStyle):
                """Fusion proxy that draws visible arrows on dark themes."""
                def drawPrimitive(self, element, option, painter, widget=None):
                    if element in (self.PrimitiveElement.PE_IndicatorSpinUp,
                                   self.PrimitiveElement.PE_IndicatorSpinDown,
                                   self.PrimitiveElement.PE_IndicatorArrowDown,
                                   self.PrimitiveElement.PE_IndicatorArrowUp):
                        painter.save()
                        painter.setRenderHint(_P.RenderHint.Antialiasing)
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(arrow_color)
                        r = option.rect
                        cx, cy = r.center().x(), r.center().y()
                        s = min(r.width(), r.height()) // 3
                        from PySide6.QtGui import QPolygon
                        if element in (self.PrimitiveElement.PE_IndicatorSpinUp,
                                       self.PrimitiveElement.PE_IndicatorArrowUp):
                            painter.drawPolygon(QPolygon([
                                _QP(cx, cy - s), _QP(cx - s, cy + s), _QP(cx + s, cy + s)
                            ]))
                        else:
                            painter.drawPolygon(QPolygon([
                                _QP(cx - s, cy - s), _QP(cx + s, cy - s), _QP(cx, cy + s)
                            ]))
                        painter.restore()
                        return
                    super().drawPrimitive(element, option, painter, widget)

            app.setStyle(_DarkArrowStyle("Fusion"))
            app.setStyleSheet(css_text)

            # Extract selection color for grid highlight
            pal = app.palette()
            m = re.search(r'selection-background-color\s*:\s*(#[0-9a-fA-F]{3,8})', css_text)
            if m:
                pal.setColor(QPalette.ColorRole.Highlight, QColor(m.group(1)))
            app.setPalette(pal)
        except Exception as e:
            log.warning(f"Operation failed: {e}")

    # Set app icon (works in taskbar on all platforms)
    from PySide6.QtGui import QIcon
    # PyInstaller sets _MEIPASS for bundled data
    base_dir = Path(getattr(sys, '_MEIPASS', Path(__file__).parent.parent.parent))
    icon_path = base_dir / "icon.png"
    if not icon_path.exists():
        icon_path = Path(__file__).parent.parent.parent / "icon.png"
    if not icon_path.exists():
        icon_path = data_dir() / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = BooruApp()
    window.show()
    sys.exit(app.exec())
