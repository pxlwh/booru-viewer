"""The multi-site fan-out: per-site fetch loop, error partitioning,
per-site tag strings, and the status line.

No pytest-asyncio in this repo — coroutines are driven with asyncio.run,
matching tests/core/test_concurrency.py. `backfill_delay=0` keeps the
backfill tests instant (the real loop sleeps 0.3s between pages).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from booru_viewer.gui.search_controller import (
    build_tags_for_sites,
    fetch_site_page,
    format_search_status,
    partition_results,
)


@dataclass
class P:
    id: int
    site_id: int | None = None
    file_url: str = "https://x/i.jpg"
    tags: str = "cat"

    @property
    def tag_list(self):
        return self.tags.split()


class FakeClient:
    """Scripted per-page responses. A page mapped to an Exception raises."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls = []

    async def search(self, tags, page, limit):
        self.calls.append((tags, page, limit))
        batch = self.pages.get(page, [])
        if isinstance(batch, Exception):
            raise batch
        return batch


# -- fetch_site_page --

def test_full_first_page_needs_no_backfill():
    posts = [P(i, site_id=1) for i in range(4)]
    c = FakeClient({1: posts})
    collected, last_page, exhausted, drops = asyncio.run(
        fetch_site_page(c, "cat", 1, 4, set(), set(), set(), backfill_delay=0)
    )
    assert [p.id for p in collected] == [0, 1, 2, 3]
    assert last_page == 1
    assert exhausted is False
    assert len(c.calls) == 1


def test_short_batch_marks_exhausted_without_backfill():
    c = FakeClient({1: [P(1, site_id=1)]})
    collected, last_page, exhausted, _ = asyncio.run(
        fetch_site_page(c, "cat", 1, 4, set(), set(), set(), backfill_delay=0)
    )
    assert len(collected) == 1
    assert exhausted is True
    assert len(c.calls) == 1


def test_backfill_advances_until_limit_met():
    """Page 1 is full but half already seen -> fetch page 2 to top up."""
    seen = {(1, 0), (1, 1)}
    page1 = [P(i, site_id=1) for i in range(4)]
    page2 = [P(i, site_id=1) for i in range(10, 14)]
    c = FakeClient({1: page1, 2: page2})
    collected, last_page, exhausted, drops = asyncio.run(
        fetch_site_page(c, "cat", 1, 4, set(), set(), seen, backfill_delay=0)
    )
    assert len(collected) == 6  # 2 survivors + 4 fresh
    assert last_page == 2
    assert drops["dedup"] == 2
    assert len(c.calls) == 2


def test_backfill_caps_at_nine_extra_pages():
    """Every page full of already-seen posts: 1 + 9 fetches, then stop."""
    posts = [P(i, site_id=1) for i in range(2)]
    seen = {(1, 0), (1, 1)}
    c = FakeClient({n: posts for n in range(1, 50)})
    collected, last_page, exhausted, _ = asyncio.run(
        fetch_site_page(c, "cat", 1, 2, set(), set(), seen, backfill_delay=0)
    )
    assert collected == []
    assert len(c.calls) == 10
    assert last_page == 10


def test_shared_seen_set_keeps_same_ids_from_two_sites():
    """The regression multi-search exists to fix: two sites, same post id."""
    seen: set = set()
    a = FakeClient({1: [P(12345, site_id=1)]})
    b = FakeClient({1: [P(12345, site_id=2)]})

    async def both():
        return await asyncio.gather(
            fetch_site_page(a, "cat", 1, 4, set(), set(), seen, backfill_delay=0),
            fetch_site_page(b, "cat", 1, 4, set(), set(), seen, backfill_delay=0),
        )

    ra, rb = asyncio.run(both())
    assert len(ra[0]) == 1 and len(rb[0]) == 1
    assert seen == {(1, 12345), (2, 12345)}


# -- partition_results --

def test_partition_splits_successes_and_errors():
    boom = RuntimeError("credentials rejected")
    oks, errors = partition_results(
        ["gelbooru", "rule34", "e621"],
        [([P(1)], 1, False, {}), boom, ([P(2)], 1, True, {})],
    )
    assert len(oks) == 2
    assert errors == [("rule34", "credentials rejected")]


def test_partition_all_failures():
    oks, errors = partition_results(
        ["a", "b"], [RuntimeError("x"), RuntimeError("y")]
    )
    assert oks == []
    assert errors == [("a", "x"), ("b", "y")]


def test_partition_preserves_order():
    oks, _ = partition_results(["a", "b"], [(["first"],), (["second"],)])
    assert [o[0] for o in oks] == [["first"], ["second"]]


# -- build_tags_for_sites --

def test_rating_syntax_is_per_site_within_one_search():
    """danbooru gets rating:e while gelbooru gets rating:explicit —
    in the same call. One syntax sent to all backends silently returns
    wrong or empty results from the others."""
    out = build_tags_for_sites("cat", "explicit", ["danbooru", "gelbooru"], 0, "All")
    assert out == ["cat rating:e", "cat rating:explicit"]


def test_shared_filters_apply_to_every_site():
    out = build_tags_for_sites("cat", "all", ["danbooru", "gelbooru"], 50, "Video")
    assert out == ["cat score:>=50 video", "cat score:>=50 video"]


# -- format_search_status --

def test_status_single_site_unchanged():
    assert format_search_status(40, 1, [], False) == "40 results"
    assert format_search_status(12, 1, [], True) == "12 results (end)"


def test_status_names_the_failed_site():
    msg = format_search_status(30, 4, [("rule34", "denied")], False)
    assert msg == "30 results — showing 3 of 4 sites — rule34: denied"


def test_status_joins_multiple_failures():
    msg = format_search_status(9, 3, [("a", "x"), ("b", "y")], True)
    assert msg == "9 results (end) — showing 1 of 3 sites — a: x; b: y"
