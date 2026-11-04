"""Tests for the native HTTP transport layer (``google_search_mcp.http_app``).

These lock in the two behaviours that make serving HTTP from this process safe,
both of which are easy to regress silently:

* the Host-header allow-list is rebuilt from config (FastMCP freezes it at
  construction time from the loopback default, which would answer 421 to a
  request addressed to ``latitude:11403``);
* the per-session FastMCP lifespan stops owning process-wide services once the
  HTTP entry point takes over (otherwise every client disconnect would close the
  shared browser pool and rebind the health port).
"""

from __future__ import annotations

import importlib
import types

import pytest

from google_search_mcp import config, http_app, server


@pytest.fixture(autouse=True)
def _restore_global_state():
    """Undo the process-global mutations these tests make."""
    external = server.is_external_lifecycle()
    settings = server.mcp.settings
    snapshot = {
        "host": settings.host,
        "port": settings.port,
        "stateless_http": settings.stateless_http,
        "json_response": settings.json_response,
        "transport_security": settings.transport_security,
    }
    try:
        yield
    finally:
        server.set_external_lifecycle(external)
        for key, value in snapshot.items():
            setattr(settings, key, value)


def _cfg(**overrides) -> types.SimpleNamespace:
    """A stand-in for ``config`` carrying only what ``http_app`` reads."""
    values = {
        "HTTP_HOST": "0.0.0.0",
        "HTTP_PORT": 11403,
        "HTTP_STATELESS": False,
        "HTTP_JSON_RESPONSE": False,
        "HTTP_ENABLE_LEGACY_SSE": True,
        "HTTP_DNS_REBINDING_PROTECTION": False,
        "HTTP_ALLOWED_HOSTS": ("127.0.0.1:*", "latitude:*"),
        "HTTP_ALLOWED_ORIGINS": ("http://127.0.0.1:*",),
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


# ── config surface ──────────────────────────────────────────────────────────


def test_transport_defaults_match_the_documented_behaviour():
    assert config.TRANSPORT == "stdio"  # stdio stays the default transport
    assert config.HTTP_PORT == 11498  # 11403 is assigned by the unit file
    assert config.HTTP_STATELESS is False
    assert config.HTTP_JSON_RESPONSE is False
    assert config.HTTP_ENABLE_LEGACY_SSE is True


def test_dns_rebinding_protection_is_off_by_default():
    """It must stay opt-in: it would 421 every request to a LAN hostname."""
    assert config.HTTP_DNS_REBINDING_PROTECTION is False
    # ...but the allow-list is still populated, so enabling it is a one-liner.
    assert "latitude:*" in config.HTTP_ALLOWED_HOSTS
    assert "127.0.0.1:*" in config.HTTP_ALLOWED_HOSTS
    assert f"{importlib.import_module('socket').gethostname()}:*" in config.HTTP_ALLOWED_HOSTS


def test_allowed_hosts_and_origins_parse_comma_separated_values(monkeypatch):
    monkeypatch.setenv("NOAPI_MCP_HTTP_ALLOWED_HOSTS", "a.example:*, b.example:11403")
    monkeypatch.setenv("NOAPI_MCP_HTTP_ALLOWED_ORIGINS", "http://a.example:*")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.HTTP_ALLOWED_HOSTS == ("a.example:*", "b.example:11403")
        assert reloaded.HTTP_ALLOWED_ORIGINS == ("http://a.example:*",)
    finally:
        monkeypatch.delenv("NOAPI_MCP_HTTP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("NOAPI_MCP_HTTP_ALLOWED_ORIGINS", raising=False)
        importlib.reload(config)


# ── settings translation ────────────────────────────────────────────────────


def test_apply_http_settings_overrides_the_frozen_transport_security():
    """Regression: FastMCP snapshots a loopback-only allow-list in __init__."""
    cfg = _cfg(HTTP_DNS_REBINDING_PROTECTION=True)

    http_app.apply_http_settings(server.mcp, cfg)

    security = server.mcp.settings.transport_security
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["127.0.0.1:*", "latitude:*"]
    assert security.allowed_origins == ["http://127.0.0.1:*"]
    assert "0.0.0.0" == server.mcp.settings.host
    assert server.mcp.settings.port == 11403


def test_apply_http_settings_honours_explicit_host_and_port():
    """A CLI ``--host`` must win over the config default, not be clobbered."""
    http_app.apply_http_settings(server.mcp, _cfg(), host="192.168.178.94", port=9999)
    assert server.mcp.settings.host == "192.168.178.94"
    assert server.mcp.settings.port == 9999


@pytest.mark.parametrize(("stateless", "json_response"), [(True, True), (False, False), (True, False)])
def test_apply_http_settings_propagates_session_model(stateless, json_response):
    cfg = _cfg(HTTP_STATELESS=stateless, HTTP_JSON_RESPONSE=json_response)
    http_app.apply_http_settings(server.mcp, cfg)
    assert server.mcp.settings.stateless_http is stateless
    assert server.mcp.settings.json_response is json_response


# ── route composition ───────────────────────────────────────────────────────


def test_route_table_serves_mcp_and_legacy_sse():
    app = http_app.build_http_app(server.mcp, _cfg())
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/mcp" in paths
    assert "/sse" in paths
    assert "/messages" in paths  # Starlette's Mount strips the trailing slash


def test_legacy_sse_routes_are_absent_when_disabled():
    app = http_app.build_http_app(server.mcp, _cfg(HTTP_ENABLE_LEGACY_SSE=False))
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/mcp" in paths
    assert "/sse" not in paths
    assert "/messages" not in paths


def test_merge_routes_drops_duplicate_paths():
    import starlette.routing

    a = starlette.routing.Route("/mcp", endpoint=lambda request: None)
    b = starlette.routing.Route("/mcp", endpoint=lambda request: None)
    c = starlette.routing.Route("/sse", endpoint=lambda request: None)

    merged = http_app._merge_routes([a], [b, c])

    assert merged == [a, c]


def test_build_http_app_takes_ownership_of_the_lifecycle():
    assert server.is_external_lifecycle() is False
    http_app.build_http_app(server.mcp, _cfg())
    assert server.is_external_lifecycle() is True


# ── lifespan ownership ──────────────────────────────────────────────────────


async def test_external_lifecycle_makes_app_lifespan_a_pass_through(monkeypatch):
    """Per-session entry must not start the health server or close the pool."""
    calls = {"startup": 0, "shutdown": 0}

    async def fake_startup(_server):
        calls["startup"] += 1
        return object()

    async def fake_shutdown(_health):
        calls["shutdown"] += 1

    monkeypatch.setattr(server, "_startup_services", fake_startup)
    monkeypatch.setattr(server, "_shutdown_services", fake_shutdown)
    server.set_external_lifecycle(True)

    async with server.app_lifespan(server.mcp) as context:
        assert context == {}

    assert calls == {"startup": 0, "shutdown": 0}


async def test_default_lifespan_runs_services_exactly_once(monkeypatch):
    """The stdio path keeps its original behaviour: one start, one stop."""
    calls = {"startup": 0, "shutdown": 0, "stopped": None}

    class _Health:
        async def stop(self):  # pragma: no cover - replaced below
            calls["stopped"] = True

    async def fake_startup(_server):
        calls["startup"] += 1
        return _Health()

    async def fake_shutdown(_health):
        calls["shutdown"] += 1

    monkeypatch.setattr(server, "_startup_services", fake_startup)
    monkeypatch.setattr(server, "_shutdown_services", fake_shutdown)
    server.set_external_lifecycle(False)

    async with server.app_lifespan(server.mcp):
        pass

    assert calls["startup"] == 1
    assert calls["shutdown"] == 1


# ── serve() wiring ─────────────────────────────────────────────────────────


def test_serve_passes_the_resolved_bind_address_to_uvicorn(monkeypatch, capsys):
    import uvicorn

    started: dict[str, object] = {}

    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app, **kwargs: started.update(app=app, **kwargs),
    )

    http_app.serve(server.mcp, host="0.0.0.0", port=11403, config_obj=_cfg())

    assert started["host"] == "0.0.0.0"
    assert started["port"] == 11403
    assert started["lifespan"] == "on"
    # Access logging off keeps service.log readable; uvicorn's own warnings only.
    assert started["access_log"] is False
    banner = capsys.readouterr().err
    assert "/mcp" in banner
    assert "stateful" in banner


def test_serve_banner_reports_stateless_mode(monkeypatch, capsys):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    http_app.serve(server.mcp, host="127.0.0.1", port=11498, config_obj=_cfg(HTTP_STATELESS=True))
    banner = capsys.readouterr().err
    assert "stateless" in banner
