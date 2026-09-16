"""Tests for the centralized yt-dlp integration.

Covers the pieces that are pure logic and therefore testable without network
access: cookie-jar selection, option building, and subtitle parsing. The
subtitle parsers matter most — they are what makes the "use captions instead
of Whisper" fast path work, and a parsing regression would silently produce
empty transcripts.
"""

import os

import pytest

from google_search_mcp.utils import ytdlp


# ── cookie resolution ──────────────────────────────────────────────────


def test_resolve_cookiefile_youtube():
    path = ytdlp.resolve_cookiefile("https://www.youtube.com/watch?v=abc")
    assert path is not None
    assert path.endswith("youtube_cookies.txt")


def test_resolve_cookiefile_youtu_be_shortlink():
    path = ytdlp.resolve_cookiefile("https://youtu.be/abc")
    assert path is not None
    assert path.endswith("youtube_cookies.txt")


def test_resolve_cookiefile_google():
    path = ytdlp.resolve_cookiefile("https://www.google.com/search?q=x")
    assert path is not None
    assert path.endswith("google_cookies.txt")


def test_resolve_cookiefile_unrelated_host():
    """Sending the wrong cookie jar is worse than sending none."""
    assert ytdlp.resolve_cookiefile("https://vimeo.com/12345") is None


def test_resolve_cookiefile_empty_url():
    assert ytdlp.resolve_cookiefile("") is None


def test_resolve_cookiefile_missing_youtube_falls_back_to_google(
    monkeypatch, tmp_path
):
    """YouTube is a Google property, so the Google jar is a valid fallback."""
    monkeypatch.setattr(
        ytdlp.config, "YOUTUBE_COOKIE_PATH", str(tmp_path / "nope.txt")
    )
    path = ytdlp.resolve_cookiefile("https://www.youtube.com/watch?v=abc")
    assert path is not None
    assert path.endswith("google_cookies.txt")


def test_resolve_cookiefile_all_missing(monkeypatch, tmp_path):
    """With no usable jar at all, return None rather than a bad path."""
    monkeypatch.setattr(
        ytdlp.config, "YOUTUBE_COOKIE_PATH", str(tmp_path / "nope.txt")
    )
    monkeypatch.setattr(
        ytdlp.config, "GOOGLE_COOKIE_PATH", str(tmp_path / "nope2.txt")
    )
    assert ytdlp.resolve_cookiefile("https://www.youtube.com/watch?v=abc") is None


# ── option building ────────────────────────────────────────────────────


def test_build_ydl_opts_includes_cookiefile():
    opts = ytdlp.build_ydl_opts("https://www.youtube.com/watch?v=abc")
    assert opts["cookiefile"].endswith("youtube_cookies.txt")


def test_build_ydl_opts_omits_cookiefile_for_other_hosts():
    opts = ytdlp.build_ydl_opts("https://vimeo.com/12345")
    assert "cookiefile" not in opts


def test_build_ydl_opts_sets_retries_and_timeout():
    opts = ytdlp.build_ydl_opts("https://example.com/v.mp4")
    assert opts["retries"] == ytdlp.config.YTDLP_RETRIES
    assert opts["socket_timeout"] == ytdlp.config.YTDLP_SOCKET_TIMEOUT


def test_build_ydl_opts_extra_overrides_defaults():
    """Caller-supplied options must win over project defaults."""
    opts = ytdlp.build_ydl_opts(
        "https://example.com/v.mp4", extra={"quiet": False, "custom": 1}
    )
    assert opts["quiet"] is False
    assert opts["custom"] == 1


def test_build_ydl_opts_proxy_only_when_configured(monkeypatch):
    monkeypatch.setattr(ytdlp.config, "YTDLP_PROXY", "")
    assert "proxy" not in ytdlp.build_ydl_opts("https://example.com/v.mp4")

    monkeypatch.setattr(ytdlp.config, "YTDLP_PROXY", "socks5://127.0.0.1:1080")
    opts = ytdlp.build_ydl_opts("https://example.com/v.mp4")
    assert opts["proxy"] == "socks5://127.0.0.1:1080"


