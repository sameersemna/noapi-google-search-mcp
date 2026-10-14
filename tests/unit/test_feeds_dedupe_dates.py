"""Phase 1 regression tests for date normalization (B4) and URL-less dedupe (B7).

* **B4** — ``published_at`` was stored as whatever the feed emitted (RFC 822,
  ISO 8601, epoch), so ``ORDER BY published_at DESC`` sorted *lexically* across
  mixed formats. It must be normalized to ISO-8601 UTC on insert.
* **B7** — items without a URL were silently dropped (some podcast / Twitter /
  arXiv items). They must now be stored, deduped by a content hash.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from google_search_mcp.tools import feeds


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "feeds.db"
    monkeypatch.setenv("FEEDS_DB_PATH", str(path))
    conn = feeds._get_feeds_db()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO subscriptions (source_type, identifier, name, feed_url, created_at)"
        " VALUES ('podcast', 'x', 'Pod', '', ?)",
        (now,),
    )
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return path, sid


# ── B4 ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "prefix"),
    [
        ("Mon, 06 Jan 2025 12:00:00 GMT", "2025-01-06T12:00:00"),
        ("2025-01-06T12:00:00Z", "2025-01-06T12:00:00"),
        ("2025-01-06T12:00:00+00:00", "2025-01-06T12:00:00"),
        ("2025-01-06T13:00:00+01:00", "2025-01-06T12:00:00"),
    ],
)
def test_normalize_published_to_iso_utc(raw, prefix):
    out = feeds._normalize_published(raw)
    assert out.startswith(prefix), f"{raw!r} -> {out!r}"
    assert out.endswith("+00:00")


def test_normalize_published_empty_and_unparseable():
    assert feeds._normalize_published("") == ""
    # Unparseable values are preserved rather than dropped.
    assert feeds._normalize_published("not a date") == "not a date"


def test_mixed_formats_sort_correctly(db):
    path, sid = db
    items = [
        {"title": "rss", "url": "u1", "published": "Mon, 06 Jan 2025 12:00:00 GMT"},
        {"title": "atom", "url": "u2", "published": "2025-01-07T00:00:00Z"},
        {"title": "epoch-ish", "url": "u3", "published": "2025-01-05T00:00:00+00:00"},
    ]
    conn = feeds._get_feeds_db()
    feeds._store_items(conn, sid, "podcast", items)
    rows = conn.execute(
        "SELECT title FROM feed_items ORDER BY published_at DESC"
    ).fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["atom", "rss", "epoch-ish"]


def test_stored_published_is_iso(db):
    path, sid = db
    conn = feeds._get_feeds_db()
    feeds._store_items(
        conn, sid, "podcast",
        [{"title": "t", "url": "u", "published": "Mon, 06 Jan 2025 12:00:00 GMT"}],
    )
    row = conn.execute("SELECT published_at FROM feed_items WHERE url='u'").fetchone()
    conn.close()
    assert row[0].startswith("2025-01-06T12:00:00") and row[0].endswith("+00:00")


# ── B7 ──────────────────────────────────────────────────────────────────────

def test_url_less_item_is_stored(db):
    path, sid = db
    conn = feeds._get_feeds_db()
    n = feeds._store_items(
        conn, sid, "podcast",
        [{"title": "No URL item", "content": "hello", "published": "2025-01-01T00:00:00+00:00"}],
    )
    conn.close()
    assert n == 1, "URL-less items must not be dropped (B7)"
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT title, dedupe_key FROM feed_items WHERE title='No URL item'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1], "url-less items need a deterministic dedupe_key"


def test_url_less_items_dedupe_by_content_hash(db):
    path, sid = db
    item = {"title": "Dupe", "content": "same", "published": "2025-01-01T00:00:00+00:00"}
    conn = feeds._get_feeds_db()
    first = feeds._store_items(conn, sid, "podcast", [item])
    second = feeds._store_items(conn, sid, "podcast", [dict(item)])
    conn.close()
    assert first == 1
    assert second == 0, "identical URL-less items must dedupe (B7)"


def test_url_less_items_with_distinct_content_are_kept(db):
    path, sid = db
    conn = feeds._get_feeds_db()
    n = feeds._store_items(
        conn, sid, "podcast",
        [
            {"title": "A", "content": "a", "published": "2025-01-01T00:00:00+00:00"},
            {"title": "B", "content": "b", "published": "2025-01-02T00:00:00+00:00"},
        ],
    )
    conn.close()
    assert n == 2, "distinct URL-less items must both be stored"
