"""Main Qt6 application window."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QObject, QUrl
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
from .favorites import FavoritesView
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
    search_error = Signal(str)
    thumb_done = Signal(int, str)
    image_done = Signal(str, str)
    image_error = Signal(str)
    fav_done = Signal(int, str)
    fav_error = Signal(str)
    autocomplete_done = Signal(list)
    batch_progress = Signal(int, int)      # current, total
    batch_done = Signal(str)
    download_progress = Signal(int, int)  # bytes_downloaded, total_bytes


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
        # Add clickable tags
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
        self._current_site: Site | None = None
        self._posts: list[Post] = []
        self._current_page = 1
        self._current_tags = ""
        self._current_rating = "all"
        self._min_score = 0
        self._loading = False
        self._last_scroll_page = 0
        self._signals = AsyncSignals()

        self._setup_signals()
        self._setup_ui()
        self._setup_menu()
        self._load_sites()

    def _setup_signals(self) -> None:
        Q = Qt.ConnectionType.QueuedConnection
        s = self._signals
        s.search_done.connect(self._on_search_done, Q)
        s.search_error.connect(self._on_search_error, Q)
        s.thumb_done.connect(self._on_thumb_done, Q)
        s.image_done.connect(self._on_image_done, Q)
        s.image_error.connect(self._on_image_error, Q)
        s.fav_done.connect(self._on_fav_done, Q)
        s.fav_error.connect(self._on_fav_error, Q)
        s.autocomplete_done.connect(self._on_autocomplete_done, Q)
        s.batch_progress.connect(self._on_batch_progress, Q)
        s.batch_done.connect(lambda m: self._status.showMessage(m), Q)
        s.download_progress.connect(self._on_download_progress, Q)

    def _clear_loading(self) -> None:
        self._loading = False

    def _on_search_error(self, e: str) -> None:
        self._loading = False
        self._status.showMessage(f"Error: {e}")

    def _on_image_error(self, e: str) -> None:
        self._dl_progress.hide()
        self._status.showMessage(f"Error: {e}")

    def _on_fav_error(self, e: str) -> None:
        self._status.showMessage(f"Error: {e}")

    def _run_async(self, coro_func, *args):
        def _worker():
            try:
                asyncio.run(coro_func(*args))
            except Exception as e:
                log.error(f"Async worker failed: {e}")
        threading.Thread(target=_worker, daemon=True).start()

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
        self._score_spin.setFixedWidth(70)
        top.addWidget(self._score_spin)

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

        self._fav_btn = QPushButton("Favorites")
        self._fav_btn.setCheckable(True)
        self._fav_btn.clicked.connect(lambda: self._switch_view(1))
        nav.addWidget(self._fav_btn)

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
        self._stack.addWidget(self._grid)

        self._favorites_view = FavoritesView(self._db)
        self._favorites_view.favorite_selected.connect(self._on_favorite_selected)
        self._favorites_view.favorite_activated.connect(self._on_favorite_activated)
        self._stack.addWidget(self._favorites_view)

        self._splitter.addWidget(self._stack)

        # Right: preview + info (vertical split)
        right = QSplitter(Qt.Orientation.Vertical)

        self._preview = ImagePreview()
        self._preview.close_requested.connect(self._close_preview)
        self._preview.open_in_default.connect(self._open_preview_in_default)
        self._preview.open_in_browser.connect(self._open_preview_in_browser)
        self._preview.favorite_requested.connect(self._favorite_from_preview)
        self._preview.save_to_folder.connect(self._save_from_preview)
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
        bottom_nav = QHBoxLayout()
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
        layout.addLayout(bottom_nav)

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
        self._fav_btn.setChecked(index == 1)
        if index == 1:
            self._favorites_view.refresh()
            self._favorites_view._grid.setFocus()
        else:
            self._grid.setFocus()

    def _on_tag_clicked(self, tag: str) -> None:
        self._search_bar.set_text(tag)
        self._on_search(tag)

    # -- Search --

    def _on_search(self, tags: str) -> None:
        self._current_tags = tags
        self._current_page = 1
        self._min_score = self._score_spin.value()
        self._do_search()

    def _prev_page(self) -> None:
        if self._current_page > 1:
            self._current_page -= 1
            self._do_search()

    def _next_page(self) -> None:
        if self._loading:
            return
        self._current_page += 1
        self._do_search()

    def _on_nav_past_end(self) -> None:
        self._nav_page_turn = "first"
        self._next_page()

    def _on_nav_before_start(self) -> None:
        if self._current_page > 1:
            self._nav_page_turn = "last"
            self._prev_page()

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

        # Append blacklisted tags as negatives
        for tag in self._db.get_blacklisted_tags():
            parts.append(f"-{tag}")

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

        async def _search():
            client = self._make_client()
            try:
                posts = await client.search(tags=search_tags, page=page, limit=limit)
                self._signals.search_done.emit(posts)
            except Exception as e:
                self._signals.search_error.emit(str(e))
            finally:
                await client.close()

        self._run_async(_search)

    def _on_search_done(self, posts: list) -> None:
        self._posts = posts
        self._status.showMessage(f"{len(posts)} results")
        thumbs = self._grid.set_posts(len(posts))
        self._grid.scroll_to_top()
        # Clear loading after a brief delay so scroll signals don't re-trigger
        QTimer.singleShot(100, self._clear_loading)

        from ..core.config import saved_dir, saved_folder_dir
        site_id = self._site_combo.currentData()
        for i, (post, thumb) in enumerate(zip(posts, thumbs)):
            if site_id and self._db.is_favorited(site_id, post.id):
                thumb.set_favorited(True)
                # Check if saved to library (not just cached)
                saved = any(
                    (saved_dir() / f"{post.id}{ext}").exists()
                    for ext in MEDIA_EXTENSIONS
                )
                if not saved:
                    # Check folders
                    favs = self._db.get_favorites(site_id=site_id)
                    for f in favs:
                        if f.post_id == post.id and f.folder:
                            saved = any(
                                (saved_folder_dir(f.folder) / f"{post.id}{ext}").exists()
                                for ext in MEDIA_EXTENSIONS
                            )
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
            except Exception:
                pass
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
                f"#{post.id}  {post.width}x{post.height}  score:{post.score}  [{post.rating}]"
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
                try:
                    path = await download_image(post.file_url, progress_callback=_progress)
                    info = f"#{post.id}  {post.width}x{post.height}  score:{post.score}  [{post.rating}]"
                    self._signals.image_done.emit(str(path), info)
                except Exception as e:
                    log.error(f"Image download failed: {e}")
                    self._signals.image_error.emit(str(e))

            self._run_async(_load)

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

    def _update_fullscreen(self, path: str, info: str) -> None:
        """Sync the fullscreen window with the current preview media."""
        if self._fullscreen_window and self._fullscreen_window.isVisible():
            self._preview._video_player.stop()
            self._fullscreen_window.set_media(path, info)

    def _on_image_done(self, path: str, info: str) -> None:
        self._dl_progress.hide()
        self._preview.set_media(path, info)
        self._status.showMessage("Loaded")
        # Update drag path on the selected thumbnail
        idx = self._grid.selected_index
        if 0 <= idx < len(self._grid._thumbs):
            self._grid._thumbs[idx]._cached_path = path
        self._update_fullscreen(path, info)

    def _on_favorite_selected(self, fav) -> None:
        self._status.showMessage(f"Favorite #{fav.post_id}")
        self._on_favorite_activated(fav)

    def _on_favorite_activated(self, fav) -> None:
        info = f"Favorite #{fav.post_id}"

        # Try local cache first
        if fav.cached_path and Path(fav.cached_path).exists():
            self._preview.set_media(fav.cached_path, info)
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
                    self._preview.set_media(str(path), info)
                    self._update_fullscreen(str(path), info)
                    return

        # Download it
        self._status.showMessage(f"Downloading #{fav.post_id}...")

        async def _dl():
            try:
                path = await download_image(fav.file_url)
                # Update cached_path in DB
                self._db.update_favorite_cache_path(fav.id, str(path))
                info = f"Favorite #{fav.post_id}"
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
            # Favorites view
            grid = self._favorites_view._grid
            favs = self._favorites_view._favorites
            idx = grid.selected_index + direction
            if 0 <= idx < len(favs):
                grid._select(idx)
                self._on_favorite_activated(favs[idx])
        else:
            idx = self._grid.selected_index + direction
            log.info(f"Navigate: direction={direction} current={self._grid.selected_index} next={idx} total={len(self._posts)}")
            if 0 <= idx < len(self._posts):
                self._grid._select(idx)
                self._on_post_activated(idx)
            elif idx >= len(self._posts) and direction == 1 and len(self._posts) > 0:
                self._nav_page_turn = "first"
                self._next_page()
            elif idx < 0 and direction == -1 and self._current_page > 1:
                self._nav_page_turn = "last"
                self._prev_page()

    def _favorite_from_preview(self) -> None:
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts):
            self._toggle_favorite(idx)

    def _save_from_preview(self, folder: str) -> None:
        idx = self._grid.selected_index
        if 0 <= idx < len(self._posts):
            target = folder if folder else None
            if folder and folder not in self._db.get_folders():
                self._db.add_folder(folder)
            self._save_to_library(self._posts[idx], target)

    def _open_fullscreen_preview(self) -> None:
        path = self._preview._current_path
        if not path:
            return
        # Pause the main preview's video player
        self._preview._video_player.stop()
        from .preview import FullscreenPreview
        cols = self._grid._flow.columns
        self._fullscreen_window = FullscreenPreview(grid_cols=cols, parent=self)
        self._fullscreen_window.navigate.connect(self._navigate_fullscreen)
        self._fullscreen_window.destroyed.connect(self._on_fullscreen_closed)
        self._fullscreen_window.set_media(path, self._preview._info_label.text())

    def _on_fullscreen_closed(self) -> None:
        self._fullscreen_window = None

    def _navigate_fullscreen(self, direction: int) -> None:
        self._navigate_preview(direction)
        # For synchronous loads (cached/favorites), update immediately
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

        copy_url = menu.addAction("Copy Image URL")
        copy_tags = menu.addAction("Copy Tags")
        menu.addSeparator()
        fav_action = menu.addAction("Unfavorite" if self._is_current_favorited(index) else "Favorite")
        menu.addSeparator()
        bl_menu = menu.addMenu("Blacklist Tag")
        for tag in post.tag_list[:20]:
            bl_menu.addAction(tag)

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
        elif action == copy_url:
            QApplication.clipboard().setText(post.file_url)
            self._status.showMessage("URL copied")
        elif action == copy_tags:
            QApplication.clipboard().setText(post.tags)
            self._status.showMessage("Tags copied")
        elif action == fav_action:
            self._toggle_favorite(index)
        elif action.parent() == bl_menu:
            tag = action.text()
            self._db.add_blacklisted_tag(tag)
            self._status.showMessage(f"Blacklisted: {tag}")

    def _on_multi_context_menu(self, indices: list, pos) -> None:
        """Context menu for multi-selected posts."""
        posts = [self._posts[i] for i in indices if 0 <= i < len(self._posts)]
        if not posts:
            return
        count = len(posts)

        menu = QMenu(self)
        fav_all = menu.addAction(f"Favorite All ({count})")

        save_menu = menu.addMenu(f"Save All to Library ({count})")
        save_unsorted = save_menu.addAction("Unsorted")
        save_folder_actions = {}
        for folder in self._db.get_folders():
            a = save_menu.addAction(folder)
            save_folder_actions[id(a)] = folder
        save_menu.addSeparator()
        save_new = save_menu.addAction("+ New Folder...")

        menu.addSeparator()
        unfav_all = menu.addAction(f"Unfavorite All ({count})")
        menu.addSeparator()
        batch_dl = menu.addAction(f"Download All ({count})...")
        copy_urls = menu.addAction("Copy All URLs")

        action = menu.exec(pos)
        if not action:
            return

        if action == fav_all:
            self._bulk_favorite(indices, posts)
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
                    self._db.remove_favorite(site_id, post.id)
                for idx in indices:
                    if 0 <= idx < len(self._grid._thumbs):
                        self._grid._thumbs[idx].set_favorited(False)
                        self._grid._thumbs[idx].set_saved_locally(False)
                self._grid._clear_multi()
                self._status.showMessage(f"Unfavorited {count} posts")
        elif action == copy_urls:
            urls = "\n".join(p.file_url for p in posts)
            QApplication.clipboard().setText(urls)
            self._status.showMessage(f"Copied {count} URLs")

    def _bulk_favorite(self, indices: list[int], posts: list[Post]) -> None:
        site_id = self._site_combo.currentData()
        if not site_id:
            return
        self._status.showMessage(f"Favoriting {len(posts)}...")

        async def _do():
            for i, (idx, post) in enumerate(zip(indices, posts)):
                if self._db.is_favorited(site_id, post.id):
                    continue
                try:
                    path = await download_image(post.file_url)
                    self._db.add_favorite(
                        site_id=site_id, post_id=post.id,
                        file_url=post.file_url, preview_url=post.preview_url,
                        tags=post.tags, rating=post.rating, score=post.score,
                        source=post.source, cached_path=str(path),
                    )
                    self._signals.fav_done.emit(idx, f"Favorited {i+1}/{len(posts)}")
                except Exception:
                    pass
            self._signals.batch_done.emit(f"Favorited {len(posts)} posts")

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
                    if site_id and not self._db.is_favorited(site_id, post.id):
                        self._db.add_favorite(
                            site_id=site_id, post_id=post.id,
                            file_url=post.file_url, preview_url=post.preview_url,
                            tags=post.tags, rating=post.rating, score=post.score,
                            source=post.source, cached_path=str(path), folder=folder,
                        )
                    self._signals.fav_done.emit(idx, f"Saved {i+1}/{len(posts)} to {where}")
                except Exception:
                    pass
            self._signals.batch_done.emit(f"Saved {len(posts)} to {where}")

        self._run_async(_do)

    def _toggle_favorite_if_not(self, post: Post) -> None:
        """Favorite a post if not already favorited."""
        site_id = self._site_combo.currentData()
        if not site_id or self._db.is_favorited(site_id, post.id):
            return

        async def _fav():
            try:
                path = await download_image(post.file_url)
                self._db.add_favorite(
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
            except Exception:
                pass

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

    def _is_current_favorited(self, index: int) -> bool:
        site_id = self._site_combo.currentData()
        if not site_id or index < 0 or index >= len(self._posts):
            return False
        return self._db.is_favorited(site_id, self._posts[index].id)

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

                # Also favorite it with the folder
                site_id = self._site_combo.currentData()
                if site_id and not self._db.is_favorited(site_id, post.id):
                    self._db.add_favorite(
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
                    )
                elif site_id and folder:
                    # Already favorited, just update the folder
                    favs = self._db.get_favorites(site_id=site_id)
                    for f in favs:
                        if f.post_id == post.id:
                            self._db.move_favorite_to_folder(f.id, folder)
                            break

                where = folder or "Unsorted"
                self._signals.fav_done.emit(
                    self._grid.selected_index,
                    f"Saved #{post.id} to {where}"
                )
            except Exception as e:
                self._signals.fav_error.emit(str(e))

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
        self._favorites_imported = False
        dlg.favorites_imported.connect(lambda: setattr(self, '_favorites_imported', True))
        dlg.exec()
        if self._favorites_imported:
            self._switch_view(1)
            self._favorites_view.refresh()

    def _apply_settings(self) -> None:
        """Re-read settings from DB and apply to UI."""
        rating = self._db.get_setting("default_rating")
        idx = self._rating_combo.findText(rating.capitalize() if rating != "all" else "All")
        if idx >= 0:
            self._rating_combo.setCurrentIndex(idx)
        self._score_spin.setValue(self._db.get_setting_int("default_score"))
        self._favorites_view.refresh()
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
        else:
            self._privacy_overlay.hide()

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
                self._toggle_favorite(idx)
                return
        elif key == Qt.Key.Key_I:
            self._toggle_info()
            return
        elif key == Qt.Key.Key_O and self._posts:
            idx = self._grid.selected_index
            if 0 <= idx < len(self._posts):
                self._open_in_default(self._posts[idx])
                return
        super().keyPressEvent(event)

    # -- Favorites --

    def _toggle_favorite(self, index: int) -> None:
        post = self._posts[index]
        site_id = self._site_combo.currentData()
        if not site_id:
            return

        if self._db.is_favorited(site_id, post.id):
            # Delete from library if saved
            favs = self._db.get_favorites(site_id=site_id)
            for f in favs:
                if f.post_id == post.id:
                    from ..core.cache import delete_from_library
                    delete_from_library(post.id, f.folder)
                    break
            self._db.remove_favorite(site_id, post.id)
            self._status.showMessage(f"Unfavorited #{post.id}")
            thumbs = self._grid._thumbs
            if 0 <= index < len(thumbs):
                thumbs[index].set_favorited(False)
                thumbs[index].set_saved_locally(False)
        else:
            self._status.showMessage(f"Favoriting #{post.id}...")

            async def _fav():
                try:
                    path = await download_image(post.file_url)
                    self._db.add_favorite(
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
                    self._signals.fav_done.emit(index, f"Favorited #{post.id}")
                except Exception as e:
                    self._signals.fav_error.emit(str(e))

            self._run_async(_fav)

    def _on_fav_done(self, index: int, msg: str) -> None:
        self._status.showMessage(msg)
        thumbs = self._grid._thumbs
        if 0 <= index < len(thumbs):
            thumbs[index].set_favorited(True)
            # Only green if actually saved to library, not just cached
            if "Saved" in msg:
                thumbs[index].set_saved_locally(True)

    def closeEvent(self, event) -> None:
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
                QLineEdit {
                    border: 1px solid #555;
                    border-radius: 2px;
                    padding: 3px;
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
    except Exception:
        pass


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
            app.setStyleSheet(custom_css.read_text())
        except Exception:
            pass

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
