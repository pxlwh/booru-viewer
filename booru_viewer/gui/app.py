"""Main Qt6 application window."""

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
from .preview import ImagePreview
from .search import SearchBar
from .sites import SiteManagerDialog
from .bookmarks import BookmarksView
from .library import LibraryView
from .settings import SettingsDialog

log = logging.getLogger("booru")


@dataclass
class SearchState:
    """Mutable state that resets on every new search."""
    shown_post_ids: set[int] = field(default_factory=set)
    page_cache: dict[int, list] = field(default_factory=dict)
    infinite_exhausted: bool = False
    infinite_last_page: int = 0
    infinite_api_exhausted: bool = False
    nav_page_turn: str | None = None
    append_queue: list = field(default_factory=list)


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

    # Tag category colors. Defaults follow the booru convention (Danbooru,
    # Gelbooru, etc.) so the panel reads naturally to anyone coming from a
    # booru site. Each is exposed as a Qt Property so a custom.qss can
    # override it via `qproperty-tag<Category>Color` selectors on
    # `InfoPanel`. An empty string means "use the default text color"
    # (the General category) and is preserved as a sentinel.
    _tag_artist_color = QColor("#f2ac08")
    _tag_character_color = QColor("#0a0")
    _tag_copyright_color = QColor("#c0f")
    _tag_species_color = QColor("#e44")
    _tag_meta_color = QColor("#888")
    _tag_lore_color = QColor("#888")

    def _get_artist(self): return self._tag_artist_color
    def _set_artist(self, c): self._tag_artist_color = QColor(c) if isinstance(c, str) else c
    tagArtistColor = Property(QColor, _get_artist, _set_artist)

    def _get_character(self): return self._tag_character_color
    def _set_character(self, c): self._tag_character_color = QColor(c) if isinstance(c, str) else c
    tagCharacterColor = Property(QColor, _get_character, _set_character)

    def _get_copyright(self): return self._tag_copyright_color
    def _set_copyright(self, c): self._tag_copyright_color = QColor(c) if isinstance(c, str) else c
    tagCopyrightColor = Property(QColor, _get_copyright, _set_copyright)

    def _get_species(self): return self._tag_species_color
    def _set_species(self, c): self._tag_species_color = QColor(c) if isinstance(c, str) else c
    tagSpeciesColor = Property(QColor, _get_species, _set_species)

    def _get_meta(self): return self._tag_meta_color
    def _set_meta(self, c): self._tag_meta_color = QColor(c) if isinstance(c, str) else c
    tagMetaColor = Property(QColor, _get_meta, _set_meta)

    def _get_lore(self): return self._tag_lore_color
    def _set_lore(self, c): self._tag_lore_color = QColor(c) if isinstance(c, str) else c
    tagLoreColor = Property(QColor, _get_lore, _set_lore)

    def _category_color(self, category: str) -> str:
        """Resolve a category name to a hex color string for inline QSS use.
        Returns "" for the General category (no override → use default text
        color) and unrecognized categories (so callers can render them with
        no color attribute set)."""
        cat = (category or "").lower()
        m = {
            "artist": self._tag_artist_color,
            "character": self._tag_character_color,
            "copyright": self._tag_copyright_color,
            "species": self._tag_species_color,
            "meta": self._tag_meta_color,
            "lore": self._tag_lore_color,
        }
        c = m.get(cat)
        return c.name() if c is not None else ""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self._title = QLabel("No post selected")
        self._title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._title)

        self._details = QLabel()
        self._details.setWordWrap(True)
        self._details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextBrowserInteraction)
        self._details.setMaximumHeight(120)
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
        source = post.source or "none"
        # Truncate display text but keep full URL for the link
        source_full = source
        if len(source) > 60:
            source_display = source[:57] + "..."
        else:
            source_display = source
        if source_full.startswith(("http://", "https://")):
            source_html = f'<a href="{source_full}" style="color: #4fc3f7;">{source_display}</a>'
        else:
            source_html = source_display
        from html import escape
        self._details.setText(
            f"Score: {post.score}\n"
            f"Rating: {post.rating or 'unknown'}\n"
            f"Filetype: {filetype}"
        )
        self._details.setTextFormat(Qt.TextFormat.RichText)
        self._details.setText(
            f"Score: {post.score}<br>"
            f"Rating: {escape(post.rating or 'unknown')}<br>"
            f"Filetype: {filetype}<br>"
            f"Source: {source_html}"
        )
        self._details.setOpenExternalLinks(True)
        # Clear old tags
        while self._tags_flow.count():
            item = self._tags_flow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if post.tag_categories:
            # Display tags grouped by category. Colors come from the
            # tag*Color Qt Properties so a custom.qss can override any of
            # them via `InfoPanel { qproperty-tagCharacterColor: ...; }`.
            for category, tags in post.tag_categories.items():
                color = self._category_color(category)
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
        # Apply saved thumbnail size
        saved_thumb = self._db.get_setting_int("thumbnail_size")
        if saved_thumb:
            import booru_viewer.gui.grid as grid_mod
            grid_mod.THUMB_SIZE = saved_thumb
        self._current_site: Site | None = None
        self._posts: list[Post] = []
        self._current_page = 1
        self._current_tags = ""
        self._current_rating = "all"
        self._min_score = 0
        self._loading = False
        self._search = SearchState()
        self._last_scroll_page = 0
        self._prefetch_pause = asyncio.Event()
        self._prefetch_pause.set()  # not paused
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
        # Debounced save for the main window state — fires from resizeEvent
        # (and from the splitter timer's flush on close). Uses the same
        # 300ms debounce pattern as the splitter saver.
        self._main_window_save_timer = QTimer(self)
        self._main_window_save_timer.setSingleShot(True)
        self._main_window_save_timer.setInterval(300)
        self._main_window_save_timer.timeout.connect(self._save_main_window_state)
        # Restore window state (geometry, floating) on the next event-loop
        # iteration — by then main.py has called show() and the window has
        # been registered with the compositor.
        QTimer.singleShot(0, self._restore_main_window_state)

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
        s.batch_done.connect(self._on_batch_done, Q)
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
        self._score_spin.setFixedWidth(50)
        self._score_spin.setFixedHeight(23)
        self._score_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        top.addWidget(self._score_spin)

        self._media_filter = QComboBox()
        self._media_filter.addItems(["All", "Animated", "Video", "GIF", "Audio"])
        self._media_filter.setToolTip("Filter by media type")
        self._media_filter.setFixedWidth(90)
        top.addWidget(self._media_filter)

        page_label = QLabel("Page")
        top.addWidget(page_label)
        self._page_spin = QSpinBox()
        self._page_spin.setRange(1, 99999)
        self._page_spin.setValue(1)
        self._page_spin.setFixedWidth(50)
        self._page_spin.setFixedHeight(23)  # match the surrounding 23px row
        self._page_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        top.addWidget(self._page_spin)

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
        self._right_splitter_save_timer.timeout.connect(self._save_right_splitter_sizes)
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
        self._main_splitter_save_timer.timeout.connect(self._save_main_splitter_sizes)
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
        self._prev_page_btn.clicked.connect(self._prev_page)
        bottom_nav.addWidget(self._prev_page_btn)
        self._next_page_btn = QPushButton("Next")
        self._next_page_btn.setFixedWidth(60)
        self._next_page_btn.clicked.connect(self._next_page)
        bottom_nav.addWidget(self._next_page_btn)
        bottom_nav.addStretch()
        layout.addWidget(self._bottom_nav)

        # Infinite scroll
        self._infinite_scroll = self._db.get_setting_bool("infinite_scroll")
        if self._infinite_scroll:
            self._bottom_nav.hide()
        self._grid.reached_bottom.connect(self._on_reached_bottom)
        self._grid.verticalScrollBar().rangeChanged.connect(self._on_scroll_range_changed)

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
        self._on_search(tag)

    # -- Search --

    def _on_search(self, tags: str) -> None:
        self._current_tags = tags
        self._current_page = self._page_spin.value()
        self._search = SearchState()
        self._min_score = self._score_spin.value()
        self._preview.clear()
        self._next_page_btn.setVisible(True)
        self._prev_page_btn.setVisible(False)
        self._do_search()

    def _prev_page(self) -> None:
        if self._current_page > 1:
            self._current_page -= 1
            if self._current_page in self._search.page_cache:
                self._signals.search_done.emit(self._search.page_cache[self._current_page])
            else:
                self._do_search()

    def _next_page(self) -> None:
        if self._loading:
            return
        self._current_page += 1
        if self._current_page in self._search.page_cache:
            self._signals.search_done.emit(self._search.page_cache[self._current_page])
            return
        self._do_search()

    def _on_nav_past_end(self) -> None:
        if self._infinite_scroll:
            return  # infinite scroll handles this via reached_bottom
        self._search.nav_page_turn = "first"
        self._next_page()

    def _on_nav_before_start(self) -> None:
        if self._infinite_scroll:
            return
        if self._current_page > 1:
            self._search.nav_page_turn = "last"
            self._prev_page()

    def _on_reached_bottom(self) -> None:
        if not self._infinite_scroll or self._loading or self._search.infinite_exhausted:
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
        shown_ids = self._search.shown_post_ids.copy()
        seen = shown_ids.copy()  # local dedup for this backfill round

        def _filter(posts):
            if bl_tags:
                posts = [p for p in posts if not bl_tags.intersection(p.tag_list)]
            if bl_posts:
                posts = [p for p in posts if p.file_url not in bl_posts]
            posts = [p for p in posts if p.id not in seen]
            seen.update(p.id for p in posts)
            return posts

        async def _search():
            client = self._make_client()
            collected = []
            last_page = page
            api_exhausted = False
            try:
                current_page = page
                batch = await client.search(tags=search_tags, page=current_page, limit=limit)
                last_page = current_page
                filtered = _filter(batch)
                collected.extend(filtered)
                if len(batch) < limit:
                    api_exhausted = True
                elif len(collected) < limit:
                    for _ in range(9):
                        await asyncio.sleep(0.3)
                        current_page += 1
                        batch = await client.search(tags=search_tags, page=current_page, limit=limit)
                        last_page = current_page
                        filtered = _filter(batch)
                        collected.extend(filtered)
                        if len(batch) < limit:
                            api_exhausted = True
                            break
                        if len(collected) >= limit:
                            break
            except Exception as e:
                log.warning(f"Infinite scroll fetch failed: {e}")
            finally:
                self._search.infinite_last_page = last_page
                self._search.infinite_api_exhausted = api_exhausted
                self._signals.search_append.emit(collected)
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

        # Media type filter
        media = self._media_filter.currentText()
        if media == "Animated":
            parts.append("animated")
        elif media == "Video":
            parts.append("video")
        elif media == "GIF":
            parts.append("animated_gif")
        elif media == "Audio":
            parts.append("audio")

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
        shown_ids = self._search.shown_post_ids.copy()
        seen = shown_ids.copy()

        def _filter(posts):
            if bl_tags:
                posts = [p for p in posts if not bl_tags.intersection(p.tag_list)]
            if bl_posts:
                posts = [p for p in posts if p.file_url not in bl_posts]
            posts = [p for p in posts if p.id not in seen]
            seen.update(p.id for p in posts)
            return posts

        async def _search():
            client = self._make_client()
            try:
                collected = []
                current_page = page
                batch = await client.search(tags=search_tags, page=current_page, limit=limit)
                filtered = _filter(batch)
                collected.extend(filtered)
                # Backfill only if first page didn't return enough after filtering
                if len(collected) < limit and len(batch) >= limit:
                    for _ in range(9):
                        await asyncio.sleep(0.3)
                        current_page += 1
                        batch = await client.search(tags=search_tags, page=current_page, limit=limit)
                        filtered = _filter(batch)
                        collected.extend(filtered)
                        log.debug(f"Backfill: page={current_page} batch={len(batch)} filtered={len(filtered)} total={len(collected)}/{limit}")
                        if len(collected) >= limit or len(batch) < limit:
                            break
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
        ss = self._search
        ss.shown_post_ids.update(p.id for p in posts)
        ss.page_cache[self._current_page] = posts
        # Cap page cache in pagination mode (infinite scroll needs all pages)
        if not self._infinite_scroll and len(ss.page_cache) > 10:
            oldest = min(ss.page_cache.keys())
            del ss.page_cache[oldest]
        limit = self._db.get_setting_int("page_size") or 40
        at_end = len(posts) < limit
        if at_end:
            self._status.showMessage(f"{len(posts)} results (end)")
        else:
            self._status.showMessage(f"{len(posts)} results")
        # Update pagination buttons
        self._prev_page_btn.setVisible(self._current_page > 1)
        self._next_page_btn.setVisible(not at_end)
        thumbs = self._grid.set_posts(len(posts))
        self._grid.scroll_to_top()
        # Clear loading after a brief delay so scroll signals don't re-trigger
        QTimer.singleShot(100, self._clear_loading)

        from ..core.config import saved_dir
        from ..core.cache import cached_path_for, cache_dir
        site_id = self._site_combo.currentData()

        # Pre-scan the library once into a flat post-id set so the per-post
        # check below is O(1). Folders are filesystem-truth — walk every
        # subdir of saved_dir() rather than consulting the bookmark folder
        # list (which used to leak DB state into library detection).
        _sd = saved_dir()
        _saved_ids: set[int] = set()
        if _sd.is_dir():
            for entry in _sd.iterdir():
                if entry.is_file() and entry.stem.isdigit():
                    _saved_ids.add(int(entry.stem))
                elif entry.is_dir():
                    for sub in entry.iterdir():
                        if sub.is_file() and sub.stem.isdigit():
                            _saved_ids.add(int(sub.stem))

        # Pre-fetch bookmarks for the site once and project to a post-id set
        # so the per-post check below is an O(1) membership test instead of
        # a synchronous SQLite query (was N queries on the GUI thread).
        _favs = self._db.get_bookmarks(site_id=site_id) if site_id else []
        _bookmarked_ids: set[int] = {f.post_id for f in _favs}

        # Pre-scan the cache dir into a name set so the per-post drag-path
        # lookup is one stat-equivalent (one iterdir) instead of N stat calls.
        _cd = cache_dir()
        _cached_names: set[str] = set()
        if _cd.exists():
            _cached_names = {f.name for f in _cd.iterdir() if f.is_file()}

        for i, (post, thumb) in enumerate(zip(posts, thumbs)):
            # Bookmark status (DB)
            if post.id in _bookmarked_ids:
                thumb.set_bookmarked(True)
            # Saved status (filesystem) — _saved_ids already covers both
            # the unsorted root and every library subdirectory.
            thumb.set_saved_locally(post.id in _saved_ids)
            # Set drag path from cache
            cached = cached_path_for(post.file_url)
            if cached.name in _cached_names:
                thumb._cached_path = str(cached)

            if post.preview_url:
                self._fetch_thumbnail(i, post.preview_url)

        # Auto-select first/last post if page turn was triggered by navigation
        turn = self._search.nav_page_turn
        if turn and posts:
            self._search.nav_page_turn = None
            if turn == "first":
                idx = 0
            else:
                idx = len(posts) - 1
            self._grid._select(idx)
            self._on_post_activated(idx)

        self._grid.setFocus()

        # Start prefetching from top of page
        if self._db.get_setting("prefetch_mode") in ("Nearby", "Aggressive") and posts:
            self._prefetch_adjacent(0)

        # Infinite scroll: if first page doesn't fill viewport, load more
        if self._infinite_scroll and posts:
            QTimer.singleShot(200, self._check_viewport_fill)

    def _on_scroll_range_changed(self, _min: int, max_val: int) -> None:
        """Scrollbar range changed (resize/splitter) — check if viewport needs filling."""
        if max_val == 0 and self._infinite_scroll and self._posts:
            QTimer.singleShot(100, self._check_viewport_fill)

    def _check_viewport_fill(self) -> None:
        """If content doesn't fill the viewport, trigger infinite scroll."""
        if not self._infinite_scroll or self._loading or self._search.infinite_exhausted:
            return
        # Force layout update so scrollbar range is current
        self._grid.widget().updateGeometry()
        QApplication.processEvents()
        sb = self._grid.verticalScrollBar()
        if sb.maximum() == 0 and self._posts:
            self._on_reached_bottom()

    def _on_search_append(self, posts: list) -> None:
        """Queue posts and add them one at a time as thumbnails arrive."""
        ss = self._search

        if not posts:
            # Only advance page if API is exhausted — otherwise we retry
            if ss.infinite_api_exhausted and ss.infinite_last_page > self._current_page:
                self._current_page = ss.infinite_last_page
            self._loading = False
            # Only mark exhausted if the API itself returned a short page,
            # not just because blacklist/dedup filtering emptied the results
            if ss.infinite_api_exhausted:
                ss.infinite_exhausted = True
                self._status.showMessage(f"{len(self._posts)} results (end)")
            else:
                # Viewport still not full ��� keep loading
                QTimer.singleShot(100, self._check_viewport_fill)
            return
        # Advance page counter past pages consumed by backfill
        if ss.infinite_last_page > self._current_page:
            self._current_page = ss.infinite_last_page
        ss.shown_post_ids.update(p.id for p in posts)
        ss.append_queue.extend(posts)
        self._drain_append_queue()

    def _drain_append_queue(self) -> None:
        """Add all queued posts to the grid at once, thumbnails load async."""
        ss = self._search
        if not ss.append_queue:
            self._loading = False
            return

        from ..core.config import saved_dir
        from ..core.cache import cached_path_for, cache_dir
        site_id = self._site_combo.currentData()
        _sd = saved_dir()
        _saved_ids: set[int] = set()
        if _sd.exists():
            _saved_ids = {int(f.stem) for f in _sd.iterdir() if f.is_file() and f.stem.isdigit()}

        # Pre-fetch bookmarks → set, and pre-scan cache dir → set, so the
        # per-post checks below avoid N synchronous SQLite/stat calls on the
        # GUI thread (matches the optimisation in _on_search_done).
        _favs = self._db.get_bookmarks(site_id=site_id) if site_id else []
        _bookmarked_ids: set[int] = {f.post_id for f in _favs}
        _cd = cache_dir()
        _cached_names: set[str] = set()
        if _cd.exists():
            _cached_names = {f.name for f in _cd.iterdir() if f.is_file()}

        posts = ss.append_queue[:]
        ss.append_queue.clear()
        start_idx = len(self._posts)
        self._posts.extend(posts)
        thumbs = self._grid.append_posts(len(posts))

        for i, (post, thumb) in enumerate(zip(posts, thumbs)):
            idx = start_idx + i
            if post.id in _bookmarked_ids:
                thumb.set_bookmarked(True)
            thumb.set_saved_locally(post.id in _saved_ids)
            cached = cached_path_for(post.file_url)
            if cached.name in _cached_names:
                thumb._cached_path = str(cached)
            if post.preview_url:
                self._fetch_thumbnail(idx, post.preview_url)

        self._status.showMessage(f"{len(self._posts)} results")

        # All done — unlock loading, evict
        self._loading = False
        self._auto_evict_cache()
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
                + (f"  {post.created_at}" if post.created_at else "")
            )
            if self._info_panel.isVisible():
                self._info_panel.set_post(post)
            self._on_post_activated(index)

    def _on_post_activated(self, index: int) -> None:
        if 0 <= index < len(self._posts):
            post = self._posts[index]
            log.info(f"Preview: #{post.id} -> {post.file_url}")
            self._preview._current_post = post
            self._preview._current_site_id = self._site_combo.currentData()
            self._preview.set_post_tags(post.tag_categories, post.tag_list)
            site_id = self._preview._current_site_id
            self._preview.update_bookmark_state(
                bool(site_id and self._db.is_bookmarked(site_id, post.id))
            )
            self._preview.update_save_state(self._is_post_saved(post.id))
            self._status.showMessage(f"Loading #{post.id}...")
            # Skip the dl_progress widget when the popout is open. The user
            # is looking at the popout, not the embedded preview area, and
            # the right splitter is set to [0, 0, 1000] so the show/hide
            # pulse on the dl_progress section forces a layout pass that
            # briefly compresses the main grid (visible flash on every
            # click, even on the same post since download_image still runs
            # against the cache and the show/hide cycle still fires).
            if not (self._fullscreen_window and self._fullscreen_window.isVisible()):
                self._dl_progress.show()
                self._dl_progress.setRange(0, 0)

            def _progress(downloaded, total):
                self._signals.download_progress.emit(downloaded, total)

            async def _load():
                self._prefetch_pause.clear()  # pause prefetch
                try:
                    path = await download_image(post.file_url, progress_callback=_progress)
                    info = (f"#{post.id}  {post.width}x{post.height}  score:{post.score}  [{post.rating}]  {Path(post.file_url.split('?')[0]).suffix.lstrip('.').upper() if post.file_url else ''}"
                            + (f"  {post.created_at}" if post.created_at else ""))
                    self._signals.image_done.emit(str(path), info)
                except Exception as e:
                    log.error(f"Image download failed: {e}")
                    self._signals.image_error.emit(str(e))
                finally:
                    self._prefetch_pause.set()  # resume prefetch

            self._run_async(_load)

            # Prefetch adjacent posts
            if self._db.get_setting("prefetch_mode") in ("Nearby", "Aggressive"):
                self._prefetch_adjacent(index)

    def _prefetch_adjacent(self, index: int) -> None:
        """Prefetch posts around the given index."""
        total = len(self._posts)
        if total == 0:
            return
        cols = self._grid._flow.columns
        mode = self._db.get_setting("prefetch_mode")

        if mode == "Nearby":
            # Just 4 cardinals: left, right, up, down
            order = []
            for offset in [1, -1, cols, -cols]:
                adj = index + offset
                if 0 <= adj < total:
                    order.append(adj)
        else:
            # Aggressive: ring expansion, capped to ~3 rows radius
            max_radius = 3
            max_posts = cols * max_radius * 2 + cols  # ~3 rows above and below
            seen = {index}
            order = []
            for dist in range(1, max_radius + 1):
                ring = set()
                for dy in (-dist, 0, dist):
                    for dx in (-dist, 0, dist):
                        if dy == 0 and dx == 0:
                            continue
                        adj = index + dy * cols + dx
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
        # Same suppression as _on_post_activated: when the popout is open,
        # don't manipulate the dl_progress widget at all. Status bar still
        # gets the byte counts so the user has feedback in the main window.
        popout_open = bool(self._fullscreen_window and self._fullscreen_window.isVisible())
        if total > 0:
            if not popout_open:
                self._dl_progress.setRange(0, total)
                self._dl_progress.setValue(downloaded)
                self._dl_progress.show()
            mb = downloaded / (1024 * 1024)
            total_mb = total / (1024 * 1024)
            self._status.showMessage(f"Downloading... {mb:.1f}/{total_mb:.1f} MB")
        elif not popout_open:
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
            # Bookmark / BL Tag / BL Post hidden on the library tab (no
            # site/post id to act on for local-only files). Save stays
            # visible — it acts as Unsave for the library file currently
            # being viewed, matching the embedded preview's library mode.
            show_full = self._stack.currentIndex() != 2
            self._fullscreen_window._bookmark_btn.setVisible(show_full)
            self._fullscreen_window._save_btn.setVisible(True)
            self._fullscreen_window._bl_tag_btn.setVisible(show_full)
            self._fullscreen_window._bl_post_btn.setVisible(show_full)
            self._update_fullscreen_state()

    def _update_fullscreen_state(self) -> None:
        """Update popout button states by mirroring the embedded preview.

        The embedded preview is the canonical owner of bookmark/save
        state — every code path that bookmarks, unsaves, navigates, or
        loads a post calls update_bookmark_state / update_save_state on
        it. Re-querying the DB and filesystem here used to drift out of
        sync with the embedded preview during async bookmark adds and
        immediately after tab switches; mirroring eliminates the gap and
        is one source of truth instead of two.
        """
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

    def _on_image_done(self, path: str, info: str) -> None:
        self._dl_progress.hide()
        if self._fullscreen_window and self._fullscreen_window.isVisible():
            # Popout is open — only show there, keep preview clear
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
        # Thumbnail eviction
        max_thumb_mb = self._db.get_setting_int("max_thumb_cache_mb") or 500
        max_thumb_bytes = max_thumb_mb * 1024 * 1024
        evicted_thumbs = evict_oldest_thumbnails(max_thumb_bytes)
        if evicted_thumbs:
            log.info(f"Auto-evicted {evicted_thumbs} thumbnails")

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
            info = f"#{p.id}  score:{p.score}  [{p.rating}]  {Path(path).suffix.lstrip('.').upper()}" + (f"  {p.created_at}" if p.created_at else "")
            self._status.showMessage(info)

    def _on_library_selected(self, path: str) -> None:
        self._show_library_post(path)

    def _on_library_activated(self, path: str) -> None:
        self._show_library_post(path)

    def _show_library_post(self, path: str) -> None:
        self._set_preview_media(path, Path(path).name)
        self._update_fullscreen(path, Path(path).name)
        self._set_library_info(path)
        # Build a Post from library metadata so toolbar actions work
        stem = Path(path).stem
        if stem.isdigit():
            post_id = int(stem)
            from ..core.api.base import Post
            meta = self._db.get_library_meta(post_id) or {}
            post = Post(
                id=post_id, file_url=meta.get("file_url", ""),
                preview_url=None, tags=meta.get("tags", ""),
                score=meta.get("score", 0), rating=meta.get("rating"),
                source=meta.get("source"),
                tag_categories=meta.get("tag_categories", {}),
            )
            self._preview._current_post = post
            self._preview._current_site_id = self._site_combo.currentData()
            self._preview.update_save_state(True)
            self._preview.set_post_tags(post.tag_categories, post.tag_list)
        else:
            self._preview._current_post = None
            self._preview.update_save_state(True)

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
            self._set_preview_media(fav.cached_path, info)
            self._update_fullscreen(fav.cached_path, info)
            return

        # Try saved library — walk by post id; the file may live in any
        # library folder regardless of which bookmark folder fav is in.
        from ..core.config import find_library_files
        for path in find_library_files(fav.post_id):
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

    def _save_main_splitter_sizes(self) -> None:
        """Persist the main grid/preview splitter sizes (debounced).

        Refuses to save when either side is collapsed (size 0). The user can
        end up with a collapsed right panel transiently — e.g. while the
        popout is open and the right panel is empty — and persisting that
        state traps them next launch with no visible preview area until they
        manually drag the splitter back.
        """
        sizes = self._splitter.sizes()
        if len(sizes) >= 2 and all(s > 0 for s in sizes):
            self._db.set_setting(
                "main_splitter_sizes", ",".join(str(s) for s in sizes)
            )

    def _save_right_splitter_sizes(self) -> None:
        """Persist the right splitter sizes (preview / dl_progress / info).

        Skipped while the popout is open — the popout temporarily collapses
        the preview pane and gives the info panel the full right column,
        and we don't want that transient layout persisted as the user's
        preferred state.
        """
        if getattr(self, '_popout_active', False):
            return
        sizes = self._right_splitter.sizes()
        if len(sizes) == 3 and sum(sizes) > 0:
            self._db.set_setting(
                "right_splitter_sizes", ",".join(str(s) for s in sizes)
            )

    def _hyprctl_main_window(self) -> dict | None:
        """Look up this main window in hyprctl clients. None off Hyprland.

        Matches by Wayland app_id (Hyprland reports it as `class`), which is
        set in run() via setDesktopFileName. Title would also work but it
        changes whenever the search bar updates the window title — class is
        constant for the lifetime of the window.
        """
        import os, subprocess, json
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return None
        try:
            result = subprocess.run(
                ["hyprctl", "clients", "-j"],
                capture_output=True, text=True, timeout=1,
            )
            for c in json.loads(result.stdout):
                cls = c.get("class") or c.get("initialClass")
                if cls == "booru-viewer":
                    # Skip the popout — it shares our class but has a
                    # distinct title we set explicitly.
                    if (c.get("title") or "").endswith("Popout"):
                        continue
                    return c
        except Exception:
            pass
        return None

    def _save_main_window_state(self) -> None:
        """Persist the main window's last mode and (separately) the last
        known floating geometry.

        Two settings keys are used:
          - main_window_was_floating ("1" / "0"): the *last* mode the window
            was in (floating or tiled). Updated on every save.
          - main_window_floating_geometry ("x,y,w,h"): the position+size the
            window had the *last time it was actually floating*. Only updated
            when the current state is floating, so a tile→close→reopen→float
            sequence still has the user's old floating dimensions to use.

        This split is important because Hyprland's resizeEvent for a tiled
        window reports the tile slot size — saving that into the floating
        slot would clobber the user's chosen floating dimensions every time
        they tiled the window.
        """
        try:
            win = self._hyprctl_main_window()
            if win is None:
                # Non-Hyprland fallback: just track Qt's frameGeometry as
                # floating. There's no real tiled concept off-Hyprland.
                g = self.frameGeometry()
                self._db.set_setting(
                    "main_window_floating_geometry",
                    f"{g.x()},{g.y()},{g.width()},{g.height()}",
                )
                self._db.set_setting("main_window_was_floating", "1")
                return
            floating = bool(win.get("floating"))
            self._db.set_setting(
                "main_window_was_floating", "1" if floating else "0"
            )
            if floating and win.get("at") and win.get("size"):
                x, y = win["at"]
                w, h = win["size"]
                self._db.set_setting(
                    "main_window_floating_geometry", f"{x},{y},{w},{h}"
                )
            # When tiled, intentionally do NOT touch floating_geometry —
            # preserve the last good floating dimensions.
        except Exception:
            pass

    def _restore_main_window_state(self) -> None:
        """One-shot restore of saved floating geometry and last mode.

        Called from __init__ via QTimer.singleShot(0, ...) so it fires on the
        next event-loop iteration — by which time the window has been shown
        and (on Hyprland) registered with the compositor.

        Entirely skipped when BOORU_VIEWER_NO_HYPR_RULES is set — that flag
        means the user wants their own windowrules to handle the main
        window. Even seeding Qt's geometry could fight a `windowrule = size`,
        so we leave the initial Qt geometry alone too.
        """
        from ..core.config import hypr_rules_enabled
        if not hypr_rules_enabled():
            return
        # Migration: clear obsolete keys from earlier schemas so they can't
        # interfere. main_window_maximized came from a buggy version that
        # used Qt's isMaximized() which lies for Hyprland tiled windows.
        # main_window_geometry was the combined-format key that's now split.
        for stale in ("main_window_maximized", "main_window_geometry"):
            if self._db.get_setting(stale):
                self._db.set_setting(stale, "")

        floating_geo = self._db.get_setting("main_window_floating_geometry")
        was_floating = self._db.get_setting_bool("main_window_was_floating")
        if not floating_geo:
            return
        parts = floating_geo.split(",")
        if len(parts) != 4:
            return
        try:
            x, y, w, h = (int(p) for p in parts)
        except ValueError:
            return
        # Seed Qt with the floating geometry — even if we're going to leave
        # the window tiled now, this becomes the xdg-toplevel preferred size,
        # which Hyprland uses when the user later toggles to floating. So
        # mid-session float-toggle picks up the saved dimensions even when
        # the window opened tiled.
        self.setGeometry(x, y, w, h)
        import os
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return
        # Slight delay so the window is registered before we try to find
        # its address. The popout uses the same pattern.
        QTimer.singleShot(
            50, lambda: self._hyprctl_apply_main_state(x, y, w, h, was_floating)
        )

    def _hyprctl_apply_main_state(self, x: int, y: int, w: int, h: int, floating: bool) -> None:
        """Apply saved floating mode + geometry to the main window via hyprctl.

        If floating==True, ensures the window is floating and resizes/moves it
        to the saved dimensions.

        If floating==False, the window is left tiled but we still "prime"
        Hyprland's per-window floating cache by briefly toggling to floating,
        applying the saved geometry, and toggling back. This is wrapped in
        a transient `no_anim` so the toggles are instant. Without this prime,
        a later mid-session togglefloating uses Hyprland's default size
        (Qt's xdg-toplevel preferred size doesn't carry through). With it,
        the user's saved floating dimensions are used.

        Skipped entirely when BOORU_VIEWER_NO_HYPR_RULES is set — that flag
        means the user wants their own windowrules to govern the main
        window and the app should keep its hands off.
        """
        import subprocess
        from ..core.config import hypr_rules_enabled
        if not hypr_rules_enabled():
            return
        win = self._hyprctl_main_window()
        if not win:
            return
        addr = win.get("address")
        if not addr:
            return
        cur_floating = bool(win.get("floating"))
        cmds: list[str] = []
        if floating:
            # Want floating: ensure floating, then size/move.
            if not cur_floating:
                cmds.append(f"dispatch togglefloating address:{addr}")
            cmds.append(f"dispatch resizewindowpixel exact {w} {h},address:{addr}")
            cmds.append(f"dispatch movewindowpixel exact {x} {y},address:{addr}")
        else:
            # Want tiled: prime the floating cache, then end on tiled. Use
            # transient no_anim so the toggles don't visibly flash through
            # a floating frame.
            cmds.append(f"dispatch setprop address:{addr} no_anim 1")
            if not cur_floating:
                cmds.append(f"dispatch togglefloating address:{addr}")
            cmds.append(f"dispatch resizewindowpixel exact {w} {h},address:{addr}")
            cmds.append(f"dispatch movewindowpixel exact {x} {y},address:{addr}")
            cmds.append(f"dispatch togglefloating address:{addr}")
            cmds.append(f"dispatch setprop address:{addr} no_anim 0")
        if not cmds:
            return
        try:
            subprocess.Popen(
                ["hyprctl", "--batch", " ; ".join(cmds)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

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
                        self._fullscreen_window._video.pause()
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
                    self._fullscreen_window._video.pause()
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
        if self._stack.currentIndex() == 1:
            # Bookmarks view
            grid = self._bookmarks_view._grid
            favs = self._bookmarks_view._bookmarks
            idx = grid.selected_index + direction
            if 0 <= idx < len(favs):
                grid._select(idx)
                self._on_bookmark_activated(favs[idx])
            elif wrap and favs:
                idx = 0 if direction > 0 else len(favs) - 1
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
            elif wrap and files:
                idx = 0 if direction > 0 else len(files) - 1
                grid._select(idx)
                self._library_view.file_activated.emit(str(files[idx]))
        else:
            idx = self._grid.selected_index + direction
            log.info(f"Navigate: direction={direction} current={self._grid.selected_index} next={idx} total={len(self._posts)}")
            if 0 <= idx < len(self._posts):
                self._grid._select(idx)
                self._on_post_activated(idx)
            elif idx >= len(self._posts) and direction > 0 and len(self._posts) > 0 and not self._infinite_scroll:
                self._search.nav_page_turn = "first"
                self._next_page()
            elif idx < 0 and direction < 0 and self._current_page > 1 and not self._infinite_scroll:
                self._search.nav_page_turn = "last"
                self._prev_page()

    def _on_video_end_next(self) -> None:
        """Auto-advance from end of video in 'Next' mode.

        Wraps to start on bookmarks/library tabs (where there is no
        pagination), so a single video looping with Next mode keeps moving
        through the list indefinitely instead of stopping at the end. Browse
        tab keeps its existing page-turn behaviour.
        """
        self._navigate_preview(1, wrap=True)
        # Sync popout if it's open
        if self._fullscreen_window and self._preview._current_path:
            self._update_fullscreen(
                self._preview._current_path,
                self._preview._info_label.text(),
            )

    def _is_post_saved(self, post_id: int) -> bool:
        """Check if a post is saved in the library (any folder).

        Walks the library by post id rather than consulting the bookmark
        folder list — library folders are filesystem-truth now, and a
        post can be in any folder regardless of bookmark state.
        """
        from ..core.config import find_library_files
        return bool(find_library_files(post_id))

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
        # delete_from_library now walks every library folder by post id
        # and deletes every match in one call — no folder hint needed.
        from ..core.cache import delete_from_library
        deleted = delete_from_library(post.id)
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
        self._remove_blacklisted_from_grid(tag=tag)

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
            self._remove_blacklisted_from_grid(post_url=post.file_url)

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
        from .preview import FullscreenPreview
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
        self._fullscreen_window.closed.connect(self._on_fullscreen_closed)
        self._fullscreen_window.privacy_requested.connect(self._toggle_privacy)
        # Set post tags for BL Tag menu
        post = self._preview._current_post
        if post:
            self._fullscreen_window.set_post_tags(post.tag_categories, post.tag_list)
        # Sync video player state from preview to popout
        pv = self._preview._video_player
        sv = self._fullscreen_window._video
        sv.volume = pv.volume
        sv.is_muted = pv.is_muted
        sv.autoplay = pv.autoplay
        sv.loop_state = pv.loop_state
        # Connect seek-after-load BEFORE set_media so we don't miss media_ready
        if video_pos > 0:
            def _seek_when_ready():
                sv.seek_to_ms(video_pos)
                try:
                    sv.media_ready.disconnect(_seek_when_ready)
                except RuntimeError:
                    pass
            sv.media_ready.connect(_seek_when_ready)
        self._fullscreen_window.set_media(path, info)
        # Always sync state — the save button is visible in both modes
        # (library mode = only Save shown, browse/bookmarks = full toolbar)
        # so its Unsave label needs to land before the user sees it.
        self._update_fullscreen_state()

    def _on_fullscreen_closed(self) -> None:
        # Persist popout window state to DB
        if self._fullscreen_window:
            from .preview import FullscreenPreview
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
        # Sync video player state from popout back to preview
        if self._fullscreen_window:
            sv = self._fullscreen_window._video
            pv = self._preview._video_player
            pv.volume = sv.volume
            pv.is_muted = sv.is_muted
            pv.autoplay = sv.autoplay
            pv.loop_state = sv.loop_state
        # Grab video position before cleanup
        video_pos = 0
        if self._fullscreen_window and self._fullscreen_window._stack.currentIndex() == 1:
            video_pos = self._fullscreen_window._video.get_position_ms()
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
        save_unsorted = save_menu.addAction("Unfiled")
        save_folder_actions = {}
        from ..core.config import library_folders
        for folder in library_folders():
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
        elif action == batch_dl:
            from .dialogs import select_directory
            dest = select_directory(self, "Download to folder")
            if dest:
                self._batch_download_posts(posts, dest)
        elif action == unfav_all:
            site_id = self._site_combo.currentData()
            if site_id:
                from ..core.cache import delete_from_library
                for post in posts:
                    # Single call now walks every library folder by post id.
                    delete_from_library(post.id)
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
        where = folder or "Unfiled"
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
                    # Store metadata for library search
                    self._db.save_library_meta(
                        post_id=post.id, tags=post.tags,
                        tag_categories=post.tag_categories,
                        score=post.score, rating=post.rating,
                        source=post.source, file_url=post.file_url,
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
                self._fullscreen_window._video.pause()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self._status.showMessage("Image not cached yet — double-click to download first")

    def _save_to_library(self, post: Post, folder: str | None) -> None:
        """Save (or relocate) an image in the library folder structure.

        If the post is already saved somewhere in the library, the existing
        file is renamed into the target folder rather than re-downloading.
        This is what makes "Save to Library → SomeFolder" act like a move
        when the post is already in Unfiled (or another folder), instead
        of producing a duplicate. rename() is atomic on the same filesystem
        so a crash mid-move can never leave both copies behind.
        """
        from ..core.config import saved_dir, saved_folder_dir, MEDIA_EXTENSIONS

        self._status.showMessage(f"Saving #{post.id} to library...")

        # Resolve destination synchronously — saved_folder_dir() does
        # the path-traversal check and may raise ValueError. Surface
        # that error here rather than from inside the async closure.
        try:
            if folder:
                dest_dir = saved_folder_dir(folder)
            else:
                dest_dir = saved_dir()
        except ValueError as e:
            self._status.showMessage(f"Invalid folder name: {e}")
            return
        dest_resolved = dest_dir.resolve()

        # Look for an existing copy of this post anywhere in the library.
        # The library is shallow (root + one level of subdirectories) so
        # this is cheap — at most one iterdir per top-level entry.
        existing: Path | None = None
        root = saved_dir()
        if root.is_dir():
            stem = str(post.id)
            for entry in root.iterdir():
                if entry.is_file() and entry.stem == stem and entry.suffix.lower() in MEDIA_EXTENSIONS:
                    existing = entry
                    break
                if entry.is_dir():
                    for sub in entry.iterdir():
                        if sub.is_file() and sub.stem == stem and sub.suffix.lower() in MEDIA_EXTENSIONS:
                            existing = sub
                            break
                    if existing is not None:
                        break

        async def _save():
            try:
                if existing is not None:
                    # Already in the library — relocate instead of re-saving.
                    if existing.parent.resolve() != dest_resolved:
                        target = dest_dir / existing.name
                        if target.exists():
                            # Destination already has a file with the same
                            # name (matched by post id, so it's the same
                            # post). Drop the source to collapse the
                            # duplicate rather than leaving both behind.
                            existing.unlink()
                        else:
                            try:
                                existing.rename(target)
                            except OSError:
                                # Cross-device rename — fall back to copy+unlink.
                                import shutil as _sh
                                _sh.move(str(existing), str(target))
                else:
                    # Not in the library yet — pull from cache and copy in.
                    path = await download_image(post.file_url)
                    ext = Path(path).suffix
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

                where = folder or "Unfiled"
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
                self._preview._video_player.pause()
            # Hide and pause popout
            if self._fullscreen_window and self._fullscreen_window.isVisible():
                if self._fullscreen_window._stack.currentIndex() == 1:
                    self._fullscreen_window._video.pause()
                self._fullscreen_window.hide()
        else:
            self._privacy_overlay.hide()
            if self._fullscreen_window:
                self._fullscreen_window.show()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, '_privacy_overlay') and self._privacy_on:
            self._privacy_overlay.setGeometry(self.rect())
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
        self._save_main_splitter_sizes()
        self._save_right_splitter_sizes()
        self._save_main_window_state()

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
                QComboBox::drop-down {
                    border: none;
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


# Base popout overlay style — always loaded *before* the user QSS so the
# floating top toolbar (`#_slideshow_toolbar`) and bottom video controls
# (`#_slideshow_controls`) get a sane translucent-black-with-white-text
# look on themes that don't define their own overlay rules. Bundled themes
# in `themes/` redefine the same selectors with their @palette colors and
# win on tie (last rule of equal specificity wins in QSS), so anyone using
# a packaged theme keeps the themed overlay; anyone with a stripped-down
# custom.qss still gets a usable overlay instead of bare letterbox.
_BASE_POPOUT_OVERLAY_QSS = """
QWidget#_slideshow_toolbar,
QWidget#_slideshow_controls {
    background: rgba(0, 0, 0, 160);
}
QWidget#_slideshow_toolbar *,
QWidget#_slideshow_controls * {
    background: transparent;
    color: white;
    border: none;
}
QWidget#_slideshow_toolbar QPushButton,
QWidget#_slideshow_controls QPushButton {
    background: transparent;
    color: white;
    border: 1px solid rgba(255, 255, 255, 80);
    padding: 2px 6px;
}
QWidget#_slideshow_toolbar QPushButton:hover,
QWidget#_slideshow_controls QPushButton:hover {
    background: rgba(255, 255, 255, 30);
}
QWidget#_slideshow_toolbar QSlider::groove:horizontal,
QWidget#_slideshow_controls QSlider::groove:horizontal {
    background: rgba(255, 255, 255, 40);
    height: 4px;
    border: none;
}
QWidget#_slideshow_toolbar QSlider::handle:horizontal,
QWidget#_slideshow_controls QSlider::handle:horizontal {
    background: white;
    width: 10px;
    margin: -4px 0;
    border: none;
}
QWidget#_slideshow_toolbar QSlider::sub-page:horizontal,
QWidget#_slideshow_controls QSlider::sub-page:horizontal {
    background: white;
}
QWidget#_slideshow_toolbar QLabel,
QWidget#_slideshow_controls QLabel {
    background: transparent;
    color: white;
}
"""


def _load_user_qss(path: Path) -> str:
    """Load a QSS file with optional @palette variable substitution.

    Qt's QSS dialect has no native variables, so we add a tiny preprocessor:

        /* @palette
           accent:        #cba6f7
           bg:            #1e1e2e
           text:          #cdd6f4
        */

        QWidget {
            background-color: ${bg};
            color: ${text};
            selection-background-color: ${accent};
        }

    The header comment block is parsed for `name: value` pairs and any
    `${name}` reference elsewhere in the file is substituted with the
    corresponding value before the QSS is handed to Qt. This lets users
    recolor a bundled theme by editing the palette block alone, without
    hunting through the body for every hex literal.

    Backward compatibility: a file without an @palette block is returned
    as-is, so plain hand-written Qt-standard QSS still loads unchanged.
    Unknown ${name} references are left in place verbatim and logged as
    warnings so typos are visible in the log.
    """
    import re
    text = path.read_text()
    palette_match = re.search(r'/\*\s*@palette\b(.*?)\*/', text, re.DOTALL)
    if not palette_match:
        return text

    palette: dict[str, str] = {}
    for raw_line in palette_match.group(1).splitlines():
        # Strip leading whitespace and any leading * from C-style continuation
        line = raw_line.strip().lstrip('*').strip()
        if not line or ':' not in line:
            continue
        key, value = line.split(':', 1)
        key = key.strip()
        value = value.strip().rstrip(';').strip()
        # Allow trailing comments on the same line
        if '/*' in value:
            value = value.split('/*', 1)[0].strip()
        if key and value:
            palette[key] = value

    refs = set(re.findall(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}', text))
    missing = refs - palette.keys()
    if missing:
        log.warning(
            f"QSS @palette: unknown vars {sorted(missing)} in {path.name} "
            f"— left in place verbatim, fix the @palette block to define them"
        )

    def replace(m):
        return palette.get(m.group(1), m.group(0))

    return re.sub(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}', replace, text)


def run() -> None:
    from ..core.config import data_dir

    app = QApplication(sys.argv)

    # Set a stable Wayland app_id so Hyprland and other compositors can
    # consistently identify our windows by class (not by title, which
    # changes when search terms appear in the title bar). Qt translates
    # setDesktopFileName into the xdg-shell app_id on Wayland.
    app.setApplicationName("booru-viewer")
    app.setDesktopFileName("booru-viewer")

    # mpv requires LC_NUMERIC=C — Qt resets the locale in QApplication(),
    # so we must restore it after Qt init but before creating any mpv instances.
    import locale
    locale.setlocale(locale.LC_NUMERIC, "C")

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
            # Run through the @palette preprocessor (see _load_user_qss
            # for the dialect). Plain Qt-standard QSS files without an
            # @palette block are returned unchanged.
            css_text = _load_user_qss(custom_css)

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
            # Prepend the base overlay defaults so even minimal custom.qss
            # files get a usable popout overlay. User rules with the same
            # selectors come last and win on tie.
            app.setStyleSheet(_BASE_POPOUT_OVERLAY_QSS + "\n" + css_text)

            # Extract selection color for grid highlight
            pal = app.palette()
            m = re.search(r'selection-background-color\s*:\s*(#[0-9a-fA-F]{3,8})', css_text)
            if m:
                pal.setColor(QPalette.ColorRole.Highlight, QColor(m.group(1)))
            app.setPalette(pal)
        except Exception as e:
            log.warning(f"Operation failed: {e}")
    else:
        # No custom.qss — still install the popout overlay defaults so the
        # floating toolbar/controls have a sane background instead of bare
        # letterbox color.
        app.setStyleSheet(_BASE_POPOUT_OVERLAY_QSS)

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
