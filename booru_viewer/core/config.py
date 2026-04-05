"""Settings, paths, constants, platform detection."""

from __future__ import annotations

import platform
import sys
from pathlib import Path

APPNAME = "booru-viewer"
IS_WINDOWS = sys.platform == "win32"


def data_dir() -> Path:
    """Return the platform-appropriate data/cache directory."""
    if IS_WINDOWS:
        base = Path.home() / "AppData" / "Roaming"
    else:
        base = Path(
            __import__("os").environ.get(
                "XDG_DATA_HOME", str(Path.home() / ".local" / "share")
            )
        )
    path = base / APPNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    """Return the image cache directory."""
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def thumbnails_dir() -> Path:
    """Return the thumbnail cache directory."""
    path = data_dir() / "thumbnails"
    path.mkdir(parents=True, exist_ok=True)
    return path


_library_dir_override: Path | None = None


def set_library_dir(path: Path | None) -> None:
    global _library_dir_override
    _library_dir_override = path


def saved_dir() -> Path:
    """Return the saved images directory."""
    if _library_dir_override:
        path = _library_dir_override
    else:
        path = data_dir() / "saved"
    path.mkdir(parents=True, exist_ok=True)
    return path


def saved_folder_dir(folder: str) -> Path:
    """Return a subfolder inside saved images."""
    path = saved_dir() / folder
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    """Return the path to the SQLite database."""
    return data_dir() / "booru.db"


# Green-on-black palette
GREEN = "#00ff00"
DARK_GREEN = "#00cc00"
DIM_GREEN = "#009900"
BG = "#000000"
BG_LIGHT = "#111111"
BG_LIGHTER = "#1a1a1a"
BORDER = "#333333"

# Defaults
DEFAULT_THUMBNAIL_SIZE = (200, 200)
DEFAULT_PAGE_SIZE = 40
USER_AGENT = f"booru-viewer/0.1 ({platform.system()})"
MEDIA_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mkv", ".avi", ".mov")
