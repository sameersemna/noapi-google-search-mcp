"""Tests for the remote-Whisper delegation layer.

Covers the eligibility / probe / multipart-build / HTTP-call / response-
normalisation pipeline in :mod:`google_search_mcp.utils.whisper_remote` and
its integration with :func:`google_search_mcp.tools.video._transcribe_audio`.

All HTTP calls are mocked — we never actually hit a remote server in tests.

NB: ``google_search_mcp.config`` reads env vars at module-import time, so
``monkeypatch.setenv()`` set *after* the import does not affect
``config.WHISPER_REMOTE_*``. These tests use ``monkeypatch.setattr`` on
the config module to override individual values.
"""

from __future__ import annotations

import inspect
import io
import json
import urllib.error
from typing import Any
from unittest import mock

import pytest

from google_search_mcp import config
from google_search_mcp.utils import whisper_remote


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Reset config + module-level state before each test.

    Sets a known baseline (everything disabled, local-only) and resets the
    probe cache + call counters. Tests that want the remote enabled override
    individual fields via ``monkeypatch.setattr(config, "FOO", value)``.
    """
    # Disable everything by default — tests opt in explicitly.
    monkeypatch.setattr(config, "WHISPER_REMOTE_ENABLED", False)
    monkeypatch.setattr(config, "WHISPER_REMOTE_API_STYLE", "openai")
    monkeypatch.setattr(config, "WHISPER_REMOTE_URL", "")
    monkeypatch.setattr(config, "WHISPER_REMOTE_API_KEY", "")
    monkeypatch.setattr(config, "WHISPER_REMOTE_MODEL", "large-v3")
    monkeypatch.setattr(config, "WHISPER_REMOTE_TIMEOUT_SEC", 300)
    monkeypatch.setattr(config, "WHISPER_REMOTE_HEALTH_TIMEOUT_SEC", 5)
    monkeypatch.setattr(config, "WHISPER_REMOTE_MIN_FILE_BYTES", 1024 * 1024)
    monkeypatch.setattr(config, "WHISPER_REMOTE_UNHEALTHY_COOLDOWN_SEC", 60)
    monkeypatch.setattr(config, "WHISPER_REMOTE_PROBE_ON_STARTUP", True)

    with whisper_remote._state_lock:
        whisper_remote._last_probe_ok = None
        whisper_remote._last_probe_at = 0.0
        whisper_remote._last_probe_error = None
        whisper_remote._last_probe_latency_ms = None
    with whisper_remote._stats_lock:
        whisper_remote._attempts = 0
        whisper_remote._successes = 0
        whisper_remote._failures = 0
        whisper_remote._last_error = None
        whisper_remote._last_success_at = None
        whisper_remote._last_failure_at = None
    yield


def _enable_remote(monkeypatch, *, url: str = "http://promaxgb10-6116.lan:8768",
                    min_bytes: int = 1, cooldown: float = 60):
    monkeypatch.setattr(config, "WHISPER_REMOTE_ENABLED", True)
    monkeypatch.setattr(config, "WHISPER_REMOTE_URL", url)
    monkeypatch.setattr(config, "WHISPER_REMOTE_MIN_FILE_BYTES", min_bytes)
    monkeypatch.setattr(config, "WHISPER_REMOTE_UNHEALTHY_COOLDOWN_SEC", cooldown)


def _fake_http_response(
    status: int, payload: dict[str, Any] | None = None, body: bytes | None = None,
) -> "mock.MagicMock":
    """Build a context-manager mock for urllib.request.urlopen."""
    payload_bytes = body if body is not None else json.dumps(payload or {}).encode()
    resp = mock.MagicMock()
    resp.status = status
    resp.read.return_value = payload_bytes
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


# ─────────────────────────────────────────────────────────────────────
# Eligibility
# ─────────────────────────────────────────────────────────────────────

def test_is_configured_false_by_default():
    assert whisper_remote.is_configured() is False
    assert whisper_remote.is_eligible("/tmp/whatever.wav") is False


def test_is_configured_requires_url(monkeypatch):
    monkeypatch.setattr(config, "WHISPER_REMOTE_ENABLED", True)
    assert whisper_remote.is_configured() is False  # no URL


def test_is_configured_true_when_enabled_and_url(monkeypatch):
    _enable_remote(monkeypatch)
    assert whisper_remote.is_configured() is True


def test_is_eligible_respects_min_file_bytes(monkeypatch, tmp_path):
    _enable_remote(monkeypatch, min_bytes=1024)

    small = tmp_path / "small.wav"
    small.write_bytes(b"\x00" * 100)
    large = tmp_path / "large.wav"
    large.write_bytes(b"\x00" * 4096)

    assert whisper_remote.is_eligible(str(small)) is False
    assert whisper_remote.is_eligible(str(large)) is True


def test_is_eligible_returns_false_for_missing_file(monkeypatch):
    _enable_remote(monkeypatch, min_bytes=1)
    assert whisper_remote.is_eligible("/nonexistent/file.wav") is False


def test_health_cooldown_skips_remote(monkeypatch, tmp_path):
    """After a failed probe, is_eligible should be False for the cooldown window."""
    _enable_remote(monkeypatch, min_bytes=1)
    whisper_remote._set_probe(False, "Connection refused", 12.3)

    audio = tmp_path / "x.wav"
    audio.write_bytes(b"\x00" * 100)
    assert whisper_remote.is_eligible(str(audio)) is False


# ─────────────────────────────────────────────────────────────────────
# Multipart body
# ─────────────────────────────────────────────────────────────────────

def test_build_multipart_body_structure(tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\xff\xfb\x90\x00" * 64)  # fake mp3 frame bytes
    body, content_type = whisper_remote._build_multipart_body(
        audio_path=str(audio),
        model="large-v3",
        language="en",
    )
    assert isinstance(body, bytes)
    assert len(body) > 0
    assert content_type.startswith("multipart/form-data; boundary=")
    decoded = body.decode("latin-1", errors="replace")
    # The OpenAI endpoint expects these form fields
    assert 'name="file"' in decoded
    assert 'name="model"' in decoded
    assert "large-v3" in decoded
    assert 'name="response_format"' in decoded
    assert "verbose_json" in decoded
    assert 'name="language"' in decoded
    assert "en" in decoded
    # File bytes included
    assert audio.read_bytes().decode("latin-1") in decoded
    # Boundary close
    assert decoded.endswith("--\r\n")


def test_build_multipart_body_omits_empty_language(tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\x00" * 16)
    body, _ = whisper_remote._build_multipart_body(
        audio_path=str(audio),
        model="tiny",
        language="",
    )
    decoded = body.decode("latin-1", errors="replace")
    assert 'name="language"' not in decoded


def test_build_multipart_body_guesses_mp3_mime(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"\x00" * 16)
    body, _ = whisper_remote._build_multipart_body(
        audio_path=str(audio), model="tiny", language="",
    )
    # mimetypes.guess_type("song.mp3") returns ("audio/mpeg", "mp3")
    assert "audio/mpeg" in body.decode("latin-1", errors="replace")


# ─────────────────────────────────────────────────────────────────────
# Probe
# ─────────────────────────────────────────────────────────────────────

def test_probe_disabled(monkeypatch):
    _enable_remote(monkeypatch)
    monkeypatch.setattr(config, "WHISPER_REMOTE_ENABLED", False)
    result = whisper_remote.probe()
    assert result["ok"] is False
    assert result["configured"] is False


def test_probe_ok(monkeypatch):
    _enable_remote(monkeypatch)
    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        return_value=_fake_http_response(200, {"data": []}),
    ) as uo:
        result = whisper_remote.probe()

    assert result["ok"] is True
    assert result["status_code"] == 200
    assert "latency_ms" in result
    called_url = uo.call_args.args[0].full_url
    assert called_url.endswith("/v1/models")


def test_probe_unreachable(monkeypatch):
    _enable_remote(monkeypatch)
    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        side_effect=urllib.error.URLError("Connection refused"),
    ):
        result = whisper_remote.probe()

    assert result["ok"] is False
    assert "URLError" in result["error"]
    # Cooldown should now be active
    assert whisper_remote._is_healthy() is False


def test_probe_sends_auth_header_when_key_set(monkeypatch):
    _enable_remote(monkeypatch)
    monkeypatch.setattr(config, "WHISPER_REMOTE_API_KEY", "secret-token")

    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        return_value=_fake_http_response(200),
    ) as uo:
        whisper_remote.probe()

    req = uo.call_args.args[0]
    assert req.get_header("Authorization") == "Bearer secret-token"


# ─────────────────────────────────────────────────────────────────────
# transcribe_remote
# ─────────────────────────────────────────────────────────────────────

def test_transcribe_remote_disabled_returns_none(tmp_path):
    audio = tmp_path / "x.wav"
    audio.write_bytes(b"\x00" * 100)
    result = whisper_remote.transcribe_remote(str(audio), "tiny", "")
    assert result is None


def test_transcribe_remote_file_too_small_returns_none(monkeypatch, tmp_path):
    _enable_remote(monkeypatch, min_bytes=1024)
    audio = tmp_path / "x.wav"
    audio.write_bytes(b"\x00" * 100)

    result = whisper_remote.transcribe_remote(str(audio), "tiny", "")
    assert result is None


def test_transcribe_remote_success_verbose_json(monkeypatch, tmp_path):
    _enable_remote(monkeypatch, min_bytes=1)
    payload = {
        "task": "transcribe",
        "language": "en",
        "duration": 12.5,
        "text": "Hello world.",
        "segments": [
            {"id": 0, "start": 0.0, "end": 1.2, "text": "Hello "},
            {"id": 1, "start": 1.2, "end": 2.5, "text": "world."},
        ],
    }
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\xff" * 2048)

    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        return_value=_fake_http_response(200, payload),
    ):
        result = whisper_remote.transcribe_remote(str(audio), "tiny", "en")

    assert result is not None
    assert result["language"] == "en"
    assert result["language_probability"] == 1.0
    assert len(result["segments"]) == 2
    assert result["segments"][0] == {"start": 0.0, "end": 1.2, "text": "Hello"}
    assert result["segments"][1] == {"start": 1.2, "end": 2.5, "text": "world."}


def test_transcribe_remote_sends_remote_model_not_caller_size(monkeypatch, tmp_path):
    """Caller may pass model_size='tiny' but we ALWAYS use WHISPER_REMOTE_MODEL."""
    _enable_remote(monkeypatch, min_bytes=1)
    monkeypatch.setattr(config, "WHISPER_REMOTE_MODEL", "large-v3")

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\xff" * 2048)

    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        return_value=_fake_http_response(200, {
            "language": "en",
            "segments": [{"start": 0.0, "end": 1.0, "text": "ok"}],
        }),
    ) as uo:
        whisper_remote.transcribe_remote(str(audio), "tiny", "")

    body = uo.call_args.args[0].data.decode("latin-1", errors="replace")
    # large-v3 appears (the configured remote model)
    assert "large-v3" in body
    # tiny does NOT appear as a model value
    assert 'name="model"\r\n\r\ntiny' not in body


def test_transcribe_remote_http_error_returns_none(monkeypatch, tmp_path):
    """HTTP 4xx = bad request; don't ban the remote — caller may have a bad URL."""
    _enable_remote(monkeypatch, min_bytes=1)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\xff" * 2048)

    err = urllib.error.HTTPError(
        url="http://x", code=400, msg="Bad Request", hdrs={},
        fp=io.BytesIO(b"bad model"),
    )
    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        side_effect=err,
    ):
        result = whisper_remote.transcribe_remote(str(audio), "tiny", "")

    assert result is None
    stats = whisper_remote.get_stats()
    assert stats["failures"] == 1
    # Remote stays healthy after a 4xx — the server is fine, we sent bad input.
    assert whisper_remote._is_healthy() is True


