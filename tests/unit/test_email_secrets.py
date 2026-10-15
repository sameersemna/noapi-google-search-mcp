"""Phase 7 tests: email credentials + read-only (PEEK) behaviour."""

from __future__ import annotations

import asyncio
import inspect

from google_search_mcp.tools import web_utils


def test_fetch_emails_uses_body_peek_not_rfc822():
    src = inspect.getsource(web_utils.fetch_emails)
    # The inner fetcher uses PEEK so mail is not marked as seen.
    assert 'mail.fetch(mid, "(BODY.PEEK[])")' in src
    assert 'mail.fetch(mid, "(RFC822)")' not in src, (
        "RFC822 marks mail as seen — not read-only"
    )


def test_fetch_emails_reads_credentials_from_env(monkeypatch):
    monkeypatch.setenv("NOAPI_MCP_IMAP_USER", "user@example.com")
    monkeypatch.setenv("NOAPI_MCP_IMAP_PASSWORD", "app-password")
    # No args -> it should attempt to connect (and fail on network), NOT return
    # the "missing credentials" message.
    out = asyncio.run(web_utils.fetch_emails(search="UNSEEN", limit=1))
    assert "Missing email credentials" not in out


def test_fetch_emails_missing_credentials_is_actionable(monkeypatch):
    monkeypatch.delenv("NOAPI_MCP_IMAP_USER", raising=False)
    monkeypatch.delenv("NOAPI_MCP_IMAP_PASSWORD", raising=False)
    out = asyncio.run(web_utils.fetch_emails())
    assert "NOAPI_MCP_IMAP_USER" in out
    assert "NOAPI_MCP_IMAP_PASSWORD" in out


def test_logging_redacts_password():
    from google_search_mcp.logging_setup import format_tool_arguments

    rendered = format_tool_arguments(
        {"email_address": "u@example.com", "password": "s3cret", "secret_key": "abc"}
    )
    assert "s3cret" not in rendered
    assert "abc" not in rendered
    assert "u@example.com" in rendered
