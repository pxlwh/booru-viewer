"""The save flow's 'do we already have this post on disk' check.

The bug this locks down: keyed on post_id alone, saving gelbooru 12345
after danbooru 12345 matched the filename, matched the id, and concluded
'already saved'. The second image was never written, and the UI showed a
saved-dot for a save that did not happen.

`_same_post_on_disk` refuses to answer for paths outside `saved_dir()`
(see its docstring), so every case here goes through the `tmp_library`
fixture rather than a bare relative `Path(...)`.
"""

from __future__ import annotations

import pytest

from booru_viewer.core.db import Database
from booru_viewer.core.library_save import _same_post_on_disk


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def test_same_site_and_post_is_the_same_post(db, tmp_library):
    db.save_library_meta(5, 12345, filename="12345.jpg")
    assert _same_post_on_disk(db, tmp_library / "12345.jpg", 12345, 5) is True


def test_same_post_id_from_another_site_is_a_different_post(db, tmp_library):
    """The regression. Previously True, silently dropping the save."""
    db.save_library_meta(5, 12345, filename="12345.jpg")
    assert _same_post_on_disk(db, tmp_library / "12345.jpg", 12345, 8) is False


def test_different_post_id_is_a_different_post(db, tmp_library):
    db.save_library_meta(5, 12345, filename="12345.jpg")
    assert _same_post_on_disk(db, tmp_library / "12345.jpg", 999, 5) is False


def test_unknown_filename_falls_through_to_legacy_check(db, tmp_library):
    """No row with that filename: digit-stem fallback for pre-0.2.3 rows."""
    assert _same_post_on_disk(db, tmp_library / "777.jpg", 777, 5) is False


def test_legacy_digit_stem_matches_when_one_candidate(db, tmp_library):
    """A pre-0.2.3 row has an empty filename; the stem is the only clue."""
    db.save_library_meta(5, 777, filename="")
    assert _same_post_on_disk(db, tmp_library / "777.jpg", 777, 5) is True


def test_legacy_digit_stem_rejects_a_different_site(db, tmp_library):
    db.save_library_meta(5, 777, filename="")
    assert _same_post_on_disk(db, tmp_library / "777.jpg", 777, 8) is False


def test_legacy_digit_stem_ambiguous_is_a_different_post(db, tmp_library):
    """Two sites both have a legacy row for 777. The safe answer is
    'different', so the collision resolver writes a new file rather than
    dropping one."""
    db.save_library_meta(5, 777, filename="")
    db.save_library_meta(8, 777, filename="")
    assert _same_post_on_disk(db, tmp_library / "777.jpg", 777, 5) is False
