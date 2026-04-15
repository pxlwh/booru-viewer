"""Shared httpx.AsyncClient constructor.

Three call sites build near-identical clients: the cache module's
download pool, ``BooruClient``'s shared API pool, and
``detect.detect_site_type``'s reach into that same pool. Centralising
the construction in one place means a future change (new SSRF hook,
new connection limit, different default UA) doesn't have to be made
three times and kept in sync.

The module does NOT manage the singletons themselves — each call site
keeps its own ``_shared_client`` and its own lock, so the cache
pool's long-lived large transfers don't compete with short JSON
requests from the API layer. ``make_client`` is a pure constructor.
"""

from __future__ import annotations

from typing import Callable, Iterable

import httpx

from .config import USER_AGENT
from .api._safety import validate_public_request


# Connection pool limits are identical across all three call sites.
# Keeping the default here centralises any future tuning.
_DEFAULT_LIMITS = httpx.Limits(max_connections=10, max_keepalive_connections=5)


def make_client(
    *,
    timeout: float = 20.0,
    accept: str | None = None,
    extra_request_hooks: Iterable[Callable] | None = None,
) -> httpx.AsyncClient:
    """Return a fresh ``httpx.AsyncClient`` with the project's defaults.

    Defaults applied unconditionally:
        - ``User-Agent`` header from ``core.config.USER_AGENT``
        - ``follow_redirects=True``
        - ``validate_public_request`` SSRF hook (always first on the
          request-hook chain; extras run after it)
        - Connection limits: 10 max, 5 keepalive

    Parameters:
        timeout: per-request timeout in seconds. Cache downloads pass
            60s for large videos; the API pool uses 20s.
        accept: optional ``Accept`` header value. The cache pool sets
            ``image/*,video/*,*/*``; the API pool leaves it unset so
            httpx's ``*/*`` default takes effect.
        extra_request_hooks: optional extra callables to run after
            ``validate_public_request``. The API clients pass their
            connection-logging hook here; detect passes the same.

    Call sites are responsible for their own singleton caching —
    ``make_client`` always returns a fresh instance.
    """
    headers: dict[str, str] = {"User-Agent": USER_AGENT}
    if accept is not None:
        headers["Accept"] = accept

    hooks: list[Callable] = [validate_public_request]
    if extra_request_hooks:
        hooks.extend(extra_request_hooks)

    return httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=timeout,
        event_hooks={"request": hooks},
        limits=_DEFAULT_LIMITS,
    )
