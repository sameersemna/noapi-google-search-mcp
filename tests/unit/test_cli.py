"""Phase 9 tests for the CLI entry points (`--version`, `--check`, transports)."""

from __future__ import annotations

import importlib

import pytest

import google_search_mcp
from google_search_mcp import main as cli_main


def test_version_flag_prints_version(capsys):
    cli_main(["--version"])
    out = capsys.readouterr().out
    assert google_search_mcp.__version__ in out
    assert "noapi-google-search-mcp" in out


def test_check_flag_exits_zero_when_required_deps_present(capsys):
    with pytest.raises(SystemExit) as ei:
        cli_main(["--check"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "Dependencies:" in out
    assert "mcp" in out and "playwright" in out
    # Critical items must be reported as required.
    assert "[required]" in out


def test_dependency_report_shape():
    report, ok = google_search_mcp._dependency_report()
    assert isinstance(report, str) and report
    assert isinstance(ok, bool)
    # The mandatory core deps are installed in the dev env.
    assert ok is True


def test_has_module_detects_missing():
    assert google_search_mcp._has_module("mcp") is True
    assert google_search_mcp._has_module("definitely_not_a_real_module_xyz") is False


def test_chromium_installed_is_bool():
    assert isinstance(google_search_mcp._chromium_installed(), bool)


@pytest.mark.parametrize("transport", ["stdio", "sse", "streamable-http"])
def test_transport_flag_selects_transport(monkeypatch, transport):
    from google_search_mcp import server

    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(server.mcp, "run", fake_run)
    cli_main(["--transport", transport, "--host", "127.0.0.1", "--port", "11498"])
    assert captured.get("transport") == transport
    assert server.mcp.settings.host == "127.0.0.1"
    assert server.mcp.settings.port == 11498


def test_default_transport_is_stdio(monkeypatch):
    from google_search_mcp import config, server

    captured: dict[str, object] = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: captured.update(kw))
    monkeypatch.setattr(config, "TRANSPORT", "stdio")
    cli_main([])
    assert captured.get("transport") == "stdio"


def test_invalid_transport_from_env_falls_back_to_stdio(monkeypatch):
    from google_search_mcp import config, server

    captured: dict[str, object] = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: captured.update(kw))
    monkeypatch.setattr(config, "TRANSPORT", "bogus")
    cli_main([])
    assert captured.get("transport") == "stdio"


def test_importing_main_module_does_not_start_server(monkeypatch):
    """Regression: __main__ used to call main() at import time (unguarded)."""
    called = {"n": 0}
    monkeypatch.setattr(google_search_mcp, "main", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    mod = importlib.import_module("google_search_mcp.__main__")
    importlib.reload(mod)
    assert called["n"] == 0, "importing the module must not start the server"
