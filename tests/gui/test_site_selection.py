"""Pure logic behind the Multi site selector: persisting the ticked
set as CSV, resolving it against the live site list, and the combo's
summary text."""

from __future__ import annotations

from dataclasses import dataclass

from booru_viewer.gui.site_selection import (
    parse_multi_site_ids,
    serialize_site_ids,
    summarize_selection,
)


@dataclass
class S:
    id: int
    name: str


SITES = [S(1, "gelbooru"), S(3, "rule34"), S(6, "danbooru")]


def test_parse_returns_sites_in_selector_order():
    out = parse_multi_site_ids("6,1", SITES)
    assert [s.id for s in out] == [1, 6]


def test_parse_drops_stale_ids_silently():
    """A site removed while ticked just disappears from the selection."""
    out = parse_multi_site_ids("1,99", SITES)
    assert [s.id for s in out] == [1]


def test_parse_empty_csv_is_empty():
    assert parse_multi_site_ids("", SITES) == []


def test_parse_ignores_junk_tokens():
    out = parse_multi_site_ids("1, x,,3", SITES)
    assert [s.id for s in out] == [1, 3]


def test_serialize_round_trips_through_parse():
    csv = serialize_site_ids([3, 1])
    assert csv == "3,1"
    assert [s.id for s in parse_multi_site_ids(csv, SITES)] == [1, 3]


def test_summary_no_sites():
    assert summarize_selection([]) == "No sites"


def test_summary_one_site_is_just_the_name():
    assert summarize_selection(["gelbooru"]) == "gelbooru"


def test_summary_up_to_three_names_listed():
    assert summarize_selection(["a", "b", "c"]) == "a, b, c"


def test_summary_four_or_more_collapses_to_a_count():
    assert summarize_selection(["a", "b", "c", "d"]) == "4 sites"
