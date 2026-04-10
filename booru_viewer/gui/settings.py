"""Settings dialog."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QTabWidget,
    QWidget,
    QLabel,
    QPushButton,
    QSpinBox,
    QComboBox,
    QCheckBox,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QGroupBox,
    QProgressBar,
)

from ..core.db import Database
from ..core.cache import cache_size_bytes, cache_file_count, clear_cache, evict_oldest
from ..core.config import (
    data_dir, cache_dir, thumbnails_dir, db_path, IS_WINDOWS,
)


class SettingsDialog(QDialog):
    """Full settings panel with tabs."""

    settings_changed = Signal()
    bookmarks_imported = Signal()

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("Settings")
        # Set only a minimum WIDTH explicitly. Leaving the minimum height
        # auto means Qt derives it from the layout's minimumSizeHint, which
        # respects the cache spinboxes' setMinimumHeight floor. A hardcoded
        # `setMinimumSize(550, 450)` was a hard floor that overrode the
        # layout's needs and let the user drag the dialog below the height
        # the cache tab actually requires, clipping the spinboxes.
        self.setMinimumWidth(550)

        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        self._tabs.addTab(self._build_general_tab(), "General")
        self._tabs.addTab(self._build_cache_tab(), "Cache")
        self._tabs.addTab(self._build_blacklist_tab(), "Blacklist")
        self._tabs.addTab(self._build_paths_tab(), "Paths")
        self._tabs.addTab(self._build_theme_tab(), "Theme")
        self._tabs.addTab(self._build_network_tab(), "Network")

        # Bottom buttons
        btns = QHBoxLayout()
        btns.addStretch()

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save_and_close)
        btns.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(cancel_btn)

        layout.addLayout(btns)

    @staticmethod
    def _spinbox_row(spinbox: QSpinBox) -> QWidget:
        """Wrap a QSpinBox in a horizontal layout with side-by-side
        [-] [spinbox] [+] buttons. Mirrors the search-bar score field
        pattern in app.py — QSpinBox's native vertical arrow buttons
        cramp the value text and read poorly in dense form layouts;
        explicit +/- buttons are clearer and respect the spinbox's
        configured singleStep.
        """
        spinbox.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        # Hard minimum height. QSS `min-height` is a hint the QFormLayout
        # can override under pressure (when the dialog is resized to its
        # absolute minimum bounds), which causes the spinbox value text to
        # vertically clip. setMinimumHeight is a Python-side floor that
        # propagates up the layout chain — the dialog's own min size grows
        # to accommodate it instead of squeezing the contents. 24px gives
        # a couple of extra pixels of headroom over the 22px native button
        # height for the 13px font, comfortable on every tested DPI/scale.
        spinbox.setMinimumHeight(24)
        container = QWidget()
        h = QHBoxLayout(container)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(2)
        h.addWidget(spinbox, 1)
        # Inline padding override matches the rest of the app's narrow
        # toolbar buttons. The new bundled themes use `padding: 2px 8px`
        # globally, but `2px 6px` here gives the +/- glyph a touch more
        # room to breathe in a 25px-wide button.
        _btn_style = "padding: 2px 6px;"
        minus = QPushButton("-")
        minus.setFixedWidth(25)
        minus.setStyleSheet(_btn_style)
        minus.clicked.connect(
            lambda: spinbox.setValue(spinbox.value() - spinbox.singleStep())
        )
        plus = QPushButton("+")
        plus.setFixedWidth(25)
        plus.setStyleSheet(_btn_style)
        plus.clicked.connect(
            lambda: spinbox.setValue(spinbox.value() + spinbox.singleStep())
        )
        h.addWidget(minus)
        h.addWidget(plus)
        return container

    # -- General tab --

    def _build_general_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        form = QFormLayout()

        # Page size
        self._page_size = QSpinBox()
        self._page_size.setRange(10, 100)
        self._page_size.setValue(self._db.get_setting_int("page_size"))
        form.addRow("Results per page:", self._spinbox_row(self._page_size))

        # Thumbnail size
        self._thumb_size = QSpinBox()
        self._thumb_size.setRange(100, 200)
        self._thumb_size.setSingleStep(20)
        self._thumb_size.setValue(self._db.get_setting_int("thumbnail_size"))
        form.addRow("Thumbnail size (px):", self._spinbox_row(self._thumb_size))

        # Default rating
        self._default_rating = QComboBox()
        self._default_rating.addItems(["all", "general", "sensitive", "questionable", "explicit"])
        current_rating = self._db.get_setting("default_rating")
        idx = self._default_rating.findText(current_rating)
        if idx >= 0:
            self._default_rating.setCurrentIndex(idx)
        form.addRow("Default rating filter:", self._default_rating)

        # Default site
        self._default_site = QComboBox()
        self._default_site.addItem("(none)", 0)
        for site in self._db.get_sites():
            self._default_site.addItem(site.name, site.id)
        default_site_id = self._db.get_setting_int("default_site_id")
        if default_site_id:
            idx = self._default_site.findData(default_site_id)
            if idx >= 0:
                self._default_site.setCurrentIndex(idx)
        form.addRow("Default site:", self._default_site)

        # Default min score
        self._default_score = QSpinBox()
        self._default_score.setRange(0, 99999)
        self._default_score.setValue(self._db.get_setting_int("default_score"))
        form.addRow("Default minimum score:", self._spinbox_row(self._default_score))

        # Preload thumbnails
        self._preload = QCheckBox("Load thumbnails automatically")
        self._preload.setChecked(self._db.get_setting_bool("preload_thumbnails"))
        form.addRow("", self._preload)

        # Prefetch adjacent posts
        self._prefetch_combo = QComboBox()
        self._prefetch_combo.addItems(["Off", "Nearby", "Aggressive"])
        prefetch_mode = self._db.get_setting("prefetch_mode") or "Off"
        idx = self._prefetch_combo.findText(prefetch_mode)
        if idx >= 0:
            self._prefetch_combo.setCurrentIndex(idx)
        form.addRow("Prefetch:", self._prefetch_combo)

        # Infinite scroll
        self._infinite_scroll = QCheckBox("Infinite scroll (replaces page buttons)")
        self._infinite_scroll.setChecked(self._db.get_setting_bool("infinite_scroll"))
        form.addRow("", self._infinite_scroll)

        # Unbookmark on save
        self._unbookmark_on_save = QCheckBox("Remove bookmark when saved to library")
        self._unbookmark_on_save.setChecked(self._db.get_setting_bool("unbookmark_on_save"))
        form.addRow("", self._unbookmark_on_save)

        # Search history
        self._search_history = QCheckBox("Record recent searches")
        self._search_history.setChecked(self._db.get_setting_bool("search_history_enabled"))
        form.addRow("", self._search_history)

        # Slideshow monitor
        from PySide6.QtWidgets import QApplication
        self._monitor_combo = QComboBox()
        self._monitor_combo.addItem("Same as app")
        for i, screen in enumerate(QApplication.screens()):
            self._monitor_combo.addItem(f"{screen.name()} ({screen.size().width()}x{screen.size().height()})")
        current_monitor = self._db.get_setting("slideshow_monitor")
        if current_monitor:
            idx = self._monitor_combo.findText(current_monitor)
            if idx >= 0:
                self._monitor_combo.setCurrentIndex(idx)
        form.addRow("Popout monitor:", self._monitor_combo)

        # File dialog platform (Linux only)
        self._file_dialog_combo = None
        if not IS_WINDOWS:
            self._file_dialog_combo = QComboBox()
            self._file_dialog_combo.addItems(["qt", "gtk"])
            current = self._db.get_setting("file_dialog_platform")
            idx = self._file_dialog_combo.findText(current)
            if idx >= 0:
                self._file_dialog_combo.setCurrentIndex(idx)
            form.addRow("File dialog (restart required):", self._file_dialog_combo)

        layout.addLayout(form)
        layout.addStretch()
        return w

    # -- Cache tab --

    def _build_cache_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # Cache stats
        stats_group = QGroupBox("Cache Statistics")
        stats_layout = QFormLayout(stats_group)

        images, thumbs = cache_file_count()
        total_bytes = cache_size_bytes()
        total_mb = total_bytes / (1024 * 1024)

        self._cache_images_label = QLabel(f"{images}")
        stats_layout.addRow("Cached images:", self._cache_images_label)

        self._cache_thumbs_label = QLabel(f"{thumbs}")
        stats_layout.addRow("Cached thumbnails:", self._cache_thumbs_label)

        self._cache_size_label = QLabel(f"{total_mb:.1f} MB")
        stats_layout.addRow("Total size:", self._cache_size_label)

        self._fav_count_label = QLabel(f"{self._db.bookmark_count()}")
        stats_layout.addRow("Bookmarks:", self._fav_count_label)

        layout.addWidget(stats_group)

        # Cache limits
        limits_group = QGroupBox("Cache Limits")
        limits_layout = QFormLayout(limits_group)

        self._max_cache = QSpinBox()
        self._max_cache.setRange(100, 50000)
        self._max_cache.setSingleStep(100)
        self._max_cache.setSuffix(" MB")
        self._max_cache.setValue(self._db.get_setting_int("max_cache_mb"))
        limits_layout.addRow("Max cache size:", self._spinbox_row(self._max_cache))

        self._max_thumb_cache = QSpinBox()
        self._max_thumb_cache.setRange(50, 10000)
        self._max_thumb_cache.setSingleStep(50)
        self._max_thumb_cache.setSuffix(" MB")
        self._max_thumb_cache.setValue(self._db.get_setting_int("max_thumb_cache_mb") or 500)
        limits_layout.addRow("Max thumbnail cache:", self._spinbox_row(self._max_thumb_cache))

        self._auto_evict = QCheckBox("Auto-evict oldest when limit reached")
        self._auto_evict.setChecked(self._db.get_setting_bool("auto_evict"))
        limits_layout.addRow("", self._auto_evict)

        self._clear_on_exit = QCheckBox("Clear cache on exit (session-only cache)")
        self._clear_on_exit.setChecked(self._db.get_setting_bool("clear_cache_on_exit"))
        limits_layout.addRow("", self._clear_on_exit)

        layout.addWidget(limits_group)

        # Cache actions
        actions_group = QGroupBox("Actions")
        actions_layout = QVBoxLayout(actions_group)

        btn_row1 = QHBoxLayout()

        clear_thumbs_btn = QPushButton("Clear Thumbnails")
        clear_thumbs_btn.clicked.connect(self._clear_thumbnails)
        btn_row1.addWidget(clear_thumbs_btn)

        clear_cache_btn = QPushButton("Clear Image Cache")
        clear_cache_btn.clicked.connect(self._clear_image_cache)
        btn_row1.addWidget(clear_cache_btn)

        actions_layout.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()

        clear_all_btn = QPushButton("Clear Everything")
        clear_all_btn.setStyleSheet(f"QPushButton {{ color: #ff4444; }}")
        clear_all_btn.clicked.connect(self._clear_all)
        btn_row2.addWidget(clear_all_btn)

        evict_btn = QPushButton("Evict to Limit Now")
        evict_btn.clicked.connect(self._evict_now)
        btn_row2.addWidget(evict_btn)

        actions_layout.addLayout(btn_row2)

        layout.addWidget(actions_group)
        layout.addStretch()
        return w

    # -- Blacklist tab --

    def _build_blacklist_tab(self) -> QWidget:
        from PySide6.QtWidgets import QTextEdit
        w = QWidget()
        layout = QVBoxLayout(w)

        self._bl_enabled = QCheckBox("Enable blacklist")
        self._bl_enabled.setChecked(self._db.get_setting_bool("blacklist_enabled"))
        layout.addWidget(self._bl_enabled)

        layout.addWidget(QLabel(
            "Posts containing these tags will be hidden from results.\n"
            "Paste tags separated by spaces or newlines:"
        ))

        self._bl_text = QTextEdit()
        self._bl_text.setPlaceholderText("tag1 tag2 tag3 ...")
        # Load existing tags into the text box
        tags = self._db.get_blacklisted_tags()
        self._bl_text.setPlainText(" ".join(tags))
        layout.addWidget(self._bl_text)

        io_row = QHBoxLayout()

        export_bl_btn = QPushButton("Export")
        export_bl_btn.clicked.connect(self._bl_export)
        io_row.addWidget(export_bl_btn)

        import_bl_btn = QPushButton("Import")
        import_bl_btn.clicked.connect(self._bl_import)
        io_row.addWidget(import_bl_btn)

        layout.addLayout(io_row)

        # Blacklisted posts
        layout.addWidget(QLabel("Blacklisted posts (by URL):"))
        self._bl_post_list = QListWidget()
        for url in sorted(self._db.get_blacklisted_posts()):
            self._bl_post_list.addItem(url)
        layout.addWidget(self._bl_post_list)

        bl_post_row = QHBoxLayout()
        add_post_btn = QPushButton("Add...")
        add_post_btn.clicked.connect(self._bl_add_post)
        bl_post_row.addWidget(add_post_btn)

        remove_post_btn = QPushButton("Remove Selected")
        remove_post_btn.clicked.connect(self._bl_remove_post)
        bl_post_row.addWidget(remove_post_btn)

        clear_posts_btn = QPushButton("Clear All")
        clear_posts_btn.clicked.connect(self._bl_clear_posts)
        bl_post_row.addWidget(clear_posts_btn)

        layout.addLayout(bl_post_row)
        return w

    def _bl_add_post(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getMultiLineText(
            self, "Add Blacklisted Posts",
            "Paste URLs (one per line or space-separated):",
        )
        if ok and text.strip():
            urls = text.replace("\n", " ").split()
            for url in urls:
                url = url.strip()
                if url:
                    self._db.add_blacklisted_post(url)
                    self._bl_post_list.addItem(url)

    def _bl_remove_post(self) -> None:
        item = self._bl_post_list.currentItem()
        if item:
            self._db.remove_blacklisted_post(item.text())
            self._bl_post_list.takeItem(self._bl_post_list.row(item))

    def _bl_clear_posts(self) -> None:
        reply = QMessageBox.question(
            self, "Confirm", "Remove all blacklisted posts?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            for url in self._db.get_blacklisted_posts():
                self._db.remove_blacklisted_post(url)
            self._bl_post_list.clear()

    # -- Paths tab --

    def _build_paths_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        form = QFormLayout()

        data = QLineEdit(str(data_dir()))
        data.setReadOnly(True)
        form.addRow("Data directory:", data)

        cache = QLineEdit(str(cache_dir()))
        cache.setReadOnly(True)
        form.addRow("Image cache:", cache)

        thumbs = QLineEdit(str(thumbnails_dir()))
        thumbs.setReadOnly(True)
        form.addRow("Thumbnails:", thumbs)

        db = QLineEdit(str(db_path()))
        db.setReadOnly(True)
        form.addRow("Database:", db)

        layout.addLayout(form)

        # Library directory (editable)
        lib_row = QHBoxLayout()
        from ..core.config import saved_dir
        current_lib = self._db.get_setting("library_dir") or str(saved_dir())
        self._library_dir = QLineEdit(current_lib)
        lib_row.addWidget(self._library_dir, stretch=1)
        browse_lib_btn = QPushButton("Browse...")
        browse_lib_btn.clicked.connect(self._browse_library_dir)
        lib_row.addWidget(browse_lib_btn)
        layout.addWidget(QLabel("Library directory:"))
        layout.addLayout(lib_row)

        # Library filename template (editable). Applies to every save action
        # — Save to Library, Save As, batch downloads, multi-select bulk
        # operations, and bookmark→library copies. Empty = post id.
        layout.addWidget(QLabel("Library filename template:"))
        self._library_filename_template = QLineEdit(
            self._db.get_setting("library_filename_template") or ""
        )
        self._library_filename_template.setPlaceholderText("e.g. %artist%_%id%   (leave blank for post id)")
        layout.addWidget(self._library_filename_template)
        tmpl_help = QLabel(
            "Tokens: %id% %md5% %ext% %rating% %score% "
            "%artist% %character% %copyright% %general% %meta% %species%\n"
            "Applies to every save action: Save to Library, Save As, Batch Download, "
            "multi-select bulk operations, and bookmark→library copies.\n"
            "All tokens work on all sites. Category tokens are fetched on demand."
        )
        tmpl_help.setWordWrap(True)
        tmpl_help.setStyleSheet("color: palette(mid); font-size: 10pt;")
        layout.addWidget(tmpl_help)

        open_btn = QPushButton("Open Data Folder")
        open_btn.clicked.connect(self._open_data_folder)
        layout.addWidget(open_btn)

        layout.addStretch()

        # Export / Import
        exp_group = QGroupBox("Backup")
        exp_layout = QHBoxLayout(exp_group)

        export_btn = QPushButton("Export Bookmarks")
        export_btn.clicked.connect(self._export_bookmarks)
        exp_layout.addWidget(export_btn)

        import_btn = QPushButton("Import Bookmarks")
        import_btn.clicked.connect(self._import_bookmarks)
        exp_layout.addWidget(import_btn)

        layout.addWidget(exp_group)

        return w

    # -- Theme tab --

    def _build_theme_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        layout.addWidget(QLabel(
            "Customize the app's appearance with a Qt stylesheet (QSS).\n"
            "Place a custom.qss file in your data directory.\n"
            "Restart the app after editing."
        ))

        css_path = data_dir() / "custom.qss"
        path_label = QLineEdit(str(css_path))
        path_label.setReadOnly(True)
        layout.addWidget(path_label)

        btn_row = QHBoxLayout()

        edit_btn = QPushButton("Edit custom.qss")
        edit_btn.clicked.connect(self._edit_custom_css)
        btn_row.addWidget(edit_btn)

        create_btn = QPushButton("Create from Template")
        create_btn.clicked.connect(self._create_css_template)
        btn_row.addWidget(create_btn)

        guide_btn = QPushButton("View Guide")
        guide_btn.clicked.connect(self._view_css_guide)
        btn_row.addWidget(guide_btn)

        layout.addLayout(btn_row)

        delete_btn = QPushButton("Delete custom.qss (Reset to Default)")
        delete_btn.clicked.connect(self._delete_custom_css)
        layout.addWidget(delete_btn)

        layout.addStretch()
        return w

    # -- Network tab --

    def _build_network_tab(self) -> QWidget:
        from ..core.cache import get_connection_log
        w = QWidget()
        layout = QVBoxLayout(w)

        layout.addWidget(QLabel(
            "All hosts contacted this session. booru-viewer only connects\n"
            "to the booru sites you configure — no telemetry or analytics."
        ))

        self._net_list = QListWidget()
        self._net_list.setAlternatingRowColors(True)
        layout.addWidget(self._net_list)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_network)
        layout.addWidget(refresh_btn)

        self._refresh_network()
        return w

    def _refresh_network(self) -> None:
        from ..core.cache import get_connection_log
        self._net_list.clear()
        log = get_connection_log()
        if not log:
            self._net_list.addItem("No connections made yet")
            return
        for host, times in log.items():
            self._net_list.addItem(f"{host}  ({len(times)} requests, last: {times[-1]})")

    def _edit_custom_css(self) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        css_path = data_dir() / "custom.qss"
        if not css_path.exists():
            css_path.write_text("/* booru-viewer custom stylesheet */\n\n")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(css_path)))

    def _create_css_template(self) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        # Open themes reference online and create a blank custom.qss for editing
        QDesktopServices.openUrl(QUrl("https://git.pax.moe/pax/booru-viewer/src/branch/main/themes"))
        css_path = data_dir() / "custom.qss"
        if not css_path.exists():
            css_path.write_text("/* booru-viewer custom stylesheet */\n/* See themes reference for examples */\n\n")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(css_path)))

    def _view_css_guide(self) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        dest = data_dir() / "custom_css_guide.txt"
        # Copy guide to appdata if not already there
        if not dest.exists():
            import sys
            # Try source tree, then PyInstaller bundle
            for candidate in [
                Path(__file__).parent / "custom_css_guide.txt",
                Path(getattr(sys, '_MEIPASS', __file__)) / "booru_viewer" / "gui" / "custom_css_guide.txt",
            ]:
                if candidate.is_file():
                    dest.write_text(candidate.read_text())
                    break
        if dest.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(dest)))
        else:
            # Fallback: show in dialog
            from PySide6.QtWidgets import QTextEdit, QDialog, QVBoxLayout
            dlg = QDialog(self)
            dlg.setWindowTitle("Custom CSS Guide")
            dlg.resize(600, 500)
            layout = QVBoxLayout(dlg)
            text = QTextEdit()
            text.setReadOnly(True)
            text.setPlainText(
                "booru-viewer Custom Stylesheet Guide\n"
                "=====================================\n\n"
                "Place a file named 'custom.qss' in your data directory.\n"
                f"Path: {data_dir() / 'custom.qss'}\n\n"
                "WIDGET REFERENCE\n"
                "----------------\n"
                "QMainWindow, QPushButton, QLineEdit, QComboBox, QScrollBar,\n"
                "QLabel, QStatusBar, QTabWidget, QTabBar, QListWidget,\n"
                "QMenu, QMenuBar, QToolTip, QDialog, QSplitter, QProgressBar,\n"
                "QSpinBox, QCheckBox, QSlider\n\n"
                "STATES: :hover, :pressed, :focus, :selected, :disabled\n\n"
                "PROPERTIES: color, background-color, border, border-radius,\n"
                "padding, margin, font-family, font-size\n\n"
                "EXAMPLE\n"
                "-------\n"
                "QPushButton {\n"
                "    background: #333; color: white;\n"
                "    border: 1px solid #555; border-radius: 4px;\n"
                "    padding: 6px 16px;\n"
                "}\n"
                "QPushButton:hover { background: #555; }\n\n"
                "Restart the app after editing custom.qss."
            )
            layout.addWidget(text)
            dlg.exec()

    def _delete_custom_css(self) -> None:
        css_path = data_dir() / "custom.qss"
        if css_path.exists():
            css_path.unlink()
            QMessageBox.information(self, "Done", "Deleted. Restart to use default theme.")
        else:
            QMessageBox.information(self, "Info", "No custom.qss found.")

    # -- Actions --

    def _refresh_stats(self) -> None:
        images, thumbs = cache_file_count()
        total_bytes = cache_size_bytes()
        total_mb = total_bytes / (1024 * 1024)
        self._cache_images_label.setText(f"{images}")
        self._cache_thumbs_label.setText(f"{thumbs}")
        self._cache_size_label.setText(f"{total_mb:.1f} MB")

    def _clear_thumbnails(self) -> None:
        count = clear_cache(clear_images=False, clear_thumbnails=True)
        QMessageBox.information(self, "Done", f"Deleted {count} thumbnails.")
        self._refresh_stats()

    def _clear_image_cache(self) -> None:
        reply = QMessageBox.question(
            self, "Confirm",
            "Delete all cached images? (Bookmarks stay in the database but cached files are removed.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            count = clear_cache(clear_images=True, clear_thumbnails=False)
            QMessageBox.information(self, "Done", f"Deleted {count} cached images.")
            self._refresh_stats()

    def _clear_all(self) -> None:
        reply = QMessageBox.question(
            self, "Confirm",
            "Delete ALL cached images and thumbnails?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            count = clear_cache(clear_images=True, clear_thumbnails=True)
            QMessageBox.information(self, "Done", f"Deleted {count} files.")
            self._refresh_stats()

    def _evict_now(self) -> None:
        max_bytes = self._max_cache.value() * 1024 * 1024
        # Protect bookmarked file paths
        protected = set()
        for fav in self._db.get_bookmarks(limit=999999):
            if fav.cached_path:
                protected.add(fav.cached_path)
        count = evict_oldest(max_bytes, protected)
        QMessageBox.information(self, "Done", f"Evicted {count} files.")
        self._refresh_stats()

    def _bl_export(self) -> None:
        from .dialogs import save_file
        path = save_file(self, "Export Blacklist", "blacklist.txt", "Text (*.txt)")
        if not path:
            return
        tags = self._bl_text.toPlainText().split()
        with open(path, "w") as f:
            f.write("\n".join(tags))
        QMessageBox.information(self, "Done", f"Exported {len(tags)} tags.")

    def _bl_import(self) -> None:
        from .dialogs import open_file
        path = open_file(self, "Import Blacklist", "Text (*.txt)")
        if not path:
            return
        try:
            with open(path) as f:
                tags = [line.strip() for line in f if line.strip()]
            existing = self._bl_text.toPlainText().split()
            merged = list(dict.fromkeys(existing + tags))
            self._bl_text.setPlainText(" ".join(merged))
            QMessageBox.information(self, "Done", f"Imported {len(tags)} tags.")
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))

    def _browse_library_dir(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, "Select Library Directory", self._library_dir.text())
        if path:
            self._library_dir.setText(path)

    def _open_data_folder(self) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir())))

    def _export_bookmarks(self) -> None:
        from .dialogs import save_file
        import json
        path = save_file(self, "Export Bookmarks", "bookmarks.json", "JSON (*.json)")
        if not path:
            return
        favs = self._db.get_bookmarks(limit=999999)
        data = [
            {
                "post_id": f.post_id,
                "site_id": f.site_id,
                "file_url": f.file_url,
                "preview_url": f.preview_url,
                "tags": f.tags,
                "rating": f.rating,
                "score": f.score,
                "source": f.source,
                "folder": f.folder,
                "bookmarked_at": f.bookmarked_at,
            }
            for f in favs
        ]
        with open(path, "w") as fp:
            json.dump(data, fp, indent=2)
        QMessageBox.information(self, "Done", f"Exported {len(data)} bookmarks.")

    def _import_bookmarks(self) -> None:
        from .dialogs import open_file
        import json
        path = open_file(self, "Import Bookmarks", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path) as fp:
                data = json.load(fp)
            count = 0
            for item in data:
                try:
                    folder = item.get("folder")
                    self._db.add_bookmark(
                        site_id=item["site_id"],
                        post_id=item["post_id"],
                        file_url=item["file_url"],
                        preview_url=item.get("preview_url"),
                        tags=item.get("tags", ""),
                        rating=item.get("rating"),
                        score=item.get("score"),
                        source=item.get("source"),
                        folder=folder,
                    )
                    if folder:
                        self._db.add_folder(folder)
                    count += 1
                except Exception:
                    pass
            QMessageBox.information(self, "Done", f"Imported {count} bookmarks.")
            self.bookmarks_imported.emit()
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))

    # -- Save --

    def _save_and_close(self) -> None:
        self._db.set_setting("page_size", str(self._page_size.value()))
        self._db.set_setting("thumbnail_size", str(self._thumb_size.value()))
        self._db.set_setting("default_rating", self._default_rating.currentText())
        self._db.set_setting("default_site_id", str(self._default_site.currentData() or 0))
        self._db.set_setting("default_score", str(self._default_score.value()))
        self._db.set_setting("preload_thumbnails", "1" if self._preload.isChecked() else "0")
        self._db.set_setting("prefetch_mode", self._prefetch_combo.currentText())
        self._db.set_setting("infinite_scroll", "1" if self._infinite_scroll.isChecked() else "0")
        self._db.set_setting("unbookmark_on_save", "1" if self._unbookmark_on_save.isChecked() else "0")
        self._db.set_setting("search_history_enabled", "1" if self._search_history.isChecked() else "0")
        self._db.set_setting("slideshow_monitor", self._monitor_combo.currentText())
        self._db.set_setting("library_dir", self._library_dir.text().strip())
        self._db.set_setting("library_filename_template", self._library_filename_template.text().strip())
        self._db.set_setting("max_cache_mb", str(self._max_cache.value()))
        self._db.set_setting("max_thumb_cache_mb", str(self._max_thumb_cache.value()))
        self._db.set_setting("auto_evict", "1" if self._auto_evict.isChecked() else "0")
        self._db.set_setting("clear_cache_on_exit", "1" if self._clear_on_exit.isChecked() else "0")
        self._db.set_setting("blacklist_enabled", "1" if self._bl_enabled.isChecked() else "0")
        # Sync blacklist from text box
        new_tags = set(self._bl_text.toPlainText().split())
        old_tags = set(self._db.get_blacklisted_tags())
        for tag in old_tags - new_tags:
            self._db.remove_blacklisted_tag(tag)
        for tag in new_tags - old_tags:
            self._db.add_blacklisted_tag(tag)
        if self._file_dialog_combo is not None:
            self._db.set_setting("file_dialog_platform", self._file_dialog_combo.currentText())
        self.settings_changed.emit()
        self.accept()
