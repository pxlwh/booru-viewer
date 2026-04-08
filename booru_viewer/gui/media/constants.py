"""Shared constants and predicates for media files."""

from __future__ import annotations

from pathlib import Path

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mkv", ".avi", ".mov")


def _is_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS
