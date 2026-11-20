"""Popout auto-fit off: no per-post resize. GitHub issue pxlwh/booru-viewer#4.

Every resize path (per-post fit, F11 exit, untile refit) funnels through
`_fit_to_content`, so the gate there covers them all.
"""

from __future__ import annotations

import pytest

# window.py imports Qt; CI installs no PySide6, so this runs locally only.
pytest.importorskip("PySide6")

from booru_viewer.gui.popout.window import FullscreenPreview  # noqa: E402


class _Probe:
    """Stands in for the window; any geometry call past the gate fails."""

    def __init__(self, auto_fit: bool) -> None:
        self._auto_fit = auto_fit

    def isFullScreen(self):
        raise AssertionError("got past the auto-fit gate")


def test_auto_fit_off_returns_before_touching_geometry():
    FullscreenPreview._fit_to_content(_Probe(auto_fit=False), 800, 600)


def test_auto_fit_on_proceeds_past_the_gate():
    with pytest.raises(AssertionError, match="past the auto-fit gate"):
        FullscreenPreview._fit_to_content(_Probe(auto_fit=True), 800, 600)
