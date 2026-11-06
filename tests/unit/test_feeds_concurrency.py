"""Phase 8 tests: concurrent ``check_feeds`` with per-source isolation.

A batch of feeds must be refreshed in parallel (bounded by
``NOAPI_MCP_FEEDS_CONCURRENCY``) and one failing/hanging source must not abort
the others.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from google_search_mcp.tools import feeds

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
{items}
</channel></rss>"""


def _rss(n: int, prefix: str = "item") -> bytes:
    items = "".join(
        f"<item><title>{prefix}{i}</title>"
        f"<link>https://example.invalid/{prefix}/{i}</link>"
        f"<pubDate>Mon, 06 Jan 2025 12:0{i}:00 GMT</pubDate></item>"
        for i in range(n)
    )
    return RSS.format(items=items).encode()


@pytest.fixture()
def feed_db(tmp_path, monkeypatch):
    db = tmp_path / "feeds.db"
    monkeypatch.setenv("FEEDS_DB_PATH", str(db))
    return db


def _count(db, source_type=None):
    conn = sqlite3.connect(db)
    if source_type:
        n = conn.execute(
            "SELECT COUNT(*) FROM feed_items WHERE source_type = ?", (source_type,)
        ).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0]
    conn.close()
    return n


def test_check_feeds_runs_concurrently(feed_db, monkeypatch):
    """Two slow feeds must be fetched with overlapping time windows.

    Asserting *overlap* (rather than a wall-clock threshold) proves genuine
    concurrency without being sensitive to the fixed overhead of `subscribe`,
    DB initialisation and item parsing.
    """
    delay = 0.35
    intervals: list[tuple[float, float]] = []

    def fake_fetch(url, *args, **kwargs):  # noqa: ARG001
        start = time.monotonic()
        time.sleep(delay)
        intervals.append((start, time.monotonic()))
        return _rss(1, prefix=url.rsplit("/", 1)[-1][:3])

    monkeypatch.setattr(feeds, "_fetch_url_bytes", fake_fetch)

    async def setup_and_run():
        await feeds.subscribe("news", "https://example.invalid/aaa.xml")
        await feeds.subscribe("news", "https://example.invalid/bbb.xml")
        return await feeds.check_feeds()

    out = asyncio.run(setup_and_run())

    assert len(intervals) == 2, f"expected 2 fetches, got {len(intervals)}"
    (a1, b1), (a2, b2) = intervals
    overlap = min(b1, b2) - max(a1, a2)
    assert overlap > delay / 2, (
        f"feeds were fetched serially (overlap {overlap:.3f}s; "
        f"windows {intervals})"
    )
    assert out.count("1 new items") == 2
    assert _count(feed_db) == 2


def test_check_feeds_isolates_failures(feed_db, monkeypatch):
    def fake_fetch(url, *args, **kwargs):  # noqa: ARG001
        if "bad" in url:
            raise RuntimeError("boom")
        return _rss(2, prefix="ok")

    monkeypatch.setattr(feeds, "_fetch_url_bytes", fake_fetch)

    async def setup_and_run():
        await feeds.subscribe("news", "https://example.invalid/good.xml", name="Good")
        await feeds.subscribe("news", "https://example.invalid/bad.xml", name="Bad")
        return await feeds.check_feeds()

    out = asyncio.run(setup_and_run())
    assert "Good: 2 new items" in out
    assert "Bad: ERROR — boom" in out
    # The healthy source's items were still stored.
    assert _count(feed_db) == 2
    assert "Total: 2 new items across 2 sources" in out


