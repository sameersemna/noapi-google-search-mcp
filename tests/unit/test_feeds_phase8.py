"""Phase 8 tests: migration 3 (HTTP validators) + search filters + retention."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from google_search_mcp.tools import feeds


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# ── migration 3 ─────────────────────────────────────────────────────────────

def test_v2_database_gains_validator_columns(tmp_path, monkeypatch):
    """A realistic v2 DB (dedupe_key present, no etag) upgrades to v3."""
    db = tmp_path / "v2.db"
    monkeypatch.setenv("FEEDS_DB_PATH", str(db))

    # Build a current-version DB, then strip the v3 column and mark it v2.
    conn = feeds._get_feeds_db()
    conn.close()
    conn = sqlite3.connect(db)
    try:
        conn.execute("ALTER TABLE subscriptions DROP COLUMN etag")
        conn.execute("ALTER TABLE subscriptions DROP COLUMN last_modified")
    except sqlite3.OperationalError:
        conn.close()
        pytest.skip("SQLite too old for DROP COLUMN")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    assert "etag" not in _columns(conn, "subscriptions")
    conn.close()

    # Re-open: migration 3 must add the columns.
    conn = feeds._get_feeds_db()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == feeds._SCHEMA_VERSION
        assert {"etag", "last_modified"} <= _columns(conn, "subscriptions")
    finally:
        conn.close()


def test_migration_3_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("FEEDS_DB_PATH", str(tmp_path / "f.db"))
    conn = feeds._get_feeds_db()
    conn.close()
    conn = feeds._get_feeds_db()  # second open must be a no-op
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == feeds._SCHEMA_VERSION
        assert {"etag", "last_modified"} <= _columns(conn, "subscriptions")
    finally:
        conn.close()


# ── search_feeds filters + snippet ──────────────────────────────────────────

@pytest.fixture()
def seeded(tmp_path, monkeypatch):
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
    rows = [
        ("Alpha transformers paper", "2026-01-10T00:00:00+00:00"),
        ("Beta transformers update", "2026-02-15T00:00:00+00:00"),
        ("Gamma unrelated", "2026-03-20T00:00:00+00:00"),
    ]
    for title, published in rows:
        conn.execute(
            "INSERT INTO feed_items (subscription_id, source_type, title, content, url,"
            " author, published_at, fetched_at, metadata, dedupe_key)"
            " VALUES (?, 'news', ?, ?, ?, '', ?, ?, '{}', ?)",
            (sid, title, f"body of {title}", f"https://example.invalid/{title[:5]}",
             published, now, f"https://example.invalid/{title[:5]}"),
        )
    conn.commit()
    conn.close()
    return db


def test_search_feeds_returns_matches(seeded):
    out = asyncio.run(feeds.search_feeds("transformers"))
    assert "Alpha transformers paper" in out
    assert "Beta transformers update" in out
    assert "Gamma unrelated" not in out


def test_search_feeds_since_filter(seeded):
    out = asyncio.run(feeds.search_feeds("transformers", since="2026-02-01"))
    assert "Beta transformers update" in out
    assert "Alpha transformers paper" not in out


def test_search_feeds_until_filter(seeded):
    out = asyncio.run(feeds.search_feeds("transformers", until="2026-01-31"))
    assert "Alpha transformers paper" in out
    assert "Beta transformers update" not in out


def test_search_feeds_since_and_until_window(seeded):
    out = asyncio.run(
        feeds.search_feeds("transformers", since="2026-01-01", until="2026-02-28")
    )
    assert "Alpha transformers paper" in out
    assert "Beta transformers update" in out
    assert "Gamma" not in out


def test_search_feeds_like_fallback_escapes_wildcards(seeded):
    # FTS may be unavailable; the LIKE fallback must still escape wildcards.
    out = asyncio.run(feeds.search_feeds("%"))
    assert "Alpha transformers paper" not in out


# ── retention pruning ───────────────────────────────────────────────────────

def test_retention_disabled_by_default_keeps_everything(seeded, monkeypatch):
    monkeypatch.setattr(feeds.config, "FEEDS_MAX_ITEMS_PER_SUB", 0)
    monkeypatch.setattr(feeds.config, "FEEDS_MAX_AGE_DAYS", 0)
    assert feeds._prune_items() == 0
    conn = sqlite3.connect(seeded)
    assert conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0] == 3
    conn.close()


def test_retention_max_items_per_subscription(seeded, monkeypatch):
    monkeypatch.setattr(feeds.config, "FEEDS_MAX_ITEMS_PER_SUB", 2)
    monkeypatch.setattr(feeds.config, "FEEDS_MAX_AGE_DAYS", 0)
    removed = feeds._prune_items()
    assert removed == 1
    conn = sqlite3.connect(seeded)
    titles = {
        r[0] for r in conn.execute("SELECT title FROM feed_items").fetchall()
    }
    conn.close()
    # The two most recent survive; the oldest is pruned.
    assert titles == {"Beta transformers update", "Gamma unrelated"}


def test_retention_max_age_days(seeded, monkeypatch):
    """Items older than the age limit are pruned; a fresh item survives."""
    monkeypatch.setattr(feeds.config, "FEEDS_MAX_ITEMS_PER_SUB", 0)
    monkeypatch.setattr(feeds.config, "FEEDS_MAX_AGE_DAYS", 60)

    # A brand-new item must not be pruned.
    fresh = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(seeded)
    conn.execute(
        "INSERT INTO feed_items (subscription_id, source_type, title, content, url,"
        " author, published_at, fetched_at, metadata, dedupe_key)"
        " VALUES (1, 'news', 'Today news', 'body', 'https://example.invalid/today',"
        " '', ?, ?, '{}', 'https://example.invalid/today')",
        (fresh, fresh),
    )
    conn.commit()
    conn.close()

    removed = feeds._prune_items()
    assert removed == 3  # the three seeded items are all far in the past

    conn = sqlite3.connect(seeded)
    titles = {r[0] for r in conn.execute("SELECT title FROM feed_items").fetchall()}
    conn.close()
    assert titles == {"Today news"}
