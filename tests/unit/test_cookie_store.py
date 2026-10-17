"""Tests for the cookie store (atomic write + restricted permissions).

The cookie jar holds session credentials, so it must be written atomically (a
crash mid-write must not leave a truncated file that breaks every later load) and
must not be world-readable.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from google_search_mcp import browser


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return self._cookies


def test_save_cookies_is_atomic_and_0600(tmp_path, monkeypatch):
    target = tmp_path / "cookies.json"
    monkeypatch.setattr(browser, "COOKIE_JSON_PATH", str(target))

    cookies = [{"name": "SID", "value": "secret-session", "domain": ".google.com"}]
    asyncio.run(browser.save_cookies(_FakeContext(cookies)))

    assert target.is_file()
    assert json.loads(target.read_text()) == cookies
    assert (os.stat(target).st_mode & 0o777) == 0o600, "cookie jar must be 0600"


def test_save_cookies_leaves_no_temp_files(tmp_path, monkeypatch):
    target = tmp_path / "cookies.json"
    monkeypatch.setattr(browser, "COOKIE_JSON_PATH", str(target))
    asyncio.run(browser.save_cookies(_FakeContext([])))

    leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".tmp-")]
    assert not leftovers, f"temp files left behind: {leftovers}"


def test_save_cookies_failure_is_swallowed(tmp_path, monkeypatch):
    """Save failures must never break a tool call."""

    class _Boom:
        async def cookies(self):
            raise RuntimeError("context gone")

    monkeypatch.setattr(browser, "COOKIE_JSON_PATH", str(tmp_path / "c.json"))
    asyncio.run(browser.save_cookies(_Boom()))  # must not raise


def test_save_cookies_overwrites_valid_json(tmp_path, monkeypatch):
    target = tmp_path / "cookies.json"
    monkeypatch.setattr(browser, "COOKIE_JSON_PATH", str(target))
    target.write_text("{ truncated")  # simulate a previously corrupt file

    asyncio.run(browser.save_cookies(_FakeContext([{"name": "A", "value": "1"}])))
    assert json.loads(target.read_text()) == [{"name": "A", "value": "1"}]
    assert (os.stat(target).st_mode & 0o777) == 0o600


@pytest.mark.parametrize("payload", [[], [{"name": "x", "value": "y"}]])
def test_save_cookies_roundtrip(tmp_path, monkeypatch, payload):
    target = tmp_path / "cookies.json"
    monkeypatch.setattr(browser, "COOKIE_JSON_PATH", str(target))
    asyncio.run(browser.save_cookies(_FakeContext(payload)))
    assert json.loads(target.read_text()) == payload
