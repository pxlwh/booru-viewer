"""Settings, paths, constants, platform detection."""

from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api.base import Post

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


def library_folders() -> list[str]:
    """List library folder names — direct subdirectories of saved_dir().

    The library is filesystem-truth: a folder exists iff there is a real
    directory on disk. There is no separate DB list of folder names. This
    is the source the "Save to Library → folder" menus everywhere should
    read from. Bookmark folders (DB-backed) are a different concept.
    """
    root = saved_dir()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir())


def find_library_files(post_id: int) -> list[Path]:
    """Return all library files matching `post_id` across every folder.

    The library has a flat shape: root + one level of subdirectories.
    We walk it shallowly (one iterdir of root + one iterdir per subdir)
    looking for any media file whose stem equals str(post_id). Used by:
    - "is this post saved?" badges (any match → yes)
    - delete_from_library (delete every match — handles duplicates left
      by the old save-to-folder copy bug in a single click)
    - the move-aware _save_to_library / library "Move to Folder" actions
    """
    matches: list[Path] = []
    root = saved_dir()
    if not root.is_dir():
        return matches
    stem = str(post_id)
    for entry in root.iterdir():
        if entry.is_file() and entry.stem == stem and entry.suffix.lower() in MEDIA_EXTENSIONS:
            matches.append(entry)
        elif entry.is_dir():
            for sub in entry.iterdir():
                if sub.is_file() and sub.stem == stem and sub.suffix.lower() in MEDIA_EXTENSIONS:
                    matches.append(sub)
    return matches


def render_filename_template(template: str, post: "Post", ext: str) -> str:
    """Render a filename template against a Post into a filesystem-safe basename.

    Tokens supported:
        %id%        post id
        %md5%       md5 hash extracted from file_url (empty if URL doesn't carry one)
        %ext%       extension without the leading dot
        %rating%    post.rating or empty
        %score%     post.score
        %artist%    underscore-joined names from post.tag_categories["artist"]
        %character% same, character category
        %copyright% same, copyright category
        %general%   same, general category
        %meta%      same, meta category
        %species%   same, species category

    The returned string is a basename including the extension. If `template`
    is empty or post-sanitization the rendered stem is empty, falls back to
    f"{post.id}{ext}" so callers always get a usable name.

    The rendered stem is capped at 200 characters before the extension is
    appended. This stays under the 255-byte ext4/NTFS filename limit for
    typical ASCII/Latin-1 templates; users typing emoji-heavy templates may
    still hit the limit but won't see a hard error from this function.

    Sanitization replaces filesystem-reserved characters (`/\\:*?"<>|`) with
    underscores, collapses whitespace runs to a single underscore, and strips
    leading/trailing dots/spaces and `..` prefixes so the rendered name can't
    escape the destination directory or trip Windows' trailing-dot quirk.
    """
    if not template:
        return f"{post.id}{ext}"

    cats = post.tag_categories or {}

    def _join_cat(name: str) -> str:
        # API clients (danbooru.py, e621.py) store categories with
        # Capitalized keys ("Artist", "Character", ...) — that's the
        # convention info_panel/preview_pane already iterate against.
        # Accept either casing here so future drift in either direction
        # doesn't silently break templates.
        items = cats.get(name) or cats.get(name.lower()) or cats.get(name.capitalize()) or []
        return "_".join(items)

    # %md5% — most boorus name files by md5 in the URL path
    # (e.g. https://cdn.donmai.us/original/0a/1b/0a1b...md5...{ext}).
    # Extract the URL stem and accept it only if it's 32 hex chars.
    md5 = ""
    try:
        from urllib.parse import urlparse
        url_path = urlparse(post.file_url).path
        url_stem = Path(url_path).stem
        if len(url_stem) == 32 and all(c in "0123456789abcdef" for c in url_stem.lower()):
            md5 = url_stem
    except Exception:
        pass

    has_ext_token = "%ext%" in template
    replacements = {
        "%id%": str(post.id),
        "%md5%": md5,
        "%ext%": ext.lstrip("."),
        "%rating%": post.rating or "",
        "%score%": str(post.score),
        "%artist%": _join_cat("Artist"),
        "%character%": _join_cat("Character"),
        "%copyright%": _join_cat("Copyright"),
        "%general%": _join_cat("General"),
        "%meta%": _join_cat("Meta"),
        "%species%": _join_cat("Species"),
    }

    rendered = template
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)

    # Sanitization: filesystem-reserved chars first, then control chars,
    # then whitespace collapse, then leading-cleanup.
    for ch in '/\\:*?"<>|':
        rendered = rendered.replace(ch, "_")
    rendered = "".join(c if ord(c) >= 32 else "_" for c in rendered)
    rendered = re.sub(r"\s+", "_", rendered)
    while rendered.startswith(".."):
        rendered = rendered[2:]
    rendered = rendered.lstrip("._")
    rendered = rendered.rstrip("._ ")

    # Length cap on the stem (before any system-appended extension).
    if len(rendered) > 200:
        rendered = rendered[:200].rstrip("._ ")

    if not rendered:
        return f"{post.id}{ext}"

    if not has_ext_token:
        rendered = rendered + ext

    return rendered


# Defaults
DEFAULT_THUMBNAIL_SIZE = (200, 200)
DEFAULT_PAGE_SIZE = 40
USER_AGENT = f"booru-viewer/0.1 ({platform.system()})"
MEDIA_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mkv", ".avi", ".mov")
