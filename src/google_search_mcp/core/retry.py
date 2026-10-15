"""Retry with exponential back-off + full jitter — transient failures only.

Rules (see the hardening brief):

* Retry **only** transient errors: timeouts, connection resets, 5xx, and 429
  (honouring ``Retry-After`` when available).
* **Never** retry a detected block (``UpstreamBlockedError`` /
  ``CircuitOpenError``) — that deepens the block.
* Never retry when the circuit breaker is open.
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
from collections.abc import Awaitable, Callable
from typing import TypeVar

from ..errors import UpstreamBlockedError

log = logging.getLogger("google_search_mcp.retry")

T = TypeVar("T")

#: HTTP status codes that are worth retrying.
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def is_transient(exc: BaseException) -> bool:
    """Return ``True`` if *exc* is a transient failure worth retrying."""
    # A detected block is never retried.
    if isinstance(exc, UpstreamBlockedError):
        return False
    if isinstance(
        exc,
        (
            asyncio.TimeoutError,
            TimeoutError,
            ConnectionResetError,
            ConnectionAbortedError,
            ConnectionRefusedError,
            socket.timeout,
            socket.gaierror,
        ),
    ):
        return True
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if isinstance(status, int) and status in TRANSIENT_STATUS:
        return True
    # Some libs raise OSError for network hiccups.
    return isinstance(exc, OSError) and not isinstance(exc, (FileNotFoundError, PermissionError))


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Extract a ``Retry-After`` hint (seconds) from an exception, if present."""
    value = getattr(exc, "retry_after", None)
    if value is None:
        headers = getattr(exc, "headers", None)
        if headers is not None:
            try:
                value = headers.get("Retry-After")
            except Exception:  # noqa: BLE001 - headers may be any mapping type
                value = None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: Callable[[], float] = random.random,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call *fn*, retrying transient failures with back-off + full jitter."""
    attempts = max(1, attempts)
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001 - classify then re-raise
            if isinstance(exc, asyncio.CancelledError):
                raise
            last_exc = exc
            if not is_transient(exc) or attempt >= attempts:
                raise
            ceiling = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = jitter() * ceiling
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                delay = max(delay, retry_after)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            log.warning(
                "transient failure (attempt %d/%d), retrying in %.2fs: %s",
                attempt, attempts, delay, exc,
            )
            await sleep(delay)
    assert last_exc is not None
    raise last_exc
