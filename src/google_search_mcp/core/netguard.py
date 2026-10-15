"""URL guard against SSRF / dangerous schemes.

Blocks the classes of URL that are never a legitimate target for the web-read
tools (``visit_page``, ``archive_webpage``, custom feed URLs, ``shorten_url``):

* non-``http(s)`` schemes — ``file://``, ``data:``, ``javascript:``, ``ftp:`` …
  (``file://`` in particular would be a local-file read primitive);
* link-local addresses (``169.254.0.0/16``, ``fe80::/10``) — i.e. the classic
  cloud-metadata endpoint ``169.254.169.254`` — which are *always* rejected
  because they are never a real page target;
* private / loopback / reserved / multicast ranges, **only when**
  ``NOAPI_MCP_ALLOW_PRIVATE_URLS=0``. The default is permissive (D5) so LAN /
  self-hosted setups keep working.

Resolution is done with ``socket.getaddrinfo`` and **every** returned address is
checked, which also defeats a hostname that resolves to a private address
(DNS-rebinding-style bypass).

Verified APIs: stdlib only (``ipaddress``, ``socket``, ``urllib.parse``).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from .. import config
from ..errors import UserInputError

#: Schemes the web tools may fetch.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Always-blocked addresses (cloud metadata), regardless of the permissive flag.
_ALWAYS_BLOCKED = frozenset({"169.254.169.254", "fd00:ec2::254"})


def _describe(ip: ipaddress._BaseAddress) -> str:
    if ip.is_link_local:
        return "link-local"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    return "disallowed"


def _check_ip(ip_str: str, *, allow_private: bool) -> str | None:
    """Return a rejection reason for *ip_str*, or ``None`` if allowed."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return "unparseable address"
    if str(ip) in _ALWAYS_BLOCKED:
        return "cloud metadata address"
    if ip.is_link_local:
        return "link-local address"
    if allow_private:
        return None
    if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return f"{_describe(ip)} address"
    return None


def check_url(url: str, *, allow_private: bool | None = None) -> str | None:
    """Return a human-readable rejection reason, or ``None`` if allowed."""
    if not url or not url.strip():
        return "empty URL"
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return "unparseable URL"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return f"scheme '{scheme or '(none)'}' is not allowed (use http or https)"

    host = parsed.hostname
    if not host:
        return "missing host"

    if allow_private is None:
        allow_private = config.ALLOW_PRIVATE_URLS

    # Literal IP? Check directly (no DNS).
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        reason = _check_ip(str(literal), allow_private=allow_private)
        return f"{host} is a {reason}" if reason else None

    # Hostname: resolve and check EVERY address (defeats rebinding to a
    # private range). Resolution failures are not a security verdict — let the
    # fetch layer report the network error.
    try:
        infos = socket.getaddrinfo(host, parsed.port or 0, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return None
    for info in infos:
        reason = _check_ip(info[4][0], allow_private=allow_private)
        if reason:
            return f"{host} resolves to a {reason}"
    return None


def assert_url_allowed(url: str, *, allow_private: bool | None = None) -> None:
    """Raise :class:`UserInputError` if *url* must not be fetched."""
    reason = check_url(url, allow_private=allow_private)
    if reason:
        raise UserInputError(f"Refusing to fetch {url!r}: {reason}.")


def is_url_allowed(url: str, *, allow_private: bool | None = None) -> bool:
    """Boolean form of :func:`check_url`."""
    return check_url(url, allow_private=allow_private) is None
