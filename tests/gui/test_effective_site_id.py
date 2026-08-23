"""One helper for 'which site does this post belong to'.

The combo's current entry is only a fallback: under multi-search most
grid posts did NOT come from the top-selected site.
"""

from __future__ import annotations

from dataclasses import dataclass

from booru_viewer.gui.site_selection import effective_site_id


@dataclass
class P:
    id: int
    site_id: int | None = None


class Bare:
    """No site_id attribute at all (pre-stamping construction sites)."""
    id = 1


def test_stamped_post_wins_over_fallback():
    assert effective_site_id(P(1, site_id=3), 9) == 3


def test_unstamped_post_falls_back():
    assert effective_site_id(P(1), 9) == 9


def test_missing_attribute_falls_back():
    assert effective_site_id(Bare(), 9) == 9


def test_zero_sentinel_falls_back():
    """0 is the library's 'unknown site' sentinel, not a real site."""
    assert effective_site_id(P(1, site_id=0), 9) == 9


def test_no_fallback_stays_none():
    assert effective_site_id(P(1), None) is None
