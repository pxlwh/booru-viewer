"""Tests for the shared network-safety helpers (SSRF guard + secret redaction)."""

from __future__ import annotations

import socket
from unittest.mock import patch

import httpx
import pytest

from booru_viewer.core.api._safety import (
    SECRET_KEYS,
    check_public_host,
    redact_params,
    redact_url,
    validate_public_request,
)


# ======================================================================
# SSRF guard — finding #1
# ======================================================================


def test_public_v4_literal_passes():
    check_public_host("8.8.8.8")
    check_public_host("1.1.1.1")


def test_loopback_v4_rejected():
    with pytest.raises(httpx.RequestError):
        check_public_host("127.0.0.1")
    with pytest.raises(httpx.RequestError):
        check_public_host("127.0.0.53")


def test_cloud_metadata_ip_rejected():
    """169.254.169.254 — AWS/GCE/Azure metadata service."""
    with pytest.raises(httpx.RequestError):
        check_public_host("169.254.169.254")


def test_rfc1918_rejected():
    with pytest.raises(httpx.RequestError):
        check_public_host("10.0.0.1")
    with pytest.raises(httpx.RequestError):
        check_public_host("172.16.5.4")
    with pytest.raises(httpx.RequestError):
        check_public_host("192.168.1.1")


def test_cgnat_rejected():
    with pytest.raises(httpx.RequestError):
        check_public_host("100.64.0.1")


def test_multicast_v4_rejected():
    with pytest.raises(httpx.RequestError):
        check_public_host("224.0.0.1")


def test_ipv6_loopback_rejected():
    with pytest.raises(httpx.RequestError):
        check_public_host("::1")


def test_ipv6_unique_local_rejected():
    with pytest.raises(httpx.RequestError):
        check_public_host("fc00::1")
    with pytest.raises(httpx.RequestError):
        check_public_host("fd12:3456:789a::1")


def test_ipv6_link_local_rejected():
    with pytest.raises(httpx.RequestError):
        check_public_host("fe80::1")


def test_ipv6_multicast_rejected():
    with pytest.raises(httpx.RequestError):
        check_public_host("ff02::1")


def test_public_v6_passes():
    # Google DNS
    check_public_host("2001:4860:4860::8888")


def test_hostname_dns_failure_raises():
    def _gaierror(*a, **kw):
        raise socket.gaierror(-2, "Name or service not known")
    with patch("socket.getaddrinfo", _gaierror):
        with pytest.raises(httpx.RequestError):
            check_public_host("nonexistent.test.invalid")


def test_hostname_resolving_to_loopback_rejected():
    def _fake(*a, **kw):
        return [(socket.AF_INET, 0, 0, "", ("127.0.0.1", 0))]
    with patch("socket.getaddrinfo", _fake):
        with pytest.raises(httpx.RequestError, match="blocked request target"):
            check_public_host("mean.example")


def test_hostname_resolving_to_metadata_rejected():
    def _fake(*a, **kw):
        return [(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))]
    with patch("socket.getaddrinfo", _fake):
        with pytest.raises(httpx.RequestError):
            check_public_host("stolen.example")


def test_hostname_resolving_to_public_passes():
    def _fake(*a, **kw):
        return [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 0))]
    with patch("socket.getaddrinfo", _fake):
        check_public_host("dns.google")


def test_hostname_with_mixed_results_rejected_on_any_private():
    """If any resolved address is private, reject — conservative."""
    def _fake(*a, **kw):
        return [
            (socket.AF_INET, 0, 0, "", ("8.8.8.8", 0)),
            (socket.AF_INET, 0, 0, "", ("127.0.0.1", 0)),
        ]
    with patch("socket.getaddrinfo", _fake):
        with pytest.raises(httpx.RequestError):
            check_public_host("dualhomed.example")


def test_empty_host_passes():
    """Edge case: httpx can call us with a relative URL mid-redirect."""
    check_public_host("")


@pytest.mark.asyncio
async def test_validate_public_request_hook_rejects_metadata():
    request = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/")
    with pytest.raises(httpx.RequestError):
        await validate_public_request(request)


@pytest.mark.asyncio
async def test_validate_public_request_hook_allows_public():
    def _fake(*a, **kw):
        return [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 0))]
    with patch("socket.getaddrinfo", _fake):
        request = httpx.Request("GET", "https://example.test/")
        await validate_public_request(request)  # must not raise


# ======================================================================
# Credential redaction — finding #3
# ======================================================================


def test_secret_keys_covers_all_booru_client_params():
    """Every secret query param used by any booru client must be in SECRET_KEYS."""
    # Danbooru: login + api_key
    # e621: login + api_key
    # Gelbooru: api_key + user_id
    # Moebooru: login + password_hash
    for key in ("login", "api_key", "user_id", "password_hash"):
        assert key in SECRET_KEYS


def test_redact_url_replaces_secrets():
    redacted = redact_url("https://x.test/posts.json?login=alice&api_key=supersecret&tags=cats")
    assert "alice" not in redacted
    assert "supersecret" not in redacted
    assert "tags=cats" in redacted
    assert "login=%2A%2A%2A" in redacted
    assert "api_key=%2A%2A%2A" in redacted


def test_redact_url_leaves_non_secret_params_alone():
    redacted = redact_url("https://x.test/posts.json?tags=cats&limit=50")
    assert redacted == "https://x.test/posts.json?tags=cats&limit=50"


def test_redact_url_no_query_passthrough():
    assert redact_url("https://x.test/") == "https://x.test/"
    assert redact_url("https://x.test/posts.json") == "https://x.test/posts.json"


def test_redact_url_password_hash_and_user_id():
    redacted = redact_url(
        "https://x.test/post.json?login=a&password_hash=b&user_id=42&tags=cats"
    )
    assert "password_hash=%2A%2A%2A" in redacted
    assert "user_id=%2A%2A%2A" in redacted
    assert "tags=cats" in redacted


def test_redact_url_preserves_fragment_and_path():
    redacted = redact_url("https://x.test/a/b/c?api_key=secret#frag")
    assert redacted.startswith("https://x.test/a/b/c?")
    assert redacted.endswith("#frag")


def test_redact_params_replaces_secrets():
    out = redact_params({"api_key": "s", "tags": "cats", "login": "alice"})
    assert out["api_key"] == "***"
    assert out["login"] == "***"
    assert out["tags"] == "cats"


def test_redact_params_empty():
    assert redact_params({}) == {}


def test_redact_params_no_secrets():
    out = redact_params({"tags": "cats", "limit": 50})
    assert out == {"tags": "cats", "limit": 50}
