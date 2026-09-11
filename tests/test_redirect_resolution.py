"""Pytest tests for Google search-result redirect URL resolution.

Covers:
  - Legacy `/url?q=...` decode
  - Modern `/goto?url=...` resolution (via a local HTTP server)
  - Non-Google URL passthrough
  - Resolution-failure fallback (returns original URL)
  - `resolve_urls` async concurrency helper
  - The in-process LRU cache
"""

import asyncio
import http.server
import threading
import urllib.parse

import pytest

from google_search_mcp.utils import network as network_mod
from google_search_mcp.utils.network import (
    resolve_url,
    resolve_urls,
    _is_google_redirect,
    _decode_legacy_redirect,
    _resolve_goto_redirect,
)


# ---------------------------------------------------------------------------
# Local HTTP server that simulates a Google /goto redirect
# ---------------------------------------------------------------------------


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    def _do_redirect(self):
        # Simulate Google's /goto endpoint: 302 to the real destination.
        self.send_response(302)
        self.send_header(
            "Location",
            f"http://127.0.0.1:{self.server.server_address[1]}/final",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _do_final(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        if self.path.startswith("/final"):
            self._do_final()
        else:
            self._do_redirect()

    def do_GET(self):
        if self.path.startswith("/final"):
            self._do_final()
        else:
            self._do_redirect()

    def log_message(self, *args):
        pass


@pytest.fixture
def redirect_server():
    """Start a local HTTP server that 302-redirects to a /final path."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the module-level LRU cache before and after each test."""
    network_mod._resolve_cache.clear()
    yield
    network_mod._resolve_cache.clear()


# ---------------------------------------------------------------------------
# Legacy /url?q= decode
# ---------------------------------------------------------------------------


def test_legacy_url_q_decode():
    dest = "https://binbaz.org.sa/fatwas/12345"
    redirect = (
        "https://www.google.com/url?q="
        + urllib.parse.quote(dest, safe="")
        + "&sa=U&ved=2ahUKEwi&usg=AOvVaw0"
    )
    assert resolve_url(redirect) == dest


def test_legacy_url_q_decode_with_other_params():
    dest = "https://example.com/path?x=1&y=2"
    redirect = (
        "https://www.google.com/url?q="
        + urllib.parse.quote(dest, safe="")
        + "&sa=U&usg=AOvVaw0"
    )
    assert resolve_url(redirect) == dest


def test_legacy_uddg_param():
    dest = "https://example.org/article"
    redirect = "https://www.google.com/url?uddg=" + urllib.parse.quote(dest, safe="")
    assert resolve_url(redirect) == dest


# ---------------------------------------------------------------------------
# Modern /goto?url= resolution
# ---------------------------------------------------------------------------


def test_modern_goto_resolution(redirect_server):
    token = "some-base64url-protobuf-token"
    redirect = f"http://127.0.0.1:{redirect_server}/goto?url={token}"
    assert _resolve_goto_redirect(redirect) == f"http://127.0.0.1:{redirect_server}/final"


# ---------------------------------------------------------------------------
# Passthrough
# ---------------------------------------------------------------------------


def test_non_google_url_passthrough():
    url = "https://shamela.ws/index.php/book/12345"
    assert resolve_url(url) == url


def test_google_search_url_passthrough():
    url = "https://www.google.com/search?q=hello&udm=14"
    assert resolve_url(url) == url


def test_empty_url_passthrough():
    assert resolve_url("") == ""


# ---------------------------------------------------------------------------
# Failure fallback
# ---------------------------------------------------------------------------


def test_resolution_failure_falls_back_to_original():
    # A /goto URL pointing at a non-routable address -> resolution fails.
    redirect = "http://www.google.com/goto?url=token"
    result = resolve_url(redirect, timeout=1)
    assert result == redirect


# ---------------------------------------------------------------------------
# Detection + decode helpers
# ---------------------------------------------------------------------------


def test_is_google_redirect_detection():
    assert _is_google_redirect("https://www.google.com/url?q=x")
    assert _is_google_redirect("https://www.google.com/goto?url=x")
    assert not _is_google_redirect("https://example.com/url?q=x")
    assert not _is_google_redirect("https://www.google.com/search?q=x")


def test_decode_legacy_redirect_helper():
    dest = "https://example.com/a"
    redirect = "https://www.google.com/url?q=" + urllib.parse.quote(dest, safe="")
    assert _decode_legacy_redirect(redirect) == dest
    # No q param -> empty
    assert _decode_legacy_redirect("https://www.google.com/url?sa=U") == ""


# ---------------------------------------------------------------------------
# resolve_urls async helper
# ---------------------------------------------------------------------------


def test_resolve_urls_async():
    dest = "https://example.com/result"
    redirect = "https://www.google.com/url?q=" + urllib.parse.quote(dest, safe="")
    urls = [redirect, "https://plain.example.org/x", redirect]
    resolved = asyncio.run(resolve_urls(urls))
    assert resolved == [dest, "https://plain.example.org/x", dest]


def test_resolve_urls_empty():
    assert asyncio.run(resolve_urls([])) == []


# ---------------------------------------------------------------------------
# LRU cache
# ---------------------------------------------------------------------------


def test_cache_memoizes_legacy_decode():
    dest = "https://example.com/cached"
    redirect = "https://www.google.com/url?q=" + urllib.parse.quote(dest, safe="")
    assert resolve_url(redirect) == dest
    assert network_mod._cache_get(redirect) == dest


def test_cache_evicts_oldest_when_full():
    old_size = network_mod.REDIRECT_RESOLVE_CACHE_SIZE
    network_mod.REDIRECT_RESOLVE_CACHE_SIZE = 2
    try:
        r1 = "https://www.google.com/url?q=" + urllib.parse.quote("https://a.example/1", safe="")
        r2 = "https://www.google.com/url?q=" + urllib.parse.quote("https://a.example/2", safe="")
        r3 = "https://www.google.com/url?q=" + urllib.parse.quote("https://a.example/3", safe="")
        resolve_url(r1)
        resolve_url(r2)
        resolve_url(r3)
        # r1 was evicted (oldest), r2 and r3 remain.
        assert network_mod._cache_get(r1) is None
        assert network_mod._cache_get(r2) is not None
        assert network_mod._cache_get(r3) is not None
    finally:
        network_mod.REDIRECT_RESOLVE_CACHE_SIZE = old_size


def test_cache_disabled_when_size_zero():
    old_size = network_mod.REDIRECT_RESOLVE_CACHE_SIZE
    network_mod.REDIRECT_RESOLVE_CACHE_SIZE = 0
    try:
        dest = "https://example.com/nocache"
        redirect = "https://www.google.com/url?q=" + urllib.parse.quote(dest, safe="")
        resolve_url(redirect)
        assert network_mod._cache_get(redirect) is None
    finally:
        network_mod.REDIRECT_RESOLVE_CACHE_SIZE = old_size


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_config_defaults_present():
    from google_search_mcp.config import (
        REDIRECT_RESOLVE_TIMEOUT,
        REDIRECT_RESOLVE_CACHE_SIZE,
    )
    assert isinstance(REDIRECT_RESOLVE_TIMEOUT, int) and REDIRECT_RESOLVE_TIMEOUT > 0
    assert isinstance(REDIRECT_RESOLVE_CACHE_SIZE, int) and REDIRECT_RESOLVE_CACHE_SIZE >= 0


# ---------------------------------------------------------------------------
# YouTube-specific resolution
# ---------------------------------------------------------------------------


def test_youtube_goto_resolved_via_title_search(monkeypatch):
    """A YouTube goto URL that fails HTTP follow should resolve via title search."""
    # A goto URL that will fail the generic HTTP follow (non-routable host).
    goto = "http://www.google.com/goto?url=some-opaque-token"
    # Monkeypatch the yt-dlp subprocess to return a known video ID.
    import subprocess

    class _FakeResult:
        returncode = 0
        stdout = "o8NPllzkFhE\n"

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeResult(), raising=False
    )
    resolved = resolve_url(goto, timeout=1, title="Linus Torvalds on Linux")
    assert resolved == "https://www.youtube.com/watch?v=o8NPllzkFhE"


def test_youtube_search_failure_falls_back_to_original(monkeypatch):
    """If the YouTube title search fails, return the original goto URL."""
    goto = "http://www.google.com/goto?url=some-opaque-token"
    import subprocess

    class _FakeResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeResult(), raising=False
    )
    resolved = resolve_url(goto, timeout=1, title="Some video title")
    assert resolved == goto


