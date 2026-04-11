"""Tests for the pure info-panel source HTML builder.

Pure Python. No Qt, no network. Validates audit finding #6 — that the
helper escapes booru-controlled `post.source` before it's interpolated
into a QTextBrowser RichText document.
"""

from __future__ import annotations

from booru_viewer.gui._source_html import build_source_html


def test_none_returns_literal_none():
    assert build_source_html(None) == "none"
    assert build_source_html("") == "none"


def test_plain_https_url_renders_escaped_anchor():
    out = build_source_html("https://example.test/post/1")
    assert out.startswith('<a href="https://example.test/post/1"')
    assert ">https://example.test/post/1</a>" in out


def test_long_url_display_text_truncated_but_href_full():
    long_url = "https://example.test/" + "a" * 200
    out = build_source_html(long_url)
    # href contains the full URL
    assert long_url in out.replace("&amp;", "&")
    # Display text is truncated to 57 chars + "..."
    assert "..." in out


def test_double_quote_in_url_escaped():
    """A `"` in the source must not break out of the href attribute."""
    hostile = 'https://attacker.test/"><img src=x>'
    out = build_source_html(hostile)
    # Raw <img> must NOT appear — html.escape converts < to &lt;
    assert "<img" not in out
    # The display text must also have the raw markup escaped.
    assert "&gt;" in out or "&quot;" in out


def test_html_tags_in_url_escaped():
    hostile = 'https://attacker.test/<script>alert(1)</script>'
    out = build_source_html(hostile)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_non_url_source_rendered_as_escaped_plain_text():
    """A source string that isn't an http(s) URL is rendered as plain
    text — no <a> tag, but still HTML-escaped."""
    out = build_source_html("not a url <b>at all</b>")
    assert "<a" not in out
    assert "<b>" not in out
    assert "&lt;b&gt;" in out


def test_javascript_url_does_not_become_anchor():
    """Sources that don't start with http(s) — including `javascript:` —
    must NOT be wrapped in an <a> tag where they'd become a clickable
    link target."""
    out = build_source_html("javascript:alert(1)")
    assert "<a " not in out
    assert "alert(1)" in out  # text content preserved (escaped)


def test_data_url_does_not_become_anchor():
    out = build_source_html("data:text/html,<script>x</script>")
    assert "<a " not in out
    assert "<script>" not in out


def test_ampersand_in_url_escaped():
    out = build_source_html("https://example.test/?a=1&b=2")
    # `&` must be `&amp;` inside the href attribute
    assert "&amp;" in out
    # Raw `&b=` is NOT acceptable as an attribute value
    assert 'href="https://example.test/?a=1&amp;b=2"' in out


def test_pixiv_real_world_source_unchanged_visually():
    """Realistic input — a normal pixiv link — should pass through with
    no surprising changes."""
    out = build_source_html("https://www.pixiv.net/artworks/12345")
    assert 'href="https://www.pixiv.net/artworks/12345"' in out
    assert "https://www.pixiv.net/artworks/12345</a>" in out
