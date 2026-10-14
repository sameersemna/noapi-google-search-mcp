"""Read-through TTL cache + singleflight for read-only tools.

Two problems this solves:

1. **Repeated identical lookups.** A model often calls the same read-only tool
   with the same arguments several times. Re-running the scraper is slow and is
   the single biggest cause of Google rate-limit blocks. A read-through cache
   (bounded in-memory LRU + optional on-disk SQLite tier) serves repeats
   instantly *and* sends fewer requests upstream.

2. **Thundering herd.** Two identical concurrent calls used to each do the full
   fetch. *Singleflight* makes identical in-flight calls share one execution.

Errors are never cached, and results are keyed by a canonical hash of the
arguments so ``query="Python"`` and ``query="python "`` collide (per-tool,
where safe).

Verified APIs: stdlib only (``hashlib``, ``sqlite3``, ``asyncio``).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from .. import config

log = logging.getLogger("google_search_mcp.cache")

# Bump when the on-disk value format changes so old entries are ignored.
_SCHEMA_VERSION = 1

# Per-tool TTLs (seconds). Unknown tools fall back to ``config.CACHE_TTL_SECONDS``.
PER_TOOL_TTL: dict[str, int] = {
    "google_search": 3600,
    "google_scholar": 3600,
    "google_books": 3600,
    "google_news": 600,
    "google_weather": 900,
    "google_finance": 60,
    "google_trends": 6 * 3600,
    "google_maps": 24 * 3600,
    "google_maps_directions": 6 * 3600,
    "google_translate": 7 * 24 * 3600,
    "wikipedia": 7 * 24 * 3600,
    "google_images": 1800,
    "google_shopping": 1800,
    "google_hotels": 1800,
    "google_flights": 1800,
    "visit_page": 900,
}


def _normalize_value(value: Any) -> Any:
    """Normalize an argument value so semantically-equal calls share a key."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped.casefold() if stripped else ""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_normalize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in sorted(value.items())}
    return value


def canonical_key(tool: str, args: dict[str, Any]) -> str:
    """Deterministic cache key for a tool call (sha256)."""
    normalized = {
        k: _normalize_value(v)
        for k, v in args.items()
        if k not in ("ctx", "fresh") and v is not None and v != ""
    }
    payload = json.dumps(
        {"tool": tool, "args": normalized, "v": _SCHEMA_VERSION},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _MemoryTier:
    """Bounded in-memory LRU with per-entry TTL."""

    def __init__(self, max_entries: int):
        self._store: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._max = max_entries
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.time():
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        async with self._lock:
            self._store[key] = (time.time() + ttl, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()


class _DiskTier:
    """Optional on-disk SQLite tier so restarts keep warm results."""

    def __init__(self, path: str):
        self._path = path
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            "  key TEXT PRIMARY KEY, value TEXT NOT NULL,"
            "  expires_at REAL NOT NULL, created_at REAL NOT NULL)"
        )
        return conn

    def _get_sync(self, key: str) -> str | None:
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            value, expires_at = row
            if expires_at < time.time():
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                conn.commit()
                return None
            return value
        except sqlite3.Error:
            return None
        finally:
            if conn is not None:
                conn.close()

    def _set_sync(self, key: str, value: str, ttl: int) -> None:
        conn = None
        try:
            now = time.time()
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, expires_at, created_at)"
                " VALUES (?, ?, ?, ?)",
                (key, value, now + ttl, now),
            )
            conn.execute("DELETE FROM cache WHERE expires_at < ?", (now,))
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            if conn is not None:
                conn.close()

    async def get(self, key: str) -> str | None:
        return await asyncio.to_thread(self._get_sync, key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        await asyncio.to_thread(self._set_sync, key, value, ttl)


class Cache:
    """Two-tier read-through cache with singleflight."""

    def __init__(self, *, max_entries: int = 512, disk_path: str | None = None):
        self.memory = _MemoryTier(max_entries)
        self.disk = _DiskTier(disk_path) if disk_path else None
        self._inflight: dict[str, asyncio.Future] = {}
        self._inflight_lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0
        self.singleflight_collapsed = 0

    async def get(self, key: str) -> str | None:
        if not config.ENABLE_CACHE:
            return None
        value = await self.memory.get(key)
        if value is None and self.disk is not None:
            value = await self.disk.get(key)
            if value is not None:
                await self.memory.set(key, value, config.CACHE_TTL_SECONDS)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        if not config.ENABLE_CACHE:
            return
        await self.memory.set(key, value, ttl)
        if self.disk is not None:
            await self.disk.set(key, value, ttl)

    async def singleflight(
        self, key: str, factory: Callable[[], Awaitable[str]]
    ) -> str:
        """Run *factory* once for concurrent identical *key* calls.

        The owner runs the factory *outside* the registry lock so waiters can
        find (and share) the in-flight future instead of serialising.
        """
        async with self._inflight_lock:
            fut = self._inflight.get(key)
            if fut is not None:
                self.singleflight_collapsed += 1
                owner = False
            else:
                fut = asyncio.get_running_loop().create_future()
                self._inflight[key] = fut
                owner = True

        if not owner:
            return await fut

        try:
            result = await factory()
        except BaseException as exc:  # noqa: BLE001 - propagate to waiters
            if not fut.done():
                fut.set_exception(exc)
            raise
        else:
            if not fut.done():
                fut.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)


# Module-level default cache used by the ``cached`` decorator.
cache = Cache(
    max_entries=config.CACHE_MAX_ENTRIES,
    disk_path=config.CACHE_DB_PATH,
)


def cached(tool_name: str, ttl: int | None = None):
    """Decorator: read-through cache a read-only async tool function.

    The cache key is derived from the function's bound arguments (excluding
    ``ctx`` and ``fresh``). Only successful results are cached; exceptions
    propagate and are never stored. ``fresh=True`` bypasses the read.
    """
    effective_ttl = ttl if ttl is not None else PER_TOOL_TTL.get(
        tool_name, config.CACHE_TTL_SECONDS
    )

    def _decorator(fn: Callable[..., Awaitable[str]]):
        sig = inspect.signature(fn)

        @wraps(fn)
        async def _wrapper(*args, **kwargs):
            try:
                bound = sig.bind(*args, **kwargs)
            except TypeError:
                return await fn(*args, **kwargs)
            bound.apply_defaults()
            call_args = dict(bound.arguments)
            fresh = bool(call_args.get("fresh", False))
            key = canonical_key(tool_name, call_args)

            if not fresh:
                hit = await cache.get(key)
                if hit is not None:
                    return hit

            result = await cache.singleflight(key, lambda: fn(*args, **kwargs))
            if isinstance(result, str) and result:
                await cache.set(key, result, effective_ttl)
            return result

        return _wrapper

    return _decorator
