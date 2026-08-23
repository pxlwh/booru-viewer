"""Round-robin merge of per-site search batches.

The interesting case is unequal batches: a site that runs out must drop
out of the rotation while the others keep cycling, otherwise the grid
ends at the shortest site's length.
"""

from __future__ import annotations

from dataclasses import dataclass

from booru_viewer.gui.search_controller import interleave


@dataclass
class P:
    id: int


def ids(posts):
    return [p.id for p in posts]


def test_equal_batches_strictly_alternate():
    a = [P(1), P(2), P(3)]
    b = [P(10), P(20), P(30)]
    c = [P(100), P(200), P(300)]
    assert ids(interleave([a, b, c], 9)) == [1, 10, 100, 2, 20, 200, 3, 30, 300]


def test_exhausted_batch_drops_out_and_others_continue():
    a = [P(1), P(2), P(3), P(4)]
    b = [P(10)]
    assert ids(interleave([a, b], 10)) == [1, 10, 2, 3, 4]


def test_single_batch_is_a_passthrough():
    a = [P(1), P(2), P(3)]
    assert ids(interleave([a], 10)) == [1, 2, 3]


def test_empty_batches_are_skipped_entirely():
    a = [P(1), P(2)]
    assert ids(interleave([[], a, []], 10)) == [1, 2]


def test_limit_truncates():
    a = [P(1), P(2), P(3)]
    b = [P(10), P(20), P(30)]
    assert ids(interleave([a, b], 4)) == [1, 10, 2, 20]


def test_no_batches_returns_empty():
    assert interleave([], 10) == []


def test_all_batches_empty_returns_empty():
    assert interleave([[], [], []], 10) == []


def test_batch_order_is_preserved():
    """Site order follows the selector, so callers can rely on it."""
    a = [P(1)]
    b = [P(2)]
    assert ids(interleave([b, a], 10)) == [2, 1]


def test_limit_of_zero_returns_empty():
    assert interleave([[P(1)], [P(2)]], 0) == []
