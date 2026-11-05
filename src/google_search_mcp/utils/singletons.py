"""Process-wide caching for heavyweight native engines.

Why this module exists
----------------------
``faster_whisper.WhisperModel``, ``rapidocr_onnxruntime.RapidOCR`` and
``onnxruntime.InferenceSession`` each allocate a large native arena — model
weights plus ctranslate2 / ONNX Runtime thread pools, tens to hundreds of
megabytes per instance.

Constructing one per tool call looked harmless: each call returns its result and
the local name goes out of scope. It is not harmless. Those native arenas are
not promptly returned to the OS, so they accumulate for the lifetime of the
server process. Observed in production: the server reached **6.9 GB RSS** after
a handful of transcription calls, exhausted a 14 GB machine, and the kernel
OOM-killer killed the editor and then the service itself.

Caching one instance per engine name fixes it: a repeated transcription reuses
the model it already paid for, and switching model size releases the old one
first, so memory stays bounded instead of growing with usage.

Design notes
------------
* **One slot per engine name.** At most one Whisper model and one OCR engine are
  resident. Replacing is deliberate: keeping every model size alive at once
  would reintroduce the same unbounded growth by another route.
* **Construction happens under the lock.** Two concurrent first calls would
  otherwise each build a model, and the second would leak the first. Building is
  serialised; *using* the engine is not.
* **Opt-out.** ``NOAPI_MCP_CACHE_ENGINES=0`` restores build-per-call for
  debugging, at the cost of the leak.
"""

from __future__ import annotations

import gc
import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from .. import config

log = logging.getLogger("google_search_mcp.engines")

_T = TypeVar("_T")

# name -> (key, instance)
_engines: dict[str, tuple[tuple[Any, ...], Any]] = {}
_lock = threading.RLock()


def cached_engine(
    name: str,
    key: tuple[Any, ...],
    factory: Callable[[], _T],
    *,
    enabled: bool | None = None,
) -> _T:
    """Return the cached engine for *name*, building it via *factory* if needed.

    Args:
        name: Engine family — ``"whisper"``, ``"rapidocr"``, ``"mobilenet"``.
            Names are the cache slot, so two call sites that want the same
            engine should share one name.
        key: Everything the engine is parameterised by (e.g. the model size). A
            different key replaces the resident instance rather than adding one.
        factory: Zero-argument callable that constructs the engine. Only called
            on a miss.
        enabled: Override ``config.CACHE_ENGINES`` (tests use this).

    Returns:
        The engine instance.
    """
    if enabled is None:
        enabled = config.CACHE_ENGINES
    if not enabled:
        return factory()

    with _lock:
        slot = _engines.get(name)
        if slot is not None and slot[0] == key:
            log.debug("engine cache hit: %s %s", name, key)
            return slot[1]

        log.debug("engine cache miss: %s %s (building)", name, key)
        instance = factory()

        if slot is not None:
            # Drop the old entry *before* installing the new one so the
            # released engine can be collected immediately.
            _engines.pop(name, None)
            _release(slot[1])

        _engines[name] = (key, instance)
        return instance


def _release(engine: Any) -> None:
    """Drop a reference to *engine* and collect, so native memory goes back.

    ``del`` only clears this frame's reference; the real release happens when
    the cache entry and any other references are gone, which ``gc.collect()``
    then completes for reference cycles. Best-effort by design — a failure here
    must never break a tool call.
    """
    try:
        del engine
        gc.collect()
    except Exception:  # pragma: no cover - defensive
        log.debug("engine release failed", exc_info=True)


def clear_engines() -> None:
    """Drop every cached engine (shutdown, or between tests)."""
    with _lock:
        engines = list(_engines.values())
        _engines.clear()
    for _key, engine in engines:
        _release(engine)


def engine_cache_info() -> dict[str, tuple[Any, ...]]:
    """Return ``{name: key}`` for the resident engines — diagnostics only."""
    with _lock:
        return {name: key for name, (key, _engine) in _engines.items()}
