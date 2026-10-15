"""Per-upstream token-bucket rate limiting.

Sending *fewer* requests upstream is the single most effective anti-block
strategy (far better than disguising them). This module throttles calls
*before* a browser context is acquired, so a burst of tool calls cannot
hammer Google.

Design notes:

* Buckets are keyed by *host group*: all ``google.*`` hosts share one bucket
  (they share the same block budget), while unrelated upstreams (reddit, HN,
  GitHub, arXiv, Wikipedia) get their own, more generous buckets.
* ``acquire`` awaits until a token is available instead of failing, and the
  clock is injectable so tests need no real sleeping.

Verified APIs: stdlib only (``asyncio``, ``time``, ``urllib.parse``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from urllib.parse import urlparse

from .. import config


class TokenBucket:
    """Continuously-refilling token bucket with an awaitable ``acquire``."""

    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ):
        self.rate = max(rate, 1e-6)
        self.capacity = float(max(burst, 1))
        self._tokens = float(self.capacity)
        self._updated = clock()
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking: consume *tokens* if available. Returns success."""
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until *tokens* can be consumed."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            if wait > 0:
                await self._sleep(wait)


# ── Host grouping ───────────────────────────────────────────────────────────

# Generous per-host defaults (requests/second, burst) for non-Google upstreams.
_HOST_DEFAULTS: dict[str, tuple[float, int]] = {
    "reddit.com": (2.0, 3),
    "news.ycombinator.com": (2.0, 3),
    "hacker-news.firebaseio.com": (2.0, 5),
    "github.com": (1.0, 2),
    "export.arxiv.org": (0.5, 2),
    "wikipedia.org": (2.0, 3),
    "paste.rs": (1.0, 2),
    "dpaste.com": (1.0, 2),
    "dpaste.org": (1.0, 2),
    "archive.org": (0.5, 2),
    "tinyurl.com": (1.0, 2),
    "s3.amazonaws.com": (2.0, 2),
}


def _host_of(url: str) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def host_group(url: str) -> str:
    """Map a URL to its rate-limit group key."""
    host = _host_of(url)
    if not host:
        return ""
    if host == "google.com" or host.endswith(".google.com") or ".google." in host:
        return "google"
    return host


class RateLimiter:
    """Owns one :class:`TokenBucket` per host group, created on demand."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    def _params_for(self, group: str) -> tuple[float, int]:
        if group == "google":
            return config.GOOGLE_RPS, config.GOOGLE_BURST
        for suffix, params in _HOST_DEFAULTS.items():
            if group == suffix or group.endswith("." + suffix):
                return params
        return (1.0, 2)  # conservative default for unknown hosts

    async def _bucket(self, group: str) -> TokenBucket:
        async with self._lock:
            bucket = self._buckets.get(group)
            if bucket is None:
                rate, burst = self._params_for(group)
                bucket = TokenBucket(rate, burst, clock=self._clock)
                self._buckets[group] = bucket
            return bucket

    async def acquire_for_url(self, url: str) -> None:
        """Throttle before requesting *url*."""
        group = host_group(url)
        if not group:
            return
        bucket = await self._bucket(group)
        await bucket.acquire()


# Module-level limiter shared by all tools.
limiter = RateLimiter()


async def acquire_for_url(url: str) -> None:
    """Convenience wrapper around the shared limiter."""
    await limiter.acquire_for_url(url)
