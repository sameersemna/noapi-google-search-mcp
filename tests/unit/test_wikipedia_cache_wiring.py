"""Phase 5 test: the read-through cache is actually wired into a real tool.

``wikipedia`` is the reference wiring: the tool delegates to a cached
implementation, so two identical calls must hit the network only once, and
``fresh=True`` must bypass the cache.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from google_search_mcp.tools import web_utils


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    from google_search_mcp.core import cache as cachemod

    monkeypatch.setattr(cachemod.config, "ENABLE_CACHE", True)
    c = cachemod.Cache(max_entries=16, disk_path=str(tmp_path / "c.db"))
    monkeypatch.setattr(cachemod, "cache", c)
    # web_utils imported `cached` at import time; rebind the decorator's cache is
    # module-level in cachemod, so patching cachemod.cache is sufficient.
    return c


def test_wikipedia_second_call_is_served_from_cache(isolated_cache, monkeypatch):
    calls = {"n": 0}
    payload = json.dumps(
        {
            "title": "Python",
            "extract": "Python is a language.",
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Python"}},
        }
    ).encode()

    def fake_urlopen(req, timeout=10):  # noqa: ARG001
        calls["n"] += 1
        return _FakeResponse(payload)

    monkeypatch.setattr(web_utils.urllib.request, "urlopen", fake_urlopen)

    first = asyncio.run(web_utils.wikipedia("Python", language="en"))
    second = asyncio.run(web_utils.wikipedia("Python", language="en"))
    assert "Python is a language." in first
    assert first == second
    assert calls["n"] == 1, "identical wikipedia calls must be served from cache"


def test_wikipedia_fresh_bypasses_cache(isolated_cache, monkeypatch):
    calls = {"n": 0}
    payload = json.dumps({"title": "X", "extract": "text", "content_urls": {}}).encode()

    def fake_urlopen(req, timeout=10):  # noqa: ARG001
        calls["n"] += 1
        return _FakeResponse(payload)

    monkeypatch.setattr(web_utils.urllib.request, "urlopen", fake_urlopen)

    asyncio.run(web_utils.wikipedia("X", language="en"))
    asyncio.run(web_utils.wikipedia("X", language="en", fresh=True))
    assert calls["n"] == 2, "fresh=True must bypass the cache"


def test_wikipedia_registered_with_fresh_param():
    import asyncio as _asyncio

    from google_search_mcp.server import mcp

    tools = {t.name: t for t in _asyncio.run(mcp.list_tools())}
    props = tools["wikipedia"].inputSchema["properties"]
    assert "fresh" in props