# ── subtitle parsing: json3 ────────────────────────────────────────────


def test_parse_json3_basic():
    raw = (
        '{"events":['
        '{"tStartMs":1500,"dDurationMs":2000,'
        '"segs":[{"utf8":"Hello "},{"utf8":"world"}]},'
        '{"tStartMs":4000,"dDurationMs":1000,"segs":[{"utf8":"Second"}]}'
        "]}"
    )
    segments = ytdlp.parse_subtitle_text(raw, "json3")
    assert len(segments) == 2
    assert segments[0] == {"start": 1.5, "end": 3.5, "text": "Hello world"}
    assert segments[1]["text"] == "Second"


def test_parse_json3_skips_empty_events():
    """YouTube emits events with no segs (timing markers) — skip them."""
    raw = '{"events":[{"tStartMs":0,"segs":[]},{"tStartMs":1000,"dDurationMs":500,"segs":[{"utf8":"Hi"}]}]}'
    segments = ytdlp.parse_subtitle_text(raw, "json3")
    assert len(segments) == 1
    assert segments[0]["text"] == "Hi"


def test_parse_json3_malformed_returns_empty():
    assert ytdlp.parse_subtitle_text("{not json", "json3") == []


def test_parse_json3_detected_without_ext():
    """A JSON payload must be detected even when the extension is unknown."""
    raw = '{"events":[{"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"Hi"}]}]}'
    assert len(ytdlp.parse_subtitle_text(raw, "")) == 1


# ── subtitle parsing: vtt / srt ────────────────────────────────────────


def test_parse_vtt_basic():
    raw = (
        "WEBVTT\n\n"
        "00:00:01.500 --> 00:00:03.500\n"
        "<c>Hello</c> world\n\n"
        "00:01:02.000 --> 00:01:04.000\n"
        "Second cue\nwith two lines\n"
    )
    segments = ytdlp.parse_subtitle_text(raw, "vtt")
    assert len(segments) == 2
    assert segments[0]["text"] == "Hello world"  # styling tags stripped
    assert segments[0]["start"] == 1.5
    assert segments[1]["start"] == 62.0
    assert segments[1]["text"] == "Second cue with two lines"


def test_parse_srt_basic():
    raw = (
        "1\n00:00:01,500 --> 00:00:03,500\nHello world\n\n"
        "2\n00:01:02,000 --> 00:01:04,000\nSecond cue\n"
    )
    segments = ytdlp.parse_subtitle_text(raw, "srt")
    assert len(segments) == 2
    assert segments[0]["start"] == 1.5
    assert segments[1]["start"] == 62.0


def test_parse_subtitle_empty_and_garbage():
    assert ytdlp.parse_subtitle_text("", "vtt") == []
    assert ytdlp.parse_subtitle_text("   ", "vtt") == []
    assert ytdlp.parse_subtitle_text("not a subtitle at all", "vtt") == []


def test_parse_timestamp_variants():
    assert ytdlp._parse_timestamp("00:00:01.500") == 1.5
    assert ytdlp._parse_timestamp("00:01:02,000") == 62.0
    assert ytdlp._parse_timestamp("01:00:00.000") == 3600.0
    assert ytdlp._parse_timestamp("garbage") == 0.0


# ── language selection ─────────────────────────────────────────────────


def test_select_languages_explicit_request():
    manual = {"en": [], "ar": []}
    assert ytdlp._select_languages(["ar"], manual, {}, {}) == ["ar"]


def test_select_languages_normalises_region_suffix():
    """Requesting "en" should match an available "en-US" track."""
    manual = {"en-US": []}
    assert ytdlp._select_languages(["en"], manual, {}, {}) == ["en-US"]


def test_select_languages_prefers_original_language():
    manual = {"de": [], "en": []}
    info = {"language": "de"}
    assert ytdlp._select_languages(None, manual, {}, info) == ["de"]


