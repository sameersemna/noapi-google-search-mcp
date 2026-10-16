"""Phase 7 tests: sha256 cache keys with legacy md5 read-fallback.

Cache keys moved from md5 to sha256 (so security scanners no longer flag a weak
hash). Existing caches written by older versions must still be readable, so the
new code keeps an md5 "legacy" path and prefers whichever file actually exists.
"""

from __future__ import annotations

import hashlib
import os

from google_search_mcp.tools import video


def test_transcript_cache_path_uses_sha256():
    url, model = "https://example.invalid/v", "tiny"
    expected = hashlib.sha256(f"{url}|{model}".encode()).hexdigest()
    path = video._transcript_cache_path(url, model)
    assert os.path.basename(path) == f"{expected}.json"
    assert "md5" not in path


def test_legacy_transcript_path_uses_md5():
    url, model = "https://example.invalid/v", "tiny"
    expected = hashlib.md5(  # noqa: S324 - asserting the legacy key on purpose
        f"{url}|{model}".encode(), usedforsecurity=False
    ).hexdigest()
    path = video._transcript_cache_path_legacy(url, model)
    assert os.path.basename(path) == f"{expected}.json"


def test_video_cache_paths_differ_by_section():
    a = video._video_cache_path("https://example.invalid/v", (0.0, 15.0))
    b = video._video_cache_path("https://example.invalid/v", (30.0, 45.0))
    assert a != b, "section must be part of the video cache key"
    assert a.endswith(".mp4")


def test_existing_cache_path_prefers_primary(tmp_path):
    primary = tmp_path / "new.json"
    legacy = tmp_path / "old.json"
    primary.write_text("{}")
    legacy.write_text("{}")
    assert video._existing_cache_path(str(primary), str(legacy)) == str(primary)


def test_existing_cache_path_falls_back_to_legacy(tmp_path):
    primary = tmp_path / "new.json"
    legacy = tmp_path / "old.json"
    legacy.write_text("{}")
    assert video._existing_cache_path(str(primary), str(legacy)) == str(legacy)


def test_existing_cache_path_returns_primary_when_neither_exists(tmp_path):
    primary = tmp_path / "new.json"
    legacy = tmp_path / "old.json"
    # Neither exists -> prefer the primary (new) location for writes.
    assert video._existing_cache_path(str(primary), str(legacy)) == str(primary)


def test_legacy_md5_cache_is_still_read(tmp_path, monkeypatch):
    """A transcript cached under the OLD md5 name must still be found."""
    monkeypatch.setattr(video, "TRANSCRIPT_CACHE_DIR", str(tmp_path))
    url, model = "https://example.invalid/v", "tiny"
    legacy = video._transcript_cache_path_legacy(url, model)
    with open(legacy, "w") as fh:
        fh.write('{"transcript": "OLD CACHE"}')

    found = video._existing_cache_path(
        video._transcript_cache_path(url, model), legacy
    )
    assert found == legacy
    with open(found) as fh:
        assert "OLD CACHE" in fh.read()
