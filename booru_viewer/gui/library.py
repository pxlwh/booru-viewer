"""Library browser widget — browse saved files on disk."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QComboBox,
    QMenu,
    QMessageBox,
    QApplication,
)

from ..core.config import saved_dir, saved_folder_dir, MEDIA_EXTENSIONS, thumbnails_dir
from .grid import ThumbnailGrid

log = logging.getLogger("booru")

LIBRARY_THUMB_SIZE = 180


class _LibThumbSignals(QObject):
    thumb_ready = Signal(int, str)
    video_thumb_request = Signal(int, str, str)  # index, source, dest


class LibraryView(QWidget):
    """Browse files saved to the library on disk."""

    file_selected = Signal(str)
    file_activated = Signal(str)

    def __init__(self, db=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._files: list[Path] = []
        self._signals = _LibThumbSignals()
        self._signals.thumb_ready.connect(
            self._on_thumb_ready, Qt.ConnectionType.QueuedConnection
        )
        self._signals.video_thumb_request.connect(
            self._capture_video_thumb, Qt.ConnectionType.QueuedConnection
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Top bar ---
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)

        self._folder_combo = QComboBox()
        self._folder_combo.setMinimumWidth(140)
        self._folder_combo.currentTextChanged.connect(lambda _: self.refresh())
        top.addWidget(self._folder_combo)

        self._sort_combo = QComboBox()
        self._sort_combo.addItems(["Date", "Name", "Size"])
        self._sort_combo.setFixedWidth(80)
        self._sort_combo.currentTextChanged.connect(lambda _: self.refresh())
        top.addWidget(self._sort_combo)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setFixedWidth(65)
        refresh_btn.clicked.connect(self.refresh)
        top.addWidget(refresh_btn)

        top.addStretch(1)
        layout.addLayout(top)

        # --- Count label ---
        self._count_label = QLabel()
        layout.addWidget(self._count_label)

        # --- Grid ---
        self._grid = ThumbnailGrid()
        self._grid.post_selected.connect(self._on_selected)
        self._grid.post_activated.connect(self._on_activated)
        self._grid.context_requested.connect(self._on_context_menu)
        self._grid.multi_context_requested.connect(self._on_multi_context_menu)
        layout.addWidget(self._grid)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Scan the selected folder, sort, display thumbnails."""
        root = saved_dir()
        if not root.exists() or not os.access(root, os.R_OK):
            self._count_label.setText("Library directory unreachable")
            self._count_label.setStyleSheet("color: #ff4444; font-weight: bold;")
            self._grid.set_posts(0)
            self._files = []
            return
        self._refresh_folders()
        self._files = self._scan_files()
        self._sort_files()

        if self._files:
            self._count_label.setText(f"{len(self._files)} files")
            self._count_label.setStyleSheet("")
        else:
            self._count_label.setText("Library empty or directory unreachable")
            self._count_label.setStyleSheet("color: #ff4444;")
        thumbs = self._grid.set_posts(len(self._files))

        lib_thumb_dir = thumbnails_dir() / "library"
        lib_thumb_dir.mkdir(parents=True, exist_ok=True)

        for i, (filepath, thumb) in enumerate(zip(self._files, thumbs)):
            thumb._cached_path = str(filepath)
            thumb.setToolTip(filepath.name)
            if not filepath.exists():
                thumb.set_missing(True)
                continue
            thumb.set_saved_locally(True)
            cached_thumb = lib_thumb_dir / f"{filepath.stem}.jpg"
            if cached_thumb.exists():
                pix = QPixmap(str(cached_thumb))
                if not pix.isNull():
                    thumb.set_pixmap(pix)
                    continue
            self._generate_thumb_async(i, filepath, cached_thumb)

    # ------------------------------------------------------------------
    # Folder list
    # ------------------------------------------------------------------

    def _refresh_folders(self) -> None:
        current = self._folder_combo.currentText()
        self._folder_combo.blockSignals(True)
        self._folder_combo.clear()
        self._folder_combo.addItem("All Files")
        self._folder_combo.addItem("Unsorted")

        root = saved_dir()
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                if entry.is_dir():
                    self._folder_combo.addItem(entry.name)

        idx = self._folder_combo.findText(current)
        if idx >= 0:
            self._folder_combo.setCurrentIndex(idx)
        self._folder_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # File scanning
    # ------------------------------------------------------------------

    def _scan_files(self) -> list[Path]:
        root = saved_dir()
        folder_text = self._folder_combo.currentText()

        if folder_text == "All Files":
            return self._collect_recursive(root)
        elif folder_text == "Unsorted":
            return self._collect_top_level(root)
        else:
            sub = root / folder_text
            if sub.is_dir():
                return self._collect_top_level(sub)
            return []

    @staticmethod
    def _collect_recursive(directory: Path) -> list[Path]:
        files: list[Path] = []
        for dirpath, _dirnames, filenames in os.walk(directory):
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() in MEDIA_EXTENSIONS:
                    files.append(p)
        return files

    @staticmethod
    def _collect_top_level(directory: Path) -> list[Path]:
        if not directory.is_dir():
            return []
        return [
            p
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS
        ]

    # ------------------------------------------------------------------
    # Sorting
    # ------------------------------------------------------------------

    def _sort_files(self) -> None:
        mode = self._sort_combo.currentText()
        if mode == "Name":
            self._files.sort(key=lambda p: p.name.lower())
        elif mode == "Size":
            self._files.sort(key=lambda p: p.stat().st_size, reverse=True)
        else:
            # Date — newest first
            self._files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    # ------------------------------------------------------------------
    # Async thumbnail generation
    # ------------------------------------------------------------------

    _VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".avi", ".mov"}

    def _generate_thumb_async(
        self, index: int, source: Path, dest: Path
    ) -> None:
        if source.suffix.lower() in self._VIDEO_EXTS:
            # Video thumbnails must run on main thread (Qt requirement)
            self._signals.video_thumb_request.emit(index, str(source), str(dest))
            return

        def _work() -> None:
            try:
                from PIL import Image
                with Image.open(source) as img:
                    img.thumbnail(
                        (LIBRARY_THUMB_SIZE, LIBRARY_THUMB_SIZE), Image.LANCZOS
                    )
                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                    img.save(str(dest), "JPEG", quality=85)
                if dest.exists():
                    self._signals.thumb_ready.emit(index, str(dest))
            except Exception as e:
                log.warning("Library thumb %d (%s) failed: %s", index, source.name, e)

        threading.Thread(target=_work, daemon=True).start()

    def _capture_video_thumb(self, index: int, source: str, dest: str) -> None:
        """Grab first frame from video. Tries ffmpeg, falls back to placeholder."""
        def _work():
            try:
                import subprocess
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", source, "-vframes", "1",
                     "-vf", f"scale={LIBRARY_THUMB_SIZE}:{LIBRARY_THUMB_SIZE}:force_original_aspect_ratio=decrease",
                     "-q:v", "5", dest],
                    capture_output=True, timeout=10,
                )
                if Path(dest).exists():
                    self._signals.thumb_ready.emit(index, dest)
                    return
            except (FileNotFoundError, Exception):
                pass
            # Fallback: generate a placeholder
            from PySide6.QtGui import QPainter, QColor, QFont
            from PySide6.QtGui import QPolygon
            from PySide6.QtCore import QPoint as QP
            pix = QPixmap(LIBRARY_THUMB_SIZE - 4, LIBRARY_THUMB_SIZE - 4)
            pix.fill(QColor(40, 40, 40))
            painter = QPainter(pix)
            painter.setPen(QColor(180, 180, 180))
            painter.setFont(QFont(painter.font().family(), 9))
            ext = Path(source).suffix.upper().lstrip(".")
            painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter, ext)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(180, 180, 180, 150))
            cx, cy = pix.width() // 2, pix.height() // 2 - 10
            painter.drawPolygon(QPolygon([QP(cx - 15, cy - 20), QP(cx - 15, cy + 20), QP(cx + 20, cy)]))
            painter.end()
            pix.save(dest, "JPEG", 85)
            if Path(dest).exists():
                self._signals.thumb_ready.emit(index, dest)

        threading.Thread(target=_work, daemon=True).start()

    def _on_thumb_ready(self, index: int, path: str) -> None:
        thumbs = self._grid._thumbs
        if 0 <= index < len(thumbs):
            pix = QPixmap(path)
            if not pix.isNull():
                thumbs[index].set_pixmap(pix)

    # ------------------------------------------------------------------
    # Selection signals
    # ------------------------------------------------------------------

    def _on_selected(self, index: int) -> None:
        if 0 <= index < len(self._files):
            self.file_selected.emit(str(self._files[index]))

    def _on_activated(self, index: int) -> None:
        if 0 <= index < len(self._files):
            self.file_activated.emit(str(self._files[index]))

    def _on_context_menu(self, index: int, pos) -> None:
        if index < 0 or index >= len(self._files):
            return
        filepath = self._files[index]

        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        menu = QMenu(self)
        open_default = menu.addAction("Open in Default App")
        open_folder = menu.addAction("Open Containing Folder")
        menu.addSeparator()
        copy_file = menu.addAction("Copy File to Clipboard")
        copy_path = menu.addAction("Copy File Path")
        menu.addSeparator()
        delete_action = menu.addAction("Delete from Library")

        action = menu.exec(pos)
        if not action:
            return

        if action == open_default:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(filepath)))
        elif action == open_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(filepath.parent)))
        elif action == copy_file:
            from PySide6.QtCore import QMimeData
            from PySide6.QtGui import QPixmap as _QP
            mime_data = QMimeData()
            mime_data.setUrls([QUrl.fromLocalFile(str(filepath.resolve()))])
            pix = _QP(str(filepath))
            if not pix.isNull():
                mime_data.setImageData(pix.toImage())
            QApplication.clipboard().setMimeData(mime_data)
        elif action == copy_path:
            QApplication.clipboard().setText(str(filepath))
        elif action == delete_action:
            reply = QMessageBox.question(
                self, "Confirm", f"Delete {filepath.name} from library?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                filepath.unlink(missing_ok=True)
                # Also remove cached thumbnail
                lib_thumb = thumbnails_dir() / "library" / f"{filepath.stem}.jpg"
                lib_thumb.unlink(missing_ok=True)
                self.refresh()

    def _on_multi_context_menu(self, indices: list, pos) -> None:
        files = [self._files[i] for i in indices if 0 <= i < len(self._files)]
        if not files:
            return

        menu = QMenu(self)
        delete_all = menu.addAction(f"Delete {len(files)} files from Library")

        action = menu.exec(pos)
        if not action:
            return

        if action == delete_all:
            reply = QMessageBox.question(
                self, "Confirm", f"Delete {len(files)} files from library?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                for f in files:
                    f.unlink(missing_ok=True)
                    lib_thumb = thumbnails_dir() / "library" / f"{f.stem}.jpg"
                    lib_thumb.unlink(missing_ok=True)
                self.refresh()
