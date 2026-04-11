"""Hyprland IPC helpers for the popout window.

Module-level functions that wrap `hyprctl` for window state queries
and dispatches. Extracted from `popout/window.py` so the popout's Qt
adapter can call them through a clean import surface and so the state
machine refactor's `FitWindowToContent` effect handler has a single
place to find them.

This module DOES touch `subprocess` and `os.environ`, so it's gated
behind the same `HYPRLAND_INSTANCE_SIGNATURE` env var check the
legacy code used. Off-Hyprland systems no-op or return None at every
entry point.

The legacy `FullscreenPreview._hyprctl_*` methods become 1-line
shims that call into this module — see commit 13's changes to
`popout/window.py`. The shims preserve byte-for-byte call-site
compatibility for the existing window.py code; commit 14's adapter
rewrite drops them in favor of direct calls.
"""

from __future__ import annotations

import json
import os
import subprocess

from ...core.config import hypr_rules_enabled, popout_aspect_lock_enabled


def _on_hyprland() -> bool:
    """True if running under Hyprland (env signature present)."""
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def get_window(window_title: str) -> dict | None:
    """Return the Hyprland window dict whose `title` matches.

    Returns None if not on Hyprland, if `hyprctl clients -j` fails,
    or if no client matches the title. The legacy `_hyprctl_get_window`
    on `FullscreenPreview` is a 1-line shim around this.
    """
    if not _on_hyprland():
        return None
    try:
        result = subprocess.run(
            ["hyprctl", "clients", "-j"],
            capture_output=True, text=True, timeout=1,
        )
        for c in json.loads(result.stdout):
            if c.get("title") == window_title:
                return c
    except Exception:
        pass
    return None


def resize(window_title: str, w: int, h: int) -> None:
    """Ask Hyprland to resize the popout and lock its aspect ratio.

    No-op on non-Hyprland systems. Tiled windows skip the resize
    (fights the layout) but still get the aspect-lock setprop if
    that's enabled.

    Behavior is gated by two independent env vars (see core/config.py):
      - BOORU_VIEWER_NO_HYPR_RULES: skip resize and no_anim parts
      - BOORU_VIEWER_NO_POPOUT_ASPECT_LOCK: skip the keep_aspect_ratio
        setprop

    Either, both, or neither may be set. The aspect-ratio carve-out
    means a ricer can opt out of in-code window management while
    still keeping mpv playback at the right shape (or vice versa).
    """
    if not _on_hyprland():
        return
    rules_on = hypr_rules_enabled()
    aspect_on = popout_aspect_lock_enabled()
    if not rules_on and not aspect_on:
        return  # nothing to dispatch
    win = get_window(window_title)
    if not win:
        return
    addr = win.get("address")
    if not addr:
        return
    cmds: list[str] = []
    if not win.get("floating"):
        # Tiled — don't resize (fights the layout). Optionally set
        # aspect lock and no_anim depending on the env vars.
        if rules_on:
            cmds.append(f"dispatch setprop address:{addr} no_anim 1")
        if aspect_on:
            cmds.append(f"dispatch setprop address:{addr} keep_aspect_ratio 1")
    else:
        if rules_on:
            cmds.append(f"dispatch setprop address:{addr} no_anim 1")
        if aspect_on:
            cmds.append(f"dispatch setprop address:{addr} keep_aspect_ratio 0")
        if rules_on:
            cmds.append(f"dispatch resizewindowpixel exact {w} {h},address:{addr}")
        if aspect_on:
            cmds.append(f"dispatch setprop address:{addr} keep_aspect_ratio 1")
    if not cmds:
        return
    _dispatch_batch(cmds)


def resize_and_move(
    window_title: str,
    w: int,
    h: int,
    x: int,
    y: int,
    win: dict | None = None,
) -> None:
    """Atomically resize and move the popout via a single hyprctl batch.

    Gated by BOORU_VIEWER_NO_HYPR_RULES (resize/move/no_anim parts)
    and BOORU_VIEWER_NO_POPOUT_ASPECT_LOCK (the keep_aspect_ratio
    parts).

    `win` may be passed in by the caller to skip the `get_window`
    subprocess call. The address is the only thing we actually need
    from it; threading it through cuts the per-fit subprocess count
    from three to one and removes ~6ms of GUI-thread blocking every
    time the popout fits to new content. The legacy
    `_hyprctl_resize_and_move` on `FullscreenPreview` already used
    this optimization; the module-level function preserves it.
    """
    if not _on_hyprland():
        return
    rules_on = hypr_rules_enabled()
    aspect_on = popout_aspect_lock_enabled()
    if not rules_on and not aspect_on:
        return
    if win is None:
        win = get_window(window_title)
    if not win or not win.get("floating"):
        return
    addr = win.get("address")
    if not addr:
        return
    cmds: list[str] = []
    if rules_on:
        cmds.append(f"dispatch setprop address:{addr} no_anim 1")
    if aspect_on:
        cmds.append(f"dispatch setprop address:{addr} keep_aspect_ratio 0")
    if rules_on:
        cmds.append(f"dispatch resizewindowpixel exact {w} {h},address:{addr}")
        cmds.append(f"dispatch movewindowpixel exact {x} {y},address:{addr}")
    if aspect_on:
        cmds.append(f"dispatch setprop address:{addr} keep_aspect_ratio 1")
    if not cmds:
        return
    _dispatch_batch(cmds)


def _dispatch_batch(cmds: list[str]) -> None:
    """Fire-and-forget hyprctl --batch with the given commands.

    Uses `subprocess.Popen` (not `run`) so the call returns
    immediately without waiting for hyprctl. The current popout code
    relied on this same fire-and-forget pattern to avoid GUI-thread
    blocking on every fit dispatch.
    """
    try:
        subprocess.Popen(
            ["hyprctl", "--batch", " ; ".join(cmds)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


def get_monitor_available_rect(monitor_id: int | None = None) -> tuple[int, int, int, int] | None:
    """Return (x, y, w, h) of a monitor's usable area, accounting for
    exclusive zones (Waybar, etc.) via the ``reserved`` field.

    Falls back to the first monitor if *monitor_id* is None or not found.
    Returns None if not on Hyprland or the query fails.
    """
    if not _on_hyprland():
        return None
    try:
        result = subprocess.run(
            ["hyprctl", "monitors", "-j"],
            capture_output=True, text=True, timeout=1,
        )
        monitors = json.loads(result.stdout)
        if not monitors:
            return None
        mon = None
        if monitor_id is not None:
            mon = next((m for m in monitors if m.get("id") == monitor_id), None)
        if mon is None:
            mon = monitors[0]
        mx = mon.get("x", 0)
        my = mon.get("y", 0)
        mw = mon.get("width", 0)
        mh = mon.get("height", 0)
        # reserved: [left, top, right, bottom]
        res = mon.get("reserved", [0, 0, 0, 0])
        left, top, right, bottom = res[0], res[1], res[2], res[3]
        return (
            mx + left,
            my + top,
            mw - left - right,
            mh - top - bottom,
        )
    except Exception:
        return None


__all__ = [
    "get_window",
    "get_monitor_available_rect",
    "resize",
    "resize_and_move",
]
