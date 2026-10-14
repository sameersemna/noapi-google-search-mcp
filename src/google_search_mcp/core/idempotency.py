"""Idempotency store for side-effecting remote writes.

LLM clients *retry*. Without protection, a retried ``paste_text`` /
``shorten_url`` / ``archive_webpage`` / ``upload_to_s3`` creates a duplicate
remote object every time. This store records the result of a completed call
keyed by a hash of its arguments, so an identical repeat within a window
returns the *original* result instead of re-posting.

Two-phase reservation guards against concurrent duplicates:

1. ``reserve(key)`` → ``True`` only for the first caller (``state='in_progress'``).
2. On success ``store(key, result)`` (``state='done'``); on failure
   ``release(key)`` so a later retry can proceed.

Values are stored as JSON-friendly strings; the store is SQLite-backed so it
survives restarts. Verified APIs: stdlib only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import time

from .. import config

log = logging.getLogger("google_search_mcp.idempotency")


def derived_key(*parts: object) -> str:
    """Deterministic key from call arguments (sha256)."""
    material = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(material.encode("utf-8", "ignore")).hexdigest()


class IdempotencyStore:
    """SQLite-backed idempotency records."""

    def __init__(self, path: str, window_seconds: int):
        self._path = path
        self._window = window_seconds

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS idempotency ("
            "  key TEXT PRIMARY KEY, tool TEXT NOT NULL, state TEXT NOT NULL,"
            "  result TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,"
            "  expires_at REAL NOT NULL)"
        )
        return conn

    # ── sync core ──────────────────────────────────────────────────────────
    def _prune(self, conn: sqlite3.Connection, now: float) -> None:
        conn.execute("DELETE FROM idempotency WHERE expires_at < ?", (now,))

    def _get_sync(self, key: str) -> str | None:
        conn = None
        try:
            conn = self._connect()
            now = time.time()
            self._prune(conn, now)
            row = conn.execute(
                "SELECT result FROM idempotency WHERE key = ? AND state = 'done'"
                " AND expires_at >= ?",
                (key, now),
            ).fetchone()
            conn.commit()
            return row[0] if row else None
        except sqlite3.Error:
            return None
        finally:
            if conn is not None:
                conn.close()

    def _reserve_sync(self, key: str, tool: str) -> bool:
        conn = None
        try:
            conn = self._connect()
            now = time.time()
            self._prune(conn, now)
            cur = conn.execute(
                "INSERT OR IGNORE INTO idempotency"
                " (key, tool, state, result, created_at, expires_at)"
                " VALUES (?, ?, 'in_progress', '', ?, ?)",
                (key, tool, now, now + self._window),
            )
            conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error:
            return True  # fail open: allow the call rather than block it
        finally:
            if conn is not None:
                conn.close()

    def _store_sync(self, key: str, tool: str, result: str) -> None:
        conn = None
        try:
            conn = self._connect()
            now = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO idempotency"
                " (key, tool, state, result, created_at, expires_at)"
                " VALUES (?, ?, 'done', ?, ?, ?)",
                (key, tool, result, now, now + self._window),
            )
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            if conn is not None:
                conn.close()

    def _release_sync(self, key: str) -> None:
        conn = None
        try:
            conn = self._connect()
            conn.execute(
                "DELETE FROM idempotency WHERE key = ? AND state = 'in_progress'",
                (key,),
            )
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            if conn is not None:
                conn.close()

    # ── async API ──────────────────────────────────────────────────────────
    async def get(self, key: str) -> str | None:
        return await asyncio.to_thread(self._get_sync, key)

    async def reserve(self, key: str, tool: str) -> bool:
        return await asyncio.to_thread(self._reserve_sync, key, tool)

    async def store(self, key: str, tool: str, result: str) -> None:
        await asyncio.to_thread(self._store_sync, key, tool, result)

    async def release(self, key: str) -> None:
        await asyncio.to_thread(self._release_sync, key)


store = IdempotencyStore(
    config.IDEMPOTENCY_DB_PATH, config.IDEMPOTENCY_WINDOW_SECONDS
)

CACHED_NOTE = "(cached — identical request already completed)"


async def lookup(key: str) -> str | None:
    """Return a prior completed result for *key*, if any."""
    return await store.get(key)


async def complete(key: str, tool: str, result: str) -> None:
    """Record the successful *result* for *key*."""
    await store.store(key, tool, result)


async def abandon(key: str) -> None:
    """Clear an in-progress reservation after a failure."""
    await store.release(key)
