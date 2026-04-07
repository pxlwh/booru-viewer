"""Settings, paths, constants, platform detection."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

APPNAME = "booru-viewer"
IS_WINDOWS = sys.platform == "win32"


def hypr_rules_enabled() -> bool:
    """Whether the in-code hyprctl dispatches that change window state
    should run.

    Returns False when BOORU_VIEWER_NO_HYPR_RULES is set in the environment.
    Callers should skip any hyprctl `dispatch` that would mutate window
    state (resize, move, togglefloating, setprop no_anim, the floating
    "prime" sequence). Read-only queries (`hyprctl clients -j`) are still
    fine — only mutations are blocked.

    The popout's keep_aspect_ratio enforcement is gated by the separate
    popout_aspect_lock_enabled() — it's a different concern.
    """
    return not os.environ.get("BOORU_VIEWER_NO_HYPR_RULES")


def popout_aspect_lock_enabled() -> bool:
    """Whether the popout's keep_aspect_ratio setprop should run.

    Returns False when BOORU_VIEWER_NO_POPOUT_ASPECT_LOCK is set in the
    environment. Independent of hypr_rules_enabled() so a ricer can free
    up the popout's shape (e.g. for fixed-square or panoramic popouts)
    while keeping the rest of the in-code hyprctl behavior, or vice versa.
    """
    return not os.environ.get("BOORU_VIEWER_NO_POPOUT_ASPECT_LOCK")


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
    """Return a subfolder inside saved images, refusing path traversal.

    Folder names should normally be filtered by `db._validate_folder_name`
    before reaching the filesystem, but this is a defense-in-depth check:
    resolve the candidate path and ensure it's still inside `saved_dir()`.
    Anything that escapes (`..`, absolute paths, symlink shenanigans) raises
    ValueError instead of silently writing to disk wherever the string points.
    """
    base = saved_dir().resolve()
    candidate = (base / folder).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as e:
        raise ValueError(f"Folder escapes saved directory: {folder!r}") from e
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def db_path() -> Path:
    """Return the path to the SQLite database."""
    return data_dir() / "booru.db"


# Defaults
DEFAULT_THUMBNAIL_SIZE = (200, 200)
DEFAULT_PAGE_SIZE = 40
USER_AGENT = f"booru-viewer/0.1 ({platform.system()})"
MEDIA_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mkv", ".avi", ".mov")
