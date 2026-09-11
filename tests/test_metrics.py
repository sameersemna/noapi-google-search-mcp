"""Pytest tests for the metrics registry and the format_error helper.

Covers:
  - format_error returns a clean message and logs the real exception
  - metrics.record_tool_call accumulates counts / errors / latency
  - metrics.snapshot returns a JSON-serializable summary
  - metrics.reset clears the registry
  - The health payload includes a "metrics" section
"""

import asyncio
import json

import pytest

from google_search_mcp import metrics
from google_search_mcp.utils.text import format_error


# ---------------------------------------------------------------------------
# format_error
# ---------------------------------------------------------------------------


def test_format_error_returns_clean_message():
    msg = format_error("Google Maps search")
    assert msg == "Google Maps search failed. Please try again."
    # No raw exception text leaked
    assert "Traceback" not in msg


def test_format_error_logs_exception(caplog):
    # format_error logs the real exception at ERROR level.
    exc = ValueError("boom")
    with caplog.at_level("ERROR", logger="google_search_mcp.utils.text"):
        msg = format_error("OCR", exc)
    assert msg == "OCR failed. Please try again."
    assert any("OCR failed" in r.message and "boom" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# metrics registry
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Reset the global metrics registry before and after each test."""
    metrics.reset()
    yield
    metrics.reset()


def test_record_and_snapshot():
    metrics.record_tool_call("google_search", ok=True, latency_ms=100.0)
    metrics.record_tool_call("google_search", ok=True, latency_ms=200.0)
    metrics.record_tool_call("google_search", ok=False, latency_ms=50.0)
    metrics.record_tool_call("google_news", ok=True, latency_ms=10.0)

    snap = metrics.snapshot()
    assert snap["total_calls"] == 4
    assert snap["total_errors"] == 1
    assert snap["overall_error_rate"] == 0.25

    gs = snap["per_tool"]["google_search"]
    assert gs["calls"] == 3
    assert gs["errors"] == 1
    assert gs["error_rate"] == round(1 / 3, 4)
    assert gs["avg_latency_ms"] == round((100 + 200 + 50) / 3, 2)

    gn = snap["per_tool"]["google_news"]
    assert gn["calls"] == 1
    assert gn["errors"] == 0


def test_snapshot_is_json_serializable():
    metrics.record_tool_call("google_search", ok=True, latency_ms=1.0)
    snap = metrics.snapshot()
    json.dumps(snap)  # must not raise


def test_reset_clears():
    metrics.record_tool_call("google_search", ok=True, latency_ms=1.0)
    assert metrics.snapshot()["total_calls"] == 1
    metrics.reset()
    assert metrics.snapshot()["total_calls"] == 0
    assert metrics.snapshot()["per_tool"] == {}


# ---------------------------------------------------------------------------
# health payload integration
# ---------------------------------------------------------------------------


def test_health_payload_has_metrics():
    from google_search_mcp import health_server
    from google_search_mcp.server import mcp

    health_server.set_mcp_server(mcp)
    payload = health_server._build_health_payload()
    assert "metrics" in payload
    assert "total_calls" in payload["metrics"]
    assert "per_tool" in payload["metrics"]


def test_metrics_wrapper_records_tool_call():
    from google_search_mcp.server import mcp

    tm = mcp._tool_manager
    tools = tm._tools
    assert "check_cookies" in tools

    async def _run():
        await tools["check_cookies"].run({}, None, False)

    asyncio.run(_run())
    snap = metrics.snapshot()
    assert snap["total_calls"] == 1
    assert snap["per_tool"]["check_cookies"]["calls"] == 1
