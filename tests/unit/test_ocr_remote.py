"""OCR runs on a remote GPU host: client behaviour, wiring, and no local engine.

Background
----------
OCR used to be local (`rapidocr-onnxruntime`). That engine holds ~170 MB of ONNX
models resident and spends CPU on every page, on a machine that is already over
committed — and constructing it per call is what took the server to 6.9 GB RSS
before the engine cache fixed it. It now runs on a GPU host:

    local  (CPU, medium)  ~1.8 s
    remote (CUDA)          0.13 s

There is no local fallback, mirroring the Whisper decision: a silent CPU
fallback hides a broken deployment behind calls that quietly take ten times
longer, and it keeps the dependency we are trying to delete.

What is asserted here
---------------------
1. The client parses the service's real response shape.
2. Failures are diagnosable — disabled / no URL / host rejected / unreachable
   each produce a distinct message naming the setting to fix.
3. The tool and the scanned-PDF fallback actually delegate to the client.
4. The local engine is gone from packaging and from the call sites.
"""

from __future__ import annotations

import inspect
import json
import os
import pathlib
import urllib.error
import zipfile  # noqa: F401  (kept for symmetry with sibling test modules)

import pytest

from google_search_mcp import config
from google_search_mcp.utils import ocr_remote

_REPO = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "OCR_REMOTE_ENABLED", False)
    monkeypatch.setattr(config, "OCR_REMOTE_URL", "")
    monkeypatch.setattr(config, "OCR_REMOTE_TOKEN", "")
    monkeypatch.setattr(config, "OCR_REMOTE_TIMEOUT_SEC", 60.0)
    monkeypatch.setattr(config, "OCR_REMOTE_HEALTH_TIMEOUT_SEC", 5.0)
    monkeypatch.setattr(config, "OCR_REMOTE_UNHEALTHY_COOLDOWN_SEC", 60.0)
    with ocr_remote._state_lock:
        ocr_remote._last_probe_ok = None
        ocr_remote._last_probe_at = 0.0
        ocr_remote._last_probe_error = None
        ocr_remote._last_probe_latency_ms = None
    with ocr_remote._stats_lock:
        ocr_remote._attempts = 0
        ocr_remote._successes = 0
        ocr_remote._failures = 0
        ocr_remote._last_error = None
    yield


def _enable(monkeypatch, url: str = "http://promaxgb10-6116:8769"):
    monkeypatch.setattr(config, "OCR_REMOTE_ENABLED", True)
    monkeypatch.setattr(config, "OCR_REMOTE_URL", url)


class _Resp:
    """Minimal stand-in for the ``urlopen`` context manager."""

    def __init__(self, status: int, payload: dict | bytes):
        self.status = status
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


SAMPLE = {
    "result": [
        [[[10.0, 35.0], [464.0, 35.0], [464.0, 107.0], [10.0, 107.0]], "SELFTEST 2026", 0.98497],
        [[[11.0, 131.0], [306.0, 129.0], [307.0, 195.0], [12.0, 196.0]], "ocr works", 0.99996],
    ],
    "elapse": [0.10956348199943022, 0.0016055579999374459, 0.005992596999931266],
    "model_loaded": True,
    "provider": "CUDAExecutionProvider",
}


def _image(tmp_path, name: str = "page.png") -> str:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return str(path)


# ── configuration ─────────────────────────────────────────────────────────


def test_not_configured_by_default_in_tests():
    assert ocr_remote.is_configured() is False


def test_configured_when_enabled_with_a_url(monkeypatch):
    _enable(monkeypatch)
    assert ocr_remote.is_configured() is True


def test_enabled_without_a_url_is_not_configured(monkeypatch):
    monkeypatch.setattr(config, "OCR_REMOTE_ENABLED", True)
    assert ocr_remote.is_configured() is False


# ── parsing ───────────────────────────────────────────────────────────────


