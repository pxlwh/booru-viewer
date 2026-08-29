"""Stale preview loads must not take over the preview.

Clicking post A (slow video) then post B used to end with A on screen: A's
download finished after B was selected and `image_done` was applied
unconditionally. The controller now issues a ticket per activation and a
result is applied only if its ticket is still the newest one.

Pure Python. No Qt, no mpv, no httpx.
"""

from __future__ import annotations

from booru_viewer.gui.media_controller import LoadGeneration


def test_fresh_ticket_is_current():
    g = LoadGeneration()
    a = g.issue()
    assert g.is_current(a)


def test_older_ticket_is_stale_once_a_newer_one_is_issued():
    g = LoadGeneration()
    a = g.issue()
    b = g.issue()
    assert not g.is_current(a), "post A finished loading after B was clicked"
    assert g.is_current(b)


def test_tickets_are_strictly_increasing_and_unique():
    g = LoadGeneration()
    seen = [g.issue() for _ in range(5)]
    assert seen == sorted(seen)
    assert len(set(seen)) == 5


def test_reissuing_never_revalidates_an_old_ticket():
    """A -> B -> A again must not resurrect A's first in-flight load; the
    second click on A gets its own ticket."""
    g = LoadGeneration()
    a1 = g.issue()
    g.issue()          # B
    a2 = g.issue()     # A again
    assert not g.is_current(a1)
    assert g.is_current(a2)
