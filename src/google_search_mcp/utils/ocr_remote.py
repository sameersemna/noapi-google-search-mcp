"""Remote OCR client — OCR runs on the GPU host, not on this machine.

Why this module exists
----------------------
``rapidocr-onnxruntime`` holds ~170 MB of ONNX models resident and burns CPU on
every page. On the deployment host (14 GB, shared with Elasticsearch, Kibana,
Logstash, Postgres and the editor) that is the largest remaining avoidable
consumer, and OCR is exactly the bursty CPU-bound work a GPU box does well.

Measured on the same 6 KB test image:

    local  (CPU, medium)  ~1.8 s   + ~170 MB resident
    remote (CUDA)          0.13 s  0 MB here

There is no local fallback. That is deliberate and mirrors the Whisper
decision: a silent CPU fallback hides a broken deployment behind calls that
quietly take ten times longer, and it keeps the dependency we are trying to
delete. An unreachable OCR host is a loud, specific error instead.

Wire protocol
-------------
``POST {OCR_REMOTE_URL}/ocr``, ``multipart/form-data`` with one field:

    file=@<image>            (filename + mime type auto-detected)

Optional ``Authorization: Bearer {OCR_REMOTE_TOKEN}``.

Response (200)::

    {
      "result": [[ [ [x,y], [x,y], [x,y], [x,y] ], "text", 0.99 ], ...],
      "elapse": [det_sec, cls_sec, rec_sec],
      "model_loaded": true,
      "provider": "CUDAExecutionProvider"      # extras are ignored
    }

That is FastOCR/``RapidOCR``'s native shape, so :func:`ocr_path` returns exactly
what ``RapidOCR()(path)`` returns and the callers' parsing is unchanged.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import TYPE_CHECKING, Any

from .. import config

if TYPE_CHECKING:  # pragma: no cover
    from ..errors import UpstreamError

log = logging.getLogger("google_search_mcp.ocr_remote")

_state_lock = threading.Lock()
_last_probe_ok: bool | None = None
_last_probe_at: float = 0.0
_last_probe_error: str | None = None
_last_probe_latency_ms: float | None = None

_stats_lock = threading.Lock()
_attempts = 0
_successes = 0
_failures = 0
_last_error: str | None = None
_last_success_at: float | None = None


def _now() -> float:
    return time.monotonic()


def _record_attempt() -> None:
    global _attempts
    with _stats_lock:
        _attempts += 1


def _record_success() -> None:
    global _successes, _last_success_at
    with _stats_lock:
        _successes += 1
        _last_success_at = time.time()


def _record_failure(err: str) -> None:
    global _failures, _last_error
    with _stats_lock:
        _failures += 1
        _last_error = err


def get_stats() -> dict[str, Any]:
    """Counters for /health and /metrics."""
    with _stats_lock:
        return {
            "enabled": config.OCR_REMOTE_ENABLED,
            "url": config.OCR_REMOTE_URL or None,
            "attempts": _attempts,
            "successes": _successes,
            "failures": _failures,
            "last_error": _last_error,
            "last_success_at": _last_success_at,
            "last_probe_ok": _last_probe_ok,
            "last_probe_at": _last_probe_at or None,
            "last_probe_latency_ms": _last_probe_latency_ms,
            "last_probe_error": _last_probe_error,
        }


def is_configured() -> bool:
    """True when the remote is enabled and has a URL. Does not check reachability."""
    return bool(config.OCR_REMOTE_ENABLED and config.OCR_REMOTE_URL)


def ocr_unavailable_error(image_path: str | None = None) -> UpstreamError:
    """Build the error raised when the (mandatory) OCR host cannot be used.

    OCR has no local fallback, so this text is the entire diagnosis. It names
    the cause and the setting to change rather than saying "OCR failed", which
    would send someone looking at the image instead of the config.
    """
    from ..errors import UpstreamError

    if not config.OCR_REMOTE_ENABLED:
        return UpstreamError(
            "OCR is unavailable: the remote OCR host is disabled. Set "
            "OCR_REMOTE_ENABLED=1 — there is no local OCR engine on this machine."
        )
    if not config.OCR_REMOTE_URL:
        return UpstreamError(
            "OCR is unavailable: OCR_REMOTE_URL is not set. Point it at the OCR "
            "service (for example OCR_REMOTE_URL=http://promaxgb10-6116:8769). "
            "There is no local OCR engine on this machine."
        )

    reason = _last_error or "no successful response"
    target = f" for {os.path.basename(image_path)}" if image_path else ""
    return UpstreamError(
        f"OCR is unavailable: the remote OCR host {config.OCR_REMOTE_URL} did "
        f"not process the image{target} ({reason}). Check that the service on "
        f"that host is running and reachable "
        f"(timeout {config.OCR_REMOTE_TIMEOUT_SEC:g}s). There is no local OCR "
        f"engine on this machine."
    )


# ---------------------------------------------------------------------------
# Health probe (cached, with cooldown so a dead host is not hammered)
# ---------------------------------------------------------------------------

def _set_probe(ok: bool, error: str | None, latency_ms: float | None) -> None:
    global _last_probe_ok, _last_probe_at, _last_probe_error, _last_probe_latency_ms
    with _state_lock:
        _last_probe_ok = ok
        _last_probe_at = _now()
        _last_probe_error = error
        _last_probe_latency_ms = latency_ms


def _is_healthy() -> bool:
    """True if the last probe succeeded, or the cooldown has expired."""
    if not is_configured():
        return False
    with _state_lock:
        if _last_probe_ok is None or _last_probe_ok:
            return True
    return (_now() - _last_probe_at) >= config.OCR_REMOTE_UNHEALTHY_COOLDOWN_SEC


def probe() -> dict[str, Any]:
    """GET the host's ``/health``. Updates cached state; never raises."""
    started = _now()
    if not is_configured():
        _set_probe(False, "not configured", None)
        return {
            "ok": False,
            "configured": False,
            "enabled": config.OCR_REMOTE_ENABLED,
            "url": config.OCR_REMOTE_URL or None,
            "error": "OCR_REMOTE_ENABLED=0 or OCR_REMOTE_URL is empty",
            "latency_ms": None,
        }

    url = config.OCR_REMOTE_URL.rstrip("/") + "/health"
    try:
        req = urllib.request.Request(url, method="GET")
        if config.OCR_REMOTE_TOKEN:
            req.add_header("Authorization", f"Bearer {config.OCR_REMOTE_TOKEN}")
        req.add_header("User-Agent", config.USER_AGENT)
        with urllib.request.urlopen(
            req, timeout=config.OCR_REMOTE_HEALTH_TIMEOUT_SEC
        ) as resp:
            latency_ms = round((_now() - started) * 1000, 1)
            ok = 200 <= resp.status < 300
            _set_probe(ok, None if ok else f"HTTP {resp.status}", latency_ms)
            payload: dict[str, Any] = {
                "ok": ok,
                "configured": True,
                "enabled": True,
                "url": config.OCR_REMOTE_URL,
                "status_code": resp.status,
                "latency_ms": latency_ms,
                "error": None if ok else f"HTTP {resp.status}",
            }
            try:
                import json as _json

                body = _json.loads(resp.read() or b"{}")
                for key in ("model_loaded", "provider", "gpu", "model_type", "rss_mb"):
                    if key in body:
                        payload[key] = body[key]
            except Exception:  # pragma: no cover - body is optional
                pass
            return payload
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
        latency_ms = round((_now() - started) * 1000, 1)
        err = f"{type(exc).__name__}: {exc}"
        _set_probe(False, err, latency_ms)
        return {
            "ok": False,
            "configured": True,
            "enabled": True,
            "url": config.OCR_REMOTE_URL,
            "latency_ms": latency_ms,
            "error": err,
        }


