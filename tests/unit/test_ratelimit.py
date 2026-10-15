"""Phase 6 tests for token-bucket rate limiting (deterministic fake clock)."""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp.core import ratelimit


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_try_acquire_consumes_burst_then_fails():
    clock = FakeClock()
    bucket = ratelimit.TokenBucket(rate=1.0, burst=2, clock=clock)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False
    clock.advance(1.0)
    assert bucket.try_acquire() is True


def test_refill_is_rate_limited():
    clock = FakeClock()
    bucket = ratelimit.TokenBucket(rate=0.5, burst=1, clock=clock)
    assert bucket.try_acquire() is True
    clock.advance(1.0)  # 0.5 tokens only
    assert bucket.try_acquire() is False
    clock.advance(1.0)  # now 1.0 token
    assert bucket.try_acquire() is True


def test_acquire_waits_then_succeeds():
    clock = FakeClock()
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds)

    bucket = ratelimit.TokenBucket(rate=1.0, burst=1, clock=clock, sleep=fake_sleep)
    asyncio.run(bucket.acquire())  # consumes the only token
    asyncio.run(bucket.acquire())  # must wait ~1s
    assert waits and waits[0] == pytest.approx(1.0, abs=0.01)


def test_google_hosts_share_one_bucket():
    for host in (
        "https://www.google.com/search?q=x",
        "https://scholar.google.com/scholar?q=x",
        "https://maps.google.com/",
        "https://news.google.com/",
        "https://trends.google.com/",
    ):
        assert ratelimit.host_group(host) == "google", host


def test_non_google_hosts_get_their_own_group():
    assert ratelimit.host_group("https://www.reddit.com/r/x/.rss") == "www.reddit.com"
    assert ratelimit.host_group("https://export.arxiv.org/api") == "export.arxiv.org"
    assert ratelimit.host_group("") == ""


def test_google_bucket_uses_config_rate():
    limiter = ratelimit.RateLimiter()
    bucket = asyncio.run(limiter._bucket("google"))
    assert bucket.rate == pytest.approx(ratelimit.config.GOOGLE_RPS)
    assert bucket.capacity == float(ratelimit.config.GOOGLE_BURST)


def test_acquire_for_url_is_noop_for_empty():
    asyncio.run(ratelimit.acquire_for_url(""))
