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


def test_stdio_transport_calls_mcp_run(monkeypatch):
    """stdio still goes through ``mcp.run`` — it owns that event loop."""
    from google_search_mcp import server

    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(server.mcp, "run", fake_run)
    cli_main(["--transport", "stdio", "--host", "127.0.0.1", "--port", "11498"])
    assert captured.get("transport") == "stdio"
    assert server.mcp.settings.host == "127.0.0.1"
    assert server.mcp.settings.port == 11498


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
def test_http_transports_are_served_by_http_app(monkeypatch, transport):
    """HTTP transports delegate to ``http_app.serve``, not ``mcp.run``.

    ``mcp.run(transport=...)`` was retired for HTTP: FastMCP's lifespan is
    entered once per session, so running it there started the health server and
    closed the browser pool on every client connect/disconnect.
    """
    from google_search_mcp import http_app, server

    served: dict[str, object] = {}

    def fake_serve(mcp_arg, host=None, port=None, **kwargs):
        served["mcp"] = mcp_arg
        served["host"] = host
        served["port"] = port

    def forbidden_run(**kwargs):  # pragma: no cover - must never fire
        raise AssertionError(f"mcp.run must not be used for {transport}")

    monkeypatch.setattr(http_app, "serve", fake_serve)
    monkeypatch.setattr(server.mcp, "run", forbidden_run)

    cli_main(["--transport", transport, "--host", "0.0.0.0", "--port", "11403"])

    assert served["mcp"] is server.mcp
    assert served["host"] == "0.0.0.0"
    assert served["port"] == 11403
    assert server.mcp.settings.host == "0.0.0.0"
    assert server.mcp.settings.port == 11403


def test_http_transport_defaults_come_from_config(monkeypatch):
    from google_search_mcp import config, http_app

    monkeypatch.setattr(config, "HTTP_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "HTTP_PORT", 11498)

    served: dict[str, object] = {}
    monkeypatch.setattr(
        http_app, "serve", lambda *a, **kw: served.update(a=a, kw=kw)
    )
    cli_main(["--transport", "streamable-http"])

    assert served["kw"]["host"] == "127.0.0.1"
    assert served["kw"]["port"] == 11498


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
