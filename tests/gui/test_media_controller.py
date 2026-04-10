"""Tests for media_controller -- prefetch order computation.

Pure Python. No Qt, no mpv, no httpx.
"""

from __future__ import annotations

import pytest

from booru_viewer.gui.media_controller import compute_prefetch_order


# ======================================================================
# Nearby mode
# ======================================================================


def test_nearby_center_returns_4_cardinals():
    """Center of a grid returns right, left, below, above."""
    order = compute_prefetch_order(index=12, total=25, columns=5, mode="Nearby")
    assert len(order) == 4
    assert 13 in order  # right
    assert 11 in order  # left
    assert 17 in order  # below (12 + 5)
    assert 7 in order   # above (12 - 5)


def test_nearby_top_left_corner_returns_2():
    """Index 0 in a grid: only right and below are in bounds."""
    order = compute_prefetch_order(index=0, total=25, columns=5, mode="Nearby")
    assert len(order) == 2
    assert 1 in order   # right
    assert 5 in order   # below


def test_nearby_bottom_right_corner_returns_2():
    """Last index in a 5x5 grid: only left and above."""
    order = compute_prefetch_order(index=24, total=25, columns=5, mode="Nearby")
    assert len(order) == 2
    assert 23 in order  # left
    assert 19 in order  # above


def test_nearby_single_post_returns_empty():
    order = compute_prefetch_order(index=0, total=1, columns=5, mode="Nearby")
    assert order == []


# ======================================================================
# Aggressive mode
# ======================================================================


def test_aggressive_returns_more_than_nearby():
    nearby = compute_prefetch_order(index=12, total=25, columns=5, mode="Nearby")
    aggressive = compute_prefetch_order(index=12, total=25, columns=5, mode="Aggressive")
    assert len(aggressive) > len(nearby)


def test_aggressive_no_duplicates():
    order = compute_prefetch_order(index=12, total=100, columns=5, mode="Aggressive")
    assert len(order) == len(set(order))


def test_aggressive_excludes_self():
    order = compute_prefetch_order(index=12, total=100, columns=5, mode="Aggressive")
    assert 12 not in order


def test_aggressive_all_in_bounds():
    order = compute_prefetch_order(index=0, total=50, columns=5, mode="Aggressive")
    for idx in order:
        assert 0 <= idx < 50


def test_aggressive_respects_cap():
    """Aggressive is capped by max_radius=3, so even with a huge grid
    the returned count doesn't blow up unboundedly."""
    order = compute_prefetch_order(index=500, total=10000, columns=10, mode="Aggressive")
    # columns * max_radius * 2 + columns = 10*3*2+10 = 70
    assert len(order) <= 70
