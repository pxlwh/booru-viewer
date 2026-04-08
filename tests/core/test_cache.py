"""Tests for `booru_viewer.core.cache` — Referer hostname matching, ugoira
zip-bomb defenses, download size caps, and validity-check fallback.

Locks in:
- `_referer_for` proper hostname suffix matching (`54ccc40` security fix)
  guarding against `imgblahgelbooru.attacker.com` mapping to gelbooru.com
- `_convert_ugoira_to_gif` cap enforcement (frame count + uncompressed size)
  before any decompression — defense against ugoira zip bombs
- `_do_download` MAX_DOWNLOAD_BYTES enforcement, both the Content-Length
  pre-check and the running-total chunk-loop guard
- `_is_valid_media` returning True on OSError so a transient EBUSY/lock
  doesn't kick off a delete + re-download loop
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from booru_viewer.core import cache
from booru_viewer.core.cache import (
    MAX_DOWNLOAD_BYTES,
    _convert_ugoira_to_gif,
    _do_download,
    _is_valid_media,
    _referer_for,
)


# -- _referer_for hostname suffix matching --

def test_referer_for_exact_and_suffix_match():
    """Real booru hostnames map to the canonical Referer for their CDN.

    Exact match and subdomain-suffix match both rewrite the Referer host
    to the canonical apex (gelbooru → `gelbooru.com`, donmai →
    `danbooru.donmai.us`). The actual request netloc is dropped — the
    point is to look like a navigation from the canonical site.
    """
    # gelbooru exact host
    assert _referer_for(urlparse("https://gelbooru.com/index.php")) \
        == "https://gelbooru.com/"
    # gelbooru subdomain rewrites to the canonical apex
    assert _referer_for(urlparse("https://img3.gelbooru.com/images/abc.jpg")) \
        == "https://gelbooru.com/"

    # donmai exact host
    assert _referer_for(urlparse("https://donmai.us/posts/123")) \
        == "https://danbooru.donmai.us/"
    # donmai subdomain rewrites to the canonical danbooru host
    assert _referer_for(urlparse("https://safebooru.donmai.us/posts/123")) \
        == "https://danbooru.donmai.us/"


def test_referer_for_rejects_substring_attacker():
    """An attacker host that contains `gelbooru.com` or `donmai.us` as a
    SUBSTRING (not a hostname suffix) must NOT pick up the booru Referer.

    Without proper suffix matching, `imgblahgelbooru.attacker.com` would
    leak the gelbooru Referer to the attacker — that's the `54ccc40`
    security fix.
    """
    # Attacker host that ends with attacker-controlled TLD
    parsed = urlparse("https://imgblahgelbooru.attacker.com/x.jpg")
    referer = _referer_for(parsed)
    assert "gelbooru.com" not in referer
    assert "imgblahgelbooru.attacker.com" in referer

    parsed = urlparse("https://donmai.us.attacker.com/x.jpg")
    referer = _referer_for(parsed)
    assert "danbooru.donmai.us" not in referer
    assert "donmai.us.attacker.com" in referer

    # Completely unrelated host preserved as-is
    parsed = urlparse("https://example.test/x.jpg")
    assert _referer_for(parsed) == "https://example.test/"


# -- Ugoira zip-bomb defenses --

def _build_ugoira_zip(path: Path, n_frames: int, frame_bytes: bytes = b"x") -> Path:
    """Build a synthetic ugoira-shaped zip with `n_frames` numbered .jpg
    entries. Content is whatever the caller passes; defaults to 1 byte.

    The cap-enforcement tests don't need decodable JPEGs — the cap fires
    before any decode happens. The filenames just need .jpg suffixes so
    `_convert_ugoira_to_gif` recognizes them as frames.
    """
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(n_frames):
            zf.writestr(f"{i:04d}.jpg", frame_bytes)
    return path


def test_ugoira_frame_count_cap_rejects_bomb(tmp_path, monkeypatch):
    """A zip with more than `UGOIRA_MAX_FRAMES` frames must be refused
    BEFORE any decompression. We monkeypatch the cap down so the test
    builds a tiny zip instead of a 5001-entry one — the cap check is
    cap > N, not cap == 5000."""
    monkeypatch.setattr(cache, "UGOIRA_MAX_FRAMES", 2)
    zip_path = _build_ugoira_zip(tmp_path / "bomb.zip", n_frames=3)
    gif_path = zip_path.with_suffix(".gif")

    result = _convert_ugoira_to_gif(zip_path)

    # Function returned the original zip (refusal path)
    assert result == zip_path
    # No .gif was written
    assert not gif_path.exists()


def test_ugoira_uncompressed_size_cap_rejects_bomb(tmp_path, monkeypatch):
    """A zip whose `ZipInfo.file_size` headers sum past
    `UGOIRA_MAX_UNCOMPRESSED_BYTES` must be refused before decompression.
    Same monkeypatch trick to keep the test data small."""
    monkeypatch.setattr(cache, "UGOIRA_MAX_UNCOMPRESSED_BYTES", 50)
    # Three 100-byte frames → 300 total > 50 cap
    zip_path = _build_ugoira_zip(
        tmp_path / "bomb.zip", n_frames=3, frame_bytes=b"x" * 100
    )
    gif_path = zip_path.with_suffix(".gif")

    result = _convert_ugoira_to_gif(zip_path)

    assert result == zip_path
    assert not gif_path.exists()


# -- _do_download MAX_DOWNLOAD_BYTES caps --


class _FakeHeaders:
    def __init__(self, mapping):
        self._m = mapping
    def get(self, key, default=None):
        return self._m.get(key.lower(), default)


class _FakeResponse:
    def __init__(self, headers, chunks):
        self.headers = _FakeHeaders({k.lower(): v for k, v in headers.items()})
        self._chunks = chunks
    def raise_for_status(self):
        pass
    async def aiter_bytes(self, _size):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamCtx:
    def __init__(self, response):
        self._resp = response
    async def __aenter__(self):
        return self._resp
    async def __aexit__(self, *_args):
        return False


class _FakeClient:
    def __init__(self, response):
        self._resp = response
    def stream(self, _method, _url, headers=None):
        return _FakeStreamCtx(self._resp)


def test_download_cap_content_length_pre_check(tmp_path):
    """When the server advertises a Content-Length larger than
    MAX_DOWNLOAD_BYTES, `_do_download` must raise BEFORE iterating any
    bytes. This is the cheap pre-check that protects against the trivial
    OOM/disk-fill attack — we don't even start streaming."""
    too_big = MAX_DOWNLOAD_BYTES + 1
    response = _FakeResponse(
        headers={"content-type": "image/jpeg", "content-length": str(too_big)},
        chunks=[b"never read"],
    )
    client = _FakeClient(response)
    local = tmp_path / "out.jpg"

    with pytest.raises(ValueError, match="Download too large"):
        asyncio.run(_do_download(client, "http://example.test/x.jpg", {}, local, None))

    # No file should have been written
    assert not local.exists()


