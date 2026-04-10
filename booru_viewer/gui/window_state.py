"""Main-window geometry and splitter persistence."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer

if TYPE_CHECKING:
    from .main_window import BooruApp

log = logging.getLogger("booru")


# -- Pure functions (tested in tests/gui/test_window_state.py) --


def parse_geometry(s: str) -> tuple[int, int, int, int] | None:
    """Parse ``"x,y,w,h"`` into a 4-tuple of ints, or *None* on bad input."""
    if not s:
        return None
    parts = s.split(",")
    if len(parts) != 4:
        return None
    try:
        vals = tuple(int(p) for p in parts)
    except ValueError:
        return None
    return vals  # type: ignore[return-value]


def format_geometry(x: int, y: int, w: int, h: int) -> str:
    """Format geometry ints into the ``"x,y,w,h"`` DB string."""
    return f"{x},{y},{w},{h}"


def parse_splitter_sizes(s: str, expected: int) -> list[int] | None:
    """Parse ``"a,b,..."`` into a list of *expected* non-negative ints.

    Returns *None* when the string is empty, has the wrong count, contains
    non-numeric values, any value is negative, or every value is zero (an
    all-zero splitter is a transient state that should not be persisted).
    """
    if not s:
        return None
    parts = s.split(",")
    if len(parts) != expected:
        return None
    try:
        sizes = [int(p) for p in parts]
    except ValueError:
        return None
    if any(v < 0 for v in sizes):
        return None
    if all(v == 0 for v in sizes):
        return None
    return sizes


def build_hyprctl_restore_cmds(
    addr: str,
    x: int,
    y: int,
    w: int,
    h: int,
    want_floating: bool,
    cur_floating: bool,
) -> list[str]:
    """Build the ``hyprctl --batch`` command list to restore window state.

    When *want_floating* is True, ensures the window is floating then
    resizes/moves.  When False, primes Hyprland's per-window floating cache
    by briefly toggling to floating (wrapped in ``no_anim``), then ends on
    tiled so a later mid-session float-toggle picks up the saved dimensions.
    """
    cmds: list[str] = []
    if want_floating:
        if not cur_floating:
            cmds.append(f"dispatch togglefloating address:{addr}")
        cmds.append(f"dispatch resizewindowpixel exact {w} {h},address:{addr}")
        cmds.append(f"dispatch movewindowpixel exact {x} {y},address:{addr}")
    else:
        cmds.append(f"dispatch setprop address:{addr} no_anim 1")
        if not cur_floating:
            cmds.append(f"dispatch togglefloating address:{addr}")
        cmds.append(f"dispatch resizewindowpixel exact {w} {h},address:{addr}")
        cmds.append(f"dispatch movewindowpixel exact {x} {y},address:{addr}")
        cmds.append(f"dispatch togglefloating address:{addr}")
        cmds.append(f"dispatch setprop address:{addr} no_anim 0")
    return cmds


# -- Controller --


class WindowStateController:
    """Owns main-window geometry persistence and Hyprland IPC."""

    def __init__(self, app: BooruApp) -> None:
        self._app = app

    # -- Splitter persistence --

    def save_main_splitter_sizes(self) -> None:
        """Persist the main grid/preview splitter sizes (debounced).

        Refuses to save when either side is collapsed (size 0). The user can
        end up with a collapsed right panel transiently -- e.g. while the
        popout is open and the right panel is empty -- and persisting that
        state traps them next launch with no visible preview area until they
        manually drag the splitter back.
        """
        sizes = self._app._splitter.sizes()
        if len(sizes) >= 2 and all(s > 0 for s in sizes):
            self._app._db.set_setting(
                "main_splitter_sizes", ",".join(str(s) for s in sizes)
            )

    def save_right_splitter_sizes(self) -> None:
        """Persist the right splitter sizes (preview / dl_progress / info).

        Skipped while the popout is open -- the popout temporarily collapses
        the preview pane and gives the info panel the full right column,
        and we don't want that transient layout persisted as the user's
        preferred state.
        """
        if self._app._popout_ctrl.is_active:
            return
        sizes = self._app._right_splitter.sizes()
        if len(sizes) == 3 and sum(sizes) > 0:
            self._app._db.set_setting(
                "right_splitter_sizes", ",".join(str(s) for s in sizes)
            )

    # -- Hyprland IPC --

    def hyprctl_main_window(self) -> dict | None:
        """Look up this main window in hyprctl clients. None off Hyprland.

        Matches by Wayland app_id (Hyprland reports it as ``class``), which is
        set in run() via setDesktopFileName. Title would also work but it
        changes whenever the search bar updates the window title -- class is
        constant for the lifetime of the window.
        """
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return None
        try:
            result = subprocess.run(
                ["hyprctl", "clients", "-j"],
                capture_output=True, text=True, timeout=1,
            )
            for c in json.loads(result.stdout):
                cls = c.get("class") or c.get("initialClass")
                if cls == "booru-viewer":
                    # Skip the popout -- it shares our class but has a
                    # distinct title we set explicitly.
                    if (c.get("title") or "").endswith("Popout"):
                        continue
                    return c
        except Exception:
            pass
        return None

    # -- Window state save / restore --

    def save_main_window_state(self) -> None:
        """Persist the main window's last mode and (separately) the last
        known floating geometry.

        Two settings keys are used:
          - main_window_was_floating ("1" / "0"): the *last* mode the window
            was in (floating or tiled). Updated on every save.
          - main_window_floating_geometry ("x,y,w,h"): the position+size the
            window had the *last time it was actually floating*. Only updated
            when the current state is floating, so a tile->close->reopen->float
            sequence still has the user's old floating dimensions to use.

        This split is important because Hyprland's resizeEvent for a tiled
        window reports the tile slot size -- saving that into the floating
        slot would clobber the user's chosen floating dimensions every time
        they tiled the window.
        """
        try:
            win = self.hyprctl_main_window()
            if win is None:
                # Non-Hyprland fallback: just track Qt's frameGeometry as
                # floating. There's no real tiled concept off-Hyprland.
                g = self._app.frameGeometry()
                self._app._db.set_setting(
                    "main_window_floating_geometry",
                    format_geometry(g.x(), g.y(), g.width(), g.height()),
                )
                self._app._db.set_setting("main_window_was_floating", "1")
                return
            floating = bool(win.get("floating"))
            self._app._db.set_setting(
                "main_window_was_floating", "1" if floating else "0"
            )
            if floating and win.get("at") and win.get("size"):
                x, y = win["at"]
                w, h = win["size"]
                self._app._db.set_setting(
                    "main_window_floating_geometry", format_geometry(x, y, w, h)
                )
            # When tiled, intentionally do NOT touch floating_geometry --
            # preserve the last good floating dimensions.
        except Exception:
            pass

    def restore_main_window_state(self) -> None:
        """One-shot restore of saved floating geometry and last mode.

        Called from __init__ via QTimer.singleShot(0, ...) so it fires on the
        next event-loop iteration -- by which time the window has been shown
        and (on Hyprland) registered with the compositor.

        Entirely skipped when BOORU_VIEWER_NO_HYPR_RULES is set -- that flag
        means the user wants their own windowrules to handle the main
        window. Even seeding Qt's geometry could fight a ``windowrule = size``,
        so we leave the initial Qt geometry alone too.
        """
        from ..core.config import hypr_rules_enabled
        if not hypr_rules_enabled():
            return
        # Migration: clear obsolete keys from earlier schemas so they can't
        # interfere. main_window_maximized came from a buggy version that
        # used Qt's isMaximized() which lies for Hyprland tiled windows.
        # main_window_geometry was the combined-format key that's now split.
        for stale in ("main_window_maximized", "main_window_geometry"):
            if self._app._db.get_setting(stale):
                self._app._db.set_setting(stale, "")

        floating_geo = self._app._db.get_setting("main_window_floating_geometry")
        was_floating = self._app._db.get_setting_bool("main_window_was_floating")
        if not floating_geo:
            return
        geo = parse_geometry(floating_geo)
        if geo is None:
            return
        x, y, w, h = geo
        # Seed Qt with the floating geometry -- even if we're going to leave
        # the window tiled now, this becomes the xdg-toplevel preferred size,
        # which Hyprland uses when the user later toggles to floating. So
        # mid-session float-toggle picks up the saved dimensions even when
        # the window opened tiled.
        self._app.setGeometry(x, y, w, h)
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return
        # Slight delay so the window is registered before we try to find
        # its address. The popout uses the same pattern.
        QTimer.singleShot(
            50, lambda: self.hyprctl_apply_main_state(x, y, w, h, was_floating)
        )

    def hyprctl_apply_main_state(
        self, x: int, y: int, w: int, h: int, floating: bool
    ) -> None:
        """Apply saved floating mode + geometry to the main window via hyprctl.

        If floating==True, ensures the window is floating and resizes/moves it
        to the saved dimensions.

        If floating==False, the window is left tiled but we still "prime"
        Hyprland's per-window floating cache by briefly toggling to floating,
        applying the saved geometry, and toggling back. This is wrapped in
        a transient ``no_anim`` so the toggles are instant.

        Skipped entirely when BOORU_VIEWER_NO_HYPR_RULES is set.
        """
        from ..core.config import hypr_rules_enabled
        if not hypr_rules_enabled():
            return
        win = self.hyprctl_main_window()
        if not win:
            return
        addr = win.get("address")
        if not addr:
            return
        cur_floating = bool(win.get("floating"))
        cmds = build_hyprctl_restore_cmds(addr, x, y, w, h, floating, cur_floating)
        if not cmds:
            return
        try:
            subprocess.Popen(
                ["hyprctl", "--batch", " ; ".join(cmds)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass
