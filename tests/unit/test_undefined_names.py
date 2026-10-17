"""Regression tests for the *undefined name* (latent NameError) bug family.

A class of bugs (B11, B12, B13, B15–B19) had the same root cause: a module used
a name it never imported, so the affected code path raised ``NameError`` at
runtime while the rest of the module imported fine and tests passed. Several of
these broke whole tools (e.g. ``google_translate`` failed on its second line;
``extract_video_clip`` failed for local files; ``google_lens`` failed for local
images).

Two guards:

1. A package-wide ``ruff --select F821,F823`` run (authoritative; skips if ruff
   is not installed). This catches the entire bug class statically.
2. Explicit assertions that the previously-undefined names are now bound, plus a
   functional test that ``google_translate`` works end to end (offline).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

_PKG_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "google_search_mcp"


# ── 1. Static guard: no undefined names anywhere in the package ─────────────

@pytest.mark.skipif(
    importlib.util.find_spec("ruff") is None, reason="ruff not installed"
)
def test_no_undefined_names_in_package():
    completed = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check",
            "--select", "F821,F823",
            "--output-format", "concise",
            "--exclude", "server_selenium.py",
            str(_PKG_ROOT),
        ],
        capture_output=True,
        text=True,
    )
    findings = [
        line for line in completed.stdout.splitlines() if line.startswith(str(_PKG_ROOT))
    ]
    assert not findings, (
        "undefined names found (latent NameError):\n  " + "\n  ".join(findings)
    )


# ── 2. Explicit binding assertions (fast, no ruff needed) ───────────────────

def test_browser_binds_config_module():
    from google_search_mcp import browser
    assert getattr(browser, "config", None) is not None


def test_search_binds_re_and_ddg_images_fallback():
    from google_search_mcp.tools import search
    assert getattr(search, "re", None) is not None
    assert getattr(search, "fallback_duckduckgo_images", None) is not None


def test_travel_binds_translate_helpers():
    from google_search_mcp.tools import travel
    for name in (
        "json", "re", "urllib", "USER_AGENT",
        "LANGUAGE_CODES", "LANG_DETECTION_CONFIDENCE_THRESHOLD",
        "detect_source_language", "split_translation_chunks",
    ):
        assert getattr(travel, name, None) is not None, f"travel.py is missing {name}"


def test_video_binds_path():
    from google_search_mcp.tools import video
    assert getattr(video, "Path", None) is not None


def test_vision_binds_path_and_datetime():
    from google_search_mcp.tools import vision
    assert getattr(vision, "Path", None) is not None
    assert getattr(vision, "datetime", None) is not None


# ── 3. Functional proof: google_translate works offline ────────────────────

class _FakeResp:
    def __init__(self, payload: str):
        self._b = payload.encode()

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_google_translate_http_helper_works_offline(monkeypatch):
    """The HTTP translate fallback used undefined urllib/json/USER_AGENT.

    It is reachable (called when the browser extraction yields nothing), so a
    NameError there broke the fallback path.
    """
    from google_search_mcp.tools import travel

    # Shape of translate.googleapis.com/translate_a/single response.
    payload = json.dumps([[["Hola mundo", "Hello world", None, None, 10]], None, "en"])

    def fake_urlopen(req, timeout=20):  # noqa: ARG001
        return _FakeResp(payload)

    monkeypatch.setattr(travel.urllib.request, "urlopen", fake_urlopen)

    out = travel._translate_via_google_http("Hello world", "en", "es")
    assert "Hola mundo" in out, out


def test_split_translation_chunks_is_used_by_http_helper(monkeypatch):
    """The helper must chunk long text via split_translation_chunks (was undefined)."""
    from google_search_mcp.tools import travel

    calls: list[str] = []

    def fake_urlopen(req, timeout=20):  # noqa: ARG001
        calls.append(req.full_url)
        return _FakeResp(json.dumps([[["ok", "x", None, None, 10]], None, "en"]))

    monkeypatch.setattr(travel.urllib.request, "urlopen", fake_urlopen)
    long_text = "word " * 1000  # forces multiple chunks (>1500 chars)
    travel._translate_via_google_http(long_text, "en", "es")
    assert len(calls) > 1, "long text should be split into multiple chunk requests"


def test_extract_video_clip_local_file_does_not_nameerror(tmp_path, monkeypatch):
    """B15: a local file path must not raise NameError on ``Path``."""
    from google_search_mcp.tools import video

    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00" * 64)

    # Stop right after the filename derivation by forcing the download helpers
    # to be unavailable, so we exercise the Path(...) line without ffmpeg.
    monkeypatch.setattr(video.ytdlp, "is_available", lambda: False)
    out = asyncio.run(video.extract_video_clip(str(f), 0, 1))
    assert "name 'Path' is not defined" not in out
    assert "yt-dlp is required" in out or "clip" in out.lower()