def test_download_cap_running_total_aborts(tmp_path, monkeypatch):
    """Servers can lie about Content-Length. The chunk loop must enforce
    the running-total cap independently and abort mid-stream as soon as
    cumulative bytes exceed `MAX_DOWNLOAD_BYTES`. We monkeypatch the cap
    down to 1024 to keep the test fast."""
    monkeypatch.setattr(cache, "MAX_DOWNLOAD_BYTES", 1024)
    # Advertise 0 (unknown) so the small-payload branch runs and the
    # running-total guard inside the chunk loop is what fires.
    response = _FakeResponse(
        headers={"content-type": "image/jpeg", "content-length": "0"},
        chunks=[b"x" * 600, b"x" * 600],  # 1200 total > 1024 cap
    )
    client = _FakeClient(response)
    local = tmp_path / "out.jpg"

    with pytest.raises(ValueError, match="exceeded cap mid-stream"):
        asyncio.run(_do_download(client, "http://example.test/x.jpg", {}, local, None))

    # The buffered-write path only writes after the loop finishes, so the
    # mid-stream abort means no file lands on disk.
    assert not local.exists()


# -- _is_valid_media OSError fallback --

def test_is_valid_media_returns_true_on_oserror(tmp_path):
    """If the file can't be opened (transient EBUSY, lock, permissions),
    `_is_valid_media` must return True so the caller doesn't delete the
    cached file. The previous behavior of returning False kicked off a
    delete + re-download loop on every access while the underlying
    OS issue persisted."""
    nonexistent = tmp_path / "definitely-not-here.jpg"
    assert _is_valid_media(nonexistent) is True
