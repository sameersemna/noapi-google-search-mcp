"""Remote Whisper server client — delegates heavy transcription to a GPU host.

The local ``faster-whisper`` model is fast enough for short clips but
crawls on hour-long audio when running CPU + int8. When a GPU host
(such as a Dell Pro Max GB10 running ``speaches``) is reachable on the
LAN, this module sends the audio file there via the OpenAI-compatible
``POST /v1/audio/transcriptions`` endpoint and returns the segments in
the same shape ``tools.video._transcribe_audio`` already produces.

Design constraints
------------------

* **Always falls back to local.** If the remote is disabled,
  unreachable, returns an error, or the file is too small, the caller
  must be able to transparently fall back to the local ``faster_whisper``
  path. We therefore return ``None`` on any failure rather than raising.
* **No new HTTP dependency.** The rest of the project uses
  ``urllib.request`` via ``asyncio.to_thread``; we follow that
  pattern. Multipart bodies are hand-rolled because they're trivial
  and adding ``requests`` would be one more dependency to audit.
* **No new Python dependency for probing.** ``urllib.request`` opens
  sockets without requiring any extra module.
* **Stateful health cache.** Probing the remote on every call would
  dominate latency for short files. We remember the last probe
  outcome with a TTL (``WHISPER_REMOTE_UNHEALTHY_COOLDOWN_SEC``) and
  skip the remote while it's marked unhealthy.

Wire protocol
-------------

The remote server can speak one of two protocols, selected by
``WHISPER_REMOTE_API_STYLE``:

**"openai" (default)** — OpenAI-compatible
``POST /v1/audio/transcriptions``::

    POST {WHISPER_REMOTE_URL}/v1/audio/transcriptions
    Authorization: Bearer {WHISPER_REMOTE_API_KEY}     (if set)
    Content-Type: multipart/form-data; boundary=...

    file=@<audio>            (filename + mime type auto-detected)
    model=<WHISPER_REMOTE_MODEL>
    response_format=verbose_json
    language=<lang>          (only if the caller passed one)

**"whispercpp"** — whisper.cpp's ``server`` example
(https://github.com/ggml-org/whisper.cpp)::

    POST {WHISPER_REMOTE_URL}/inference
    Content-Type: multipart/form-data; boundary=...

    file=@<audio>            (filename + mime type auto-detected)
    temperature=0.0
    temperature_inc=0.2
    response_format=verbose_json

whispercpp has no per-request ``model`` field (the server has its
model loaded via ``/load``). ``language`` is also ignored — whisper.cpp
auto-detects the language and returns it as a full English name
(``"english"``) at the top level plus an ISO-coded
``language_probabilities`` map. The normaliser converts the name to
its ISO code.

Successful response (``response_format=verbose_json``) for both
styles is shape-compatible::

    {
      "task": "transcribe",
      "language": "en",                # openai: ISO; whisper.cpp: full name
      "duration": 123.4,
      "text": "...",
      "segments": [
        {"id": 0, "start": 0.0, "end": 4.2, "text": "...",
         "tokens": [...], "temperature": 0.0, ...}
      ]
    }

whispercpp additionally returns ``detected_language`` (string, English
name), ``detected_language_probability`` (float), and a top-level
``language_probabilities`` map keyed by ISO codes. These are used to
produce an ISO language code + a probability score even when the
``language`` field is a name like ``"english"``.

This is normalised into the project's internal shape::

    {
      "segments": [{"start": float, "end": float, "text": str}, ...],
      "language": str,                 # ISO code ("en")
      "language_probability": float,
    }

so that ``_format_and_cache_transcript`` and the on-disk cache remain
identical whether the result came from local or remote Whisper.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import socket
import threading
import time
import uuid
import urllib.error
import urllib.request
from typing import Any

from .. import config

log = logging.getLogger("google_search_mcp.whisper_remote")


# ---------------------------------------------------------------------------
# State — last probe outcome + cooldown
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_last_probe_ok: bool | None = None
_last_probe_at: float = 0.0
_last_probe_error: str | None = None
_last_probe_latency_ms: float | None = None

# Per-call counters (read by /health, /metrics).
_stats_lock = threading.Lock()
_attempts: int = 0
_successes: int = 0
_failures: int = 0
_last_error: str | None = None
_last_success_at: float | None = None
_last_failure_at: float | None = None


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
    global _failures, _last_error, _last_failure_at
    with _stats_lock:
        _failures += 1
        _last_error = err
        _last_failure_at = time.time()


def get_stats() -> dict[str, Any]:
    """Return current call counters — read by /health and /metrics."""
    with _stats_lock:
        return {
            "enabled": config.WHISPER_REMOTE_ENABLED,
            "url": config.WHISPER_REMOTE_URL or None,
            "model": config.WHISPER_REMOTE_MODEL if config.WHISPER_REMOTE_ENABLED else None,
            "min_file_bytes": config.WHISPER_REMOTE_MIN_FILE_BYTES,
            "attempts": _attempts,
            "successes": _successes,
            "failures": _failures,
            "last_error": _last_error,
            "last_success_at": _last_success_at,
            "last_failure_at": _last_failure_at,
            "last_probe_ok": _last_probe_ok,
            "last_probe_at": _last_probe_at if _last_probe_at else None,
            "last_probe_latency_ms": _last_probe_latency_ms,
            "last_probe_error": _last_probe_error,
        }


def _set_probe(ok: bool, error: str | None, latency_ms: float | None) -> None:
    global _last_probe_ok, _last_probe_at, _last_probe_error, _last_probe_latency_ms
    with _state_lock:
        _last_probe_ok = ok
        _last_probe_at = _now()
        _last_probe_error = error
        _last_probe_latency_ms = latency_ms


def _is_healthy() -> bool:
    """True if the cached probe says healthy, or the cooldown has expired."""
    if not config.WHISPER_REMOTE_ENABLED:
        return False
    if not config.WHISPER_REMOTE_URL:
        return False
    with _state_lock:
        if _last_probe_ok is None:
            return True  # never probed yet — assume healthy
        if _last_probe_ok:
            return True
        # Last probe failed — are we still in cooldown?
        elapsed = _now() - _last_probe_at
        return elapsed >= config.WHISPER_REMOTE_UNHEALTHY_COOLDOWN_SEC


# ---------------------------------------------------------------------------
# Eligibility — should we even try the remote for this file?
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """True if the remote is enabled and has a URL configured.

    Does NOT consider reachability — use ``is_healthy()`` for that.
    """
    return bool(config.WHISPER_REMOTE_ENABLED and config.WHISPER_REMOTE_URL)


def is_eligible(audio_path: str) -> bool:
    """True if the file is large enough to bother with the network round-trip."""
    if not is_configured():
        return False
    if not _is_healthy():
        return False
    try:
        size = os.path.getsize(audio_path)
    except OSError:
        return False
    return size >= config.WHISPER_REMOTE_MIN_FILE_BYTES


# ---------------------------------------------------------------------------
# API-style dispatch — endpoint URLs, language normalisation
# ---------------------------------------------------------------------------

# whisper.cpp's verbose_json returns the detected language as a full
# English name ("english", "german", "japanese"). The OpenAI-compatible
# servers return ISO codes directly. This map lets the normaliser
# produce a stable ISO code regardless of which server we talked to.
_LANGUAGE_NAME_TO_ISO: dict[str, str] = {
    "english": "en", "chinese": "zh", "german": "de", "spanish": "es",
    "russian": "ru", "korean": "ko", "french": "fr", "japanese": "ja",
    "portuguese": "pt", "turkish": "tr", "polish": "pl", "catalan": "ca",
    "dutch": "nl", "arabic": "ar", "swedish": "sv", "italian": "it",
    "indonesian": "id", "hindi": "hi", "finnish": "fi", "vietnamese": "vi",
    "hebrew": "he", "ukrainian": "uk", "greek": "el", "malay": "ms",
    "czech": "cs", "romanian": "ro", "danish": "da", "hungarian": "hu",
    "tamil": "ta", "norwegian": "no", "thai": "th", "urdu": "ur",
    "croatian": "hr", "bulgarian": "bg", "lithuanian": "lt", "latin": "la",
    "maori": "mi", "malayalam": "ml", "welsh": "cy", "slovak": "sk",
    "telugu": "te", "persian": "fa", "latvian": "lv", "bengali": "bn",
    "serbian": "sr", "azerbaijani": "az", "slovenian": "sl", "kannada": "kn",
    "estonian": "et", "macedonian": "mk", "breton": "br", "basque": "eu",
    "icelandic": "is", "armenian": "hy", "nepali": "ne", "mongolian": "mn",
    "bosnian": "bs", "kazakh": "kk", "albanian": "sq", "swahili": "sw",
    "galician": "gl", "marathi": "mr", "punjabi": "pa", "sinhala": "si",
    "khmer": "km", "shona": "sn", "yoruba": "yo", "somali": "so",
    "afrikaans": "af", "occitan": "oc", "georgian": "ka", "belarusian": "be",
    "tajik": "tg", "sindhi": "sd", "gujarati": "gu", "amharic": "am",
    "yiddish": "yi", "lao": "lo", "uzbek": "uz", "faroese": "fo",
    "haitian creole": "ht", "pashto": "ps", "turkmen": "tk", "nynorsk": "nn",
    "maltese": "mt", "sanskrit": "sa", "luxembourgish": "lb", "myanmar": "my",
    "tibetan": "bo", "tagalog": "tl", "malagasy": "mg", "assamese": "as",
    "tatar": "tt", "hawaiian": "haw", "lingala": "ln", "hausa": "ha",
    "bashkir": "ba", "javanese": "jv", "sundanese": "su",
}


def _language_to_iso(language: str | None) -> str:
    """Convert a language string (ISO code or English name) to ISO code.

    whisper.cpp returns full names like ``"english"``. OpenAI-compatible
    servers return ISO codes directly. This helper normalises both to
    an ISO code; returns ``""`` when the input is empty or unknown.
    """
    if not language:
        return ""
    s = language.strip()
    if not s:
        return ""
    # Already a short ISO code? Lowercase, 2-3 letters.
    if len(s) <= 3 and s.isalpha():
        return s.lower()
    # Look up the full name.
    return _LANGUAGE_NAME_TO_ISO.get(s.lower(), s)


def _probe_url() -> str:
    """Health-probe URL for the configured API style."""
    if config.WHISPER_REMOTE_API_STYLE == "whispercpp":
        return config.WHISPER_REMOTE_URL.rstrip("/") + "/health"
    return config.WHISPER_REMOTE_URL.rstrip("/") + "/v1/models"


def _transcribe_url() -> str:
    """Transcription endpoint URL for the configured API style."""
    if config.WHISPER_REMOTE_API_STYLE == "whispercpp":
        return config.WHISPER_REMOTE_URL.rstrip("/") + "/inference"
    return config.WHISPER_REMOTE_URL.rstrip("/") + "/v1/audio/transcriptions"


# ---------------------------------------------------------------------------
# Health probe — short, cheap GET against the style-specific endpoint
# ---------------------------------------------------------------------------

def probe() -> dict[str, Any]:
    """Probe the remote server. Updates cached state. Always returns a dict
    suitable for direct inclusion in /health."""
    started = _now()
    if not is_configured():
        _set_probe(False, "not configured", None)
        return {
            "ok": False,
            "configured": False,
            "enabled": config.WHISPER_REMOTE_ENABLED,
            "url": config.WHISPER_REMOTE_URL or None,
            "error": "WHISPER_REMOTE_ENABLED=0 or WHISPER_REMOTE_URL is empty",
            "latency_ms": None,
        }

    url = _probe_url()
    try:
        req = urllib.request.Request(url, method="GET")
        if config.WHISPER_REMOTE_API_KEY:
            req.add_header(
                "Authorization", f"Bearer {config.WHISPER_REMOTE_API_KEY}"
            )
        req.add_header("User-Agent", config.USER_AGENT)
        with urllib.request.urlopen(
            req, timeout=config.WHISPER_REMOTE_HEALTH_TIMEOUT_SEC
        ) as resp:
            ok = 200 <= resp.status < 300
            latency_ms = round((_now() - started) * 1000, 1)
            if ok:
                _set_probe(True, None, latency_ms)
                return {
                    "ok": True,
                    "configured": True,
                    "enabled": True,
                    "url": config.WHISPER_REMOTE_URL,
                    "status_code": resp.status,
                    "latency_ms": latency_ms,
                    "error": None,
                }
            _set_probe(False, f"HTTP {resp.status}", latency_ms)
            return {
                "ok": False,
                "configured": True,
                "enabled": True,
                "url": config.WHISPER_REMOTE_URL,
                "status_code": resp.status,
                "latency_ms": latency_ms,
                "error": f"HTTP {resp.status}",
            }
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
        latency_ms = round((_now() - started) * 1000, 1)
        err = f"{type(exc).__name__}: {exc}"
        _set_probe(False, err, latency_ms)
        return {
            "ok": False,
            "configured": True,
            "enabled": True,
            "url": config.WHISPER_REMOTE_URL,
            "latency_ms": latency_ms,
            "error": err,
        }


def startup_probe() -> None:
    """Run a probe in the calling thread if enabled — used by app_lifespan."""
    if not config.WHISPER_REMOTE_ENABLED:
        return
    if not config.WHISPER_REMOTE_PROBE_ON_STARTUP:
        return
    try:
        result = probe()
        if result["ok"]:
            log.info(
                "Remote Whisper probe OK: %s (%.0f ms)",
                config.WHISPER_REMOTE_URL,
                result.get("latency_ms") or 0.0,
            )
        else:
            log.warning(
                "Remote Whisper probe FAILED: %s — %s. Will fall back to local.",
                config.WHISPER_REMOTE_URL,
                result.get("error"),
            )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Remote Whisper probe raised: %s", exc)


# ---------------------------------------------------------------------------
# Multipart upload — hand-rolled (no requests, no httpx)
# ---------------------------------------------------------------------------

def _build_multipart_body(
    audio_path: str,
    *,
    model: str,
    language: str,
    response_format: str = "verbose_json",
    api_style: str = "openai",
) -> tuple[bytes, str]:
    """Build a ``multipart/form-data`` body for the configured API style.

    Returns ``(body_bytes, content_type_with_boundary)``.

    Hand-rolled rather than using ``email.mime.*`` because the latter is
    designed for *e-mail* messages and its quirks
    (``MIMEText(_text, _subtype, _charset)`` API where the third
    positional argument is the charset name, not a subtype string)
    produce subtly broken multipart bodies. The format itself is simple
    enough to build by hand:

        --boundary\r\n
        Content-Disposition: form-data; name="<field>"\r\n
        \r\n
        <value>\r\n
        --boundary\r\n
        Content-Disposition: form-data; name="<field>"; filename="<name>"\r\n
        Content-Type: <mime>\r\n
        \r\n
        <file bytes>\r\n
        --boundary--\r\n

    The two supported API styles emit different sets of text fields:

    * **openai**: ``model``, ``language`` (optional), ``response_format``
    * **whispercpp**: ``temperature``, ``temperature_inc``, ``response_format``

    whisper.cpp has no ``model`` field (the model is loaded server-side
    via ``/load``) and no ``language`` hint (it always auto-detects).
    """
    mime, _ = mimetypes.guess_type(audio_path)
    if mime is None:
        mime = "application/octet-stream"

    boundary = f"----WspRmBoundary{uuid.uuid4().hex}"

    crlf = b"\r\n"
    boundary_line = b"--" + boundary.encode("ascii")
    parts: list[bytes] = []

    def _text_part(name: str, value: str) -> bytes:
        return (
            boundary_line + crlf
            + f'Content-Disposition: form-data; name="{name}"'.encode("utf-8")
            + crlf
            + crlf
            + value.encode("utf-8")
            + crlf
        )

    def _file_part(name: str, filename: str, file_mime: str, data: bytes) -> bytes:
        return (
            boundary_line + crlf
            + f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode("utf-8")
            + crlf
            + f"Content-Type: {file_mime}".encode("utf-8")
            + crlf
            + crlf
            + data
            + crlf
        )

    if api_style == "whispercpp":
        parts.append(_text_part("temperature", "0.0"))
        parts.append(_text_part("temperature_inc", "0.2"))
        parts.append(_text_part("response_format", response_format))
    else:
        # openai (default)
        parts.append(_text_part("model", model))
        if language:
            parts.append(_text_part("language", language))
        parts.append(_text_part("response_format", response_format))

    with open(audio_path, "rb") as f:
        file_bytes = f.read()
    parts.append(
        _file_part("file", os.path.basename(audio_path), mime, file_bytes)
    )

    body = b"".join(parts) + boundary_line + b"--" + crlf
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


# ---------------------------------------------------------------------------
# Main entry point — transcribe_remote
# ---------------------------------------------------------------------------

def transcribe_remote(
    audio_path: str,
    model_size: str,
    language: str,
) -> dict | None:
    """Transcribe ``audio_path`` via the remote server.

    Returns the same dict shape as ``tools.video._transcribe_audio``:

        {
          "segments": [{"start": float, "end": float, "text": str}, ...],
          "language": str,
          "language_probability": float,
        }

    Returns ``None`` on ANY failure — the caller is expected to fall
    back to the local path. Never raises.
    """
    if not is_eligible(audio_path):
        return None

    _record_attempt()
    started = _now()

    url = _transcribe_url()
    api_style = config.WHISPER_REMOTE_API_STYLE
    # We always send WHISPER_REMOTE_MODEL, NOT the caller's model_size.
    # Local is always tiny; the point of the remote is to use a heavier
    # model on the GPU. Ignored by whisper.cpp (no model field).
    body, content_type = _build_multipart_body(
        audio_path,
        model=config.WHISPER_REMOTE_MODEL,
        language=language or "",
        api_style=api_style,
    )

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("User-Agent", config.USER_AGENT)
    req.add_header(
        "X-Request-ID", f"google-search-mcp-{uuid.uuid4().hex[:16]}"
    )
    if config.WHISPER_REMOTE_API_KEY:
        req.add_header(
            "Authorization", f"Bearer {config.WHISPER_REMOTE_API_KEY}"
        )

    try:
        with urllib.request.urlopen(
            req, timeout=config.WHISPER_REMOTE_TIMEOUT_SEC
        ) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        # HTTP 4xx — payload is almost certainly a structured error
        # message; read it for the log.
        try:
            err_body = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            err_body = ""
        _record_failure(f"HTTP {exc.code}: {err_body}")
        log.warning(
            "Remote Whisper returned HTTP %s: %s",
            exc.code,
            err_body or "<no body>",
        )
        # 4xx = our request was wrong; don't ban the remote for an
        # hour over a transient bad model name. Mark healthy so we
        # try again next call.
        _set_probe(True, None, round((_now() - started) * 1000, 1))
        return None
    except (
        urllib.error.URLError,
        socket.timeout,
        ConnectionError,
        TimeoutError,
        OSError,
    ) as exc:
        err = f"{type(exc).__name__}: {exc}"
        _record_failure(err)
        _set_probe(False, err, round((_now() - started) * 1000, 1))
        log.warning(
            "Remote Whisper unreachable (%s); falling back to local. "
            "Cooldown %s s before retry.",
            err,
            config.WHISPER_REMOTE_UNHEALTHY_COOLDOWN_SEC,
        )
        return None
    except Exception as exc:  # pragma: no cover - defensive
        err = f"{type(exc).__name__}: {exc}"
        _record_failure(err)
        log.warning("Remote Whisper unexpected error: %s", err)
        return None

    if not (200 <= status < 300):
        _record_failure(f"HTTP {status}: {raw[:200]!r}")
        log.warning("Remote Whisper returned non-2xx status %s", status)
        return None

    # Parse JSON response.
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _record_failure(f"json decode: {exc}")
        log.warning("Remote Whisper returned non-JSON: %s", exc)
        return None

    result = _normalise_response(payload)
    if result is None:
        _record_failure("response missing 'segments' or 'language'")
        return None

    latency_ms = round((_now() - started) * 1000, 1)
    _record_success()
    _set_probe(True, None, latency_ms)
    log.info(
        "Remote Whisper OK: %d segments, language=%s, %.1f s wallclock",
        len(result["segments"]),
        result["language"],
        latency_ms / 1000.0,
    )
    return result


def _normalise_response(payload: Any) -> dict | None:
    """Convert a verbose_json payload (openai or whisper.cpp) to internal shape.

    Handles:
    1. Standard ``verbose_json`` (OpenAI: ``language`` is ISO; whisper.cpp:
       ``language`` may be a full English name and ``language_probabilities``
       is keyed by ISO codes).
    2. Plain ``json`` (``{"text": "..."}`` — no segments, very coarse)
    3. ``text`` (not JSON) — caller would have already failed in json.loads.
    """
    if not isinstance(payload, dict):
        return None

    language_raw = payload.get("language") or ""
    segments_raw = payload.get("segments") or []
    if language_raw and isinstance(segments_raw, list) and segments_raw:
        out_segments: list[dict] = []
        for seg in segments_raw:
            if not isinstance(seg, dict):
                continue
            start = seg.get("start")
            end = seg.get("end")
            text = seg.get("text")
            if start is None or end is None or text is None:
                continue
            out_segments.append(
                {"start": float(start), "end": float(end), "text": str(text).strip()}
            )
        if not out_segments:
            return None

        # Normalise language to an ISO code. For whisper.cpp the
        # ``language`` field is a full English name; we translate via
        # _language_to_iso. For OpenAI-compatible servers it's already
        # an ISO code (passes through unchanged).
        language = _language_to_iso(str(language_raw))

        # Best-effort language probability:
        #   - OpenAI verbose_json doesn't include this — default to 1.0
        #   - whisper.cpp includes ``language_probabilities`` keyed by
        #     ISO codes; we look up our normalised code there
        prob = payload.get("language_probability")
        if prob is None:
            lang_probs = payload.get("language_probabilities") or {}
            if isinstance(lang_probs, dict) and language:
                prob = lang_probs.get(language)
        try:
            prob_f = float(prob) if prob is not None else 1.0
        except (TypeError, ValueError):
            prob_f = 1.0

        return {
            "segments": out_segments,
            "language": language,
            "language_probability": prob_f,
        }

    # Fallback: json response_format with only "text" — split into a
    # single segment. Not great, but lets the call succeed instead of
    # returning a hard error.
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return {
            "segments": [{"start": 0.0, "end": 0.0, "text": text.strip()}],
            "language": _language_to_iso(str(language_raw)),
            "language_probability": 1.0,
        }

    return None