def test_check_feeds_respects_concurrency_limit(feed_db, monkeypatch):
    """With concurrency 2 and 4 sources, at most 2 fetches run at once."""
    monkeypatch.setattr(feeds.config, "FEEDS_CONCURRENCY", 2)
    active = {"now": 0, "peak": 0}

    def fake_fetch(url, *args, **kwargs):  # noqa: ARG001
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.15)
        active["now"] -= 1
        return _rss(1)

    monkeypatch.setattr(feeds, "_fetch_url_bytes", fake_fetch)

    async def setup_and_run():
        for i in range(4):
            await feeds.subscribe("news", f"https://example.invalid/f{i}.xml")
        return await feeds.check_feeds()

    asyncio.run(setup_and_run())
    assert active["peak"] <= 2, f"concurrency cap exceeded (peak={active['peak']})"


def test_check_feeds_no_subscriptions(feed_db):
    assert "No subscriptions to check" in asyncio.run(feeds.check_feeds())


# ─────────────────────────────────────────────────────────────────────
# Thread affinity of the per-subscription connection
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_transcribe_connection_is_created_on_calling_thread(monkeypatch):
    """The connection must be born on the thread that uses it.

    Regression: the connection used to be opened with ``asyncio.to_thread``,
    so it lived on a pool thread while every ``conn.execute()`` ran on the
    event loop thread. sqlite3 refuses that ("SQLite objects created in a
    thread can only be used in that same thread"), and because the error also
    fired from ``conn.close()`` it hit even subscriptions whose transcription
    returned early — every YouTube feed reported ERROR and nothing was stored.
    """
    import threading

    creation_thread: list[int] = []
    real_get_db = feeds._get_feeds_db

    def recording_get_db():
        creation_thread.append(threading.get_ident())
        return real_get_db()

    monkeypatch.setattr(feeds, "_get_feeds_db", recording_get_db)

    async def fake_transcribe(conn, sub_id, items, ctx=None):
        # sqlite3 would raise here if the connection came from another thread.
        conn.execute("SELECT 1").fetchone()
        return ""

    monkeypatch.setattr(feeds, "_auto_transcribe_youtube", fake_transcribe)

    await feeds._auto_transcribe_youtube_isolated(1, [])

    assert creation_thread == [threading.get_ident()], (
        "connection must be created on the event-loop thread"
    )


@pytest.mark.asyncio
async def test_auto_transcribe_isolated_closes_without_thread_error(monkeypatch):
    """Closing must not raise, including on the early-return path."""
    async def early_return(conn, sub_id, items, ctx=None):
        # Simulates the "remote not configured" short-circuit: conn untouched.
        return " (auto-transcription skipped)"

    monkeypatch.setattr(feeds, "_auto_transcribe_youtube", early_return)

    note = await feeds._auto_transcribe_youtube_isolated(1, [])

    assert note == " (auto-transcription skipped)"


@pytest.mark.asyncio
async def test_failed_transcription_still_removes_downloaded_audio(
    feed_db, tmp_path, monkeypatch
):
    """A failed transcription must not orphan its downloaded audio.

    Regression: cleanup sat on the success path only, so every failure left a
    full audio download behind. When an undecodable-codec bug made every
    transcription fail, the cache accumulated more than 3 GB of audio that no
    code path would ever revisit.
    """
    from google_search_mcp.utils import whisper_remote

    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"\x00" * 1024)

    monkeypatch.setattr(feeds.ytdlp, "is_available", lambda: True)
    monkeypatch.setattr(whisper_remote, "is_configured", lambda: True)
    monkeypatch.setattr(
        feeds, "_download_audio",
        lambda url, cache_dir: {
            "title": "T", "duration": 5, "uploader": "U",
            "audio_path": str(audio),
        },
    )

    def boom(*args, **kwargs):
        raise RuntimeError("remote rejected the upload")

    monkeypatch.setattr(feeds, "_transcribe_audio", boom)

    conn = feeds._get_feeds_db()
    try:
        note = await feeds._auto_transcribe_youtube(
            conn, 1, [{"url": "https://www.youtube.com/watch?v=abcdefghijk"}],
        )
    finally:
        conn.close()

    assert "failed" in note
    assert not audio.exists(), "audio must be removed even when transcription fails"
