"""Tests for `booru_viewer.core.api.base` — the lazy `_shared_client`
singleton on `BooruClient`.

Locks in the lock-and-recheck pattern at `base.py:90-108`. Without it,
two threads racing on first `.client` access would both see
`_shared_client is None`, both build an `httpx.AsyncClient`, and one of
them would leak (overwritten without aclose).
"""

from __future__ import annotations

import threading
from unittest.mock import patch, MagicMock

import pytest

from booru_viewer.core.api.base import BooruClient


class _StubClient(BooruClient):
    """Concrete subclass so we can instantiate `BooruClient` for the test
    — the base class has abstract `search` / `get_post` methods."""
    api_type = "stub"

    async def search(self, tags="", page=1, limit=40):
        return []

    async def get_post(self, post_id):
        return None


def test_shared_client_singleton_under_concurrency(reset_shared_clients):
    """N threads racing on first `.client` access must result in exactly
    one `httpx.AsyncClient` constructor call. The threading.Lock guards
    the check-and-set so the second-and-later callers re-read the now-set
    `_shared_client` after acquiring the lock instead of building their
    own."""
    constructor_calls = 0
    constructor_lock = threading.Lock()

    def _fake_async_client(*args, **kwargs):
        nonlocal constructor_calls
        with constructor_lock:
            constructor_calls += 1
        m = MagicMock()
        m.is_closed = False
        return m

    # Barrier so all threads hit the property at the same moment
    n_threads = 10
    barrier = threading.Barrier(n_threads)
    results = []
    results_lock = threading.Lock()

    client_instance = _StubClient("http://example.test")

    def _worker():
        barrier.wait()
        c = client_instance.client
        with results_lock:
            results.append(c)

    with patch("booru_viewer.core.api.base.httpx.AsyncClient",
               side_effect=_fake_async_client):
        threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert constructor_calls == 1, (
        f"Expected exactly one httpx.AsyncClient construction, "
        f"got {constructor_calls}"
    )
    # All threads got back the same shared instance
    assert len(results) == n_threads
    assert all(r is results[0] for r in results)
