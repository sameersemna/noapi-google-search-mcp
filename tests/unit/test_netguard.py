"""Phase 7 tests for the SSRF URL guard (netguard).

Default is permissive for private/LAN addresses (D5), but dangerous *schemes*
and the cloud-metadata endpoint are ALWAYS rejected, and strict mode
(``ALLOW_PRIVATE_URLS=0``) rejects loopback/private/reserved.
"""

from __future__ import annotations

import pytest

from google_search_mcp.core import netguard
from google_search_mcp.errors import UserInputError


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file://localhost/etc/shadow",
        "data:text/plain,hello",
        "javascript:alert(1)",
        "ftp://example.com/x",
        "gopher://example.com/",
    ],
)
def test_dangerous_schemes_always_rejected(url):
    assert netguard.is_url_allowed(url, allow_private=True) is False


def test_cloud_metadata_always_rejected_even_when_permissive():
    assert netguard.is_url_allowed("http://169.254.169.254/latest/meta-data/", allow_private=True) is False
    assert netguard.is_url_allowed("http://169.254.169.254/", allow_private=False) is False


def test_link_local_always_rejected():
    assert netguard.is_url_allowed("http://169.254.1.1/", allow_private=True) is False


def test_permissive_default_allows_lan_and_loopback():
    # D5: LAN-friendly by default.
    assert netguard.is_url_allowed("http://127.0.0.1:11499/health", allow_private=True) is True
    assert netguard.is_url_allowed("http://192.168.1.50:8080/", allow_private=True) is True
    assert netguard.is_url_allowed("http://10.0.0.5/x", allow_private=True) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://[::1]/",
    ],
)
def test_strict_mode_rejects_private_and_loopback(url):
    assert netguard.is_url_allowed(url, allow_private=False) is False


def test_public_https_allowed():
    assert netguard.is_url_allowed("https://example.com/path?q=1", allow_private=True) is True
    assert netguard.is_url_allowed("https://example.com/", allow_private=False) is True


def test_empty_and_scheme_less_rejected():
    assert netguard.is_url_allowed("") is False
    assert netguard.is_url_allowed("example.com/x") is False


def test_assert_raises_user_input_error():
    with pytest.raises(UserInputError) as ei:
        netguard.assert_url_allowed("file:///etc/passwd")
    assert "Refusing" in str(ei.value)


def test_hostname_resolving_to_private_is_rejected_in_strict(monkeypatch):
    # Simulate a hostname that resolves to a private address (rebinding).
    def fake_getaddrinfo(host, port, **kwargs):
        return [(2, 1, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake_getaddrinfo)
    assert netguard.is_url_allowed("http://sneaky.example.com/", allow_private=False) is False
    assert netguard.is_url_allowed("http://sneaky.example.com/", allow_private=True) is True


def test_default_reads_config(monkeypatch):
    monkeypatch.setattr(netguard.config, "ALLOW_PRIVATE_URLS", False)
    assert netguard.is_url_allowed("http://127.0.0.1/") is False
    monkeypatch.setattr(netguard.config, "ALLOW_PRIVATE_URLS", True)
    assert netguard.is_url_allowed("http://127.0.0.1/") is True