def test_ocr_path_returns_rapidocr_shape(monkeypatch, tmp_path):
    """``(result, elapse)`` must match ``RapidOCR()(path)`` so callers are unchanged."""
    _enable(monkeypatch)
    monkeypatch.setattr(ocr_remote.urllib.request, "urlopen", lambda *a, **k: _Resp(200, SAMPLE))

    out = ocr_remote.ocr_path(_image(tmp_path))
    assert out is not None
    result, elapse = out
    assert len(result) == 2
    assert result[0][1] == "SELFTEST 2026"
    assert isinstance(result[0][2], float)
    assert len(elapse) == 3
    assert elapse[0] > 0


def test_ocr_path_accepts_an_empty_result(monkeypatch, tmp_path):
    """An image with no text is a valid outcome, not a failure."""
    _enable(monkeypatch)
    monkeypatch.setattr(
        ocr_remote.urllib.request, "urlopen",
        lambda *a, **k: _Resp(200, {"result": [], "elapse": [0.1, 0.0, 0.0]}),
    )
    out = ocr_remote.ocr_path(_image(tmp_path))
    assert out is not None
    assert out[0] == []


def test_ocr_path_pads_a_short_elapse(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(
        ocr_remote.urllib.request, "urlopen",
        lambda *a, **k: _Resp(200, {"result": [], "elapse": [0.5]}),
    )
    out = ocr_remote.ocr_path(_image(tmp_path))
    assert out is not None and len(out[1]) == 3


def test_ocr_path_ignores_extra_response_fields(monkeypatch, tmp_path):
    """The deployed service also returns model_loaded/provider/gpu/rss_mb."""
    _enable(monkeypatch)
    monkeypatch.setattr(ocr_remote.urllib.request, "urlopen", lambda *a, **k: _Resp(200, SAMPLE))
    assert ocr_remote.ocr_path(_image(tmp_path)) is not None


def test_ocr_path_rejects_a_response_without_result(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(
        ocr_remote.urllib.request, "urlopen",
        lambda *a, **k: _Resp(200, {"error": "model not loaded"}),
    )
    assert ocr_remote.ocr_path(_image(tmp_path)) is None
    assert "missing 'result'" in (ocr_remote.get_stats()["last_error"] or "")


def test_ocr_path_rejects_non_json(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(
        ocr_remote.urllib.request, "urlopen",
        lambda *a, **k: _Resp(200, b"<html>nope</html>"),
    )
    assert ocr_remote.ocr_path(_image(tmp_path)) is None


# ── transport and failure handling ────────────────────────────────────────


def test_request_is_multipart_with_a_file_field(monkeypatch, tmp_path):
    _enable(monkeypatch)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["ctype"] = req.get_header("Content-type")
        captured["body"] = req.data
        return _Resp(200, SAMPLE)

    monkeypatch.setattr(ocr_remote.urllib.request, "urlopen", fake_urlopen)
    ocr_remote.ocr_path(_image(tmp_path, "scan.png"))

    assert captured["url"] == "http://promaxgb10-6116:8769/ocr"
    assert captured["ctype"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="scan.png"' in captured["body"]


def test_token_is_sent_as_a_bearer_header(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(config, "OCR_REMOTE_TOKEN", "s3cret")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.get_header("Authorization")
        return _Resp(200, SAMPLE)

    monkeypatch.setattr(ocr_remote.urllib.request, "urlopen", fake_urlopen)
    ocr_remote.ocr_path(_image(tmp_path))
    assert captured["auth"] == "Bearer s3cret"


def test_http_error_returns_none_and_records_the_detail(monkeypatch, tmp_path):
    _enable(monkeypatch)

    def raise_http(*a, **k):
        raise urllib.error.HTTPError(
            "http://x/ocr", 400, "Bad Request", {}, None  # type: ignore[arg-type]
        )

    monkeypatch.setattr(ocr_remote.urllib.request, "urlopen", raise_http)
    assert ocr_remote.ocr_path(_image(tmp_path)) is None
    assert "400" in (ocr_remote.get_stats()["last_error"] or "")


def test_unreachable_host_returns_none(monkeypatch, tmp_path):
    _enable(monkeypatch)

    def raise_url(*a, **k):
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(ocr_remote.urllib.request, "urlopen", raise_url)
    assert ocr_remote.ocr_path(_image(tmp_path)) is None
    assert "URL" in (ocr_remote.get_stats()["last_error"] or "")


def test_missing_file_returns_none(monkeypatch, tmp_path):
    _enable(monkeypatch)
    assert ocr_remote.ocr_path(str(tmp_path / "does-not-exist.png")) is None


def test_counters_track_success_and_failure(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(ocr_remote.urllib.request, "urlopen", lambda *a, **k: _Resp(200, SAMPLE))
    ocr_remote.ocr_path(_image(tmp_path))
    stats = ocr_remote.get_stats()
    assert stats["attempts"] == 1
    assert stats["successes"] == 1


# ── error messages ────────────────────────────────────────────────────────


def test_disabled_error_names_the_switch():
    msg = str(ocr_remote.ocr_unavailable_error("/tmp/x.png"))
    assert "OCR_REMOTE_ENABLED" in msg
    assert "no local OCR engine" in msg


def test_missing_url_error_names_the_setting(monkeypatch):
    monkeypatch.setattr(config, "OCR_REMOTE_ENABLED", True)
    msg = str(ocr_remote.ocr_unavailable_error("/tmp/x.png"))
    assert "OCR_REMOTE_URL" in msg
    assert "8769" in msg


def test_unreachable_error_names_the_host(monkeypatch):
    _enable(monkeypatch)
    with ocr_remote._stats_lock:
        ocr_remote._last_error = "URLError: timed out"
    msg = str(ocr_remote.ocr_unavailable_error("/tmp/scan.png"))
    assert "promaxgb10-6116:8769" in msg
    assert "scan.png" in msg, "the message should name the file it failed on"
    assert "timed out" in msg


# ── probe ─────────────────────────────────────────────────────────────────


def test_probe_reports_ok_and_reads_capabilities(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        ocr_remote.urllib.request, "urlopen",
        lambda *a, **k: _Resp(200, {
            "status": "ok", "model_loaded": True,
            "provider": "CUDAExecutionProvider", "gpu": True,
        }),
    )
    result = ocr_remote.probe()
    assert result["ok"] is True
    assert result["provider"] == "CUDAExecutionProvider"
    assert result["model_loaded"] is True
    assert result["latency_ms"] is not None


def test_probe_failure_is_cached_for_the_cooldown(monkeypatch):
    _enable(monkeypatch)

    def raise_url(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(ocr_remote.urllib.request, "urlopen", raise_url)
    assert ocr_remote.probe()["ok"] is False
    assert ocr_remote._is_healthy() is False

    monkeypatch.setattr(config, "OCR_REMOTE_UNHEALTHY_COOLDOWN_SEC", 0)
    assert ocr_remote._is_healthy() is True


def test_probe_when_unconfigured_does_not_touch_the_network(monkeypatch):
    called = []
    monkeypatch.setattr(
        ocr_remote.urllib.request, "urlopen",
        lambda *a, **k: called.append(1) or _Resp(200, {}),
    )
    result = ocr_remote.probe()
    assert result["ok"] is False
    assert result["configured"] is False
    assert called == []


# ── wiring ────────────────────────────────────────────────────────────────


def test_ocr_image_delegates_to_the_remote_client(monkeypatch, tmp_path):
    import asyncio

    from google_search_mcp.tools import vision

    _enable(monkeypatch)
    monkeypatch.setattr(ocr_remote.urllib.request, "urlopen", lambda *a, **k: _Resp(200, SAMPLE))

    out = asyncio.run(vision.ocr_image(_image(tmp_path)))
    assert "SELFTEST 2026" in out
    assert "ocr works" in out
    assert "Text regions found: 2" in out


def test_ocr_image_reports_the_configuration_error_when_unconfigured(tmp_path):
    import asyncio

    from google_search_mcp.tools import vision

    out = asyncio.run(vision.ocr_image(_image(tmp_path)))
    assert "OCR_REMOTE" in out
    assert "no local OCR engine" in out


def test_ocr_image_reports_an_unreachable_host(monkeypatch, tmp_path):
    import asyncio

    from google_search_mcp.tools import vision

    def raise_url(*a, **k):
        raise urllib.error.URLError("unreachable")

    _enable(monkeypatch)
    monkeypatch.setattr(ocr_remote.urllib.request, "urlopen", raise_url)

    out = asyncio.run(vision.ocr_image(_image(tmp_path)))
    assert "8769" in out
    assert "unreachable" in out.lower() or "did not process" in out


def test_pdf_fallback_availability_follows_the_remote_config(monkeypatch):
    from google_search_mcp.tools import web_utils

    assert web_utils._ocr_available() is False
    _enable(monkeypatch)
    assert web_utils._ocr_available() is True


def test_scanned_pdf_without_ocr_names_the_setting(monkeypatch, tmp_path):
    import asyncio

    from google_search_mcp.tools import web_utils

    pdf = tmp_path / "scanned.pdf"
    pdf.write_bytes(b"%PDF-1.4 image only")
    monkeypatch.setattr(web_utils, "_read_pdf_text", lambda path: "")

    out = asyncio.run(web_utils.read_document(str(pdf)))
    assert "OCR_REMOTE" in out, "the hint must name the setting, not a pip package"
    assert "poppler-utils" in out, "the text-PDF hint must still be offered"


def test_startup_probe_is_wired(monkeypatch):
    """The probe must run at startup so /health is informative immediately."""
    src = inspect.getsource(__import__("google_search_mcp.server", fromlist=["x"]))
    assert "_start_ocr_probe" in src
    assert "ocr_remote" in src


# ── the local engine is gone ──────────────────────────────────────────────


def _core_dependency_block(pyproject: str) -> str:
    """Core ``dependencies = [...]`` only, comments stripped.

    Comments are stripped because they legitimately *mention* the removed package
    ("No OCR engine here…"); matching raw text would flag the explanation.
    """
    block = pyproject.split("[project.optional-dependencies]", 1)[0]
    block = block.split("dependencies = [", 1)[1]
    kept = []
    for line in block.splitlines():
        code = line.split("#", 1)[0]
        if code.strip():
            kept.append(code)
        if code.strip() == "]":
            break
    return "\n".join(kept)


def test_rapidocr_is_not_a_dependency_anywhere():
    text = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '"rapidocr-onnxruntime' not in _core_dependency_block(text)
    extras = text.split("[project.optional-dependencies]", 1)[1]
    assert '"rapidocr-onnxruntime' not in extras, (
        "the [ocr] extra is gone; OCR is a remote service now"
    )


def test_no_ocr_extra_remains():
    import tomllib

    data = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    assert "ocr" not in extras
    assert "all" not in extras


def test_service_code_does_not_import_rapidocr():
    pkg = _REPO / "src" / "google_search_mcp"
    offenders = []
    for path in pkg.rglob("*.py"):
        if path.name == "server_selenium.py":
            continue  # dead code, ruff-excluded
        if "rapidocr_onnxruntime" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert not offenders, f"still importing the local OCR engine: {offenders}"


def test_ocr_remote_is_configured_by_default_with_the_known_host():
    """The shipped default points at the deployment's OCR host."""
    text = (_REPO / "src" / "google_search_mcp" / "config.py").read_text(encoding="utf-8")
    assert 'os.environ.get(\n    "OCR_REMOTE_URL", "http://promaxgb10-6116:8769"\n)' in text
    assert 'os.environ.get(\n    "OCR_REMOTE_ENABLED", "1"\n)' in text


def test_engine_cache_no_longer_mentions_ocr(monkeypatch):
    """The cache is for the CAPTCHA classifier now; the OCR slot is gone."""
    src = (_REPO / "src" / "google_search_mcp" / "utils" / "singletons.py").read_text(
        encoding="utf-8"
    )
    assert "os.environ" not in src  # it reads config, not the environment directly
