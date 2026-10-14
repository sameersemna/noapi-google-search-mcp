"""Phase 3 tests for namespaced ``NOAPI_MCP_*`` configuration.

New tunables must be readable via ``NOAPI_MCP_<NAME>`` while the legacy bare
name keeps working (backward compatibility).
"""

from __future__ import annotations

import importlib

from google_search_mcp import config


def test_env_helper_prefers_namespaced(monkeypatch):
    monkeypatch.setenv("FOO_SETTING", "legacy")
    monkeypatch.setenv("NOAPI_MCP_FOO_SETTING", "namespaced")
    assert config.env("FOO_SETTING") == "namespaced"


def test_env_helper_falls_back_to_legacy(monkeypatch):
    monkeypatch.delenv("NOAPI_MCP_FOO_SETTING", raising=False)
    monkeypatch.setenv("FOO_SETTING", "legacy")
    assert config.env("FOO_SETTING") == "legacy"


def test_env_helper_default(monkeypatch):
    monkeypatch.delenv("NOAPI_MCP_FOO_SETTING", raising=False)
    monkeypatch.delenv("FOO_SETTING", raising=False)
    assert config.env("FOO_SETTING", "dflt") == "dflt"


def test_env_helpers_types(monkeypatch):
    monkeypatch.setenv("NOAPI_MCP_SOME_INT", "7")
    monkeypatch.setenv("NOAPI_MCP_SOME_FLOAT", "1.5")
    monkeypatch.setenv("NOAPI_MCP_SOME_FLAG", "yes")
    assert config.env_int("SOME_INT", 0) == 7
    assert config.env_float("SOME_FLOAT", 0.0) == 1.5
    assert config.env_flag("SOME_FLAG", False) is True
    # invalid int falls back to default rather than crashing
    monkeypatch.setenv("NOAPI_MCP_BAD_INT", "not-an-int")
    assert config.env_int("BAD_INT", 42) == 42


def test_default_settings_present():
    # Values later phases depend on must exist with sane defaults.
    assert config.MAX_BROWSERS >= 1
    assert config.ALLOWED_ROOTS, "ALLOWED_ROOTS must default to at least the home dir"
    assert config.ALLOW_PRIVATE_URLS is True  # D5: LAN-friendly default
    assert config.GOOGLE_RPS > 0
    assert config.TRANSPORT in ("stdio", "streamable-http", "sse")
    assert config.CIRCUIT_COOLDOWN_SECONDS > 0


def test_namespaced_override_takes_effect(monkeypatch):
    monkeypatch.setenv("NOAPI_MCP_MAX_BROWSERS", "9")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.MAX_BROWSERS == 9
    finally:
        monkeypatch.delenv("NOAPI_MCP_MAX_BROWSERS", raising=False)
        importlib.reload(config)
