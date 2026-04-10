"""Shared fixtures for the booru-viewer test suite.

All fixtures here are pure-Python — no Qt, no mpv, no network. Filesystem
writes go through `tmp_path` (or fixtures that wrap it). Module-level globals
that the production code mutates (the concurrency loop, the httpx singletons)
get reset around each test that touches them.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def tmp_db(tmp_path):
    """Fresh `Database` instance writing to a temp file. Auto-closes."""
    from booru_viewer.core.db import Database
    db = Database(tmp_path / "test.db")
    yield db
    db.close()


@pytest.fixture
def tmp_library(tmp_path):
    """Point `saved_dir()` at `tmp_path/saved` for the duration of the test.

    Uses `core.config.set_library_dir` (the official override hook) so the
    redirect goes through the same code path the GUI uses for the
    user-configurable library location. Tear-down restores the previous
    value so tests can run in any order without bleed.
    """
    from booru_viewer.core import config
    saved = tmp_path / "saved"
    saved.mkdir()
    original = config._library_dir_override
    config.set_library_dir(saved)
    yield saved
    config.set_library_dir(original)


@pytest.fixture
def reset_app_loop():
    """Reset `concurrency._app_loop` between tests.

    The module global is set once at app startup in production; tests need
    to start from a clean slate to assert the unset-state behavior.
    """
    from booru_viewer.core import concurrency
    original = concurrency._app_loop
    concurrency._app_loop = None
    yield
    concurrency._app_loop = original


@pytest.fixture
def reset_shared_clients():
    """Reset both shared httpx singletons (cache module + BooruClient class).

    Both are class/module-level globals; tests that exercise the lazy-init
    + lock pattern need them cleared so the test sees a fresh first-call
    race instead of a leftover instance from a previous test.
    """
    from booru_viewer.core.api.base import BooruClient
    from booru_viewer.core import cache
    original_booru = BooruClient._shared_client
    original_cache = cache._shared_client
    BooruClient._shared_client = None
    cache._shared_client = None
    yield
    BooruClient._shared_client = original_booru
    cache._shared_client = original_cache
