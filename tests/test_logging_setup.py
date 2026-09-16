"""Tests for request-level tool-call logging.

The MCP SDK logs only the request *type* ("Processing request of type
CallToolRequest"), which makes the service log hard to follow. We wrap the
registered ``CallToolRequest`` handler so the tool name and a redacted
argument summary are logged instead.

These tests cover the pure formatting helpers plus the handler wrapper, so a
future SDK change that breaks the wrapper is caught here rather than silently
degrading the logs.
"""

import asyncio
import logging

import pytest

from google_search_mcp import logging_setup


# ── argument formatting ────────────────────────────────────────────────


def test_format_arguments_empty():
    assert logging_setup.format_tool_arguments(None) == ""
    assert logging_setup.format_tool_arguments({}) == ""


def test_format_arguments_basic():
    result = logging_setup.format_tool_arguments({"query": "python", "num_results": 5})
    assert "query='python'" in result
    assert "num_results=5" in result


@pytest.mark.parametrize(
    "key",
    ["password", "PASSWORD", "api_key", "token", "secret_key", "authorization"],
)
def test_format_arguments_redacts_secrets(key):
    """Credential-bearing arguments must never reach the log."""
    result = logging_setup.format_tool_arguments({key: "super-secret-value"})
    assert "super-secret-value" not in result
    assert "***" in result


def test_format_arguments_truncates_long_values():
    """Base64 images would otherwise flood the log."""
    result = logging_setup.format_tool_arguments({"image_source": "A" * 5000})
    assert len(result) < 200
    assert "chars>" in result


def test_format_arguments_truncates_total_length():
    args = {f"key{i}": "value" * 20 for i in range(50)}
    result = logging_setup.format_tool_arguments(args)
    assert len(result) <= logging_setup._MAX_ARGS_CHARS + 3


def test_format_arguments_handles_non_dict():
    assert logging_setup.format_tool_arguments("not-a-dict") == ""


# ── handler wrapper ────────────────────────────────────────────────────


class _FakeParams:
    def __init__(self, name, arguments=None):
        self.name = name
        self.arguments = arguments


class _FakeRequest:
    def __init__(self, name, arguments=None):
        self.params = _FakeParams(name, arguments)


class _FakeServer:
    def __init__(self, handler):
        self.request_handlers = {_call_tool_request_type(): handler}


class _FakeMCP:
    def __init__(self, handler):
        self._mcp_server = _FakeServer(handler)


def _call_tool_request_type():
    from mcp import types

    return types.CallToolRequest


def test_install_wraps_handler_and_logs_tool_name(caplog):
    """The wrapper must log the tool name and forward the result unchanged."""
    calls = []

    async def original(req):
        calls.append(req)
        return "tool-result"

    mcp = _FakeMCP(original)
    assert logging_setup.install_tool_call_logging(mcp) is True

    handler = mcp._mcp_server.request_handlers[_call_tool_request_type()]
    with caplog.at_level(logging.INFO, logger="google_search_mcp"):
        result = asyncio.run(handler(_FakeRequest("google_search", {"query": "x"})))

    assert result == "tool-result"
    assert len(calls) == 1
    assert "CallToolRequest: google_search" in caplog.text
    assert "query='x'" in caplog.text


def test_install_is_idempotent():
    """A second install must not double-wrap (which would double-log)."""

    async def original(req):
        return None

    mcp = _FakeMCP(original)
    assert logging_setup.install_tool_call_logging(mcp) is True
    assert logging_setup.install_tool_call_logging(mcp) is False


def test_install_returns_false_on_unknown_layout():
    """An unrecognised SDK layout must degrade gracefully, not raise."""

    class _Weird:
        pass

    assert logging_setup.install_tool_call_logging(_Weird()) is False


def test_wrapper_propagates_exceptions(caplog):
    """Tool errors must still surface to the caller."""

    async def original(req):
        raise ValueError("boom")

    mcp = _FakeMCP(original)
    logging_setup.install_tool_call_logging(mcp)
    handler = mcp._mcp_server.request_handlers[_call_tool_request_type()]

    with caplog.at_level(logging.INFO, logger="google_search_mcp"):
        with pytest.raises(ValueError, match="boom"):
            asyncio.run(handler(_FakeRequest("failing_tool")))

    assert "CallToolRequest: failing_tool" in caplog.text


def test_real_server_has_wrapper_installed():
    """The production server must actually install the wrapper."""
    from google_search_mcp.server import mcp
    from mcp import types

    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    assert getattr(handler, "_gsmcp_logged", False) is True


# ── logging configuration ──────────────────────────────────────────────


def test_configure_logging_does_not_duplicate_handlers():
    """Calling configure twice must not add a second handler."""
    root = logging.getLogger()
    before = len(root.handlers)
    logging_setup.configure_logging()
    logging_setup.configure_logging()
    assert len(root.handlers) == before


def test_configure_logging_accepts_invalid_level():
    """A bogus LOG_LEVEL must fall back to INFO, not crash."""
    logging_setup.configure_logging("NOT_A_LEVEL")
    assert logging.getLogger().level == logging.INFO