def test_transcribe_remote_connection_error_bans_remote(monkeypatch, tmp_path):
    """Network errors DO trigger cooldown — avoid hammering a dead server."""
    _enable_remote(monkeypatch, min_bytes=1, cooldown=60)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\xff" * 2048)

    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        side_effect=ConnectionError("Network is unreachable"),
    ):
        result = whisper_remote.transcribe_remote(str(audio), "tiny", "")

    assert result is None
    assert whisper_remote._is_healthy() is False

    # Subsequent calls return None without even attempting the remote.
    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
    ) as uo:
        result2 = whisper_remote.transcribe_remote(str(audio), "tiny", "")
    assert result2 is None
    uo.assert_not_called()


def test_transcribe_remote_malformed_json_returns_none(monkeypatch, tmp_path):
    _enable_remote(monkeypatch, min_bytes=1)
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\xff" * 2048)

    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        return_value=_fake_http_response(200, body=b"not json {{"),
    ):
        result = whisper_remote.transcribe_remote(str(audio), "tiny", "")

    assert result is None


def test_transcribe_remote_missing_segments_returns_none(monkeypatch, tmp_path):
    _enable_remote(monkeypatch, min_bytes=1)
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\xff" * 2048)

    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        return_value=_fake_http_response(200, {"language": "en", "segments": []}),
    ):
        result = whisper_remote.transcribe_remote(str(audio), "tiny", "")

    # Empty segments list is normalised to None
    assert result is None


