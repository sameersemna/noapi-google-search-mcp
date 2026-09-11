"""Lightweight in-process metrics registry for MCP tool calls.

Tracks per-tool call counts, success/failure, and latency so the
``/health`` endpoint (and any monitoring agent) can see real usage
without adding a heavy dependency like prometheus_client.

Thread-safe: all mutations happen under a lock. The registry is a
module-level singleton; tools record via :func:`record_tool_call` and
the health server reads via :func:`snapshot`.
"""

import threading
import time
from collections import defaultdict
from typing import Any


class _MetricsRegistry:
    """Thread-safe accumulator of per-tool call statistics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, int] = defaultdict(int)
        self._errors: dict[str, int] = defaultdict(int)
        self._total_latency_ms: dict[str, float] = defaultdict(float)
        self._started_at: float = time.monotonic()

    def record(
        self,
        tool: str,
        *,
        ok: bool,
        latency_ms: float,
    ) -> None:
        """Record one tool invocation.

        Args:
            tool: The tool name (e.g. ``google_search``).
            ok: Whether the call succeeded (no exception escaped).
            latency_ms: Wall-clock duration of the call in milliseconds.
        """
        with self._lock:
            self._calls[tool] += 1
            self._total_latency_ms[tool] += latency_ms
            if not ok:
                self._errors[tool] += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of all metrics."""
        with self._lock:
            calls = dict(self._calls)
            errors = dict(self._errors)
            total_latency = dict(self._total_latency_ms)

        per_tool: dict[str, Any] = {}
        for name in sorted(calls):
            n = calls[name]
            errs = errors.get(name, 0)
            per_tool[name] = {
                "calls": n,
                "errors": errs,
                "error_rate": round(errs / n, 4) if n else 0.0,
                "avg_latency_ms": round(total_latency.get(name, 0.0) / n, 2)
                if n
                else 0.0,
            }

        total_calls = sum(calls.values())
        total_errors = sum(errors.values())
        return {
            "total_calls": total_calls,
            "total_errors": total_errors,
            "overall_error_rate": round(total_errors / total_calls, 4)
            if total_calls
            else 0.0,
            "uptime_sec": round(time.monotonic() - self._started_at, 3),
            "per_tool": per_tool,
        }


# Module-level singleton.
_REGISTRY = _MetricsRegistry()


def record_tool_call(tool: str, *, ok: bool, latency_ms: float) -> None:
    """Record a single tool invocation in the global registry."""
    _REGISTRY.record(tool, ok=ok, latency_ms=latency_ms)


def snapshot() -> dict[str, Any]:
    """Return the global metrics snapshot (JSON-serializable)."""
    return _REGISTRY.snapshot()


def reset() -> None:
    """Clear all recorded metrics (mainly for tests)."""
    global _REGISTRY
    _REGISTRY = _MetricsRegistry()
