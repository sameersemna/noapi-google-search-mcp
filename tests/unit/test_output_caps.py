"""Phase 7 tests: output caps for documents and e-mail bodies.

Large documents must be truncated with a clear marker rather than flooding the
model context, and the caps must be configurable via NOAPI_MCP_* env vars.
"""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp.tools import web_utils


def test_cap_text_leaves_short_text_untouched():
    assert web_utils._cap_text("hello", 100) == "hello"


def test_cap_text_truncates_with_marker():
    out = web_utils._cap_text("x" * 500, 100, "500 B")
    assert out.startswith("x" * 100)
    assert "truncated" in out
    assert "100 characters" in out
    assert "500 B" in out
    assert len(out) < 500


def test_cap_text_zero_or_negative_is_unlimited():
    assert web_utils._cap_text("y" * 1000, 0) == "y" * 1000


def test_read_document_truncates_large_plain_text(tmp_path, monkeypatch):
    p = tmp_path / "big.txt"
    p.write_text("z" * 5000)
    monkeypatch.setattr(web_utils, "MAX_DOCUMENT_CHARS", 500)

    out = asyncio.run(web_utils.read_document(str(p)))
    assert "truncated" in out
    # body capped at 500 z's (plus the header/marker)
    assert out.count("z") == 500


def test_read_document_truncates_large_html(tmp_path, monkeypatch):
    p = tmp_path / "big.html"
    p.write_text("<html><body>" + ("<p>hello world</p>" * 300) + "</body></html>")
    monkeypatch.setattr(web_utils, "MAX_DOCUMENT_CHARS", 400)

    out = asyncio.run(web_utils.read_document(str(p)))
    assert "truncated" in out


def test_document_cap_is_configurable(monkeypatch):
    from google_search_mcp import config

    monkeypatch.setenv("NOAPI_MCP_MAX_DOCUMENT_CHARS", "1234")
    import importlib

    reloaded = importlib.reload(config)
    try:
        assert reloaded.MAX_DOCUMENT_CHARS == 1234
    finally:
        monkeypatch.delenv("NOAPI_MCP_MAX_DOCUMENT_CHARS", raising=False)
        importlib.reload(config)


def test_email_body_cap_is_configurable(monkeypatch):
    from google_search_mcp import config

    monkeypatch.setenv("NOAPI_MCP_MAX_EMAIL_BODY_CHARS", "777")
    import importlib

    reloaded = importlib.reload(config)
    try:
        assert reloaded.MAX_EMAIL_BODY_CHARS == 777
    finally:
        monkeypatch.delenv("NOAPI_MCP_MAX_EMAIL_BODY_CHARS", raising=False)
        importlib.reload(config)


def test_default_caps_are_sensible():
    from google_search_mcp import config

    assert config.MAX_DOCUMENT_CHARS >= 10_000
    assert 100 <= config.MAX_EMAIL_BODY_CHARS <= 100_000
