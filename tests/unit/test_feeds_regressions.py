"""Phase 1 regression tests for the feeds subsystem (offline).

These tests exercise the pure/DB parts of ``tools/feeds.py`` — no network.
Network access is monkeypatched. Each test corresponds to an audit finding:

* **B1** — ``subscribe(reddit, ...)`` must strip the ``r/`` / ``/r/`` *prefix*,
  not the characters ``r`` and ``/`` (``str.lstrip`` strips a *character set*).
* **B8** — arXiv feed URLs must use ``https://`` (not ``http://``).
"""

from __future__ import annotations

import sqlite3

import pytest

from google_search_mcp.tools import feeds


@pytest.fixture()
def feeds_db(tmp_path, monkeypatch):
    """Point the feeds DB at an isolated temp file for this test."""
    db = tmp_path / "feeds.db"
    monkeypatch.setenv("FEEDS_DB_PATH", str(db))
    return db


def _get_sub(feeds_db, source_type: str, identifier: str):
    conn = sqlite3.connect(feeds_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM subscriptions WHERE source_type = ? AND identifier = ?",
        (source_type, identifier),
    ).fetchone()
    conn.close()
    return row


# ── B1: reddit identifier normalization ────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("rust", "rust"),
        ("r/rust", "rust"),
        ("/r/rust/", "rust"),
        ("r/LocalLLaMA", "LocalLLaMA"),
        ("LocalLLaMA", "LocalLLaMA"),
        ("r/rprogramming", "rprogramming"),
        ("Rust", "Rust"),
    ],
)
@pytest.mark.asyncio
async def test_subscribe_reddit_identifier_normalization(feeds_db, raw, expected):
    await feeds.subscribe("reddit", raw)
    row = _get_sub(feeds_db, "reddit", expected)
    assert row is not None, (
        f"reddit identifier for input {raw!r} should be {expected!r}, "
        f"stored rows: {[dict(r) for r in _all_subs(feeds_db)]}"
    )
    assert row["feed_url"] == f"https://www.reddit.com/r/{expected}/.rss"


def _all_subs(feeds_db):
    conn = sqlite3.connect(feeds_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM subscriptions").fetchall()
    conn.close()
    return rows


# ── B8: arxiv URL must be https ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_arxiv_stores_https_feed_url(feeds_db):
    await feeds.subscribe("arxiv", "cs.AI")
    row = _get_sub(feeds_db, "arxiv", "cs.AI")
    assert row is not None
    assert row["feed_url"].startswith("https://export.arxiv.org/"), row["feed_url"]


@pytest.mark.asyncio
async def test_check_source_arxiv_requests_https(monkeypatch):
    captured: dict[str, str] = {}

    def fake_fetch(url, *args, **kwargs):
        captured["url"] = url
        return b"<feed xmlns='http://www.w3.org/2005/Atom'></feed>"

    monkeypatch.setattr(feeds, "_fetch_url_bytes", fake_fetch)
    await feeds._check_source_arxiv("cs.AI")
    assert captured["url"].startswith("https://export.arxiv.org/"), captured["url"]
