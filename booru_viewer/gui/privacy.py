"""Privacy-screen overlay for the main window."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QWidget

if TYPE_CHECKING:
    from .main_window import BooruApp


class PrivacyController:
    """Owns the privacy overlay toggle and popout coordination."""

    def __init__(self, app: BooruApp) -> None:
        self._app = app
        self._on = False
        self._overlay: QWidget | None = None
        self._popout_was_visible = False
        self._preview_was_playing = False

    @property
    def is_active(self) -> bool:
        return self._on

    def resize_overlay(self) -> None:
        """Re-fit the overlay to the main window's current rect."""
        if self._overlay is not None and self._on:
            self._overlay.setGeometry(self._app.rect())

    def toggle(self) -> None:
        if self._overlay is None:
            self._overlay = QWidget(self._app)
            self._overlay.setStyleSheet("background: black;")
            self._overlay.hide()

        self._on = not self._on
        if self._on:
            self._overlay.setGeometry(self._app.rect())
            self._overlay.raise_()
            self._overlay.show()
            self._app.setWindowTitle("booru-viewer")
            # Pause preview video, remembering whether it was playing
            self._preview_was_playing = False
            if self._app._preview._stack.currentIndex() == 1:
                mpv = self._app._preview._video_player._mpv
                self._preview_was_playing = mpv is not None and not mpv.pause
                self._app._preview._video_player.pause()
            # Delegate popout hide-and-pause to FullscreenPreview so it
            # can capture its own geometry for restore.
            self._popout_was_visible = bool(
                self._app._popout_ctrl.window
                and self._app._popout_ctrl.window.isVisible()
            )
            if self._popout_was_visible:
                self._app._popout_ctrl.window.privacy_hide()
        else:
            self._overlay.hide()
            # Resume embedded preview video only if it was playing before
            if self._preview_was_playing and self._app._preview._stack.currentIndex() == 1:
                self._app._preview._video_player.resume()
            # Restore the popout via its own privacy_show method, which
            # also re-dispatches the captured geometry to Hyprland (Qt
            # show() alone doesn't preserve position on Wayland) and
            # resumes its video.
            if self._popout_was_visible and self._app._popout_ctrl.window:
                self._app._popout_ctrl.window.privacy_show()
