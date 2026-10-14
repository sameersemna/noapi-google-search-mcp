"""Phase 3 tests for the typed error model + MCP ``ToolError`` conversion."""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from google_search_mcp import errors


def test_error_hierarchy_defaults():
    assert issubclass(errors.UserInputError, errors.GoogleMcpError)
    assert issubclass(errors.UpstreamBlockedError, errors.GoogleMcpError)
    assert issubclass(errors.UpstreamError, errors.GoogleMcpError)
    assert issubclass(errors.DependencyMissingError, errors.GoogleMcpError)
    assert str(errors.UserInputError()).strip()


def test_to_tool_error_preserves_google_error_message():
    exc = errors.UpstreamBlockedError("Google is rate limiting; try later.")
    te = errors.to_tool_error(exc)
    assert isinstance(te, ToolError)
    assert "try later" in str(te)


def test_to_tool_error_generic_does_not_leak_internals():
    te = errors.to_tool_error(ValueError("SECRET internal detail"), tool="foo")
    assert isinstance(te, ToolError)
    assert "SECRET" not in str(te)
    assert "foo" in str(te) or "failed" in str(te)


def test_tool_guard_converts_typed_error():
    @errors.tool_guard
    async def t():
        raise errors.UserInputError("bad input")

    with pytest.raises(ToolError) as ei:
        asyncio.run(t())
    assert "bad input" in str(ei.value)


def test_tool_guard_converts_crash_without_leaking():
    @errors.tool_guard
    async def t():
        raise RuntimeError("boom internal")

    with pytest.raises(ToolError) as ei:
        asyncio.run(t())
    assert "boom internal" not in str(ei.value)
    assert "failed" in str(ei.value).lower()


def test_tool_guard_passes_through_result():
    @errors.tool_guard
    async def t():
        return "ok"

    assert asyncio.run(t()) == "ok"


def test_tool_guard_propagates_cancellation():
    @errors.tool_guard
    async def t():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(t())
