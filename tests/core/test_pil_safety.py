"""Tests for the project-wide PIL decompression-bomb cap (audit #8).

The cap lives in `booru_viewer/core/__init__.py` so any import of
any `booru_viewer.core.*` submodule installs it first — independent
of whether `core.cache` is on the import path. Both checks are run
in a fresh subprocess so the assertion isn't masked by some other
test's previous import.
"""

from __future__ import annotations

import subprocess
import sys

EXPECTED = 256 * 1024 * 1024


def _run(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_core_package_import_installs_cap():
    """Importing the core package alone must set MAX_IMAGE_PIXELS."""
    out = _run(
        "import booru_viewer.core; "
        "from PIL import Image; "
        "print(Image.MAX_IMAGE_PIXELS)"
    )
    assert int(out) == EXPECTED


def test_core_images_import_installs_cap():
    """The original audit concern: importing core.images without first
    importing core.cache must still set the cap."""
    out = _run(
        "from booru_viewer.core import images; "
        "from PIL import Image; "
        "print(Image.MAX_IMAGE_PIXELS)"
    )
    assert int(out) == EXPECTED


def test_core_cache_import_still_installs_cap():
    """Regression: the old code path (importing cache first) must keep
    working after the move."""
    out = _run(
        "from booru_viewer.core import cache; "
        "from PIL import Image; "
        "print(Image.MAX_IMAGE_PIXELS)"
    )
    assert int(out) == EXPECTED
