"""Unified save flow for writing Post media to disk.

This module owns the single function (`save_post_file`) that every save
site in the app routes through. It exists to keep filename-template
rendering, sequential collision suffixes, same-post idempotency, and
the conditional `library_meta` write all in one place instead of
duplicated across the save sites that used to live in
`gui/main_window.py` and `gui/bookmarks.py`.

Boundary rule: this module imports from `core.cache`, `core.config`,
`core.db`. It does NOT import from `gui/`. That's how both `bookmarks.py`
and `main_window.py` can call into it without dragging in a circular
import.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .config import render_filename_template, saved_dir
from .db import Database

if TYPE_CHECKING:
    from .api.base import Post


_CATEGORY_TOKENS = {"%artist%", "%character%", "%copyright%", "%general%", "%meta%", "%species%"}


async def save_post_file(
    src: Path,
    post: "Post",
    dest_dir: Path,
    db: Database,
    in_flight: set[str] | None = None,
    explicit_name: str | None = None,
    category_fetcher=None,
) -> Path:
    """Copy a Post's already-cached media file into `dest_dir`.

    Single source of truth for "write a Post to disk." Every save site
    — Browse Save, multi-select bulk save, Save As, Download All, multi-
    select Download All, bookmark→library, bookmark Save As — routes
    through this function.

    Filename comes from the `library_filename_template` setting,
    rendered against the Post via `render_filename_template`. If
    `explicit_name` is set (the user typed a name into a Save As
    dialog), the template is bypassed and `explicit_name` is used as
    the basename. Collision resolution still runs in case the user
    picked an existing path that belongs to a different post.

    Collision resolution: if the chosen basename exists at `dest_dir`
    or is already claimed by an earlier iteration of the current batch
    (via `in_flight`), and the existing copy belongs to a *different*
    post, sequential `_1`, `_2`, `_3`, ... suffixes are appended until
    a free name is found. If the existing copy is the same post
    (verified by `library_meta` lookup or the legacy digit-stem
    fallback), the chosen basename is returned unchanged and the copy
    is skipped — the re-save is idempotent.

    `library_meta` write: if the resolved destination is inside
    `saved_dir()`, a `library_meta` row is written for the post,
    including the resolved filename. This is the case for Save to
    Library (any folder), bulk Save to Library, batch Download into a
    library folder, multi-select batch Download into a library folder,
    Save As into a library folder (a deliberate behavior change from
    v0.2.3 — Save As never wrote meta before), and bookmark→library
    copies.

    Parameters:
        src: cached media file to copy from. Must already exist on disk
            (caller is responsible for `download_image()` or
            `cached_path_for()`).
        post: Post object whose tags drive template rendering and
            populate the `library_meta` row.
        dest_dir: target directory. Created if missing. Anywhere on
            disk; only matters for the `library_meta` write whether
            it's inside `saved_dir()`.
        db: Database instance. Used for the same-post-on-disk lookup
            during collision resolution and the conditional meta write.
        in_flight: optional set of basenames already claimed by earlier
            iterations of the current batch. The chosen basename is
            added to this set before return. Pass `None` for single-
            file saves; pass a shared `set()` (one per batch
            invocation, never reused across invocations) for batches.
        explicit_name: optional override. When set, the template is
            bypassed and this basename (already including extension)
            is used as the starting point for collision resolution.

    Returns:
        The actual `Path` the file landed at after collision
        resolution. Callers use this for status messages and signal
        emission.
    """
    if explicit_name is not None:
        basename = explicit_name
    else:
        template = db.get_setting("library_filename_template")
        # If the template uses category tokens and the post has no
        # categories yet, fetch them synchronously before rendering.
        # This guarantees the filename is correct even when saving
        # a post the user hasn't clicked (no prior ensure from the
        # info panel path).
        if (
            category_fetcher is not None
            and not post.tag_categories
            and template
            and any(tok in template for tok in _CATEGORY_TOKENS)
        ):
            await category_fetcher.ensure_categories(post)
        basename = render_filename_template(template, post, src.suffix)

    in_flight_set: set[str] = in_flight if in_flight is not None else set()
    final_basename = _resolve_collision(
        dest_dir,
        basename,
        post.id,
        in_flight_set,
        lambda path, pid: _same_post_on_disk(db, path, pid),
    )

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / final_basename

    # Skip the copy if same-post-on-disk made the chosen basename
    # match an existing copy of this post (idempotent re-save).
    if not dest.exists():
        shutil.copy2(src, dest)

    if in_flight is not None:
        in_flight.add(final_basename)

    if _is_in_library(dest):
        db.save_library_meta(
            post_id=post.id,
            tags=post.tags,
            tag_categories=post.tag_categories,
            score=post.score,
            rating=post.rating,
            source=post.source,
            file_url=post.file_url,
            filename=final_basename,
        )

    return dest


def _is_in_library(path: Path) -> bool:
    """True if `path` is inside `saved_dir()`. Wraps `is_relative_to`
    in a try/except for older Pythons where it raises on non-relative
    paths instead of returning False."""
    try:
        return path.is_relative_to(saved_dir())
    except ValueError:
        return False


def _same_post_on_disk(db: Database, path: Path, post_id: int) -> bool:
    """True if `path` is already a saved copy of `post_id`.

    Looks up the path's basename in `library_meta` first; if no row,
    falls back to the legacy v0.2.3 digit-stem heuristic (a file named
    `12345.jpg` is treated as belonging to post 12345). Returns False
    when `path` is outside `saved_dir()` — we can't tell who owns
    files anywhere else.
    """
    try:
        if not path.is_relative_to(saved_dir()):
            return False
    except ValueError:
        return False

    existing_id = db.get_library_post_id_by_filename(path.name)
    if existing_id is not None:
        return existing_id == post_id

    # Legacy v0.2.3 fallback: rows whose filename column is empty
    # belong to digit-stem files. Mirrors the digit-stem checks in
    # gui/library.py.
    if path.stem.isdigit():
        return int(path.stem) == post_id

    return False


def _resolve_collision(
    dest_dir: Path,
    basename: str,
    post_id: int,
    in_flight: set[str],
    same_post_check: Callable[[Path, int], bool],
) -> str:
    """Return a basename that won't collide at `dest_dir`.

    Same-post collisions — the basename already belongs to this post,
    on disk — are returned unchanged so the caller skips the copy and
    the re-save is idempotent. Different-post collisions get sequential
    `_1`, `_2`, `_3`, ... suffixes until a free name is found.

    The `in_flight` set is consulted alongside on-disk state so that
    earlier iterations of the same batch don't get re-picked for later
    posts in the same call.
    """
    target = dest_dir / basename
    if basename not in in_flight and not target.exists():
        return basename
    if target.exists() and same_post_check(target, post_id):
        return basename

    stem, dot, ext = basename.rpartition(".")
    if not dot:
        stem, ext = basename, ""
    else:
        ext = "." + ext

    n = 1
    while n <= 9999:
        candidate = f"{stem}_{n}{ext}"
        cand_path = dest_dir / candidate
        if candidate not in in_flight and not cand_path.exists():
            return candidate
        if cand_path.exists() and same_post_check(cand_path, post_id):
            return candidate
        n += 1

    # Defensive fallback. 10k collisions for one rendered name means
    # something is structurally wrong (template renders to a constant?
    # filesystem state corruption?); break the loop with the post id
    # so the user gets *some* file rather than an exception.
    return f"{stem}_{post_id}{ext}"
