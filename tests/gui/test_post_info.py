"""The preview info line.

Extracted from an inline f-string so the site label can be tested without
standing up a QApplication.
"""

from __future__ import annotations

from dataclasses import dataclass

from booru_viewer.gui.post_info import format_post_info


@dataclass
class P:
    id: int = 42
    score: int = 7
    rating: str | None = "s"
    created_at: str = ""
    site_id: int | None = None


def test_without_a_site_matches_the_old_format():
    assert format_post_info(P(), "jpg") == "#42  score:7  [s]  JPG"


def test_with_a_site_name():
    assert format_post_info(P(), "jpg", "Gelbooru") == "#42  score:7  [s]  JPG  Gelbooru"


def test_created_at_is_appended_when_present():
    out = format_post_info(P(created_at="2026-01-01"), "jpg", "Gelbooru")
    assert out.endswith("2026-01-01")
    assert "Gelbooru" in out


def test_empty_site_name_is_identical_to_omitting_it():
    assert format_post_info(P(), "jpg", "") == format_post_info(P(), "jpg")


def test_suffix_is_uppercased():
    assert "PNG" in format_post_info(P(), "png")