def startup_probe() -> None:
    """Probe once at startup so ``/health`` is informative immediately."""
    if not is_configured() or not config.OCR_REMOTE_PROBE_ON_STARTUP:
        return
    try:
        result = probe()
        if result.get("ok"):
            log.info(
                "Remote OCR probe OK: %s (%.0f ms)",
                config.OCR_REMOTE_URL,
                result.get("latency_ms") or 0.0,
            )
        else:
            log.warning(
                "Remote OCR probe FAILED: %s — %s. OCR tools will fail until "
                "the host is reachable.",
                config.OCR_REMOTE_URL,
                result.get("error"),
            )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Remote OCR probe raised: %s", exc)


# ---------------------------------------------------------------------------
# Multipart upload — hand-rolled, matching utils/whisper_remote.py
# ---------------------------------------------------------------------------

def _build_multipart(image_path: str) -> tuple[bytes, str]:
    """Return ``(body, content_type)`` for a one-file multipart upload."""
    boundary = f"----noapi{uuid.uuid4().hex}"
    filename = os.path.basename(image_path) or "image"
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    with open(image_path, "rb") as fh:
        payload = fh.read()

    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n"
        "\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + payload + tail, f"multipart/form-data; boundary={boundary}"


def _ocr_url() -> str:
    return config.OCR_REMOTE_URL.rstrip("/") + "/ocr"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def ocr_path(image_path: str) -> tuple[list[Any], list[float]] | None:
    """Run OCR on *image_path* via the remote host.

    Blocking — call it through ``asyncio.to_thread`` from async code.

    Returns ``(result, elapse)`` in ``RapidOCR()(path)``'s exact shape, so
    callers' sorting/formatting code is unchanged, or ``None`` on any failure
    (the caller raises :func:`ocr_unavailable_error`).
    """
    if not is_configured():
        return None

    _record_attempt()
    started = _now()

    try:
        body, content_type = _build_multipart(image_path)
    except OSError as exc:
        _record_failure(f"read {image_path}: {exc}")
        return None

    req = urllib.request.Request(_ocr_url(), data=body, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("User-Agent", config.USER_AGENT)
    if config.OCR_REMOTE_TOKEN:
        req.add_header("Authorization", f"Bearer {config.OCR_REMOTE_TOKEN}")

    try:
        with urllib.request.urlopen(
            req, timeout=config.OCR_REMOTE_TIMEOUT_SEC
        ) as resp:
            if not (200 <= resp.status < 300):
                _record_failure(f"HTTP {resp.status}")
                return None
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # pragma: no cover - defensive
            pass
        _record_failure(f"HTTP {exc.code} {detail}".strip())
        _set_probe(False, f"HTTP {exc.code}", None)
        return None
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
        _record_failure(f"{type(exc).__name__}: {exc}")
        _set_probe(False, f"{type(exc).__name__}: {exc}", None)
        return None

    try:
        import json

        payload = json.loads(raw)
    except Exception:
        _record_failure("response was not valid JSON")
        return None

    result = payload.get("result")
    if not isinstance(result, list):
        _record_failure("response missing 'result'")
        return None

    elapse_raw = payload.get("elapse")
    elapse: list[float] = []
    if isinstance(elapse_raw, list):
        for value in elapse_raw:
            try:
                elapse.append(float(value))
            except (TypeError, ValueError):
                elapse.append(0.0)
    while len(elapse) < 3:
        elapse.append(0.0)

    _record_success()
    elapsed_ms = round((_now() - started) * 1000, 1)
    log.info(
        "Remote OCR OK: %d regions in %.0f ms (%s)",
        len(result),
        elapsed_ms,
        payload.get("provider") or "unknown provider",
    )
    # Refresh the probe timestamp so a long-running process keeps reporting
    # the host as healthy without an extra HTTP request.
    _set_probe(True, None, None)
    return result, elapse[:3]
