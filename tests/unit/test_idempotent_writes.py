"""Phase 5 tests: idempotent remote writes (shorten_url, archive_webpage)."""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp.core import idempotency
from google_search_mcp.tools import web_utils


@pytest.fixture()
def idem_store(tmp_path, monkeypatch):
    s = idempotency.IdempotencyStore(str(tmp_path / "idem.db"), window_seconds=3600)
    monkeypatch.setattr(idempotency, "store", s)
    return s


class _Resp:
    def __init__(self, body: str):
        self._b = body.encode()
        self.url = "https://web.archive.org/web/2026/https://example.com/"

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_shorten_url_posts_once_on_repeat(idem_store, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):  # noqa: ARG001
        calls["n"] += 1
        return _Resp("https://tinyurl.com/abc")

    monkeypatch.setattr(web_utils.urllib.request, "urlopen", fake_urlopen)

    first = asyncio.run(web_utils.shorten_url("https://example.com/very/long/path"))
    second = asyncio.run(web_utils.shorten_url("https://example.com/very/long/path"))
    assert "tinyurl.com/abc" in first
    assert "tinyurl.com/abc" in second
    assert idempotency.CACHED_NOTE in second
    assert calls["n"] == 1, "identical shorten_url calls must hit the API once"


def test_shorten_url_rejects_file_scheme(idem_store):
    from google_search_mcp.errors import UserInputError

    with pytest.raises(UserInputError):
        asyncio.run(web_utils.shorten_url("file:///etc/passwd"))


def test_archive_webpage_dedupes_on_repeat(idem_store, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):  # noqa: ARG001
        calls["n"] += 1
        return _Resp("https://web.archive.org/web/2026/https://example.com/")

    monkeypatch.setattr(web_utils.urllib.request, "urlopen", fake_urlopen)

    first = asyncio.run(web_utils.archive_webpage("https://example.com/article"))
    second = asyncio.run(web_utils.archive_webpage("https://example.com/article"))
    assert "Archived:" in first
    assert idempotency.CACHED_NOTE in second
