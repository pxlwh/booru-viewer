"""Full media preview — image viewer with zoom/pan and video player."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import Qt, QPointF, QRect, Signal, QTimer, Property
from PySide6.QtGui import QPixmap, QPainter, QWheelEvent, QMouseEvent, QKeyEvent, QMovie, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMainWindow,
    QStackedWidget, QPushButton, QSlider, QMenu, QInputDialog, QStyle,
)

import mpv as mpvlib

_log = logging.getLogger("booru")


# -- Refactor compatibility shims (deleted in commit 14) --
from .media.constants import VIDEO_EXTENSIONS, _is_video  # re-export for refactor compat
from .popout.viewport import Viewport, _DRIFT_TOLERANCE  # re-export for refactor compat
from .media.image_viewer import ImageViewer  # re-export for refactor compat
from .media.mpv_gl import _MpvGLWidget, _MpvOpenGLSurface  # re-export for refactor compat
from .media.video_player import _ClickSeekSlider, VideoPlayer  # re-export for refactor compat
from .popout.window import FullscreenPreview  # re-export for refactor compat
from .preview_pane import ImagePreview  # re-export for refactor compat
