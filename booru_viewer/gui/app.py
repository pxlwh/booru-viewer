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


# -- Refactor compatibility shims (deleted in commit 14) --
from .search_state import SearchState  # re-export for refactor compat
from .log_handler import LogHandler  # re-export for refactor compat
from .async_signals import AsyncSignals  # re-export for refactor compat
from .info_panel import InfoPanel  # re-export for refactor compat
from .main_window import BooruApp  # re-export for refactor compat
from .app_runtime import run  # re-export for refactor compat
