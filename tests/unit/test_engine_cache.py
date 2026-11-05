"""Heavyweight native engines must be built once, not once per call.

The bug this guards against
---------------------------
``_transcribe_audio_local`` constructed a ``faster_whisper.WhisperModel`` on
every call, and nothing released it. ctranslate2 allocates its model weights and
thread pool arenas natively; CPython does not return them promptly, so they
accumulated for the life of the process.

Observed consequence: after a handful of ``transcribe_local`` calls the server
sat at **6.9 GB RSS** on a 14 GB machine. The kernel OOM-killer then killed the
editor (`code`) and finally the server itself:

    Out of memory: Killed process 310235 (python) anon-rss:6873480kB
    task_memcg=/system.slice/mcp-google.service

The same build-per-call shape existed at three more sites: ``ocr_image``
(RapidOCR), the scanned-PDF OCR fallback (RapidOCR), and the CAPTCHA classifier
(``onnxruntime.InferenceSession``).

What is asserted here
---------------------
1. The cache returns one instance per (name, key) and builds it only once.
2. A different key **replaces** the resident engine instead of adding a second
   one — otherwise switching model size reintroduces unbounded growth.
3. Every call site actually routes through the cache. These read the shipped
   source, because the alternative is constructing a real Whisper model in a
   unit test, which is exactly the memory cost this exists to avoid.
"""

from __future__ import annotations

import inspect

import pytest

from google_search_mcp.utils import singletons


class _Engine:
    """Stand-in for a native engine: records construction and release."""

    def __init__(self, key, log):
        self.key = key
        self.log = log
        log.append(("build", key))

    def __del__(self):
        try:
            self.log.append(("del", self.key))
        except Exception:  # pragma: no cover - interpreter shutdown
            pass


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch):
    monkeypatch.setattr(singletons.config, "CACHE_ENGINES", True)
    singletons.clear_engines()
    yield
    singletons.clear_engines()


def test_factory_runs_once_for_the_same_key():
    log: list = []
    factory = lambda: _Engine("tiny", log)  # noqa: E731

    first = singletons.cached_engine("whisper", ("tiny",), factory)
    second = singletons.cached_engine("whisper", ("tiny",), factory)

    assert first is second
    assert log.count(("build", "tiny")) == 1


def test_different_keys_do_not_build_twice_concurrently_kept():
    """A new key replaces the old engine; only one stays resident."""
    log: list = []

    singletons.cached_engine("whisper", ("tiny",), lambda: _Engine("tiny", log))
    singletons.cached_engine("whisper", ("base",), lambda: _Engine("base", log))

    info = singletons.engine_cache_info()
    assert info == {"whisper": ("base",)}, "only one Whisper model may be resident"


def test_same_key_returns_cached_instance_after_a_switch_back():
    log: list = []
    singletons.cached_engine("whisper", ("tiny",), lambda: _Engine("tiny", log))
    singletons.cached_engine("whisper", ("base",), lambda: _Engine("base", log))
    singletons.cached_engine("whisper", ("tiny",), lambda: _Engine("tiny", log))

    # tiny was rebuilt after being evicted; that is correct (bounded memory
    # beats rebuild avoidance), and base must no longer be resident.
    assert singletons.engine_cache_info() == {"whisper": ("tiny",)}
    assert log.count(("build", "tiny")) == 2


def test_distinct_names_are_independent_slots():
    log: list = []
    singletons.cached_engine("whisper", ("tiny",), lambda: _Engine("w", log))
    singletons.cached_engine("rapidocr", (), lambda: _Engine("r", log))

    assert set(singletons.engine_cache_info()) == {"whisper", "rapidocr"}


def test_disabled_cache_builds_every_time(monkeypatch):
    monkeypatch.setattr(singletons.config, "CACHE_ENGINES", False)
    log: list = []
    factory = lambda: _Engine("tiny", log)  # noqa: E731

    singletons.cached_engine("whisper", ("tiny",), factory)
    singletons.cached_engine("whisper", ("tiny",), factory)

    assert log.count(("build", "tiny")) == 2
    assert singletons.engine_cache_info() == {}


def test_clear_engines_drops_everything():
    singletons.cached_engine("whisper", ("tiny",), lambda: _Engine("t", []))
    singletons.clear_engines()
    assert singletons.engine_cache_info() == {}


# ── call sites must use the cache ──────────────────────────────────────────


def _src(module) -> str:
    return inspect.getsource(module)


def test_transcribe_path_does_not_construct_whisper_per_call():
    from google_search_mcp.tools import video

    src = _src(video)
    assert "cached_engine(" in src, "video.py must route Whisper through the cache"
    # The only construction may be inside the cached factory (a lambda), so a
    # bare assignment like `model = WhisperModel(...)` must not exist.
    assert "model = WhisperModel(" not in src, (
        "WhisperModel must not be constructed per call — that is the 6.9 GB leak"
    )


def test_ocr_sites_use_the_cache():
    from google_search_mcp.tools import vision, web_utils

    for name, module in (("vision.py", vision), ("web_utils.py", web_utils)):
        src = _src(module)
        assert "cached_engine(" in src, f"{name} must use the engine cache"
        assert "engine = RapidOCR()" not in src, (
            f"{name} constructs RapidOCR per call — that leaks an ORT arena"
        )
        assert "ocr = RapidOCR()" not in src, (
            f"{name} constructs RapidOCR per call — that leaks an ORT arena"
        )


def test_captcha_classifier_uses_the_cache():
    from google_search_mcp import captcha

    src = _src(captcha)
    assert "cached_engine(" in src
    assert "session = ort.InferenceSession(" not in src, (
        "an InferenceSession per CAPTCHA leaked an ORT arena on every block"
    )


def test_engine_cache_is_on_by_default():
    from google_search_mcp import config

    assert config.CACHE_ENGINES is True, (
        "defaulting to off would restore the leak for every deployment"
    )
