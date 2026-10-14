"""Phase 5 tests for idempotency + atomic writes."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from google_search_mcp.core import atomic, idempotency


@pytest.fixture()
def idem_store(tmp_path, monkeypatch):
    s = idempotency.IdempotencyStore(str(tmp_path / "idem.db"), window_seconds=3600)
    monkeypatch.setattr(idempotency, "store", s)
    return s


# ── idempotency store ───────────────────────────────────────────────────────

def test_derived_key_is_stable_and_distinct():
    assert idempotency.derived_key("a", 1) == idempotency.derived_key("a", 1)
    assert idempotency.derived_key("a") != idempotency.derived_key("b")


def test_reserve_is_exclusive(idem_store):
    assert asyncio.run(idem_store.reserve("k", "t")) is True
    assert asyncio.run(idem_store.reserve("k", "t")) is False


def test_store_then_get_returns_result(idem_store):
    asyncio.run(idem_store.reserve("k", "t"))
    asyncio.run(idem_store.store("k", "t", "RESULT"))
    assert asyncio.run(idem_store.get("k")) == "RESULT"


def test_release_allows_retry(idem_store):
    asyncio.run(idem_store.reserve("k", "t"))
    asyncio.run(idem_store.release("k"))
    assert asyncio.run(idem_store.reserve("k", "t")) is True


def test_get_before_completion_is_none(idem_store):
    asyncio.run(idem_store.reserve("k", "t"))
    assert asyncio.run(idem_store.get("k")) is None


# ── paste_text end-to-end dedupe ────────────────────────────────────────────

def test_paste_text_posts_once_on_repeat(idem_store, monkeypatch):
    from google_search_mcp.tools import web_utils

    posts = {"n": 0}

    class _Resp:
        def __init__(self, body): self._b = body.encode()
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *e): return False

    def fake_urlopen(req, timeout=10):  # noqa: ARG001
        posts["n"] += 1
        return _Resp("https://paste.rs/abc123")

    monkeypatch.setattr(web_utils.urllib.request, "urlopen", fake_urlopen)

    first = asyncio.run(web_utils.paste_text("hello world"))
    second = asyncio.run(web_utils.paste_text("hello world"))
    assert "https://paste.rs/abc123" in first
    assert "https://paste.rs/abc123" in second
    assert idempotency.CACHED_NOTE in second
    assert posts["n"] == 1, "identical paste_text calls must post only once"


def test_paste_text_distinct_content_posts_twice(idem_store, monkeypatch):
    from google_search_mcp.tools import web_utils

    posts = {"n": 0}

    class _Resp:
        def read(self): return b"https://paste.rs/x"
        def __enter__(self): return self
        def __exit__(self, *e): return False

    def fake_urlopen(req, timeout=10):  # noqa: ARG001
        posts["n"] += 1
        return _Resp()

    monkeypatch.setattr(web_utils.urllib.request, "urlopen", fake_urlopen)
    asyncio.run(web_utils.paste_text("content A"))
    asyncio.run(web_utils.paste_text("content B"))
    assert posts["n"] == 2


# ── atomic writes ───────────────────────────────────────────────────────────

def test_atomic_write_text_and_mode(tmp_path):
    p = tmp_path / "secret.json"
    atomic.atomic_write_text(str(p), "hello")
    assert p.read_text() == "hello"
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_atomic_write_json(tmp_path):
    p = tmp_path / "d.json"
    atomic.atomic_write_json(str(p), {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}


def test_atomic_write_leaves_no_partial_on_failure(tmp_path, monkeypatch):
    p = tmp_path / "out.txt"
    p.write_text("original")

    # Simulate a crash during the write.
    real_replace = os.replace

    def boom(src, dst):
        raise RuntimeError("crash")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RuntimeError):
        atomic.atomic_write_text(str(p), "NEW")
    monkeypatch.setattr(os, "replace", real_replace)

    # Original file is untouched and no temp files are left behind.
    assert p.read_text() == "original"
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".tmp-")]
    assert not leftovers, f"temp files left behind: {leftovers}"
