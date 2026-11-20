"""A category fetch must always clear the info panel's pending flag.

While `_categories_pending` is set the panel skips the flat tag list.
The flag clears only via `categories_updated`, so a fetch that failed or
came back empty used to leave the tag area blank for good (rule34 on a
cold cache, GitHub issue pxlwh/booru-viewer#3).
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from booru_viewer.core.api.base import Post
from booru_viewer.gui.main_window import BooruApp


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    t = threading.Thread(target=lp.run_forever, daemon=True)
    t.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    t.join()
    lp.close()


def _app(loop, fetcher):
    emitted = threading.Event()
    app = SimpleNamespace(
        _make_client=lambda: SimpleNamespace(category_fetcher=fetcher),
        _info_panel=SimpleNamespace(_categories_pending=False),
        _signals=SimpleNamespace(categories_updated=SimpleNamespace(
            emit=lambda post: emitted.set())),
        _async_loop=loop,
    )
    return app, emitted


def _post(**kw):
    return Post(id=1, file_url="u", preview_url=None, tags="a b",
                score=0, rating=None, source=None, **kw)


class _Fetcher:
    def __init__(self, fail):
        self.fail = fail
        self.calls = 0

    async def ensure_categories(self, post):
        self.calls += 1
        if self.fail:
            raise RuntimeError("scrape failed")


@pytest.mark.parametrize("fail", [False, True])
def test_emits_when_fetch_finds_nothing_or_fails(loop, fail):
    app, emitted = _app(loop, _Fetcher(fail))
    BooruApp._ensure_post_categories_async(app, _post())
    assert app._info_panel._categories_pending is True
    assert emitted.wait(2)


def test_inline_categories_skip_the_fetch(loop):
    fetcher = _Fetcher(False)
    app, emitted = _app(loop, fetcher)
    post = _post(tag_categories={"General": ["a", "b"]})
    BooruApp._ensure_post_categories_async(app, post)
    assert app._info_panel._categories_pending is False
    assert fetcher.calls == 0


def test_partial_categories_still_fetch(loop):
    fetcher = _Fetcher(False)
    app, emitted = _app(loop, fetcher)
    post = _post(tag_categories={"General": ["a"]})
    BooruApp._ensure_post_categories_async(app, post)
    assert emitted.wait(2)
    assert fetcher.calls == 1