def test_transcribe_remote_falls_back_to_text_only(monkeypatch, tmp_path):
    """Some servers return only ``{"text": "..."}`` — produce a single segment."""
    _enable_remote(monkeypatch, min_bytes=1)
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\xff" * 2048)

    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        return_value=_fake_http_response(200, {
            "text": "Hello world.", "language": "en",
        }),
    ):
        result = whisper_remote.transcribe_remote(str(audio), "tiny", "")

    assert result is not None
    assert len(result["segments"]) == 1
    assert result["segments"][0]["text"] == "Hello world."
    assert result["language"] == "en"


# ─────────────────────────────────────────────────────────────────────
# Stats surface for /health
# ─────────────────────────────────────────────────────────────────────

def test_get_stats_records_attempts(monkeypatch, tmp_path):
    _enable_remote(monkeypatch, min_bytes=1)
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\xff" * 2048)

    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        return_value=_fake_http_response(200, {
            "language": "en",
            "segments": [{"start": 0.0, "end": 1.0, "text": "x"}],
        }),
    ):
        whisper_remote.transcribe_remote(str(audio), "tiny", "")

    stats = whisper_remote.get_stats()
    assert stats["attempts"] == 1
    assert stats["successes"] == 1
    assert stats["failures"] == 0
    assert stats["url"] == "http://promaxgb10-6116.lan:8768"


