"""Tests for `booru_viewer.core.concurrency` — the persistent-loop handle.

Locks in:
- `get_app_loop` raises a clear RuntimeError if `set_app_loop` was never
  called (the production code uses this to bail loudly when async work
  is scheduled before the loop thread starts)
- `run_on_app_loop` round-trips a coroutine result from a worker-thread
  loop back to the calling thread via `concurrent.futures.Future`
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from booru_viewer.core import concurrency
from booru_viewer.core.concurrency import (
    get_app_loop,
    run_on_app_loop,
    set_app_loop,
)


def test_get_app_loop_raises_before_set(reset_app_loop):
    """Calling `get_app_loop` before `set_app_loop` is a configuration
    error — the production code expects a clear RuntimeError so callers
    bail loudly instead of silently scheduling work onto a None loop."""
    with pytest.raises(RuntimeError, match="not initialized"):
        get_app_loop()


def test_run_on_app_loop_round_trips_result(reset_app_loop):
    """Spin up a real asyncio loop in a worker thread, register it via
    `set_app_loop`, then from the test (main) thread schedule a coroutine
    via `run_on_app_loop` and assert the result comes back through the
    `concurrent.futures.Future` interface."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run_loop():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()
    ready.wait(timeout=2)

    try:
        set_app_loop(loop)

        async def _produce():
            return 42

        fut = run_on_app_loop(_produce())
        assert fut.result(timeout=2) == 42
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
        loop.close()
