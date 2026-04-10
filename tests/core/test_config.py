"""Tests for `booru_viewer.core.config` — path traversal guard on
`saved_folder_dir` and the shallow walk in `find_library_files`.

Locks in:
- `saved_folder_dir` resolve-and-relative_to check (`54ccc40` defense in
  depth alongside `_validate_folder_name`)
- `find_library_files` matching exactly the root + 1-level subdirectory
  layout that the library uses, with the right MEDIA_EXTENSIONS filter
"""

from __future__ import annotations

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