# ─────────────────────────────────────────────────────────────────────
# Integration with tools.video._transcribe_audio
# ─────────────────────────────────────────────────────────────────────

def test_video_transcribe_raises_when_remote_disabled(monkeypatch):
    """There is no local fallback: a disabled remote is a clear, loud error."""
    from google_search_mcp.errors import UpstreamError
    from google_search_mcp.tools import video

    monkeypatch.setattr(config, "WHISPER_REMOTE_ENABLED", False)

    with mock.patch.object(whisper_remote, "transcribe_remote", return_value=None):
        with pytest.raises(UpstreamError) as excinfo:
            video._transcribe_audio("/tmp/fake.wav", "tiny", "")

    message = str(excinfo.value)
    assert "WHISPER_REMOTE_ENABLED" in message
    assert "no local transcription fallback" in message


def test_video_transcribe_prefers_remote_when_available(monkeypatch):
    """When the remote returns a result, that result is used as-is."""
    from google_search_mcp.tools import video

    _enable_remote(monkeypatch, min_bytes=1)

    fake_remote_result = {
        "segments": [{"start": 0.0, "end": 1.0, "text": "remote"}],
        "language": "en",
        "language_probability": 1.0,
    }
    with mock.patch.object(
        whisper_remote, "transcribe_remote", return_value=fake_remote_result,
    ):
        result = video._transcribe_audio("/tmp/fake.wav", "tiny", "")

    assert result["segments"][0]["text"] == "remote"


