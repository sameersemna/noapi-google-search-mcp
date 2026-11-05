"""OCR microservice — RapidOCR (PaddleOCR models on ONNX Runtime) over HTTP.

Why a separate service
----------------------
The MCP server on ``latitude`` runs OCR through RapidOCR, which loads ~300 MB of
ONNX models and burns CPU on every page. On a 14 GB box shared with ELK that is
the largest remaining avoidable consumer, and OCR is exactly the kind of bursty,
CPU-bound work a Grace-Blackwell host does well. Moving it off ``latitude``
removes both the RSS and the CPU spikes.

Design constraints
------------------
* **Same engine, same output.** This wraps the *identical* RapidOCR build the MCP
  server uses, and returns its native shape:

      {"result": [[box, text, confidence], ...], "elapse": [det, cls, rec]}

  so the client swap is mechanical and the parsing logic upstream is unchanged.

* **Load the model once.** Building ``RapidOCR()`` per request was the bug that
  took the MCP process to 6.9 GB (native arenas are not promptly returned to the
  OS). The engine is constructed at startup and reused for the process lifetime —
  which is the main reason this design is safe at all.

* **Bounded concurrency.** OCR is CPU-bound and ONNX Runtime already
  multi-threads per inference. Letting requests pile up makes everything slower,
  so calls serialise behind a semaphore (``OCR_CONCURRENCY``, default 2).

* **No secrets on the command line.** An optional bearer token is read from
  ``OCR_SERVER_TOKEN``; empty means no auth, which is fine on a trusted LAN.

Run:  uvicorn app:app --host 0.0.0.0 --port 8769
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import Any

import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("ocr-server")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

MAX_UPLOAD_BYTES = int(os.environ.get("OCR_MAX_UPLOAD_BYTES", 25 * 1024 * 1024))
CONCURRENCY = max(1, int(os.environ.get("OCR_CONCURRENCY", "2")))
TOKEN = os.environ.get("OCR_SERVER_TOKEN", "").strip()

# RapidOCR's own language/model selection (v1.4 accepts these file paths; both
# default to the bundled models, so normally leave them unset).
DET_MODEL = os.environ.get("OCR_DET_MODEL") or None
CLS_MODEL = os.environ.get("OCR_CLS_MODEL") or None
REC_MODEL = os.environ.get("OCR_REC_MODEL") or None

app = FastAPI(title="ocr-server", version="1.0.0")

_engine: Any = None
_engine_lock = threading.Lock()
_semaphore = threading.BoundedSemaphore(CONCURRENCY)
_stats = {"requests": 0, "errors": 0, "last_error": None}
_stats_lock = threading.Lock()


def get_engine() -> Any:
    """Return the process-wide RapidOCR engine, building it on first use.

    Built under a lock: two concurrent first requests would otherwise each load
    ~300 MB of models and leak the loser.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR

            kwargs: dict[str, Any] = {}
            if DET_MODEL:
                kwargs["det_model_path"] = DET_MODEL
            if CLS_MODEL:
                kwargs["cls_model_path"] = CLS_MODEL
            if REC_MODEL:
                kwargs["rec_model_path"] = REC_MODEL
            log.info("loading RapidOCR models (once per process)")
            _engine = RapidOCR(**kwargs)
            log.info("RapidOCR ready")
    return _engine


def _run_ocr(image_path: str) -> dict[str, Any]:
    """Synchronous OCR call — runs in a worker thread, never on the event loop."""
    with _semaphore:
        engine = get_engine()
        result, elapse = engine(image_path)

    if not result:
        return {"result": [], "elapse": _normalise_elapse(elapse)}

    boxes: list[list[Any]] = []
    for entry in result:
        box, text, confidence = entry[0], entry[1], entry[2]
        boxes.append([
            np.asarray(box, dtype=float).round(2).tolist(),
            str(text),
            round(float(confidence), 6),
        ])
    return {"result": boxes, "elapse": _normalise_elapse(elapse)}


def _normalise_elapse(elapse: Any) -> list[float]:
    """RapidOCR returns (det, cls, rec) seconds. Coerce to plain floats."""
    if not elapse:
        return [0.0, 0.0, 0.0]
    try:
        values = [float(v) for v in elapse]
    except (TypeError, ValueError):
        return [0.0, 0.0, 0.0]
    while len(values) < 3:
        values.append(0.0)
    return [round(v, 4) for v in values[:3]]


def _check_auth(request: Request) -> None:
    if not TOKEN:
        return
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and header[7:] == TOKEN:
        return
    raise HTTPException(status_code=401, detail="unauthorized")


# ── routes ────────────────────────────────────────────────────────────────


@app.get("/")
async def index() -> dict[str, Any]:
    """Liveness + discoverability. Served even before the model is loaded."""
    return {
        "service": "ocr-server",
        "engine": "rapidocr-onnxruntime",
        "endpoints": {"POST /ocr": "multipart 'file' -> text regions"},
        "ready": _engine is not None,
        "concurrency": CONCURRENCY,
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    with _stats_lock:
        stats = dict(_stats)
    return {"ok": True, "model_loaded": _engine is not None, **stats}


@app.post("/ocr")
async def ocr(request: Request, file: UploadFile = File(...)) -> JSONResponse:
    """OCR one image.

    Accepts a multipart upload in the field ``file``. Returns RapidOCR's native
    shape so the MCP server can use the response without translation.
    """
    _check_auth(request)

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"upload is {len(payload)} bytes, limit is {MAX_UPLOAD_BYTES}",
        )

    suffix = os.path.splitext(file.filename or "")[1] or ".img"
    with _stats_lock:
        _stats["requests"] += 1

    # RapidOCR takes a path, not bytes. Write to a private temp file and always
    # remove it — a leaked temp dir per call would fill the disk.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=suffix, prefix="ocr_", delete=False
        ) as tmp:
            tmp.write(payload)
            tmp_path = tmp.name
        result = await run_in_threadpool(_run_ocr, tmp_path)
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        with _stats_lock:
            _stats["errors"] += 1
            _stats["last_error"] = f"{type(exc).__name__}: {exc}"
        log.exception("OCR failed for %s", file.filename)
        raise HTTPException(status_code=500, detail=f"OCR failed: {exc}") from exc
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@app.on_event("startup")
async def warmup() -> None:
    """Load the models at boot so the first real request is not slow.

    A first-request load would arrive as a multi-second latency spike that looks
    like a failure to the client, which has a timeout.
    """
    if os.environ.get("OCR_WARMUP", "1").strip().lower() in ("1", "true", "yes", "on"):
        await run_in_threadpool(get_engine)
