"""Tests for `booru_viewer.core.config` — path traversal guard on
`saved_folder_dir` and the shallow walk in `find_library_files`.

Locks in:
- `saved_folder_dir` resolve-and-relative_to check (`54ccc40` defense in
  depth alongside `_validate_folder_name`)
- `find_library_files` matching exactly the root + 1-level subdirectory
  layout that the library uses, with the right MEDIA_EXTENSIONS filter
- `data_dir` chmods its directory to 0o700 on POSIX (audit #4)
"""

from __future__ import annotations

import os
import sys

import pytest

from booru_viewer.core import config
from booru_viewer.core.config import find_library_files, saved_folder_dir


# -- saved_folder_dir traversal guard --

def test_saved_folder_dir_rejects_dotdot(tmp_library):
    """`..` and any path that resolves outside `saved_dir()` must raise
    ValueError, not silently mkdir somewhere unexpected. We test literal
    `..` shapes only — symlink escapes are filesystem-dependent and
    flaky in tests."""
    with pytest.raises(ValueError, match="escapes saved directory"):
        saved_folder_dir("..")
    with pytest.raises(ValueError, match="escapes saved directory"):
        saved_folder_dir("../escape")
    with pytest.raises(ValueError, match="escapes saved directory"):
        saved_folder_dir("foo/../..")


# -- find_library_files shallow walk --

def test_find_library_files_walks_root_and_one_level(tmp_library):
    """Library has a flat shape: `saved/<post_id>.<ext>` at the root, or
    `saved/<folder>/<post_id>.<ext>` one level deep. The walk must:
    - find matches at both depths
    - filter by MEDIA_EXTENSIONS (skip .txt and other non-media)
    - filter by exact stem (skip unrelated post ids)
    """
    # Root-level match
    (tmp_library / "123.jpg").write_bytes(b"")
    # One-level subfolder match
    (tmp_library / "folder1").mkdir()
    (tmp_library / "folder1" / "123.png").write_bytes(b"")
    # Different post id — must be excluded
    (tmp_library / "folder2").mkdir()
    (tmp_library / "folder2" / "456.gif").write_bytes(b"")
    # Wrong extension — must be excluded even with the right stem
    (tmp_library / "123.txt").write_bytes(b"")

    matches = find_library_files(123)
    match_names = {p.name for p in matches}

    assert match_names == {"123.jpg", "123.png"}


# -- data_dir permissions (audit finding #4) --

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod check")
def test_data_dir_chmod_700(tmp_path, monkeypatch):
    """`data_dir()` chmods the platform data dir to 0o700 on POSIX so the
    SQLite DB and api_key columns inside aren't readable by other local
    users on shared machines or networked home dirs."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = config.data_dir()
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o700, f"expected 0o700, got {oct(mode)}"
    # Idempotent: a second call leaves the mode at 0o700.
    config.data_dir()
    mode2 = os.stat(path).st_mode & 0o777
    assert mode2 == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod check")
def test_data_dir_tightens_loose_existing_perms(tmp_path, monkeypatch):
    """If a previous version (or external tooling) left the dir at 0o755,
    the next data_dir() call must tighten it back to 0o700."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    pre = tmp_path / config.APPNAME
    pre.mkdir()
    os.chmod(pre, 0o755)
    config.data_dir()
    mode = os.stat(pre).st_mode & 0o777
    assert mode == 0o700
