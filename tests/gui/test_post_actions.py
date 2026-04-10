"""Tests for post_actions -- bookmark-done message parsing, library membership.

Pure Python. No Qt, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booru_viewer.gui.post_actions import is_batch_message, is_in_library


# ======================================================================
# is_batch_message
# ======================================================================


def test_batch_message_saved_fraction():
    assert is_batch_message("Saved 3/10 to Unfiled") is True


def test_batch_message_bookmarked_fraction():
    assert is_batch_message("Bookmarked 1/5") is True


def test_not_batch_single_bookmark():
    assert is_batch_message("Bookmarked #12345 to Unfiled") is False


def test_not_batch_download_path():
    assert is_batch_message("Downloaded to /home/user/pics") is False


def test_error_message_with_status_codes_is_false_positive():
    """The heuristic matches '9/5' in '429/503' -- it's a known
    false positive of the simple check. The function is only ever
    called on status bar messages the app itself generates, and
    real error messages don't hit this pattern in practice."""
    assert is_batch_message("Error: HTTP 429/503") is True


def test_not_batch_empty():
    assert is_batch_message("") is False


# ======================================================================
# is_in_library
# ======================================================================


def test_is_in_library_direct_child(tmp_path):
    root = tmp_path / "saved"
    root.mkdir()
    child = root / "12345.jpg"
    child.touch()
    assert is_in_library(child, root) is True


def test_is_in_library_subfolder(tmp_path):
    root = tmp_path / "saved"
    sub = root / "cats"
    sub.mkdir(parents=True)
    child = sub / "67890.png"
    child.touch()
    assert is_in_library(child, root) is True


def test_is_in_library_outside(tmp_path):
    root = tmp_path / "saved"
    root.mkdir()
    outside = tmp_path / "other" / "pic.jpg"
    outside.parent.mkdir()
    outside.touch()
    assert is_in_library(outside, root) is False


def test_is_in_library_traversal_resolved(tmp_path):
    """is_relative_to operates on the literal path segments, so an
    unresolved '..' still looks relative. With resolved paths (which
    is how the app calls it), the escape is correctly rejected."""
    root = tmp_path / "saved"
    root.mkdir()
    sneaky = (root / ".." / "other.jpg").resolve()
    assert is_in_library(sneaky, root) is False
