"""Tests for the response-shape rules that decide API type and auth failure.

Regression cover for the 2026_08_19 rule34.xxx break: the site started
serving a 403 HTML block page at /posts.json and a 200 JSON *string*
("Missing authentication...") at the Gelbooru endpoint. The first made
detection answer "danbooru", the second made searches look empty.

No network, no Qt — synthetic responses only.
"""

from __future__ import annotations

import pytest

from booru_viewer.core.api.base import BooruAuthError
from booru_viewer.core.api.detect import _looks_json
from booru_viewer.core.api.gelbooru import GelbooruClient


class FakeResponse:
    def __init__(self, content_type: str):
        self.headers = {"content-type": content_type}


# ---------------------------------------------------------------------------
# _looks_json — separates "API says unauthorized" from "WAF says no"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content_type, expected", [
    ("application/json; charset=utf-8", True),
    ("application/json", True),
    ("text/json", True),
    ("APPLICATION/JSON", True),
    ("text/html; charset=UTF-8", False),
    ("text/plain", False),
    ("", False),
])
def test_looks_json(content_type, expected):
    assert _looks_json(FakeResponse(content_type)) is expected


def test_looks_json_missing_header():
    class NoHeaders:
        headers: dict = {}
    assert _looks_json(NoHeaders()) is False


# ---------------------------------------------------------------------------
# GelbooruClient._unwrap — a bare string is a refusal, not an empty page
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return GelbooruClient("https://rule34.xxx")


def test_unwrap_string_raises_auth_error(client):
    with pytest.raises(BooruAuthError):
        client._unwrap("Missing authentication. Go to api.rule34.xxx for more information")


def test_unwrap_auth_error_message_does_not_echo_server_body(client):
    """The body goes to the log, never into UI-facing text."""
    with pytest.raises(BooruAuthError) as exc:
        client._unwrap("Missing authentication. Go to api.rule34.xxx for more information")
    assert "rule34" not in str(exc.value)
    assert "API key" in str(exc.value)


def test_unwrap_dict_returns_posts(client):
    assert client._unwrap({"post": [{"id": 1}]}) == [{"id": 1}]


def test_unwrap_dict_without_post_key_is_empty(client):
    assert client._unwrap({"@attributes": {"count": 0}}) == []


def test_unwrap_list_passes_through(client):
    assert client._unwrap([{"id": 7}]) == [{"id": 7}]


@pytest.mark.parametrize("data", [None, 42, True])
def test_unwrap_other_scalars_are_empty_not_errors(client, data):
    assert client._unwrap(data) == []
