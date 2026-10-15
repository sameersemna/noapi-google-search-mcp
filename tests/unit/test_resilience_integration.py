"""Phase 6 integration tests: the breaker fails fast, and typed errors
surface as MCP ``isError`` (ToolError) through the real tool-call path.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from google_search_mcp import server
from google_search_mcp.core import circuit, ratelimit
from google_search_mcp.errors import DependencyMissingError, UpstreamBlockedError


@pytest.fixture()
def no_throttle(monkeypatch):
    async def _noop(_url: str) -> None:
        return None

    monkeypatch.setattr(ratelimit, "acquire_for_url", _noop)


def test_browse_google_fails_fast_when_circuit_open(no_throttle, monkeypatch):
    from google_search_mcp import anti_bot

    class _OpenBreaker:
        def check(self):
            raise circuit.CircuitOpenError(remaining=300, cooldown=600)

        def record_block(self):  # pragma: no cover - not reached
            return 600

        def record_success(self):  # pragma: no cover - not reached
            pass

    monkeypatch.setattr(circuit, "google_breaker", _OpenBreaker())

    async def run():
        async with anti_bot.browse_google("https://www.google.com/search?q=x"):
            pass  # pragma: no cover - must raise before yielding

    with pytest.raises(UpstreamBlockedError) as ei:
        asyncio.run(run())
    assert "minute" in str(ei.value).lower() or "blocked" in str(ei.value).lower()


@pytest.fixture()
def patch_tool_fn():
    """Temporarily replace a registered tool's function, restoring it after."""
    saved: list[tuple[object, object]] = []

    def _patch(name: str, fn):
        tool = server.mcp._tool_manager._tools[name]
        saved.append((tool, tool.fn))
        object.__setattr__(tool, "fn", fn)
        return tool

    yield _patch
    for tool, original in saved:
        object.__setattr__(tool, "fn", original)


def test_typed_error_becomes_tool_error_via_tool_run(patch_tool_fn):
    """A tool raising a GoogleMcpError must surface as a ToolError (isError)."""

    async def _boom(*args, **kwargs):
        raise UpstreamBlockedError("Google is rate limiting; try later.")

    tool = patch_tool_fn("wikipedia", _boom)

    with pytest.raises(ToolError) as ei:
        asyncio.run(tool.run({"query": "x"}))
    assert "rate limiting" in str(ei.value)


def test_missing_dependency_error_is_actionable(patch_tool_fn):
    async def _boom(*args, **kwargs):
        raise DependencyMissingError(
            "rapidocr-onnxruntime is not installed. Install with: "
            'pip install "noapi-google-search-mcp[ocr]"'
        )

    tool = patch_tool_fn("ocr_image", _boom)

    with pytest.raises(ToolError) as ei:
        asyncio.run(tool.run({"image_source": "/tmp/x.png"}))
    assert "pip install" in str(ei.value)
