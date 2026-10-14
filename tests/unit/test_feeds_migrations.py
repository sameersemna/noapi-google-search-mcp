"""Phase 1 regression tests for the feeds schema migration system (offline).

Covers **B4** (mixed date formats), **B5** (missing FTS ``UPDATE`` trigger) and
**B7** (no ``dedupe_key`` / URL-less items dropped) at the schema level:

* ``PRAGMA user_version`` is used to run ordered migrations.
* A database created by the *old* (pre-hardening) schema upgrades to the new
  schema: adds ``dedupe_key`` with ``UNIQUE(subscription_id, dedupe_key)``,
  normalizes ``published_at`` to ISO-8601 UTC, and installs the FTS
  ``AFTER UPDATE`` trigger.
* Running the migration twice is a no-op (idempotent).
"""

from __future__ import annotations

import sqlite3

import pytest

from google_search_mcp.tools import feeds

# The schema exactly as shipped before hardening (feed_items had
# UNIQUE(subscription_id, url), no dedupe_key, no UPDATE trigger).
LEGACY_SCHEMA = """
CREATE TABLE subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    identifier TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    feed_url TEXT NOT NULL DEFAULT '',
    last_checked TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source_type, identifier)
);
CREATE TABLE feed_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(subscription_id, url),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
);
CREATE INDEX idx_items_sub ON feed_items(subscription_id);
CREATE INDEX idx_items_type ON feed_items(source_type);
CREATE INDEX idx_items_pub ON feed_items(published_at DESC);
CREATE VIRTUAL TABLE feed_items_fts USING fts5(
    title, content, author, content='feed_items', content_rowid='id'
);
CREATE TRIGGER fts_ai AFTER INSERT ON feed_items BEGIN
    INSERT INTO feed_items_fts(rowid, title, content, author)
    VALUES (new.id, new.title, new.content, new.author);
END;
CREATE TRIGGER fts_ad AFTER DELETE ON feed_items BEGIN
    INSERT INTO feed_items_fts(feed_items_fts, rowid, title, content, author)
    VALUES ('delete', old.id, old.title, old.content, old.author);
END;
"""


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _user_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _make_legacy_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO subscriptions (source_type, identifier, name, feed_url,"
        " created_at) VALUES ('news', 'bbc', 'BBC', '', '2026-01-01T00:00:00+00:00')"
    )
    # RFC 822 date (as served by RSS) — must be normalized to ISO-8601 UTC.
    conn.execute(
        "INSERT INTO feed_items (subscription_id, source_type, title, content,"
        " url, author, published_at, fetched_at, metadata)"
        " VALUES (1, 'news', 'Old Item', 'body', 'https://example.invalid/a',"
        " '', 'Mon, 06 Jan 2025 12:00:00 GMT', '2026-01-01T00:00:00+00:00', '{}')"
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    _make_legacy_db(db)
    monkeypatch.setenv("FEEDS_DB_PATH", str(db))
    return db


def test_legacy_db_upgrades_to_latest_version(legacy_db):
    conn = feeds._get_feeds_db()
    try:
        assert _user_version(conn) == feeds._SCHEMA_VERSION
        assert "dedupe_key" in _columns(conn, "feed_items")
    finally:
        conn.close()


def test_upgrade_adds_unique_dedupe_index(legacy_db):
    conn = feeds._get_feeds_db()
    try:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='feed_items'"
        ).fetchone()[0]
        assert "dedupe_key" in ddl
        assert "UNIQUE(subscription_id,dedupe_key)" in ddl.replace(" ", "")
        # backfilled value should be the URL for legacy rows
        row = conn.execute("SELECT dedupe_key FROM feed_items WHERE id=1").fetchone()
        assert row[0] == "https://example.invalid/a"
        # the UNIQUE constraint must actually be enforced
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO feed_items (subscription_id, source_type, url,"
                " fetched_at, dedupe_key) VALUES (1, 'news', 'other',"
                " '2026-01-01T00:00:00+00:00', 'https://example.invalid/a')"
            )
    finally:
        conn.close()


def test_upgrade_normalizes_published_at(legacy_db):
    conn = feeds._get_feeds_db()
    try:
        row = conn.execute("SELECT published_at FROM feed_items WHERE id=1").fetchone()
        # "Mon, 06 Jan 2025 12:00:00 GMT" -> ISO-8601 UTC
        assert row[0].startswith("2025-01-06T12:00:00"), row[0]
        assert row[0].endswith("+00:00"), row[0]
    finally:
        conn.close()


def test_upgrade_installs_fts_update_trigger(legacy_db):
    conn = feeds._get_feeds_db()
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        assert "fts_au" in names, f"missing FTS UPDATE trigger; have {names}"
        # The UPDATE trigger must keep the FTS index in sync.
        conn.execute(
            "UPDATE feed_items SET title='Renamed Item' WHERE id=1"
        )
        conn.commit()
        hit = conn.execute(
            "SELECT rowid FROM feed_items_fts WHERE feed_items_fts MATCH 'Renamed'"
        ).fetchall()
        assert hit, "FTS index desynced after UPDATE (B5)"
        old = conn.execute(
            "SELECT rowid FROM feed_items_fts WHERE feed_items_fts MATCH 'Old'"
        ).fetchall()
        assert not old, "stale FTS row survived UPDATE"
    finally:
        conn.close()


def test_migration_is_idempotent(legacy_db):
    conn = feeds._get_feeds_db()
    conn.close()
    conn = feeds._get_feeds_db()  # second open must not re-run/duplicate
    try:
        assert _user_version(conn) == feeds._SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0] == 1
        # exactly one copy of the FTS rebuild trigger
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='fts_au'"
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_fresh_db_is_created_at_latest_version(tmp_path, monkeypatch):
    monkeypatch.setenv("FEEDS_DB_PATH", str(tmp_path / "fresh.db"))
    conn = feeds._get_feeds_db()
    try:
        assert _user_version(conn) == feeds._SCHEMA_VERSION
        assert "dedupe_key" in _columns(conn, "feed_items")
    finally:
        conn.close()
