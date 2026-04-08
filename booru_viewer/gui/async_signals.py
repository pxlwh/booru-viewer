"""Qt signal hub for async worker results."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class AsyncSignals(QObject):
    """Signals for async worker results."""
    search_done = Signal(list)
    search_append = Signal(list)
    search_error = Signal(str)
    thumb_done = Signal(int, str)
    image_done = Signal(str, str)
    image_error = Signal(str)
    # Fast-path for uncached video posts: emit the remote URL directly
    # so mpv can start streaming + decoding immediately instead of
    # waiting for download_image to write the whole file to disk first.
    # download_image still runs in parallel to populate the cache for
    # next time. Args: (url, info, width, height) — width/height come
    # from post.width/post.height for the popout pre-fit optimization.
    video_stream = Signal(str, str, int, int)
    bookmark_done = Signal(int, str)
    bookmark_error = Signal(str)
    autocomplete_done = Signal(list)
    batch_progress = Signal(int, int)      # current, total
    batch_done = Signal(str)
    download_progress = Signal(int, int)  # bytes_downloaded, total_bytes
    prefetch_progress = Signal(int, float)  # index, progress (0-1 or -1 to hide)
