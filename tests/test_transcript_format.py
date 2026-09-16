"""Tests for the shared transcript formatter.

Both the caption path and the Whisper path funnel through
``_format_and_cache_transcript`` so that ``search_transcript`` can read the
cached ``segments`` regardless of which source produced them. These tests pin
that contract down, plus the long-video condensation behaviour.
"""

import json

import pytest

from google_search_mcp.tools import video


def _segments(count: int, step: float = 5.0) -> list[dict]:
    return [
        {"start": i * step, "end": i * step + step, "text": f"segment {i}"}
        for i in range(count)
    ]


def test_formats_header_and_segments(tmp_path):
    cache = tmp_path / "t.json"
    out = video._format_and_cache_transcript(
        url="https://example.com/v",
        cache_path=str(cache),
        title="My Video",
        uploader="My Channel",
        duration=120.0,
        language="en",
        language_note="manual captions",
        segments=_segments(3),
    )

    assert "Title: My Video" in out
    assert "Channel: My Channel" in out
    assert "Duration: 2:00" in out
    assert "Language: en (manual captions)" in out
    assert "URL: https://example.com/v" in out
    assert "[0:00 - 0:05] segment 0" in out
    assert "Total segments: 3" in out


def test_writes_cache_with_segments(tmp_path):
    """search_transcript depends on the cached `segments` key."""
    cache = tmp_path / "t.json"
    video._format_and_cache_transcript(
        url="https://example.com/v",
        cache_path=str(cache),
        title="T",
        uploader="U",
        duration=60.0,
        language="en",
        language_note="x",
        segments=_segments(2),
    )

    data = json.loads(cache.read_text())
    assert data["url"] == "https://example.com/v"
    assert data["title"] == "T"
    assert len(data["segments"]) == 2
    assert "transcript" in data


def test_short_video_returns_full_transcript(tmp_path):
    cache = tmp_path / "t.json"
    out = video._format_and_cache_transcript(
        url="u",
        cache_path=str(cache),
        title="T",
        uploader="U",
        duration=300.0,  # under the 600s threshold
        language="en",
        language_note="x",
        segments=_segments(20),
    )
    assert "--- Transcript ---" in out
    assert "condensed" not in out
    assert "segment 19" in out


def test_long_video_returns_condensed_preview(tmp_path):
    """A long video must not flood the model's context."""
    cache = tmp_path / "t.json"
    out = video._format_and_cache_transcript(
        url="u",
        cache_path=str(cache),
        title="T",
        uploader="U",
        duration=1200.0,  # over the 600s threshold
        language="en",
        language_note="x",
        segments=_segments(100),
    )
    assert "condensed" in out
    assert "--- First 15 segments ---" in out
    assert "--- Last 15 segments ---" in out
    assert "search_transcript" in out
    # Middle segments must be omitted from the preview.
    assert "segment 50" not in out


def test_long_video_still_caches_full_transcript(tmp_path):
    """Condensation is presentation-only; the cache keeps everything."""
    cache = tmp_path / "t.json"
    video._format_and_cache_transcript(
        url="u",
        cache_path=str(cache),
        title="T",
        uploader="U",
        duration=1200.0,
        language="en",
        language_note="x",
        segments=_segments(100),
    )
    data = json.loads(cache.read_text())
    assert len(data["segments"]) == 100
    assert "segment 50" in data["transcript"]


def test_cache_write_failure_is_not_fatal(tmp_path):
    """A cache write error must not lose the transcript."""
    out = video._format_and_cache_transcript(
        url="u",
        cache_path="/nonexistent-dir/does/not/exist/t.json",
        title="T",
        uploader="U",
        duration=60.0,
        language="en",
        language_note="x",
        segments=_segments(2),
    )
    assert "segment 0" in out


def test_caption_and_whisper_paths_produce_same_shape(tmp_path):
    """Both sources must yield an identical transcript structure."""
    caption = video._format_and_cache_transcript(
        url="u",
        cache_path=str(tmp_path / "a.json"),
        title="T",
        uploader="U",
        duration=60.0,
        language="en",
        language_note="manual captions",
        segments=_segments(2),
    )
    whisper = video._format_and_cache_transcript(
        url="u",
        cache_path=str(tmp_path / "b.json"),
        title="T",
        uploader="U",
        duration=60.0,
        language="en",
        language_note="confidence: 98%",
        segments=_segments(2),
    )
    # Same structure, differing only in the language note.
    assert caption.replace("manual captions", "X") == whisper.replace(
        "confidence: 98%", "X"
    )