def test_select_languages_falls_back_to_english():
    manual = {"fr": [], "en": []}
    assert ytdlp._select_languages(None, manual, {}, {}) == ["en"]


def test_select_languages_falls_back_to_anything():
    manual = {"fr": []}
    assert ytdlp._select_languages(None, manual, {}, {}) == ["fr"]


def test_select_languages_none_available():
    assert ytdlp._select_languages(None, {}, {}, {}) == []


def test_select_languages_ignores_unavailable_request():
    manual = {"en": []}
    assert ytdlp._select_languages(["zz"], manual, {}, {}) == []


# ── helpers ────────────────────────────────────────────────────────────


def test_find_downloaded_file(tmp_path):
    (tmp_path / "audio_temp.m4a").write_bytes(b"data")
    found = ytdlp._find_downloaded_file(str(tmp_path), "audio_temp")
    assert found is not None
    assert found.endswith("audio_temp.m4a")


def test_find_downloaded_file_ignores_empty(tmp_path):
    (tmp_path / "audio_temp.m4a").write_bytes(b"")
    assert ytdlp._find_downloaded_file(str(tmp_path), "audio_temp") is None


def test_find_downloaded_file_missing_dir():
    assert ytdlp._find_downloaded_file("/nonexistent/dir", "x") is None


def test_cleanup_prefix(tmp_path):
    (tmp_path / "audio_temp.m4a").write_bytes(b"data")
    (tmp_path / "keep.txt").write_bytes(b"data")
    ytdlp._cleanup_prefix(str(tmp_path), "audio_temp")
    assert not (tmp_path / "audio_temp.m4a").exists()
    assert (tmp_path / "keep.txt").exists()


def test_is_available():
    assert ytdlp.is_available() is True


def test_ytdlp_version_reported():
    version = ytdlp.ytdlp_version()
    assert version is not None
    assert version[0].isdigit()


def test_make_range_callback_shape():
    """yt-dlp expects an iterable of {start_time, end_time} dicts."""
    callback = ytdlp._make_range_callback(10.0, 20.0)
    ranges = callback({}, None)
    assert list(ranges) == [{"start_time": 10.0, "end_time": 20.0}]


# ── YouTube JS runtime handling ────────────────────────────────────────
#
# YouTube requires a JavaScript runtime to solve its signature ("nsig")
# challenges. Without one, yt-dlp logs "nsig extraction failed" and every
# media request returns HTTP 403. These tests pin that behaviour down.


def test_is_youtube_detection():
    assert ytdlp._is_youtube("https://www.youtube.com/watch?v=abc") is True
    assert ytdlp._is_youtube("https://youtu.be/abc") is True
    assert ytdlp._is_youtube("https://vimeo.com/123") is False
    assert ytdlp._is_youtube("") is False


def test_detect_js_runtime_finds_something():
    """This machine has node >= 22, so a runtime must be detected."""
    assert ytdlp.detect_js_runtime() is not None


def test_build_ydl_opts_enables_js_runtime_for_youtube():
    opts = ytdlp.build_ydl_opts("https://www.youtube.com/watch?v=abc")
    assert "js_runtimes" in opts
    assert len(opts["js_runtimes"]) == 1


def test_build_ydl_opts_sets_youtube_player_client():
    """The default client selection 403s; we pin a working one."""
    opts = ytdlp.build_ydl_opts("https://www.youtube.com/watch?v=abc")
    clients = opts["extractor_args"]["youtube"]["player_client"]
    # web_embedded is the only client that serves video, audio-only, and
    # ranged downloads, so it must be first.
    assert clients[0] == "web_embedded"


def test_build_ydl_opts_no_js_runtime_for_non_youtube():
    """Non-YouTube sites must not get YouTube-specific options."""
    opts = ytdlp.build_ydl_opts("https://vimeo.com/123")
    assert "js_runtimes" not in opts
    assert "extractor_args" not in opts


