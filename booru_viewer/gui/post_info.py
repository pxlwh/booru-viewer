"""Composition of the preview pane's info line.

Pure and Qt-free so the format can be tested directly. Extracted from an
inline f-string in main_window when the info line gained a site label —
with several boorus in one grid, "which site is this from" stops being
inferable from the selector.
"""

from __future__ import annotations


def format_post_info(post, suffix: str, site_name: str = "") -> str:
    """Build the one-line post summary shown under the preview.

    *suffix* is the file extension without a dot; it is uppercased.
    *site_name* is omitted entirely when empty, so single-site mode and
    posts with no recorded site read exactly as before.
    """
    parts = [f"#{post.id}", f"score:{post.score}", f"[{post.rating}]", suffix.upper()]
    if site_name:
        parts.append(site_name)
    if getattr(post, "created_at", ""):
        parts.append(post.created_at)
    return "  ".join(parts)
