"""Phase 8 tests: conditional GET (ETag / Last-Modified) in check_feeds."""

from __future__ import annotations

import asyncio
import sqlite3
import urllib.error

import pytest

from google_search_mcp.tools import feeds

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>Fresh</title><link>https://example.invalid/1</link>
<pubDate>Mon, 06 Jan 2025 12:00:00 GMT</pubDate></item>
</channel></rss>"""


@pytest.fixture()
def feed_db(tmp_path, monkeypatch):
    db = tmp_path / "feeds.db"
    monkeypatch.setenv("FEEDS_DB_PATH", str(db))
    return db


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_feed_bytes_sends_validators(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=15):  # noqa: ARG001
        captured["headers"] = dict(req.header_items())
        return _FakeResponse(RSS, {"ETag": '"v2"', "Last-Modified": "Mon, 06 Jan 2025"})

    monkeypatch.setattr(feeds.urllib.request, "urlopen", fake_urlopen)
    data, etag, last_modified = feeds._fetch_feed_bytes(
        "https://example.invalid/f.xml", '"v1"', "Sun, 05 Jan 2025"
    )
    assert data == RSS
    assert etag == '"v2"'
    assert last_modified == "Mon, 06 Jan 2025"
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["if-none-match"] == '"v1"'
    assert headers["if-modified-since"] == "Sun, 05 Jan 2025"


def test_fetch_feed_bytes_treats_304_as_none(monkeypatch):
    def fake_urlopen(req, timeout=15):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)

    monkeypatch.setattr(feeds.urllib.request, "urlopen", fake_urlopen)
    data, etag, last_modified = feeds._fetch_feed_bytes(
        "https://example.invalid/f.xml", '"v1"', "Sun, 05 Jan 2025"
    )
    assert data is None
    # validators carry forward so the next check can be conditional again
    assert etag == '"v1"'
    assert last_modified == "Sun, 05 Jan 2025"


def test_fetch_feed_bytes_without_validators_uses_legacy_path(monkeypatch):
    """No validators -> delegate to _fetch_url_bytes (keeps old tests working)."""
    calls = {"n": 0}

    def fake_legacy(url, *args, **kwargs):  # noqa: ARG001
        calls["n"] += 1
        return RSS

    monkeypatch.setattr(feeds, "_fetch_url_bytes", fake_legacy)
    data, etag, last_modified = feeds._fetch_feed_bytes("https://example.invalid/f.xml")
    assert data == RSS and etag == "" and last_modified == ""
    assert calls["n"] == 1


def test_check_feeds_stores_and_reuses_etag(feed_db, monkeypatch):
    calls: list[tuple] = []

    def fake_fetch(url, etag="", last_modified=""):
        calls.append((url, etag, last_modified))
        if etag == '"v1"':
            return None, etag, last_modified  # 304
        return RSS, '"v1"', "Mon, 06 Jan 2025"

    monkeypatch.setattr(feeds, "_fetch_feed_bytes", fake_fetch)

    async def run():
        await feeds.subscribe("news", "https://example.invalid/f.xml")
        first = await feeds.check_feeds()
        second = await feeds.check_feeds()
        return first, second

    first, second = asyncio.run(run())

    assert "1 new items" in first
    assert "not modified" in second
    assert "Total: 0 new items" in second

    # The validator was persisted and then sent back on the next check.
    conn = sqlite3.connect(feed_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT etag, last_modified FROM subscriptions").fetchone()
    conn.close()
    assert row["etag"] == '"v1"'
    assert row["last_modified"] == "Mon, 06 Jan 2025"
    assert calls[1][1] == '"v1"', "second check must send If-None-Match"

    # Only one item was ever stored (the 304 stored nothing).
    conn = sqlite3.connect(feed_db)
    assert conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0] == 1
    conn.close()
