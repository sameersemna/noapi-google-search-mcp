#!/usr/bin/env python3
"""
Unit tests for Google search-result redirect URL resolution.

Covers:
  - Legacy `/url?q=...` decode
  - Modern `/goto?url=...` resolution (via a local HTTP server)
  - Non-Google URL passthrough
  - Resolution-failure fallback (returns original URL)
  - `resolve_urls` async concurrency helper
"""

import asyncio
import http.server
import os
import sys
import threading
import urllib.parse

sys.path.insert(0, "src")

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
        self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/final")
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


def _start_redirect_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_legacy_url_q_decode():
    """Legacy `/url?q=...` should be URL-decoded to the destination."""
    dest = "https://binbaz.org.sa/fatwas/12345"
    redirect = (
        "https://www.google.com/url?q="
        + urllib.parse.quote(dest, safe="")
        + "&sa=U&ved=2ahUKEwi&usg=AOvVaw0"
    )
    assert resolve_url(redirect) == dest


def test_legacy_url_q_decode_with_other_params():
    """The `q` param should win even when other params are present."""
    dest = "https://example.com/path?x=1&y=2"
    redirect = (
        "https://www.google.com/url?q="
        + urllib.parse.quote(dest, safe="")
        + "&sa=U&usg=AOvVaw0"
    )
    assert resolve_url(redirect) == dest


def test_legacy_uddg_param():
    """DuckDuckGo-style `uddg` param should also be decoded."""
    dest = "https://example.org/article"
    redirect = "https://www.google.com/url?uddg=" + urllib.parse.quote(dest, safe="")
    assert resolve_url(redirect) == dest


def test_modern_goto_resolution():
    """Modern `/goto?url=...` should be followed to the final destination."""
    server, port = _start_redirect_server()
    try:
        token = "some-base64url-protobuf-token"
        redirect = f"http://127.0.0.1:{port}/goto?url={token}"
        assert _resolve_goto_redirect(redirect) == f"http://127.0.0.1:{port}/final"
    finally:
        server.shutdown()


def test_non_google_url_passthrough():
    """Non-Google URLs should pass through unchanged."""
    url = "https://shamela.ws/index.php/book/12345"
    assert resolve_url(url) == url


def test_google_search_url_passthrough():
    """A plain google.com/search URL is not a redirect wrapper -> unchanged."""
    url = "https://www.google.com/search?q=hello&udm=14"
    assert resolve_url(url) == url


def test_resolution_failure_falls_back_to_original():
    """If resolution fails, the original URL is returned (never dropped)."""
    # A /goto URL pointing at a non-routable address -> resolution fails.
    redirect = "http://www.google.com/goto?url=token"
    result = resolve_url(redirect, timeout=1)
    assert result == redirect


def test_empty_url_passthrough():
    assert resolve_url("") == ""


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


def test_resolve_urls_async():
    """`resolve_urls` resolves a list concurrently and preserves order."""
    dest = "https://example.com/result"
    redirect = "https://www.google.com/url?q=" + urllib.parse.quote(dest, safe="")
    urls = [redirect, "https://plain.example.org/x", redirect]
    resolved = asyncio.run(resolve_urls(urls))
    assert resolved == [dest, "https://plain.example.org/x", dest]


def test_resolve_urls_empty():
    assert asyncio.run(resolve_urls([])) == []


if __name__ == "__main__":
    import traceback

    tests = [
        test_legacy_url_q_decode,
        test_legacy_url_q_decode_with_other_params,
        test_legacy_uddg_param,
        test_modern_goto_resolution,
        test_non_google_url_passthrough,
        test_google_search_url_passthrough,
        test_resolution_failure_falls_back_to_original,
        test_empty_url_passthrough,
        test_is_google_redirect_detection,
        test_decode_legacy_redirect_helper,
        test_resolve_urls_async,
        test_resolve_urls_empty,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
    sys.exit(0 if passed == len(tests) else 1)