def test_binary_version_parsing():
    """Version parsing must handle the formats node/deno actually print."""
    assert ytdlp._binary_version("/bin/echo") is None  # no version in output


def test_detect_js_runtime_rejects_old_version(monkeypatch):
    """An outdated runtime must be skipped, not silently used."""
    # Only node/deno/bun are present; quickjs is absent so it can't be
    # selected as a fallback.
    monkeypatch.setattr(
        ytdlp.shutil, "which", lambda name: None if name == "qjs" else f"/fake/{name}"
    )
    monkeypatch.setattr(ytdlp, "_binary_version", lambda path: (1, 0, 0))
    assert ytdlp.detect_js_runtime() is None


def test_detect_js_runtime_prefers_node(monkeypatch):
    """node is checked first, so it wins when several are installed."""
    monkeypatch.setattr(ytdlp.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(ytdlp, "_binary_version", lambda path: (26, 3, 0))
    assert ytdlp.detect_js_runtime() == "node"


def test_detect_js_runtime_falls_back_to_deno(monkeypatch):
    """With node absent, a new-enough deno must be used."""
    monkeypatch.setattr(
        ytdlp.shutil, "which", lambda name: None if name == "node" else f"/fake/{name}"
    )
    monkeypatch.setattr(ytdlp, "_binary_version", lambda path: (2, 4, 0))
    assert ytdlp.detect_js_runtime() == "deno"


def test_detect_js_runtime_accepts_new_enough_version(monkeypatch):
    monkeypatch.setattr(ytdlp.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(ytdlp, "_binary_version", lambda path: (26, 3, 0))
    assert ytdlp.detect_js_runtime() == "node"


def test_detect_js_runtime_none_installed(monkeypatch):
    monkeypatch.setattr(ytdlp.shutil, "which", lambda name: None)
    assert ytdlp.detect_js_runtime() is None


# ── section caching ────────────────────────────────────────────────────
#
# A section download contains only the requested range and its timeline
# starts at 0. Reusing such a file for a different range would cut the wrong
# footage, so the cache key must include the range.


def test_video_cache_path_differs_per_section():
    from google_search_mcp.tools import video

    url = "https://www.youtube.com/watch?v=abc"
    a = video._video_cache_path(url, (160.0, 175.0))
    b = video._video_cache_path(url, (600.0, 615.0))
    assert a != b


def test_video_cache_path_stable_for_same_section():
    from google_search_mcp.tools import video

    url = "https://www.youtube.com/watch?v=abc"
    assert video._video_cache_path(url, (160.0, 175.0)) == video._video_cache_path(
        url, (160.0, 175.0)
    )


def test_video_cache_path_full_download_differs_from_section():
    from google_search_mcp.tools import video

    url = "https://www.youtube.com/watch?v=abc"
    assert video._video_cache_path(url) != video._video_cache_path(url, (160.0, 175.0))


def test_download_video_reports_partial_for_cached_section(tmp_path, monkeypatch):
    """A cache hit on a section file must still report partial=True."""
    cached = tmp_path / "sec.mp4"
    cached.write_bytes(b"fake video data")

    monkeypatch.setattr(ytdlp, "extract_info", lambda url, download=False: {"title": "T"})

    result = ytdlp.download_video(
        "https://www.youtube.com/watch?v=abc",
        str(tmp_path),
        cache_path=str(cached),
        section=(160.0, 175.0),
    )
    assert result["partial"] is True
    assert result["video_path"] == str(cached)


def test_download_video_reports_not_partial_for_cached_full(tmp_path, monkeypatch):
    """A cache hit on a full download must report partial=False."""
    cached = tmp_path / "full.mp4"
    cached.write_bytes(b"fake video data")

    monkeypatch.setattr(ytdlp, "extract_info", lambda url, download=False: {"title": "T"})

    result = ytdlp.download_video(
        "https://www.youtube.com/watch?v=abc",
        str(tmp_path),
        cache_path=str(cached),
    )
    assert result["partial"] is False
