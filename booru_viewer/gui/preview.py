"""Full media preview — image viewer with zoom/pan and video player."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QPoint, QPointF, Signal, QUrl
from PySide6.QtGui import QPixmap, QPainter, QWheelEvent, QMouseEvent, QKeyEvent, QColor, QMovie
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMainWindow,
    QStackedWidget, QPushButton, QSlider, QMenu, QInputDialog, QStyle,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget

from ..core.config import MEDIA_EXTENSIONS

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mkv", ".avi", ".mov")


def _is_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


class FullscreenPreview(QMainWindow):
    """Fullscreen media viewer with navigation — images, GIFs, and video."""

    navigate = Signal(int)  # direction: -1/+1 for left/right, -cols/+cols for up/down
    bookmark_requested = Signal()
    save_toggle_requested = Signal()  # save or unsave depending on state
    closed = Signal()

    def __init__(self, grid_cols: int = 3, show_actions: bool = True, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("booru-viewer — Fullscreen")
        self._grid_cols = grid_cols

        central = QWidget()
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Top toolbar
        self._toolbar = QWidget()
        toolbar = QHBoxLayout(self._toolbar)
        toolbar.setContentsMargins(8, 4, 8, 4)

        self._bookmark_btn = QPushButton("Bookmark")
        self._bookmark_btn.setFixedWidth(80)
        self._bookmark_btn.clicked.connect(self.bookmark_requested)
        toolbar.addWidget(self._bookmark_btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.setFixedWidth(70)
        self._save_btn.clicked.connect(self.save_toggle_requested)
        toolbar.addWidget(self._save_btn)
        self._is_saved = False

        if not show_actions:
            self._bookmark_btn.hide()
            self._save_btn.hide()

        toolbar.addStretch()

        self._info_label = QLabel()
        toolbar.addWidget(self._info_label)

        main_layout.addWidget(self._toolbar)

        # Media stack
        self._stack = QStackedWidget()
        main_layout.addWidget(self._stack, stretch=1)

        self._viewer = ImageViewer()
        self._viewer.close_requested.connect(self.close)
        self._stack.addWidget(self._viewer)

        self._video = VideoPlayer()
        self._video.play_next.connect(lambda: self.navigate.emit(1))
        self._stack.addWidget(self._video)

        self.setCentralWidget(central)

        from PySide6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)
        self.showFullScreen()

    def update_state(self, bookmarked: bool, saved: bool) -> None:
        self._bookmark_btn.setText("Unbookmark" if bookmarked else "Bookmark")
        self._bookmark_btn.setFixedWidth(90 if bookmarked else 80)
        self._is_saved = saved
        self._save_btn.setText("Unsave" if saved else "Save")

    def set_media(self, path: str, info: str = "") -> None:
        self._info_label.setText(info)
        ext = Path(path).suffix.lower()
        if _is_video(path):
            self._viewer.clear()
            self._video.stop()
            self._video.play_file(path, info)
            self._stack.setCurrentIndex(1)
        elif ext == ".gif":
            self._video.stop()
            self._viewer.set_gif(path, info)
            self._stack.setCurrentIndex(0)
        else:
            self._video.stop()
            pix = QPixmap(path)
            if not pix.isNull():
                self._viewer.set_image(pix, info)
            self._stack.setCurrentIndex(0)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QLineEdit, QTextEdit, QSpinBox, QComboBox
        if event.type() == QEvent.Type.KeyPress:
            # Only intercept when slideshow is the active window
            if not self.isActiveWindow():
                return super().eventFilter(obj, event)
            # Don't intercept keys when typing in text inputs
            if isinstance(obj, (QLineEdit, QTextEdit, QSpinBox, QComboBox)):
                return super().eventFilter(obj, event)
            key = event.key()
            mods = event.modifiers()
            if key == Qt.Key.Key_H and mods & Qt.KeyboardModifier.ControlModifier:
                self._toolbar.setVisible(not self._toolbar.isVisible())
                # Also hide video controls if showing video
                if self._stack.currentIndex() == 1:
                    for child in self._video.findChildren(QPushButton):
                        child.setVisible(self._toolbar.isVisible())
                    for child in self._video.findChildren(QSlider):
                        child.setVisible(self._toolbar.isVisible())
                    for child in self._video.findChildren(QLabel):
                        child.setVisible(self._toolbar.isVisible())
                return True
            elif key in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
                self.close()
                return True
            elif key in (Qt.Key.Key_Left, Qt.Key.Key_H):
                self.navigate.emit(-1)
                return True
            elif key in (Qt.Key.Key_Right, Qt.Key.Key_L):
                self.navigate.emit(1)
                return True
            elif key in (Qt.Key.Key_Up, Qt.Key.Key_K):
                self.navigate.emit(-self._grid_cols)
                return True
            elif key in (Qt.Key.Key_Down, Qt.Key.Key_J):
                self.navigate.emit(self._grid_cols)
                return True
            elif key == Qt.Key.Key_F11:
                if self.isFullScreen():
                    self.showNormal()
                else:
                    self.showFullScreen()
                return True
            elif key == Qt.Key.Key_Space and self._stack.currentIndex() == 1:
                self._video._toggle_play()
                return True
            elif key == Qt.Key.Key_Period and self._stack.currentIndex() == 1:
                self._video._seek_relative(5000)
                return True
            elif key == Qt.Key.Key_Comma and self._stack.currentIndex() == 1:
                self._video._seek_relative(-5000)
                return True
        if event.type() == QEvent.Type.Wheel and self._stack.currentIndex() == 1 and self.isActiveWindow():
            delta = event.angleDelta().y()
            if delta:
                vol = self._video._audio.volume()
                vol = max(0.0, min(1.0, vol + (0.05 if delta > 0 else -0.05)))
                self._video._audio.setVolume(vol)
                self._video._vol_slider.setValue(int(vol * 100))
                return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.instance().removeEventFilter(self)
        self._video.stop()
        self.closed.emit()
        super().closeEvent(event)


# -- Image Viewer (zoom/pan) --

class ImageViewer(QWidget):
    """Zoomable, pannable image viewer."""

    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._movie: QMovie | None = None
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self._drag_start: QPointF | None = None
        self._drag_offset = QPointF(0, 0)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._info_text = ""

    def set_image(self, pixmap: QPixmap, info: str = "") -> None:
        self._stop_movie()
        self._pixmap = pixmap
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self._info_text = info
        self._fit_to_view()
        self.update()

    def set_gif(self, path: str, info: str = "") -> None:
        self._stop_movie()
        self._movie = QMovie(path)
        self._movie.frameChanged.connect(self._on_gif_frame)
        self._movie.start()
        self._info_text = info
        # Set initial pixmap from first frame
        self._pixmap = self._movie.currentPixmap()
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self._fit_to_view()
        self.update()

    def _on_gif_frame(self) -> None:
        if self._movie:
            self._pixmap = self._movie.currentPixmap()
            self.update()

    def _stop_movie(self) -> None:
        if self._movie:
            self._movie.stop()
            self._movie = None

    def clear(self) -> None:
        self._stop_movie()
        self._pixmap = None
        self._info_text = ""
        self.update()

    def _fit_to_view(self) -> None:
        if not self._pixmap:
            return
        vw, vh = self.width(), self.height()
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw == 0 or ph == 0:
            return
        scale_w = vw / pw
        scale_h = vh / ph
        self._zoom = min(scale_w, scale_h, 1.0)
        self._offset = QPointF(
            (vw - pw * self._zoom) / 2,
            (vh - ph * self._zoom) / 2,
        )

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(pal.ColorRole.Window))
        if self._pixmap:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.translate(self._offset)
            p.scale(self._zoom, self._zoom)
            p.drawPixmap(0, 0, self._pixmap)
            p.resetTransform()
        p.end()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._pixmap:
            return
        mouse_pos = event.position()
        old_zoom = self._zoom
        delta = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 1 / 1.15
        self._zoom = max(0.1, min(self._zoom * factor, 20.0))
        ratio = self._zoom / old_zoom
        self._offset = mouse_pos - ratio * (mouse_pos - self._offset)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._fit_to_view()
            self.update()
        elif event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position()
            self._drag_offset = QPointF(self._offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is not None:
            delta = event.position() - self._drag_start
            self._offset = self._drag_offset + delta
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_start = None
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
            self.close_requested.emit()
        elif event.key() == Qt.Key.Key_0:
            self._fit_to_view()
            self.update()
        elif event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._zoom = min(self._zoom * 1.2, 20.0)
            self.update()
        elif event.key() == Qt.Key.Key_Minus:
            self._zoom = max(self._zoom / 1.2, 0.1)
            self.update()
        else:
            event.ignore()

    def resizeEvent(self, event) -> None:
        if self._pixmap:
            self._fit_to_view()
            self.update()


class _ClickSeekSlider(QSlider):
    """Slider that jumps to the clicked position instead of page-stepping."""
    clicked_position = Signal(int)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            val = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), int(event.position().x()), self.width()
            )
            self.setValue(val)
            self.clicked_position.emit(val)
        super().mousePressEvent(event)


# -- Video Player --

class VideoPlayer(QWidget):
    """Video player with transport controls."""

    play_next = Signal()  # emitted when video ends in "next" mode

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Video surface
        self._video_widget = QVideoWidget()
        self._video_widget.setAutoFillBackground(True)
        layout.addWidget(self._video_widget, stretch=1)

        # Player
        self._player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video_widget)
        self._audio.setVolume(0.5)

        # Controls bar
        controls = QHBoxLayout()
        controls.setContentsMargins(4, 2, 4, 2)

        self._play_btn = QPushButton("Play")
        self._play_btn.setFixedWidth(65)
        self._play_btn.clicked.connect(self._toggle_play)
        controls.addWidget(self._play_btn)

        self._time_label = QLabel("0:00")
        self._time_label.setFixedWidth(45)
        controls.addWidget(self._time_label)

        self._seek_slider = _ClickSeekSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.sliderMoved.connect(self._seek)
        self._seek_slider.clicked_position.connect(self._seek)
        controls.addWidget(self._seek_slider, stretch=1)

        self._duration_label = QLabel("0:00")
        self._duration_label.setFixedWidth(45)
        controls.addWidget(self._duration_label)

        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(50)
        self._vol_slider.setFixedWidth(80)
        self._vol_slider.valueChanged.connect(self._set_volume)
        controls.addWidget(self._vol_slider)

        self._mute_btn = QPushButton("Mute")
        self._mute_btn.setFixedWidth(80)
        self._mute_btn.clicked.connect(self._toggle_mute)
        controls.addWidget(self._mute_btn)

        self._autoplay = True
        self._autoplay_btn = QPushButton("Auto")
        self._autoplay_btn.setFixedWidth(50)
        self._autoplay_btn.setCheckable(True)
        self._autoplay_btn.setChecked(True)
        self._autoplay_btn.setToolTip("Auto-play videos when selected")
        self._autoplay_btn.clicked.connect(self._toggle_autoplay)
        controls.addWidget(self._autoplay_btn)

        self._loop_mode = True
        self._loop_btn = QPushButton("Loop")
        self._loop_btn.setFixedWidth(55)
        self._loop_btn.setCheckable(True)
        self._loop_btn.setChecked(True)
        self._loop_btn.setToolTip("Loop: replay video / Next: play next post")
        self._loop_btn.clicked.connect(self._toggle_loop)
        controls.addWidget(self._loop_btn)

        layout.addLayout(controls)

        # Signals
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_error)
        self._current_file: str | None = None
        self._error_fired = False

    def play_file(self, path: str, info: str = "") -> None:
        self._current_file = path
        self._error_fired = False
        self._player.setLoops(
            QMediaPlayer.Loops.Infinite if self._loop_mode else 1
        )
        self._player.setSource(QUrl.fromLocalFile(path))
        if self._autoplay:
            self._player.play()
        else:
            self._player.pause()

    def _toggle_autoplay(self, checked: bool = True) -> None:
        self._autoplay = self._autoplay_btn.isChecked()
        self._autoplay_btn.setText("Auto" if self._autoplay else "Man.")

    def _toggle_loop(self) -> None:
        self._loop_mode = self._loop_btn.isChecked()
        self._loop_btn.setText("Loop" if self._loop_mode else "Next")
        self._player.setLoops(
            QMediaPlayer.Loops.Infinite if self._loop_mode else 1
        )

    def stop(self) -> None:
        self._player.stop()
        self._player.setSource(QUrl())

    def _toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _seek(self, pos: int) -> None:
        self._player.setPosition(pos)

    def _seek_relative(self, ms: int) -> None:
        pos = max(0, self._player.position() + ms)
        self._player.setPosition(pos)

    def _set_volume(self, val: int) -> None:
        self._audio.setVolume(val / 100.0)

    def _toggle_mute(self) -> None:
        self._audio.setMuted(not self._audio.isMuted())
        self._mute_btn.setText("Unmute" if self._audio.isMuted() else "Mute")

    def _on_position(self, pos: int) -> None:
        if not self._seek_slider.isSliderDown():
            self._seek_slider.setValue(pos)
        self._time_label.setText(self._fmt(pos))

    def _on_duration(self, dur: int) -> None:
        self._seek_slider.setRange(0, dur)
        self._duration_label.setText(self._fmt(dur))

    def _on_state(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._play_btn.setText("Pause")
        else:
            self._play_btn.setText("Play")

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia and not self._loop_mode:
            self.play_next.emit()

    def _on_error(self, error, msg: str = "") -> None:
        if self._current_file and not self._error_fired:
            self._error_fired = True
            import logging
            logging.getLogger("booru").warning(f"Video playback error: {error} {msg} ({self._current_file})")

    @staticmethod
    def _fmt(ms: int) -> str:
        s = ms // 1000
        m = s // 60
        return f"{m}:{s % 60:02d}"


# -- Combined Preview (image + video) --

class ImagePreview(QWidget):
    """Combined media preview — auto-switches between image and video."""

    close_requested = Signal()
    open_in_default = Signal()
    open_in_browser = Signal()
    save_to_folder = Signal(str)
    unsave_requested = Signal()
    bookmark_requested = Signal()
    navigate = Signal(int)  # -1 = prev, +1 = next
    fullscreen_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._folders_callback = None
        self._current_path: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack)

        # Image viewer (index 0)
        self._image_viewer = ImageViewer()
        self._image_viewer.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._image_viewer.close_requested.connect(self.close_requested)
        self._stack.addWidget(self._image_viewer)

        # Video player (index 1)
        self._video_player = VideoPlayer()
        self._video_player.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._video_player.play_next.connect(lambda: self.navigate.emit(1))
        self._stack.addWidget(self._video_player)

        # Info label
        self._info_label = QLabel()
        self._info_label.setStyleSheet("padding: 2px 6px;")
        layout.addWidget(self._info_label)

        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    # Keep these for compatibility with app.py accessing them
    @property
    def _pixmap(self):
        return self._image_viewer._pixmap

    @property
    def _info_text(self):
        return self._image_viewer._info_text

    def set_folders_callback(self, callback) -> None:
        self._folders_callback = callback

    def set_image(self, pixmap: QPixmap, info: str = "") -> None:
        self._video_player.stop()
        self._image_viewer.set_image(pixmap, info)
        self._stack.setCurrentIndex(0)
        self._info_label.setText(info)
        self._current_path = None

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

    def clear(self) -> None:
        self._video_player.stop()
        self._image_viewer.clear()
        self._info_label.setText("")
        self._current_path = None

    def _on_context_menu(self, pos) -> None:
        menu = QMenu(self)
        fav_action = menu.addAction("Bookmark")

        save_menu = menu.addMenu("Save to Library")
        save_unsorted = save_menu.addAction("Unsorted")
        save_menu.addSeparator()
        save_folder_actions = {}
        if self._folders_callback:
            for folder in self._folders_callback():
                a = save_menu.addAction(folder)
                save_folder_actions[id(a)] = folder
        save_menu.addSeparator()
        save_new = save_menu.addAction("+ New Folder...")

        menu.addSeparator()
        copy_image = None
        if self._stack.currentIndex() == 0 and self._image_viewer._pixmap:
            copy_image = menu.addAction("Copy Image to Clipboard")
        open_action = menu.addAction("Open in Default App")
        browser_action = menu.addAction("Open in Browser")

        # Image-specific
        reset_action = None
        if self._stack.currentIndex() == 0:
            reset_action = menu.addAction("Reset View")

        menu.addSeparator()
        unsave_action = menu.addAction("Unsave from Library")

        slideshow_action = None
        if self._current_path:
            slideshow_action = menu.addAction("Slideshow Mode")
        clear_action = menu.addAction("Clear Preview")

        action = menu.exec(self.mapToGlobal(pos))
        if not action:
            return
        if action == fav_action:
            self.bookmark_requested.emit()
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
            QApplication.clipboard().setPixmap(self._image_viewer._pixmap)
        elif action == open_action:
            self.open_in_default.emit()
        elif action == browser_action:
            self.open_in_browser.emit()
        elif action == reset_action:
            self._image_viewer._fit_to_view()
            self._image_viewer.update()
        elif action == unsave_action:
            self.unsave_requested.emit()
        elif action == slideshow_action:
            self.fullscreen_requested.emit()
        elif action == clear_action:
            self.close_requested.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            event.ignore()
        else:
            super().mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        if self._stack.currentIndex() == 1:
            delta = event.angleDelta().y()
            vol = self._video_player._audio.volume()
            vol = max(0.0, min(1.0, vol + (0.05 if delta > 0 else -0.05)))
            self._video_player._audio.setVolume(vol)
            self._video_player._vol_slider.setValue(int(vol * 100))
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._stack.currentIndex() == 0:
            self._image_viewer.keyPressEvent(event)
        elif event.key() == Qt.Key.Key_Space:
            self._video_player._toggle_play()
        elif event.key() == Qt.Key.Key_Period:
            self._video_player._seek_relative(5000)
        elif event.key() == Qt.Key.Key_Comma:
            self._video_player._seek_relative(-5000)
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_H):
            self.navigate.emit(-1)
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_L):
            self.navigate.emit(1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
