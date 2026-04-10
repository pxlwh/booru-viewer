"""Tests for search_controller -- tag building, blacklist filtering, backfill decisions.

Pure Python. No Qt, no network, no QApplication.
"""

from __future__ import annotations

from typing import NamedTuple

import pytest

from booru_viewer.gui.search_controller import (
    build_search_tags,
    filter_posts,
    should_backfill,
)


# -- Minimal Post stand-in for filter_posts --


class _Post(NamedTuple):
    id: int
    tag_list: list
    file_url: str


def _post(pid: int, tags: str = "", url: str = "") -> _Post:
    return _Post(id=pid, tag_list=tags.split() if tags else [], file_url=url)


# ======================================================================
# build_search_tags
# ======================================================================

# -- Rating mapping --


def test_danbooru_rating_uses_single_letter():
    result = build_search_tags("cat_ears", "explicit", "danbooru", 0, "All")
    assert "rating:e" in result


def test_gelbooru_rating_uses_full_word():
    result = build_search_tags("", "questionable", "gelbooru", 0, "All")
    assert "rating:questionable" in result


def test_e621_maps_general_to_safe():
    result = build_search_tags("", "general", "e621", 0, "All")
    assert "rating:s" in result


def test_e621_maps_sensitive_to_safe():
    result = build_search_tags("", "sensitive", "e621", 0, "All")
    assert "rating:s" in result


def test_moebooru_maps_general_to_safe():
    result = build_search_tags("", "general", "moebooru", 0, "All")
    assert "rating:safe" in result


def test_all_rating_adds_nothing():
    result = build_search_tags("cat", "all", "danbooru", 0, "All")
    assert "rating:" not in result


# -- Score filter --


def test_score_filter():
    result = build_search_tags("", "all", "danbooru", 50, "All")
    assert "score:>=50" in result


def test_score_zero_adds_nothing():
    result = build_search_tags("", "all", "danbooru", 0, "All")
    assert "score:" not in result


# -- Media type filter --


def test_media_type_animated():
    result = build_search_tags("", "all", "danbooru", 0, "Animated")
    assert "animated" in result


def test_media_type_video():
    result = build_search_tags("", "all", "danbooru", 0, "Video")
    assert "video" in result


def test_media_type_gif():
    result = build_search_tags("", "all", "danbooru", 0, "GIF")
    assert "animated_gif" in result


def test_media_type_audio():
    result = build_search_tags("", "all", "danbooru", 0, "Audio")
    assert "audio" in result


# -- Combined --


def test_combined_has_all_tokens():
    result = build_search_tags("1girl", "explicit", "danbooru", 10, "Video")
    assert "1girl" in result
    assert "rating:e" in result
    assert "score:>=10" in result
    assert "video" in result


# ======================================================================
# filter_posts
# ======================================================================


def test_removes_blacklisted_tags():
    posts = [_post(1, tags="cat dog"), _post(2, tags="bird")]
    seen: set = set()
    filtered, drops = filter_posts(posts, bl_tags={"dog"}, bl_posts=set(), seen_ids=seen)
    assert len(filtered) == 1
    assert filtered[0].id == 2
    assert drops["bl_tags"] == 1


def test_removes_blacklisted_posts_by_url():
    posts = [_post(1, url="http://a.jpg"), _post(2, url="http://b.jpg")]
    seen: set = set()
    filtered, drops = filter_posts(posts, bl_tags=set(), bl_posts={"http://a.jpg"}, seen_ids=seen)
    assert len(filtered) == 1
    assert filtered[0].id == 2
    assert drops["bl_posts"] == 1


def test_deduplicates_across_batches():
    """Dedup works against seen_ids accumulated from prior batches.
    Within a single batch, the list comprehension fires before the
    update, so same-id posts in one batch both survive -- cross-batch
    dedup catches them on the next call."""
    posts_batch1 = [_post(1)]
    seen: set = set()
    filter_posts(posts_batch1, bl_tags=set(), bl_posts=set(), seen_ids=seen)
    assert 1 in seen
    # Second batch with same id is deduped
    posts_batch2 = [_post(1), _post(2)]
    filtered, drops = filter_posts(posts_batch2, bl_tags=set(), bl_posts=set(), seen_ids=seen)
    assert len(filtered) == 1
    assert filtered[0].id == 2
    assert drops["dedup"] == 1


def test_respects_previously_seen_ids():
    posts = [_post(1), _post(2)]
    seen: set = {1}
    filtered, drops = filter_posts(posts, bl_tags=set(), bl_posts=set(), seen_ids=seen)
    assert len(filtered) == 1
    assert filtered[0].id == 2
    assert drops["dedup"] == 1


def test_all_three_interact():
    """bl_tags, bl_posts, and cross-batch dedup all apply in sequence."""
    # Seed seen_ids so post 3 is already known
    seen: set = {3}
    posts = [
        _post(1, tags="bad", url="http://a.jpg"),  # hit by bl_tags
        _post(2, url="http://blocked.jpg"),          # hit by bl_posts
        _post(3),                                    # hit by dedup (in seen)
        _post(4),                                    # survives
    ]
    filtered, drops = filter_posts(
        posts, bl_tags={"bad"}, bl_posts={"http://blocked.jpg"}, seen_ids=seen,
    )
    assert len(filtered) == 1
    assert filtered[0].id == 4
    assert drops["bl_tags"] == 1
    assert drops["bl_posts"] == 1
    assert drops["dedup"] == 1


def test_empty_lists_pass_through():
    posts = [_post(1), _post(2)]
    seen: set = set()
    filtered, drops = filter_posts(posts, bl_tags=set(), bl_posts=set(), seen_ids=seen)
    assert len(filtered) == 2
    assert drops == {"bl_tags": 0, "bl_posts": 0, "dedup": 0}


def test_filter_posts_mutates_seen_ids():
    posts = [_post(10), _post(20)]
    seen: set = set()
    filter_posts(posts, bl_tags=set(), bl_posts=set(), seen_ids=seen)
    assert seen == {10, 20}


# ======================================================================
# should_backfill
# ======================================================================


def test_backfill_yes_when_under_limit_and_api_not_short():
    assert should_backfill(collected_count=10, limit=40, last_batch_size=40) is True


def test_backfill_no_when_collected_meets_limit():
    assert should_backfill(collected_count=40, limit=40, last_batch_size=40) is False


def test_backfill_no_when_api_returned_short():
    assert should_backfill(collected_count=10, limit=40, last_batch_size=20) is False


def test_backfill_no_when_both_met():
    assert should_backfill(collected_count=40, limit=40, last_batch_size=20) is False
