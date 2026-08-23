"""Dedup must key on (site_id, post_id), not post_id alone.

Post ids are unique per booru. Keying on the id alone means danbooru's
post 12345 silently suppresses gelbooru's post 12345 — a bug that only
appears when two sites are selected.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from booru_viewer.gui.search_controller import filter_posts


@dataclass
class P:
    id: int
    site_id: int | None = None
    file_url: str = "https://x/i.jpg"
    tags: str = "cat"

    @property
    def tag_list(self):
        return self.tags.split()


def test_same_id_from_different_sites_both_survive():
    seen: set = set()
    kept, drops = filter_posts([P(12345, site_id=1), P(12345, site_id=2)], set(), set(), seen)
    assert len(kept) == 2
    assert drops["dedup"] == 0


def test_same_id_from_the_same_site_is_deduped():
    seen: set = set()
    kept, drops = filter_posts([P(12345, site_id=1), P(12345, site_id=1)], set(), set(), seen)
    assert len(kept) == 1
    assert drops["dedup"] == 1


def test_seen_is_populated_with_tuples():
    seen: set = set()
    filter_posts([P(7, site_id=3)], set(), set(), seen)
    assert seen == {(3, 7)}


def test_unstamped_posts_still_dedup_against_each_other():
    """site_id None is a valid key — single-site mode is unchanged."""
    seen: set = set()
    kept, _ = filter_posts([P(7), P(7)], set(), set(), seen)
    assert len(kept) == 1
    assert seen == {(None, 7)}


def test_blacklist_still_applies():
    seen: set = set()
    kept, drops = filter_posts([P(1, site_id=1, tags="dog")], {"dog"}, set(), seen)
    assert kept == []
    assert drops["bl_tags"] == 1
