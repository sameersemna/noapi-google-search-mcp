"""Phase 5 tests for the read-through TTL cache + singleflight."""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp.core import cache as cachemod


@pytest.fixture()
def fresh_cache(monkeypatch):
    monkeypatch.setattr(cachemod.config, "ENABLE_CACHE", True)
    c = cachemod.Cache(max_entries=16, disk_path=None)
    monkeypatch.setattr(cachemod, "cache", c)
    return c


def test_canonical_key_normalizes_case_and_whitespace():
    a = cachemod.canonical_key("wikipedia", {"query": "  Python ", "language": "en"})
    b = cachemod.canonical_key("wikipedia", {"query": "python", "language": "EN"})
    assert a == b


def test_canonical_key_drops_empty_and_none():
    a = cachemod.canonical_key("t", {"q": "x", "site": "", "region": None})
    b = cachemod.canonical_key("t", {"q": "x"})
    assert a == b


def test_canonical_key_differs_for_different_args():
    assert cachemod.canonical_key("t", {"q": "a"}) != cachemod.canonical_key("t", {"q": "b"})
    assert cachemod.canonical_key("t", {"q": "a"}) != cachemod.canonical_key("u", {"q": "a"})


def test_cache_hit_avoids_second_call(fresh_cache):
    calls = {"n": 0}

    @cachemod.cached("wikipedia", ttl=60)
    async def tool(query: str):
        calls["n"] += 1
        return f"result:{query}"

    assert asyncio.run(tool("hello")) == "result:hello"
    assert asyncio.run(tool("hello")) == "result:hello"
    assert calls["n"] == 1, "second identical call must be served from cache"


def test_fresh_bypasses_cache_read(fresh_cache):
    calls = {"n": 0}

    @cachemod.cached("wikipedia", ttl=60)
    async def tool(query: str, fresh: bool = False):
        calls["n"] += 1
        return f"r{calls['n']}"

    assert asyncio.run(tool("q")) == "r1"
    assert asyncio.run(tool("q")) == "r1"          # cached
    assert asyncio.run(tool("q", fresh=True)) == "r2"  # bypassed
    assert calls["n"] == 2


def test_errors_are_not_cached(fresh_cache):
    calls = {"n": 0}

    @cachemod.cached("wikipedia", ttl=60)
    async def tool(query: str):
        calls["n"] += 1
        raise RuntimeError("boom")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            asyncio.run(tool("q"))
    assert calls["n"] == 2, "errors must not be cached"


def test_singleflight_collapses_concurrent_identical_calls(fresh_cache):
    calls = {"n": 0}

    async def scenario():
        @cachemod.cached("google_search", ttl=60)
        async def tool(query: str):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return f"result:{query}"

        return await asyncio.gather(*[tool("same") for _ in range(5)])

    results = asyncio.run(scenario())
    assert results == ["result:same"] * 5
    assert calls["n"] == 1, "singleflight must collapse identical concurrent calls"


def test_cache_disabled_still_works(monkeypatch):
    monkeypatch.setattr(cachemod.config, "ENABLE_CACHE", False)
    c = cachemod.Cache(max_entries=4, disk_path=None)
    monkeypatch.setattr(cachemod, "cache", c)
    calls = {"n": 0}

    @cachemod.cached("wikipedia", ttl=60)
    async def tool(query: str):
        calls["n"] += 1
        return "x"

    asyncio.run(tool("q"))
    asyncio.run(tool("q"))
    assert calls["n"] == 2, "when disabled the cache must be a pass-through"


def test_disk_tier_roundtrip(tmp_path):
    c = cachemod.Cache(max_entries=4, disk_path=str(tmp_path / "c.db"))
    asyncio.run(c.set("k1", "v1", 60))
    # a second cache instance sharing the disk file should see the value
    c2 = cachemod.Cache(max_entries=4, disk_path=str(tmp_path / "c.db"))
    assert asyncio.run(c2.get("k1")) == "v1"
