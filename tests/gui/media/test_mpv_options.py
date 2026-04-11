"""Tests for the pure mpv kwargs builder.

Pure Python. No Qt, no mpv, no network. The helper is importable
from the CI environment that installs only httpx + Pillow + pytest.
"""

from __future__ import annotations

from booru_viewer.gui.media._mpv_options import build_mpv_kwargs


def test_ytdl_disabled():
    """Finding #2 — mpv must not delegate URLs to yt-dlp."""
    kwargs = build_mpv_kwargs(is_windows=False)
    assert kwargs["ytdl"] == "no"


def test_load_scripts_disabled():
    """Finding #2 — no auto-loading of ~/.config/mpv/scripts."""
    kwargs = build_mpv_kwargs(is_windows=False)
    assert kwargs["load_scripts"] == "no"


def test_protocol_whitelist_restricts_to_file_and_http():
    """Finding #2 — lavf demuxer must only accept file + HTTP(S) + TLS/TCP."""
    kwargs = build_mpv_kwargs(is_windows=False)
    value = kwargs["demuxer_lavf_o"]
    assert isinstance(value, str)
    assert value.startswith("protocol_whitelist=")
    allowed = set(value.split("=", 1)[1].split(","))
    # `file` must be present — cached local clips and .part files use it.
    assert "file" in allowed
    # HTTP(S) + supporting protocols for network videos.
    assert "http" in allowed
    assert "https" in allowed
    assert "tls" in allowed
    assert "tcp" in allowed
    # Dangerous protocols must NOT appear.
    for banned in ("concat", "subfile", "data", "udp", "rtp", "crypto"):
        assert banned not in allowed


def test_input_conf_nulled_on_posix():
    """Finding #2 — on POSIX, skip loading ~/.config/mpv/input.conf."""
    kwargs = build_mpv_kwargs(is_windows=False)
    assert kwargs["input_conf"] == "/dev/null"


def test_input_conf_skipped_on_windows():
    """Finding #2 — input_conf gate is POSIX-only; Windows omits the key."""
    kwargs = build_mpv_kwargs(is_windows=True)
    assert "input_conf" not in kwargs


def test_existing_options_preserved():
    """Regression: pre-audit playback/audio tuning must remain."""
    kwargs = build_mpv_kwargs(is_windows=False)
    # Discord screen-share audio fix (see mpv_gl.py comment).
    assert kwargs["ao"] == "pulse,wasapi,"
    assert kwargs["audio_client_name"] == "booru-viewer"
    # Network tuning from the uncached-video fast path.
    assert kwargs["cache"] == "yes"
    assert kwargs["cache_pause"] == "no"
    assert kwargs["demuxer_max_bytes"] == "50MiB"
    assert kwargs["network_timeout"] == "10"
    # Existing input lockdown (primary — input_conf is defense-in-depth).
    assert kwargs["input_default_bindings"] is False
    assert kwargs["input_vo_keyboard"] is False
