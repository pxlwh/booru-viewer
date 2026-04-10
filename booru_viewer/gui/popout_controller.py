"""Popout (fullscreen preview) lifecycle, state sync, and geometry persistence."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main_window import BooruApp

log = logging.getLogger("booru")


# -- Pure functions (tested in tests/gui/test_popout_controller.py) --


def build_video_sync_dict(
    volume: int,
    mute: bool,
    autoplay: bool,
    loop_state: int,
    position_ms: int,
) -> dict:
    """Build the video-state transfer dict used on popout open/close."""
    return {
        "volume": volume,
        "mute": mute,
        "autoplay": autoplay,
        "loop_state": loop_state,
        "position_ms": position_ms,
    }


# -- Controller --


class PopoutController:
    """Owns popout lifecycle, state sync, and geometry persistence."""

    def __init__(self, app: BooruApp) -> None:
        self._app = app
        self._fullscreen_window = None
        self._popout_active = False
        self._info_was_visible = False
        self._right_splitter_sizes: list[int] = []

    @property
    def window(self):
        return self._fullscreen_window

    @property
    def is_active(self) -> bool:
        return self._popout_active

    # -- Open --

    def open(self) -> None:
        path = self._app._preview._current_path
        if not path:
            return
        info = self._app._preview._info_label.text()
        video_pos = 0
        if self._app._preview._stack.currentIndex() == 1:
            video_pos = self._app._preview._video_player.get_position_ms()
        self._popout_active = True
        self._info_was_visible = self._app._info_panel.isVisible()
        self._right_splitter_sizes = self._app._right_splitter.sizes()
        self._app._preview.clear()
        self._app._preview.hide()
        self._app._info_panel.show()
        self._app._right_splitter.setSizes([0, 0, 1000])
        self._app._preview._current_path = path
        idx = self._app._grid.selected_index
        if 0 <= idx < len(self._app._posts):
            self._app._info_panel.set_post(self._app._posts[idx])
        from .popout.window import FullscreenPreview
        saved_geo = self._app._db.get_setting("slideshow_geometry")
        saved_fs = self._app._db.get_setting_bool("slideshow_fullscreen")
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
        cols = self._app._grid._flow.columns
        show_actions = self._app._stack.currentIndex() != 2
        monitor = self._app._db.get_setting("slideshow_monitor")
        self._fullscreen_window = FullscreenPreview(grid_cols=cols, show_actions=show_actions, monitor=monitor, parent=self._app)
        self._fullscreen_window.navigate.connect(self.navigate)
        self._fullscreen_window.play_next_requested.connect(self._app._on_video_end_next)
        from ..core.config import library_folders
        self._fullscreen_window.set_folders_callback(library_folders)
        self._fullscreen_window.save_to_folder.connect(self._app._post_actions.save_from_preview)
        self._fullscreen_window.unsave_requested.connect(self._app._post_actions.unsave_from_preview)
        if show_actions:
            self._fullscreen_window.bookmark_requested.connect(self._app._post_actions.bookmark_from_preview)
            self._fullscreen_window.set_bookmark_folders_callback(self._app._db.get_folders)
            self._fullscreen_window.bookmark_to_folder.connect(self._app._post_actions.bookmark_to_folder_from_preview)
            self._fullscreen_window.blacklist_tag_requested.connect(self._app._post_actions.blacklist_tag_from_popout)
            self._fullscreen_window.blacklist_post_requested.connect(self._app._post_actions.blacklist_post_from_popout)
        self._fullscreen_window.open_in_default.connect(self._app._open_preview_in_default)
        self._fullscreen_window.open_in_browser.connect(self._app._open_preview_in_browser)
        self._fullscreen_window.closed.connect(self.on_closed)
        self._fullscreen_window.privacy_requested.connect(self._app._privacy.toggle)
        post = self._app._preview._current_post
        if post:
            self._fullscreen_window.set_post_tags(post.tag_categories, post.tag_list)
        pv = self._app._preview._video_player
        self._fullscreen_window.sync_video_state(
            volume=pv.volume,
            mute=pv.is_muted,
            autoplay=pv.autoplay,
            loop_state=pv.loop_state,
        )
        if video_pos > 0:
            self._fullscreen_window.connect_media_ready_once(
                lambda: self._fullscreen_window.seek_video_to(video_pos)
            )
        pre_w = post.width if post else 0
        pre_h = post.height if post else 0
        self._fullscreen_window.set_media(path, info, width=pre_w, height=pre_h)
        self.update_state()

    # -- Close --

    def on_closed(self) -> None:
        if self._fullscreen_window:
            from .popout.window import FullscreenPreview
            fs = FullscreenPreview._saved_fullscreen
            geo = FullscreenPreview._saved_geometry
            self._app._db.set_setting("slideshow_fullscreen", "1" if fs else "0")
            if geo:
                self._app._db.set_setting("slideshow_geometry", f"{geo.x()},{geo.y()},{geo.width()},{geo.height()}")
        self._app._preview.show()
        if not self._info_was_visible:
            self._app._info_panel.hide()
        if self._right_splitter_sizes:
            self._app._right_splitter.setSizes(self._right_splitter_sizes)
        self._popout_active = False
        video_pos = 0
        if self._fullscreen_window:
            vstate = self._fullscreen_window.get_video_state()
            pv = self._app._preview._video_player
            pv.volume = vstate["volume"]
            pv.is_muted = vstate["mute"]
            pv.autoplay = vstate["autoplay"]
            pv.loop_state = vstate["loop_state"]
            video_pos = vstate["position_ms"]
        path = self._app._preview._current_path
        info = self._app._preview._info_label.text()
        self._fullscreen_window = None
        if path:
            if video_pos > 0:
                def _seek_preview():
                    self._app._preview._video_player.seek_to_ms(video_pos)
                    try:
                        self._app._preview._video_player.media_ready.disconnect(_seek_preview)
                    except RuntimeError:
                        pass
                self._app._preview._video_player.media_ready.connect(_seek_preview)
            self._app._preview.set_media(path, info)

    # -- Navigation --

    def navigate(self, direction: int) -> None:
        self._app._navigate_preview(direction)

    # -- State sync --

    def update_media(self, path: str, info: str) -> None:
        """Sync the popout with new media from browse/bookmark/library."""
        if self._fullscreen_window and self._fullscreen_window.isVisible():
            self._app._preview._video_player.stop()
            cp = self._app._preview._current_post
            w = cp.width if cp else 0
            h = cp.height if cp else 0
            self._fullscreen_window.set_media(path, info, width=w, height=h)
            show_full = self._app._stack.currentIndex() != 2
            self._fullscreen_window.set_toolbar_visibility(
                bookmark=show_full,
                save=True,
                bl_tag=show_full,
                bl_post=show_full,
            )
            self.update_state()

    def update_state(self) -> None:
        """Update popout button states by mirroring the embedded preview."""
        if not self._fullscreen_window:
            return
        self._fullscreen_window.update_state(
            self._app._preview._is_bookmarked,
            self._app._preview._is_saved,
        )
        post = self._app._preview._current_post
        if post is not None:
            self._fullscreen_window.set_post_tags(
                post.tag_categories or {}, post.tag_list
            )
