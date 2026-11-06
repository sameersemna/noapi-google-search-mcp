"""YouTube triage, caption and discovery tools (yt-dlp, no GPU).

What is covered
---------------
* The metadata cache: one ``extract_info`` per URL instead of one per call,
  expiry, bounded size, and — critically — failures are never cached.
* ``get_captions``: full text, query filtering with timestamps, no-caption
  handling, the ``include_automatic=False`` case, and that it seeds the
  transcript cache so ``search_transcript`` works afterwards.
* ``search_transcript``: falls back to captions instead of refusing.
* ``youtube_search`` / ``list_channel_videos``: flat listing and URL form.
* A guard that none of these touch the GPU host (``_transcribe_audio``).

No test here hits YouTube: every network boundary is stubbed, because the live
suite is where real requests belong and because YouTube's auth wall is
intermittent enough to make live assertions flaky.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from google_search_mcp.tools import youtube
from google_search_mcp.utils import ytdlp

# ── metadata cache ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_cache():
    ytdlp.clear_info_cache()
    yield
    ytdlp.clear_info_cache()


def _fake_ydl(monkeypatch, calls: list, result: dict | None = None):
    """Stub yt_dlp.YoutubeDL so extract_info is counted, never executed."""

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            calls.append(url)
            return result if result is not None else {"title": "T", "duration": 10}

    class FakeModule:
        YoutubeDL = FakeYDL

    monkeypatch.setattr(ytdlp, "_require_ytdlp", lambda: FakeModule)
    return FakeModule


def test_extract_info_is_cached(monkeypatch):
    calls: list = []
    _fake_ydl(monkeypatch, calls)

    a = ytdlp.extract_info("https://youtu.be/x")
    b = ytdlp.extract_info("https://youtu.be/x")

    assert len(calls) == 1, "the second call must come from the cache"
    assert a["title"] == b["title"] == "T"


def test_extract_info_fresh_bypasses_the_cache(monkeypatch):
    calls: list = []
    _fake_ydl(monkeypatch, calls)
    ytdlp.extract_info("https://youtu.be/x")
    ytdlp.extract_info("https://youtu.be/x", fresh=True)
    assert len(calls) == 2


def test_extract_info_does_not_cache_failures(monkeypatch):
    """A transient failure must not poison the cache for 10 minutes."""
    calls: list = []

    class BoomYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            calls.append(url)
            raise RuntimeError("temporary failure")

    monkeypatch.setattr(ytdlp, "_require_ytdlp", lambda: type("M", (), {"YoutubeDL": BoomYDL}))

    for _ in range(2):
        with pytest.raises(RuntimeError):
            ytdlp.extract_info("https://youtu.be/y")
    assert len(calls) == 2, "a failure must be retried, not cached"


def test_extract_info_does_not_cache_empty_results(monkeypatch):
    calls: list = []
    _fake_ydl(monkeypatch, calls, result={})
    ytdlp.extract_info("https://youtu.be/z")
    ytdlp.extract_info("https://youtu.be/z")
    assert len(calls) == 2


def test_cache_expires(monkeypatch):
    calls: list = []
    _fake_ydl(monkeypatch, calls)
    monkeypatch.setattr(ytdlp, "_INFO_TTL_SEC", 0.0)

    ytdlp.extract_info("https://youtu.be/x")
    ytdlp.extract_info("https://youtu.be/x")
    assert len(calls) == 2


def test_cache_is_bounded(monkeypatch):
    calls: list = []
    _fake_ydl(monkeypatch, calls)
    monkeypatch.setattr(ytdlp, "_INFO_MAX_ENTRIES", 3)

    for i in range(10):
        ytdlp.extract_info(f"https://youtu.be/v{i}")
    with ytdlp._info_lock:
        assert len(ytdlp._info_cache) <= 3


# ── get_captions ──────────────────────────────────────────────────────────

SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Welcome to the talk"},
    {"start": 4.0, "end": 9.0, "text": "Today we cover memory bandwidth"},
    {"start": 9.0, "end": 14.0, "text": "which limits inference speed"},
    {"start": 14.0, "end": 20.0, "text": "Thanks for watching"},
]


def _captions(source: str = "manual", lang: str = "en") -> dict:
    return {
        "title": "A Talk",
        "duration": 20,
        "uploader": "Someone",
        "tracks": {
            lang: {
                "source": source,
                "ext": "json3",
                "segments": SEGMENTS,
                "text": "\n".join(f"[{s['start']:.0f}s] {s['text']}" for s in SEGMENTS),
            }
        },
    }


@pytest.fixture
def stub_captions(monkeypatch):
    """Stub the caption fetch and the transcript-cache seeding."""
    state = {"calls": [], "seeded": []}

    def fake_download(url, languages=None, include_automatic=True, **kw):
        state["calls"].append((url, languages, include_automatic))
        return _captions()

    monkeypatch.setattr(ytdlp, "is_available", lambda: True)
    monkeypatch.setattr(ytdlp, "download_subtitles", fake_download)
    monkeypatch.setattr(ytdlp, "list_subtitles", lambda url: {"manual": {"en": "json3"}, "automatic": {}})

    def fake_seed(url, captions, lang, track):
        state["seeded"].append((url, lang))

    async def async_seed(url, captions, lang, track):
        fake_seed(url, captions, lang, track)

    monkeypatch.setattr(youtube, "_seed_transcript_cache", async_seed)
    return state


def test_get_captions_returns_text_with_source_label(stub_captions):
    out = asyncio.run(youtube.get_captions("https://youtu.be/x"))
    assert "A Talk" in out
    assert "captions (manual, en)" in out
    assert "memory bandwidth" in out
    assert "search_transcript" in out


def test_get_captions_query_returns_passages_and_timestamps(stub_captions):
    out = asyncio.run(youtube.get_captions("https://youtu.be/x", query="memory bandwidth"))
    assert "memory bandwidth" in out
    assert "start_seconds=" in out, "clip extraction needs the numeric timestamps"
    assert ">>> " in out, "the matching line must be marked"
    assert "Welcome to the talk" in out, "surrounding context should be included"


def test_get_captions_query_miss_is_explained(stub_captions):
    out = asyncio.run(youtube.get_captions("https://youtu.be/x", query="quantum chromodynamics"))
    assert "No mention" in out
    assert "broader keyword" in out


def test_get_captions_seeds_the_transcript_cache(stub_captions):
    """So search_transcript / extract_video_clip work without transcribing."""
    asyncio.run(youtube.get_captions("https://youtu.be/x"))
    assert stub_captions["seeded"] == [("https://youtu.be/x", "en")]


def test_get_captions_passes_language_selection(monkeypatch, stub_captions):
    asyncio.run(youtube.get_captions("https://youtu.be/x", languages="de, en"))
    _url, langs, _auto = stub_captions["calls"][0]
    assert langs == ["de", "en"]


def test_get_captions_respects_max_characters(monkeypatch, stub_captions):
    out = asyncio.run(youtube.get_captions("https://youtu.be/x", max_characters=20))
    assert "truncated" in out


def test_get_captions_without_captions_points_at_transcription(monkeypatch):
    monkeypatch.setattr(ytdlp, "is_available", lambda: True)
    monkeypatch.setattr(
        ytdlp, "download_subtitles",
        lambda *a, **k: {"title": "No Subs", "tracks": {}},
    )
    monkeypatch.setattr(ytdlp, "list_subtitles", lambda url: {"manual": {}, "automatic": {}})
    out = asyncio.run(youtube.get_captions("https://youtu.be/x"))
    assert "No captions available" in out
    assert "transcribe_video" in out


def test_get_captions_automatic_disabled_is_explained(monkeypatch):
    monkeypatch.setattr(ytdlp, "is_available", lambda: True)
    monkeypatch.setattr(
        ytdlp, "download_subtitles",
        lambda *a, **k: {"title": "Auto Only", "tracks": {}},
    )
    monkeypatch.setattr(
        ytdlp, "list_subtitles",
        lambda url: {"manual": {}, "automatic": ["en", "de"]},
    )
    out = asyncio.run(youtube.get_captions("https://youtu.be/x", include_automatic=False))
    assert "auto-generated" in out
    assert "include_automatic=True" in out


# ── youtube_video_info ────────────────────────────────────────────────────

INFO = {
    "title": "Deep Dive",
    "uploader": "A Channel",
    "duration": 3600,
    "view_count": 1234567,
    "upload_date": "20260115",
    "description": "A long talk about storage engines and throughput.",
    "subtitles": {"en": [{"ext": "json3"}]},
    "automatic_captions": {"de": [{"ext": "json3"}], "fr": [{"ext": "json3"}]},
    "chapters": [
        {"start_time": 0, "end_time": 600, "title": "Intro"},
        {"start_time": 600, "end_time": 1800, "title": "Storage engines"},
    ],
    "webpage_url": "https://www.youtube.com/watch?v=abc",
}


def _stub_info(monkeypatch, info=INFO):
    monkeypatch.setattr(ytdlp, "is_available", lambda: True)
    monkeypatch.setattr(ytdlp, "extract_info", lambda url, **kw: info)


def test_video_info_reports_chapters_and_captions(monkeypatch):
    _stub_info(monkeypatch)
    out = asyncio.run(youtube.youtube_video_info("https://www.youtube.com/watch?v=abc"))
    assert "Deep Dive" in out
    assert "A Channel" in out
    assert "1:00:00" in out, "duration should be human readable"
    assert "1,234,567" in out, "view counts should be grouped"
    assert "2026-01-15" in out
    assert "Storage engines" in out and "start_seconds=600" in out
    assert "human captions" in out


def test_video_info_says_when_only_auto_captions_exist(monkeypatch):
    info = dict(INFO, subtitles={})
    _stub_info(monkeypatch, info)
    out = asyncio.run(youtube.youtube_video_info("https://www.youtube.com/watch?v=abc"))
    assert "auto-generated captions" in out
    assert "transcribe_video when exact wording matters" in out


def test_video_info_says_when_no_captions_exist(monkeypatch):
    info = dict(INFO, subtitles={}, automatic_captions={}, chapters=[])
    _stub_info(monkeypatch, info)
    out = asyncio.run(youtube.youtube_video_info("https://www.youtube.com/watch?v=abc"))
    assert "no captions" in out
    assert "Chapters: none published" in out


def test_video_info_reports_the_auth_wall_actionably(monkeypatch):
    monkeypatch.setattr(ytdlp, "is_available", lambda: True)

    def boom(url, **kw):
        raise RuntimeError(
            "ERROR: [youtube] x: Sign in to confirm you're not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication."
        )

    monkeypatch.setattr(ytdlp, "extract_info", boom)
    out = asyncio.run(youtube.youtube_video_info("https://www.youtube.com/watch?v=abc"))
    assert "cookies" in out.lower()
    assert "try again" not in out.casefold(), "retrying cannot fix an auth wall"


# ── discovery ─────────────────────────────────────────────────────────────


def test_youtube_search_lists_videos(monkeypatch):
    monkeypatch.setattr(ytdlp, "is_available", lambda: True)
    monkeypatch.setattr(
        ytdlp, "search_videos",
        lambda q, n: [
            {"title": "First", "url": "u1", "duration": 90, "channel": "C1", "view_count": 5},
            {"title": "Second", "url": "u2", "duration": 3600, "channel": "C2"},
        ],
    )
    out = asyncio.run(youtube.youtube_search("storage engines"))
    assert "First" in out and "Second" in out
    assert "[1:30]" in out and "[1:00:00]" in out
    assert "storage engines" in out


def test_youtube_search_clamps_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr(ytdlp, "is_available", lambda: True)

    def fake(q, n):
        seen["n"] = n
        return []

    monkeypatch.setattr(ytdlp, "search_videos", fake)
    asyncio.run(youtube.youtube_search("x", limit=999))
    assert seen["n"] == 25, "limit must be clamped to the documented maximum"


def test_channel_target_normalisation():
    assert youtube._channel_target("@3Blue1Brown").endswith("/@3Blue1Brown/videos")
    assert youtube._channel_target("Blender").endswith("/@Blender/videos")
    assert youtube._channel_target("UC" + "x" * 22).endswith("/channel/UC" + "x" * 22 + "/videos")
    url = "https://www.youtube.com/playlist?list=PL1"
    assert youtube._channel_target(url) == url
    with pytest.raises(ValueError):
        youtube._channel_target("   ")


def test_list_channel_videos_renders_entries(monkeypatch):
    monkeypatch.setattr(ytdlp, "is_available", lambda: True)
    monkeypatch.setattr(
        ytdlp, "list_entries",
        lambda t, n: {"title": "A Channel - Videos",
                      "entries": [{"title": "Ep 1", "url": "u1", "duration": 600}]},
    )
    out = asyncio.run(youtube.list_channel_videos("@Someone"))
    assert "Ep 1" in out and "[10:00]" in out


def test_list_channel_videos_explains_empty_result(monkeypatch):
    monkeypatch.setattr(ytdlp, "is_available", lambda: True)
    monkeypatch.setattr(ytdlp, "list_entries", lambda t, n: {"title": "", "entries": []})
    out = asyncio.run(youtube.list_channel_videos("@Nobody"))
    assert "No videos found" in out
    assert "featured" in out, "explain the common reason rather than just failing"


# ── no GPU involvement ────────────────────────────────────────────────────


def test_no_youtube_tool_transcribes():
    """These tools must never reach the Whisper host."""
    import inspect

    src = inspect.getsource(youtube)
    assert "_transcribe_audio" not in src
    assert "whisper_remote" not in src
    assert "download_audio" not in src, "no audio download either"


def test_no_media_download_in_discovery(monkeypatch):
    """Discovery must stay flat: skip_download + extract_flat on every request."""
    seen = {}

    class FakeYDL:
        def __init__(self, opts):
            seen.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return {"title": "T", "entries": [{"title": "e", "url": "v1", "duration": 1}]}

    monkeypatch.setattr(ytdlp, "_require_ytdlp", lambda: type("M", (), {"YoutubeDL": FakeYDL}))
    ytdlp.list_entries("https://www.youtube.com/@x/videos", 3)

    assert seen.get("skip_download") is True
    assert seen.get("extract_flat") == "in_playlist"
    assert seen.get("playlistend") == 3
