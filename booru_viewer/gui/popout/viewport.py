"""Popout viewport math: persistent intent + drift tolerance."""

from __future__ import annotations

from typing import NamedTuple


class Viewport(NamedTuple):
    """Where and how large the user wants popout content to appear.

    Three numbers + an anchor mode, no aspect. Aspect is a property of
    the currently-displayed post and is recomputed from actual content
    on every navigation. The viewport stays put across navigations; the
    window rect is a derived projection (Viewport, content_aspect) →
    (x,y,w,h).

    `long_side` is the binding edge length: for landscape it becomes
    width, for portrait it becomes height. Symmetric across the two
    orientations, which is the property that breaks the
    width-anchor ratchet that the previous `_fit_to_content` had.

    `anchor` controls which point of the window stays fixed across
    navigations as the window size changes with aspect ratio:
    ``"center"`` (default) pins the window center; ``"tl"``/``"tr"``/
    ``"bl"``/``"br"`` pin the corresponding corner. The window
    grows/shrinks away from the anchored corner. The user can drag the
    window anywhere — the anchor only affects resize direction, not
    screen position.

    `center_x`/`center_y` hold the anchor point coordinates (center
    of the window in center mode, the pinned corner in corner modes).
    """
    center_x: float
    center_y: float
    long_side: float
    anchor: str = "center"


def anchor_point(x: float, y: float, w: float, h: float, anchor: str) -> tuple[float, float]:
    """Extract the anchor point from a window rect based on anchor mode."""
    if anchor == "tl":
        return (x, y)
    if anchor == "tr":
        return (x + w, y)
    if anchor == "bl":
        return (x, y + h)
    if anchor == "br":
        return (x + w, y + h)
    return (x + w / 2, y + h / 2)


# Maximum drift between our last-dispatched window rect and the current
# Hyprland-reported rect that we still treat as "no user action happened."
# Anything within this tolerance is absorbed (Hyprland gap rounding,
# subpixel accumulation, decoration accounting). Anything beyond it is
# treated as "the user dragged or resized the window externally" and the
# persistent viewport gets updated from current state.
#
# 2px is small enough not to false-positive on real user drags (which
# are always tens of pixels minimum) and large enough to absorb the
# 1-2px per-nav drift that compounds across many navigations.
_DRIFT_TOLERANCE = 2
