"""Phase 1 regression tests for ``utils/ytdlp.py`` (offline, fake yt-dlp).

* **B2** — ``download_audio`` previously wrote to a shared fixed name
  (``audio_temp``) in a shared cache dir and deleted every matching file first,
  so two concurrent ``transcribe_video`` calls clobbered each other. It must
  now use a unique per-call working directory.
"""

from __future__ import annotations

import os

import pytest

from google_search_mcp.utils import ytdlp


class _FakeYDL:
    """Minimal stand-in for yt_dlp.YoutubeDL that writes the requested file."""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):  # noqa: ARG002
        outtmpl = self.opts["outtmpl"]
        path = outtmpl.replace("%(ext)s", "m4a")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"audio-bytes:" + url.encode())
        return {"title": "T", "duration": 1, "uploader": "U"}


class _FakeYtDlpModule:
    YoutubeDL = _FakeYDL


@pytest.fixture()
def fake_ytdlp(monkeypatch):
    monkeypatch.setattr(ytdlp, "_require_ytdlp", lambda: _FakeYtDlpModule)


def test_concurrent_audio_downloads_use_distinct_paths(tmp_path, fake_ytdlp):
    r1 = ytdlp.download_audio("https://example.invalid/1", str(tmp_path))
    r2 = ytdlp.download_audio("https://example.invalid/2", str(tmp_path))

    assert r1["audio_path"] != r2["audio_path"], (
        "two downloads shared a path — fixed-name temp file race (B2)"
    )
    assert os.path.isfile(r1["audio_path"])
    assert os.path.isfile(r2["audio_path"])
    # Neither file was deleted by the other call (old wipe-all behaviour).
    with open(r1["audio_path"], "rb") as fh:
        assert fh.read() == b"audio-bytes:https://example.invalid/1"
    with open(r2["audio_path"], "rb") as fh:
        assert fh.read() == b"audio-bytes:https://example.invalid/2"


def test_stale_dir_cleanup_is_age_gated(tmp_path):
    fresh = tmp_path / "audio_fresh"
    fresh.mkdir()
    (fresh / "audio.m4a").write_bytes(b"x")

    stale = tmp_path / "audio_stale"
    stale.mkdir()
    (stale / "audio.m4a").write_bytes(b"y")
    old = 1000.0
    os.utime(stale, (old, old))

    ytdlp._cleanup_stale_dirs(str(tmp_path), "audio_", max_age_sec=3600)

    assert fresh.exists(), "fresh dir must not be deleted (would break concurrent calls)"
    assert not stale.exists(), "stale dir should be pruned"