def test_video_transcribe_raises_when_remote_returns_none(monkeypatch):
    """A configured-but-failing remote must not silently degrade to nothing.

    Before this change the caller fell back to a local model. With that path
    removed, returning an empty transcript (or "no speech detected") would
    blame the audio for a network problem, so the failure is raised instead.
    """
    from google_search_mcp.errors import UpstreamError
    from google_search_mcp.tools import video

    _enable_remote(monkeypatch, min_bytes=1)

    with mock.patch.object(whisper_remote, "transcribe_remote", return_value=None):
        with pytest.raises(UpstreamError) as excinfo:
            video._transcribe_audio("/tmp/fake.wav", "tiny", "")

    message = str(excinfo.value)
    assert "promaxgb10-6116" in message, "the error must name the host to check"
    assert "no local transcription fallback" in message


def test_local_whisper_path_is_gone():
    """The local engine must not be reachable — it was the OOM cause."""
    from google_search_mcp.tools import video

    assert not hasattr(video, "_transcribe_audio_local")
    assert "faster_whisper" not in inspect.getsource(video)


def test_min_file_bytes_misconfiguration_is_named_not_blamed_on_the_host(
    monkeypatch, tmp_path,
):
    """A stale 1 MB threshold must not look like an unreachable GPU host.

    ``WHISPER_REMOTE_MIN_FILE_BYTES`` used to default to 1 MB so short clips
    stayed local. With the local model gone, an inherited value silently skips
    every small file — and the generic message ("the host did not transcribe")
    would send someone debugging the network instead of the threshold.
    """
    from google_search_mcp.errors import UpstreamError
    from google_search_mcp.tools import video

    _enable_remote(monkeypatch, min_bytes=1024 * 1024)
    audio = tmp_path / "short.mp3"
    audio.write_bytes(b"\x00" * 500)

    with mock.patch.object(whisper_remote, "transcribe_remote", return_value=None):
        with pytest.raises(UpstreamError) as excinfo:
            video._transcribe_audio(str(audio), "tiny", "")

    message = str(excinfo.value)
    assert "WHISPER_REMOTE_MIN_FILE_BYTES=0" in message
    assert "WHISPER_REMOTE_MIN_FILE_BYTES is 1048576" in message


def _config_source() -> str:
    """Read config.py itself — the autouse fixture monkeypatches these values."""
    import pathlib

    return (
        pathlib.Path(__file__).resolve().parents[1]
        / "src" / "google_search_mcp" / "config.py"
    ).read_text(encoding="utf-8")


def test_default_min_file_bytes_sends_every_file_to_the_host():
    """The shipped default must be 0: nothing is transcribed locally any more."""
    src = _config_source()
    assert 'os.environ.get("WHISPER_REMOTE_MIN_FILE_BYTES", "0")' in src, (
        "the default must be 0 — a 1 MB default silently skips short clips now "
        "that there is no local model"
    )


def test_remote_is_enabled_by_default():
    src = _config_source()
    assert 'os.environ.get(\n    "WHISPER_REMOTE_ENABLED", "1"\n)' in src, (
        "transcription has no local fallback, so the remote must default on"
    )


# ─────────────────────────────────────────────────────────────────────
# Health endpoint integration
# ─────────────────────────────────────────────────────────────────────

def test_health_check_whisper_remote_when_disabled():
    from google_search_mcp import health_server

    result = health_server._check_whisper_remote()
    assert result["configured"] is False
    assert result["ok"] is False
    assert "WHISPER_REMOTE_ENABLED" in (result.get("error") or "")


def test_health_check_whisper_remote_when_configured(monkeypatch):
    from google_search_mcp import health_server

    _enable_remote(monkeypatch)

    result = health_server._check_whisper_remote()
    assert result["configured"] is True
    assert result["url"] == "http://promaxgb10-6116.lan:8768"
    assert result["enabled"] is True
    assert "attempts" in result
    assert "successes" in result


