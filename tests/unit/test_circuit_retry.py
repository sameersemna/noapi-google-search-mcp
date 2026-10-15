"""Phase 6 tests for the circuit breaker and retry policy (fake clock)."""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp.core import circuit, retry
from google_search_mcp.errors import UpstreamBlockedError


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


# ── circuit breaker ─────────────────────────────────────────────────────────

def test_breaker_starts_closed():
    b = circuit.CircuitBreaker(clock=FakeClock())
    assert b.is_open is False
    b.check()  # must not raise


def test_block_opens_circuit_for_cooldown():
    clock = FakeClock()
    b = circuit.CircuitBreaker(base_cooldown=600, max_cooldown=3600, clock=clock)
    b.record_block()
    assert b.is_open is True
    with pytest.raises(circuit.CircuitOpenError):
        b.check()
    assert b.remaining() == pytest.approx(600)


def test_circuit_closes_after_cooldown():
    clock = FakeClock()
    b = circuit.CircuitBreaker(base_cooldown=600, max_cooldown=3600, clock=clock)
    b.record_block()
    clock.advance(601)
    assert b.is_open is False
    b.check()


def test_cooldown_escalates_exponentially_up_to_max():
    clock = FakeClock()
    b = circuit.CircuitBreaker(base_cooldown=600, max_cooldown=3600, clock=clock)
    assert b.record_block() == 600
    assert b.record_block() == 1200
    assert b.record_block() == 2400
    assert b.record_block() == 3600
    assert b.record_block() == 3600  # clamped


def test_success_resets_backoff():
    clock = FakeClock()
    b = circuit.CircuitBreaker(base_cooldown=600, max_cooldown=3600, clock=clock)
    b.record_block()
    b.record_block()
    b.record_success()
    assert b.is_open is False
    assert b.record_block() == 600  # back to base after a success


def test_circuit_open_error_is_upstream_blocked():
    # So tool_guard/error handling treats it uniformly.
    assert issubclass(circuit.CircuitOpenError, UpstreamBlockedError)
    err = circuit.CircuitOpenError(remaining=120, cooldown=600)
    assert "minute" in str(err)


# ── retry ───────────────────────────────────────────────────────────────────

def test_is_transient_classification():
    assert retry.is_transient(asyncio.TimeoutError()) is True
    assert retry.is_transient(ConnectionResetError()) is True
    assert retry.is_transient(ValueError("bad")) is False
    # A detected block is NEVER transient.
    assert retry.is_transient(UpstreamBlockedError("blocked")) is False

    class _HttpErr(Exception):
        status = 503

    assert retry.is_transient(_HttpErr()) is True

    class _NotFound(Exception):
        status = 404

    assert retry.is_transient(_NotFound()) is False


def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}
    slept: list[float] = []

    async def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise asyncio.TimeoutError()
        return "ok"

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    result = asyncio.run(
        retry.with_retries(fn, attempts=5, jitter=lambda: 1.0, sleep=fake_sleep)
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert slept == [0.5, 1.0]  # exponential


def test_retry_gives_up_after_attempts():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise asyncio.TimeoutError()

    async def noop(_s: float) -> None:
        return None

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(retry.with_retries(fn, attempts=3, sleep=noop))
    assert calls["n"] == 3


def test_retry_does_not_retry_blocks():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise UpstreamBlockedError("blocked")

    async def noop(_s: float) -> None:
        return None

    with pytest.raises(UpstreamBlockedError):
        asyncio.run(retry.with_retries(fn, attempts=5, sleep=noop))
    assert calls["n"] == 1, "a detected block must not be retried"


def test_retry_does_not_retry_nontransient():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise ValueError("permanent")

    async def noop(_s: float) -> None:
        return None

    with pytest.raises(ValueError):
        asyncio.run(retry.with_retries(fn, attempts=5, sleep=noop))
    assert calls["n"] == 1


def test_retry_honours_retry_after():
    calls = {"n": 0}
    slept: list[float] = []

    class _TooMany(Exception):
        status = 429
        retry_after = 5

    async def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _TooMany()
        return "ok"

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    assert asyncio.run(
        retry.with_retries(fn, jitter=lambda: 0.0, sleep=fake_sleep)
    ) == "ok"
    assert slept == [5.0], "Retry-After must be respected"
