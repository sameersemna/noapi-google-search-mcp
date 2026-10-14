"""Phase 1 regression tests for feeds LIKE-wildcard escaping (offline, B6).

User input is interpolated into ``LIKE '%...%'`` patterns. Un-escaped ``%`` /
``_`` act as wildcards, so e.g. searching for ``"%"`` matched *every* row.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from google_search_mcp.tools import feeds


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    db = tmp_path / "feeds.db"
    monkeypatch.setenv("FEEDS_DB_PATH", str(db))
    conn = feeds._get_feeds_db()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO subscriptions (source_type, identifier, name, feed_url, created_at)"
        " VALUES ('news', 'x', 'Foo News', '', ?)",
        (now,),
    )
    sid = cur.lastrowid
    conn.execute(
        "INSERT INTO feed_items (subscription_id, source_type, title, content, url,"
        " author, published_at, fetched_at, metadata)"
        " VALUES (?, 'news', 'Hello World', 'body text', 'https://example.invalid/1',"
        " '', '2026-01-01T00:00:00+00:00', ?, '{}')",
        (sid, now),
    )
    conn.commit()
    conn.close()
    return db


def test_like_escape_helper():
    assert feeds._like_escape("100%") == "100\\%"
    assert feeds._like_escape("a_b") == "a\\_b"
    assert feeds._like_escape("back\\slash") == "back\\\\slash"


@pytest.mark.asyncio
async def test_search_feeds_percent_is_literal(seeded_db):
    out = await feeds.search_feeds("%")
    assert "Hello World" not in out, "bare '%' must not match every row (B6)"
    # sanity: a real substring still matches
    assert "Hello World" in await feeds.search_feeds("Hello")


@pytest.mark.asyncio
async def test_search_feeds_underscore_is_literal(seeded_db):
    out = await feeds.search_feeds("_")
    assert "Hello World" not in out, "bare '_' must not match every single char (B6)"


@pytest.mark.asyncio
async def test_get_feed_items_source_filter_escapes_wildcards(seeded_db):
    # A source filter of "%" must not match the "Foo News" subscription.
    out = await feeds.get_feed_items(source="%")
    assert "Hello World" not in out, "source filter must escape LIKE wildcards (B6)"
    assert "Hello World" in await feeds.get_feed_items(source="Foo")


def test_db_row_count(seeded_db):
    conn = sqlite3.connect(seeded_db)
    assert conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0] == 1
    conn.close()