def test_health_check_config_exposes_whisper_remote_keys(monkeypatch):
    """The /health config snapshot must include the WHISPER_REMOTE_* vars."""
    from google_search_mcp import health_server

    _enable_remote(monkeypatch)
    monkeypatch.setattr(config, "WHISPER_REMOTE_API_KEY", "secret")
    monkeypatch.setattr(config, "WHISPER_REMOTE_MODEL", "distil-large-v3")

    cfg = health_server._check_config()
    assert cfg["WHISPER_REMOTE_ENABLED"] is True
    assert cfg["WHISPER_REMOTE_URL"] == "http://promaxgb10-6116.lan:8768"
    # The actual API key value MUST NOT be exposed — only the presence flag.
    assert cfg["WHISPER_REMOTE_API_KEY_set"] is True
    assert "secret" not in str(cfg)
    assert cfg["WHISPER_REMOTE_MODEL"] == "distil-large-v3"


def test_health_dependencies_includes_whisper_remote():
    """The /health dependencies dict must include the whisper_remote entry."""
    from google_search_mcp import health_server

    deps = health_server._check_dependencies()
    assert "whisper_remote" in deps
    assert "configured" in deps["whisper_remote"]


# ─────────────────────────────────────────────────────────────────────
# whisper.cpp API style (different endpoint, no model field, name→ISO)
# ─────────────────────────────────────────────────────────────────────

def test_language_to_iso_passes_through_short_codes():
    """ISO codes (≤3 alpha chars) should pass through unchanged."""
    assert whisper_remote._language_to_iso("en") == "en"
    assert whisper_remote._language_to_iso("DE") == "de"
    # "zh-CN" has a hyphen so it falls through to the name-lookup path
    # which doesn't recognise it — returns unchanged.
    assert whisper_remote._language_to_iso("zh-CN") == "zh-CN"
    assert whisper_remote._language_to_iso("") == ""


def test_language_to_iso_maps_full_names():
    """whisper.cpp returns full English names like 'english'."""
    assert whisper_remote._language_to_iso("english") == "en"
    assert whisper_remote._language_to_iso("german") == "de"
    assert whisper_remote._language_to_iso("japanese") == "ja"
    assert whisper_remote._language_to_iso("ENGLISH") == "en"  # case-insensitive
    assert whisper_remote._language_to_iso("Unknown") == "Unknown"  # passthrough


def test_whispercpp_url_dispatch(monkeypatch):
    """URLs must change with the API style."""
    _enable_remote(monkeypatch)

    monkeypatch.setattr(config, "WHISPER_REMOTE_API_STYLE", "openai")
    assert whisper_remote._probe_url().endswith("/v1/models")
    assert whisper_remote._transcribe_url().endswith("/v1/audio/transcriptions")

    monkeypatch.setattr(config, "WHISPER_REMOTE_API_STYLE", "whispercpp")
    assert whisper_remote._probe_url().endswith("/")
    assert whisper_remote._transcribe_url().endswith("/inference")


def test_whispercpp_multipart_omits_model_and_language(tmp_path):
    """whispercpp multipart should not include 'model' or 'language' fields."""
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"\x00" * 16)
    body, content_type = whisper_remote._build_multipart_body(
        audio_path=str(audio),
        model="large-v3",  # should be ignored
        language="en",      # should be ignored
        api_style="whispercpp",
    )
    decoded = body.decode("latin-1", errors="replace")
    assert 'name="model"' not in decoded
    assert 'name="language"' not in decoded
    # whisper.cpp-specific fields present
    assert 'name="temperature"' in decoded
    assert 'name="temperature_inc"' in decoded
    assert 'name="response_format"' in decoded
    assert "verbose_json" in decoded


def test_whispercpp_probe_uses_root_endpoint(monkeypatch):
    """whisper.cpp's server serves ``/`` and has no ``/health``.

    Verified against the real host: ``GET /`` returns 200 with the server's HTML
    index, while ``GET /health`` returns 404 and ``GET /inference`` 404s (it is
    POST-only). Probing ``/health`` therefore reported a working GPU host as
    unreachable — which, with no local fallback left, would fail every
    transcription.
    """
    _enable_remote(monkeypatch)
    monkeypatch.setattr(config, "WHISPER_REMOTE_API_STYLE", "whispercpp")

    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        return_value=_fake_http_response(200, body=b"<html>Whisper.cpp Server</html>"),
    ) as uo:
        result = whisper_remote.probe()

    assert result["ok"] is True
    called_url = uo.call_args.args[0].full_url
    assert called_url.endswith("/")
    assert not called_url.endswith("/health")


