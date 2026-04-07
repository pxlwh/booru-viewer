"""Process-wide handle to the app's persistent asyncio event loop.

The GUI runs Qt on the main thread and a single long-lived asyncio loop in
a daemon thread (`BooruApp._async_thread`). Every async piece of code in the
app — searches, downloads, autocomplete, site detection, bookmark thumb
loading — must run on that one loop. Without this guarantee, the shared
httpx clients (which httpx binds to whatever loop first instantiated them)
end up attached to a throwaway loop from a `threading.Thread + asyncio.run`
worker, then break the next time the persistent loop tries to use them
("attached to a different loop" / "Event loop is closed").

This module is the single source of truth for "the loop". `BooruApp.__init__`
calls `set_app_loop()` once after constructing it; everything else uses
`run_on_app_loop()` to schedule coroutines from any thread.

Why a module global instead of passing the loop everywhere: it avoids
threading a parameter through every dialog, view, and helper. There's only
one loop in the process, ever, so a global is the honest representation.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future
from typing import Any, Awaitable, Callable

log = logging.getLogger("booru")

_app_loop: asyncio.AbstractEventLoop | None = None


def set_app_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Register the persistent event loop. Called once at app startup."""
    global _app_loop
    _app_loop = loop


def get_app_loop() -> asyncio.AbstractEventLoop:
    """Return the persistent event loop. Raises if `set_app_loop` was never called."""
    if _app_loop is None:
        raise RuntimeError(
            "App event loop not initialized — call set_app_loop() before "
            "scheduling any async work."
        )
    return _app_loop


def run_on_app_loop(
    coro: Awaitable[Any],
    done_callback: Callable[[Future], None] | None = None,
) -> Future:
    """Schedule `coro` on the app's persistent event loop from any thread.

    Returns a `concurrent.futures.Future` (not asyncio.Future) — same shape as
    `asyncio.run_coroutine_threadsafe`. If `done_callback` is provided, it
    runs on the loop thread when the coroutine finishes; the callback is
    responsible for marshaling results back to the GUI thread (typically by
    emitting a Qt Signal connected with `Qt.ConnectionType.QueuedConnection`).
    """
    fut = asyncio.run_coroutine_threadsafe(coro, get_app_loop())
    if done_callback is not None:
        fut.add_done_callback(done_callback)
    return fut