def test_youtube_resolution_skipped_without_title():
    """Without a title, the YouTube fallback is not attempted."""
    goto = "http://www.google.com/goto?url=some-opaque-token"
    # No title -> generic follow fails -> returns original, no yt-dlp call.
    resolved = resolve_url(goto, timeout=1)
    assert resolved == goto


def test_non_youtube_goto_still_resolved_via_http(redirect_server):
    """A non-YouTube goto URL should still resolve via the HTTP follow."""
    token = "some-base64url-protobuf-token"
    goto = f"http://127.0.0.1:{redirect_server}/goto?url={token}"
    # The HTTP follow (Location header) succeeds first, so no yt-dlp needed.
    resolved = _resolve_goto_redirect(goto, timeout=5)
    assert resolved == f"http://127.0.0.1:{redirect_server}/final"


def test_resolve_urls_passes_titles(monkeypatch):
    """resolve_urls should pass aligned titles to resolve_url."""
    import subprocess

    class _FakeResult:
        returncode = 0
        stdout = "abcDEFghijk\n"

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeResult(), raising=False
    )
    goto = "http://www.google.com/goto?url=opaque"
    urls = [goto, "https://plain.example.org/x"]
    titles = ["A YouTube video title", ""]
    resolved = asyncio.run(resolve_urls(urls, timeout=1, titles=titles))
    assert resolved[0] == "https://www.youtube.com/watch?v=abcDEFghijk"
    assert resolved[1] == "https://plain.example.org/x"
