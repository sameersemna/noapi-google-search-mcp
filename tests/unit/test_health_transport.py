"""Tests for the ``/health`` transport field.

``/health`` used to hardcode ``"transport": "stdio"``. Every HTTP deployment
therefore advertised stdio — the same kind of stale label that made the retired
``mcp-proxy --transport streamable-http`` flag look effective when it was being
silently ignored.

The field must describe what the process is *actually* serving. The CLI resolves
``--transport`` (falling back to ``NOAPI_MCP_TRANSPORT``), so the value has to be
recorded at startup rather than inferred from config in the health module.
"""

from __future__ import annotations

import pytest

from google_search_mcp import health_server, server
from google_search_mcp import main as cli_main


@pytest.fixture(autouse=True)
def _restore_transport():
    original = health_server.get_active_transport()
    try:
        yield
    finally:
        health_server.set_active_transport(original)


def test_health_reports_the_recorded_transport():
    health_server.set_mcp_server(server.mcp)
    health_server.set_active_transport("streamable-http")
    assert health_server._check_mcp()["transport"] == "streamable-http"


def test_health_reports_stdio_by_default():
    health_server.set_active_transport("stdio")
    assert health_server._check_mcp()["transport"] == "stdio"


def test_empty_transport_falls_back_to_stdio():
    health_server.set_active_transport("")
    assert health_server.get_active_transport() == "stdio"


def test_tool_inventory_is_still_reported_alongside_the_transport():
    """The transport field is additive: the tool list must keep working."""
    health_server.set_mcp_server(server.mcp)
    info = health_server._check_mcp()
    assert info["tools"] == len(server.mcp._tool_manager._tools)
    assert "google_search" in info["tool_names"]


@pytest.mark.parametrize("transport", ["stdio", "sse", "streamable-http"])
def test_cli_records_the_transport_it_serves(monkeypatch, transport):
    """Regression: /health said 'stdio' for a process serving HTTP."""
    from google_search_mcp import http_app

    async def fake_startup(_server):
        return None

    async def noop(*_a, **_k):
        return None

    # Never actually bind a port or run uvicorn.
    monkeypatch.setattr(http_app, "serve", lambda *a, **k: None)
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: None)
    monkeypatch.setattr(server, "_startup_services", fake_startup)
    monkeypatch.setattr(server, "_shutdown_services", noop)

    cli_main(["--transport", transport, "--host", "127.0.0.1", "--port", "11498"])

    assert health_server.get_active_transport() == transport


# starlette.testclient warns that its httpx binding is deprecated. Filtered here
# rather than globally so a real deprecation elsewhere is still visible.
@pytest.mark.filterwarnings("ignore:Using `httpx` with `starlette.testclient`")
def test_health_endpoint_reflects_the_transport(monkeypatch):
    """End-to-end through the ASGI app, not just the helper."""
    from starlette.testclient import TestClient

    health_server.set_mcp_server(server.mcp)
    health_server.set_active_transport("streamable-http")

    app = health_server.build_app()
    with TestClient(app) as client:
        payload = client.get("/health").json()

    assert payload["mcp"]["transport"] == "streamable-http"
    assert payload["mcp"]["tools"] == len(server.mcp._tool_manager._tools)


def test_transport_is_not_read_from_config():
    """Guards the regression directly: config.TRANSPORT is only the *default*.

    A process started with ``--transport streamable-http`` keeps
    ``config.TRANSPORT == "stdio"``, so reading config here would be wrong.
    """
    from google_search_mcp import config

    health_server.set_active_transport("streamable-http")
    if config.TRANSPORT == "stdio":
        assert health_server._check_mcp()["transport"] == "streamable-http"
