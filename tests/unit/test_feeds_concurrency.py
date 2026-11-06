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


# ─────────────────────────────────────────────────────────────────────
# Captions before the GPU, and a bounded budget
# ─────────────────────────────────────────────────────────────────────

def _enable_yt(monkeypatch):
    from google_search_mcp.utils import whisper_remote

    monkeypatch.setattr(feeds.ytdlp, "is_available", lambda: True)
    monkeypatch.setattr(whisper_remote, "is_configured", lambda: True)


@pytest.mark.asyncio
async def test_captions_are_used_before_downloading_audio(
    feed_db, tmp_path, monkeypatch
):
    """Captions cost ~6 s and no GPU; audio+Whisper costs ~60 s of GPU.

    The feed path used to skip captions entirely and go straight to the GPU
    host, so a check that could have taken seconds took tens of minutes.
    """
    _enable_yt(monkeypatch)
    monkeypatch.setattr(feeds, "TRANSCRIPT_CACHE_DIR", str(tmp_path))

    from google_search_mcp.tools import video

    # The cache must NOT exist yet, or the "needs transcription" filter would
    # skip this video before the loop ever runs.
    cache = tmp_path / "cached.json"

    async def fake_load_segments(url, model_size):
        # Mirror the real _load_segments: it writes the cache and returns the
        # segments plus a source label naming where the text came from.
        video._format_and_cache_transcript(
            url=url, cache_path=str(cache), title="T", uploader="U",
            duration=60.0, language="en", language_note="manual captions",
            segments=[{"start": 0, "end": 1, "text": "hi"}],
            source_label="captions (manual, en)",
        )
        return [{"start": 0, "end": 1, "text": "hi"}], "T", "captions (manual, en)"

    monkeypatch.setattr(video, "_load_segments", fake_load_segments)
    monkeypatch.setattr(feeds, "_transcript_cache_path", lambda url, ms: str(cache))
    monkeypatch.setattr(
        feeds, "_download_audio",
        lambda *a, **k: pytest.fail("must not download audio when captions exist"),
    )
    monkeypatch.setattr(
        feeds, "_transcribe_audio",
        lambda *a, **k: pytest.fail("must not use the GPU when captions exist"),
    )

    conn = feeds._get_feeds_db()
    try:
        note = await feeds._auto_transcribe_youtube(
            conn, 1, [{"url": "https://www.youtube.com/watch?v=abcdefghijk"}],
        )
    finally:
        conn.close()

    assert "1 transcribed" in note


@pytest.mark.asyncio
async def test_transcription_stops_when_budget_is_spent(feed_db, tmp_path, monkeypatch):
    """check_feeds must return in bounded time.

    Without a budget, 6 channels x 5 videos x ~60 s is half an hour and the
    MCP client gives up long before the call returns. Each video finished here
    is cached, so the leftovers are picked up cheaply on the next check.
    """
    _enable_yt(monkeypatch)
    monkeypatch.setattr(feeds, "TRANSCRIBE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        feeds, "_transcript_cache_path",
        lambda url, ms: str(tmp_path / f"{url.rsplit('=', 1)[-1]}.json"),
    )

    from google_search_mcp.tools import video

    items = [
        {"url": f"https://www.youtube.com/watch?v=video{i:07d}"} for i in range(5)
    ]
    calls = {"load": 0, "download": 0}

    async def fake_load(url, model_size):
        calls["load"] += 1
        time.sleep(0.6)  # two of these already exceed the 1 s budget
        return None, "T", ""  # no captions available

    def fake_download(url, cache_dir):
        calls["download"] += 1
        raise RuntimeError("no audio")  # keeps the test off the GPU and disk

    monkeypatch.setattr(video, "_load_segments", fake_load)
    monkeypatch.setattr(feeds, "_download_audio", fake_download)
    monkeypatch.setattr(feeds.config, "FEEDS_TRANSCRIBE_BUDGET_SEC", 1)

    conn = feeds._get_feeds_db()
    try:
        note = await feeds._auto_transcribe_youtube(conn, 1, items)
    finally:
        conn.close()

    assert calls["load"] < len(items), "budget must cut the batch short"
    assert "queued for next check" in note


@pytest.mark.parametrize(
    "message, deferred",
    [
        ("ERROR: [youtube] J8EYTcGPftA: This live event will begin in a few moments.", True),
        ("ERROR: [youtube] x: Premieres in 3 hours", True),
        ("ERROR: [youtube] x: This live event has ended", True),
        ("ERROR: [youtube] x: Video unavailable", False),
        ("HTTP Error 429: Too Many Requests", False),
        ("This video is members-only", False),
    ],
)
def test_not_yet_available_classification(message, deferred):
    """A scheduled stream is "not started yet", not a failure.

    Reporting it as a failure made a perfectly healthy feed look broken: the
    video simply has no captions and no audio until it begins.
    """
    assert feeds._is_not_yet_available(message) is deferred


@pytest.mark.asyncio
async def test_scheduled_live_stream_is_not_counted_as_failure(
    feed_db, tmp_path, monkeypatch
):
    """End-to-end: a premiere must not produce "N failed"."""
    _enable_yt(monkeypatch)
    monkeypatch.setattr(feeds, "TRANSCRIBE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        feeds, "_transcript_cache_path", lambda url, ms: str(tmp_path / "nope.json")
    )

    from google_search_mcp.tools import video

    async def load_raises(url, model_size):
        raise RuntimeError(
            "ERROR: [youtube] x: This live event will begin in a few moments."
        )

    monkeypatch.setattr(video, "_load_segments", load_raises)

    conn = feeds._get_feeds_db()
    try:
        note = await feeds._auto_transcribe_youtube(
            conn, 1, [{"url": "https://www.youtube.com/watch?v=abcdefghijk"}],
        )
    finally:
        conn.close()

    assert "failed" not in note
    assert "not started yet" in note
