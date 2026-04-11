"""Network-safety helpers for httpx clients.

Keeps SSRF guards and secret redaction in one place so every httpx
client in the project can share a single implementation. All helpers
here are pure stdlib + httpx; no Qt, no project-side imports.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


# ---------------------------------------------------------------------------
# SSRF guard — finding #1
# ---------------------------------------------------------------------------

_BLOCKED_V4 = [
    ipaddress.ip_network("0.0.0.0/8"),      # this-network
    ipaddress.ip_network("10.0.0.0/8"),     # RFC1918
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),    # loopback
    ipaddress.ip_network("169.254.0.0/16"), # link-local (incl. 169.254.169.254 metadata)
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918
    ipaddress.ip_network("192.0.0.0/24"),   # IETF protocol assignments
    ipaddress.ip_network("192.168.0.0/16"), # RFC1918
    ipaddress.ip_network("198.18.0.0/15"),  # benchmark
    ipaddress.ip_network("224.0.0.0/4"),    # multicast
    ipaddress.ip_network("240.0.0.0/4"),    # reserved
]

_BLOCKED_V6 = [
    ipaddress.ip_network("::1/128"),     # loopback
    ipaddress.ip_network("::/128"),      # unspecified
    ipaddress.ip_network("::ffff:0:0/96"), # IPv4-mapped (covers v4 via v6)
    ipaddress.ip_network("64:ff9b::/96"),  # well-known NAT64
    ipaddress.ip_network("fc00::/7"),    # unique local
    ipaddress.ip_network("fe80::/10"),   # link-local
    ipaddress.ip_network("ff00::/8"),    # multicast
]


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    nets = _BLOCKED_V4 if isinstance(ip, ipaddress.IPv4Address) else _BLOCKED_V6
    return any(ip in net for net in nets)


def check_public_host(host: str) -> None:
    """Raise httpx.RequestError if ``host`` is (or resolves to) a non-public IP.

    Blocks loopback, RFC1918, link-local (including the 169.254.169.254
    cloud-metadata endpoint), unique-local v6, and similar. Used by both
    the initial request and every redirect hop — see
    ``validate_public_request`` for the async wrapper.
    """
    if not host:
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _is_blocked_ip(ip):
            raise httpx.RequestError(f"blocked address: {host}")
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise httpx.RequestError(f"DNS resolution failed for {host}: {e}")
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if addr in seen:
            continue
        seen.add(addr)
        try:
            resolved = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            continue
        if _is_blocked_ip(resolved):
            raise httpx.RequestError(
                f"blocked request target {host} -> {addr}"
            )


async def validate_public_request(request: httpx.Request) -> None:
    """httpx request event hook — rejects private/metadata targets.

    Fires on every hop including redirects. The initial request to a
    user-configured booru base_url is also validated; this intentionally
    blocks users from pointing the app at ``http://localhost/`` or an
    RFC1918 address (behavior change from v0.2.5).

    Limitation: TOCTOU / DNS rebinding. We resolve the host here, but
    the kernel will re-resolve when the TCP connection actually opens,
    and a rebinder that returns a public IP on first query and a
    private IP on the second can bypass this hook. The project's threat
    model is a *malicious booru returning a 3xx to a private address* —
    not an active rebinder controlling the DNS recursor — so this check
    is the intended defense line. If the threat model ever widens, the
    follow-up is a custom httpx transport that validates post-connect.
    """
    host = request.url.host
    if not host:
        return
    await asyncio.to_thread(check_public_host, host)


# ---------------------------------------------------------------------------
# Credential redaction — finding #3
# ---------------------------------------------------------------------------

# Case-sensitive; matches the literal param names every booru client
# uses today (verified via grep across danbooru/e621/gelbooru/moebooru).
SECRET_KEYS: frozenset[str] = frozenset({
    "login",
    "api_key",
    "user_id",
    "password_hash",
})


def redact_url(url: str) -> str:
    """Replace secret query params with ``***`` in a URL string.

    Preserves ordering and non-secret params. Empty-query URLs pass
    through unchanged.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    redacted = [(k, "***" if k in SECRET_KEYS else v) for k, v in pairs]
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(redacted),
        parts.fragment,
    ))


def redact_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``params`` with secret keys replaced by ``***``."""
    return {k: ("***" if k in SECRET_KEYS else v) for k, v in params.items()}
