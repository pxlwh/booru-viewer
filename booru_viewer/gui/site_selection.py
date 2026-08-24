"""Pure helpers for the Multi site-selection UI.

Qt-free on purpose so they are testable without a QApplication; the
widget wiring (checkbox, checkable combo items, popup event filter)
lives in main_window.py.
"""

from __future__ import annotations


def parse_multi_site_ids(csv: str, sites: list) -> list:
    """Resolve a persisted CSV of site ids against the current sites.

    Returns matches in *sites* order — selector order, which is also
    the interleave order. Ids that no longer exist are dropped
    silently: a site removed while ticked just disappears from the
    selection instead of erroring.
    """
    if not csv:
        return []
    wanted = set()
    for part in csv.split(","):
        part = part.strip()
        if part.isdigit():
            wanted.add(int(part))
    return [s for s in sites if s.id in wanted]


def serialize_site_ids(site_ids: list[int]) -> str:
    """Inverse of `parse_multi_site_ids`, minus stale-id dropping."""
    return ",".join(str(i) for i in site_ids)


def summarize_selection(names: list[str]) -> str:
    """Combo display text while Multi is on.

    Two or more names collapse to a count: the combo is sized to fit
    site names, so a joined list clips mid-word. The caller puts the
    full list in the combo's tooltip instead.
    """
    if not names:
        return "No sites"
    if len(names) == 1:
        return names[0]
    return f"{len(names)} sites"


def effective_site_id(post, fallback):
    """The site a post belongs to, preferring the post's own stamp.

    Falls back to *fallback* (usually the site combo's current id) for
    unstamped posts — bookmark-tab and library-built Posts, or any
    construction path that predates stamping. Uses ``or`` rather than
    an ``is None`` check so the library's 0 sentinel also falls back.
    """
    return getattr(post, "site_id", None) or fallback
