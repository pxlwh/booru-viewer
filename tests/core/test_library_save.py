"""Tests for save_post_file.

Pins the contract that category_fetcher is a *required* keyword arg
(no silent default) so a forgotten plumb can't result in a save that
drops category tokens from the filename template.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from booru_viewer.core.library_save import save_post_file


@dataclass
class FakePost:
    id: int = 12345
    tags: str = "1girl greatartist"
    tag_categories: dict = field(default_factory=dict)
    score: int = 0
    rating: str = ""
    source: str = ""
    file_url: str = ""


class PopulatingFetcher:
    """ensure_categories fills in the artist category from scratch,
    emulating the HTML-scrape/batch-API happy path."""

    def __init__(self, categories: dict[str, list[str]]):
        self._categories = categories
        self.calls = 0

    async def ensure_categories(self, post) -> None:
        self.calls += 1
        post.tag_categories = dict(self._categories)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_category_fetcher_is_keyword_only_and_required():
    """Signature check: category_fetcher must be explicit at every
    call site — no ``= None`` default that callers can forget."""
    sig = inspect.signature(save_post_file)
    param = sig.parameters["category_fetcher"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
        "category_fetcher should be keyword-only"
    )
    assert param.default is inspect.Parameter.empty, (
        "category_fetcher must not have a default — forcing every caller "
        "to pass it (even as None) is the whole point of this contract"
    )


def test_template_category_populated_via_fetcher(tmp_path, tmp_db):
    """Post with empty tag_categories + a template using %artist% +
    a working fetcher → saved filename includes the fetched artist
    instead of falling back to the bare id."""
    src = tmp_path / "src.jpg"
    src.write_bytes(b"fake-image-bytes")
    dest_dir = tmp_path / "dest"

    tmp_db.set_setting("library_filename_template", "%artist%_%id%")

    post = FakePost(id=12345, tag_categories={})
    fetcher = PopulatingFetcher({"Artist": ["greatartist"]})

    result = _run(save_post_file(
        src, post, dest_dir, tmp_db,
        category_fetcher=fetcher,
    ))

    assert fetcher.calls == 1, "fetcher should be invoked exactly once"
    assert result.name == "greatartist_12345.jpg", (
        f"expected templated filename, got {result.name!r}"
    )
    assert result.exists()


def test_none_fetcher_accepted_when_categories_prepopulated(tmp_path, tmp_db):
    """Pass-None contract: sites like Danbooru/e621 return ``None``
    from ``_get_category_fetcher`` because Post already arrives with
    tag_categories populated. ``save_post_file`` must accept None
    explicitly — the change is about forcing callers to think, not
    about forbidding None."""
    src = tmp_path / "src.jpg"
    src.write_bytes(b"x")
    dest_dir = tmp_path / "dest"

    tmp_db.set_setting("library_filename_template", "%artist%_%id%")

    post = FakePost(id=999, tag_categories={"Artist": ["inlineartist"]})

    result = _run(save_post_file(
        src, post, dest_dir, tmp_db,
        category_fetcher=None,
    ))

    assert result.name == "inlineartist_999.jpg"
    assert result.exists()


def test_fetcher_not_called_when_template_has_no_category_tokens(tmp_path, tmp_db):
    """Purely-id template → fetcher ``ensure_categories`` never
    invoked, even when categories are empty (the fetch is expensive
    and would be wasted)."""
    src = tmp_path / "src.jpg"
    src.write_bytes(b"x")
    dest_dir = tmp_path / "dest"

    tmp_db.set_setting("library_filename_template", "%id%")

    post = FakePost(id=42, tag_categories={})
    fetcher = PopulatingFetcher({"Artist": ["unused"]})

    _run(save_post_file(
        src, post, dest_dir, tmp_db,
        category_fetcher=fetcher,
    ))

    assert fetcher.calls == 0