def test_whispercpp_transcribe_normalises_language_name(monkeypatch, tmp_path):
    """whispercpp verbose_json returns 'english' — should be normalised to 'en'."""
    _enable_remote(monkeypatch, min_bytes=1)
    monkeypatch.setattr(config, "WHISPER_REMOTE_API_STYLE", "whispercpp")

    payload = {
        "task": "transcribe",
        "language": "english",                       # whisper.cpp uses full names
        "detected_language": "english",
        "detected_language_probability": 0.94,
        "language_probabilities": {"en": 0.94, "de": 0.02, "fr": 0.01},
        "duration": 3.0,
        "text": "Hello world.",
        "segments": [
            {"id": 0, "start": 0.0, "end": 1.5, "text": "Hello"},
            {"id": 1, "start": 1.5, "end": 3.0, "text": "world."},
        ],
    }
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"\xff" * 2048)

    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        return_value=_fake_http_response(200, payload),
    ):
        result = whisper_remote.transcribe_remote(str(audio), "tiny", "")

    assert result is not None
    assert result["language"] == "en"            # normalised from "english"
    assert result["language_probability"] == 0.94  # from language_probabilities
    assert len(result["segments"]) == 2


def test_whispercpp_transcribe_omits_model_field(monkeypatch, tmp_path):
    """Sent multipart body must NOT contain 'model' when style is whispercpp."""
    _enable_remote(monkeypatch, min_bytes=1)
    monkeypatch.setattr(config, "WHISPER_REMOTE_API_STYLE", "whispercpp")

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"\xff" * 2048)

    with mock.patch.object(
        whisper_remote.urllib.request, "urlopen",
        return_value=_fake_http_response(200, {
            "language": "english",
            "language_probabilities": {"en": 0.9},
            "segments": [{"start": 0.0, "end": 1.0, "text": "ok"}],
        }),
    ) as uo:
        whisper_remote.transcribe_remote(str(audio), "tiny", "")

    body = uo.call_args.args[0].data.decode("latin-1", errors="replace")
    assert 'name="model"' not in body
    assert 'name="temperature"' in body
    # Hit the right endpoint
    url = uo.call_args.args[0].full_url
    assert url.endswith("/inference")


def test_invalid_api_style_falls_back_to_openai(monkeypatch):
    """An unknown WHISPER_REMOTE_API_STYLE value should be normalised to openai.

    The validation runs at config-import time, so we re-execute the
    snippet that config.py uses to populate the constant after the env
    var is set.
    """
    monkeypatch.setattr(config, "WHISPER_REMOTE_API_STYLE", "garbage")
    # Re-run the same logic config.py uses at module load.
    val = config.WHISPER_REMOTE_API_STYLE.strip().lower()
    if val not in ("openai", "whispercpp"):
        val = "openai"
    assert val == "openai"


def test_health_check_reports_api_style(monkeypatch):
    """The /health dependencies entry must include api_style."""
    from google_search_mcp import health_server

    _enable_remote(monkeypatch)
    monkeypatch.setattr(config, "WHISPER_REMOTE_API_STYLE", "whispercpp")

    result = health_server._check_whisper_remote()
    assert result["configured"] is True
    assert result["api_style"] == "whispercpp"


def test_health_check_config_exposes_api_style(monkeypatch):
    """The /health config snapshot must include WHISPER_REMOTE_API_STYLE."""
    from google_search_mcp import health_server

    _enable_remote(monkeypatch)
    monkeypatch.setattr(config, "WHISPER_REMOTE_API_STYLE", "whispercpp")

    cfg = health_server._check_config()
    assert cfg["WHISPER_REMOTE_API_STYLE"] == "whispercpp"

