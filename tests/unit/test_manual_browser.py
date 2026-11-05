"""Tests for ``--manual-browser`` — the headful window that shares the session.

The command exists so an operator can do things by hand (log in, accept a consent
dialog, find a feed URL) and have the MCP tools pick up the result. Two properties
matter and are easy to break:

* it must never start the MCP server, the health endpoint or the browser pool;
* it must use a profile of its own. The service holds
  ``~/.config/google-mcp-browser`` for its whole lifetime and that profile is
  ~1.3 GB, so sharing the directory is neither safe (concurrent SQLite writers on
  ``Cookies``) nor practical to copy. The session is shared through the cookie
  file instead.
"""

from __future__ import annotations

import os

import pytest

from google_search_mcp import config, manual_browser
from google_search_mcp import main as cli_main


@pytest.fixture()
def no_display(monkeypatch):
    monkeypatch.setattr(
        "google_search_mcp.browser._check_display_available", lambda: False
    )


@pytest.fixture()
def display(monkeypatch):
    monkeypatch.setattr(
        "google_search_mcp.browser._check_display_available", lambda: True
    )


# ── profile selection ───────────────────────────────────────────────────────


def test_default_profile_is_separate_from_the_service_profile():
    """Sharing BROWSER_DATA_DIR would contend with the running service."""
    default = manual_browser._profile_dir(None)
    assert default != config.BROWSER_DATA_DIR
    assert default.endswith("google-mcp-browser-manual")
    assert default.startswith(config.CONFIG_DIR)


def test_profile_override_is_expanded():
    got = manual_browser._profile_dir("~/some/profile")
    assert got == os.path.expanduser("~/some/profile")
    assert os.path.isabs(got)


def test_profile_override_accepts_a_relative_path():
    got = manual_browser._profile_dir("rel/profile")
    assert os.path.isabs(got)


# ── no display ─────────────────────────────────────────────────────────────


def test_no_display_returns_a_failure_and_does_not_launch(monkeypatch, no_display):
    called = {"session": 0}

    async def fake_session(*_a, **_k):  # pragma: no cover - must not run
        called["session"] += 1
        return 0

    monkeypatch.setattr(manual_browser, "_session", fake_session)

    assert manual_browser.run_manual_browser() == 1
    assert called["session"] == 0


# ── the session wiring ────────────────────────────────────────────────────


def test_run_passes_url_profile_and_timeout_to_the_session(monkeypatch, display, tmp_path):
    seen: dict[str, object] = {}

    async def fake_session(url, profile, timeout_sec):
        seen.update(url=url, profile=profile, timeout_sec=timeout_sec)
        return 0

    monkeypatch.setattr(manual_browser, "_session", fake_session)
    rc = manual_browser.run_manual_browser(
        "https://example.com/feeds", profile_dir=str(tmp_path / "p"), timeout_sec=30
    )

    assert rc == 0
    assert seen["url"] == "https://example.com/feeds"
    assert seen["timeout_sec"] == 30
    assert str(tmp_path / "p") in str(seen["profile"])


def test_run_creates_the_profile_directory(monkeypatch, display, tmp_path):
    target = tmp_path / "fresh" / "profile"

    async def fake_session(*_a, **_k):
        return 0

    monkeypatch.setattr(manual_browser, "_session", fake_session)
    manual_browser.run_manual_browser(profile_dir=str(target))
    assert target.is_dir()


def test_negative_timeout_is_clamped(monkeypatch, display):
    seen: dict[str, object] = {}

    async def fake_session(_url, _profile, timeout_sec):
        seen["timeout"] = timeout_sec
        return 0

    monkeypatch.setattr(manual_browser, "_session", fake_session)
    manual_browser.run_manual_browser(timeout_sec=-5)
    assert seen["timeout"] == 0


def test_empty_url_falls_back_to_the_default(monkeypatch, display):
    seen: dict[str, object] = {}

    async def fake_session(url, _profile, _timeout):
        seen["url"] = url
        return 0

    monkeypatch.setattr(manual_browser, "_session", fake_session)
    manual_browser.run_manual_browser("")
    assert seen["url"] == manual_browser.DEFAULT_URL


# ── CLI routing ────────────────────────────────────────────────────────────


def test_cli_routes_manual_browser_and_does_not_start_the_server(monkeypatch):
    recorded: dict[str, object] = {}

    def fake_run(url, *, profile_dir=None, timeout_sec=0):
        recorded.update(url=url, profile_dir=profile_dir, timeout_sec=timeout_sec)
        return 0

    def forbidden(*_a, **_k):  # pragma: no cover - must never fire
        raise AssertionError("--manual-browser must not start the MCP transport")

    monkeypatch.setattr("google_search_mcp.manual_browser.run_manual_browser", fake_run)
    from google_search_mcp import http_app, server

    monkeypatch.setattr(server.mcp, "run", forbidden)
    monkeypatch.setattr(http_app, "serve", forbidden)

    with pytest.raises(SystemExit) as exit_info:
        cli_main(["--manual-browser", "https://example.com", "--browser-timeout", "15"])

    assert exit_info.value.code == 0
    assert recorded["url"] == "https://example.com"
    assert recorded["timeout_sec"] == 15


def test_cli_manual_browser_without_a_url_uses_the_default(monkeypatch):
    recorded: dict[str, object] = {}

    def fake_run(url, *, profile_dir=None, timeout_sec=0):  # noqa: ARG001
        recorded["url"] = url
        return 0

    monkeypatch.setattr("google_search_mcp.manual_browser.run_manual_browser", fake_run)

    with pytest.raises(SystemExit):
        cli_main(["--manual-browser"])

    assert recorded["url"] == "https://www.google.com"


def test_cli_forwards_the_profile_override(monkeypatch, tmp_path):
    recorded: dict[str, object] = {}

    def fake_run(_url, *, profile_dir=None, timeout_sec=0):  # noqa: ARG001
        recorded["profile_dir"] = profile_dir
        return 0

    monkeypatch.setattr("google_search_mcp.manual_browser.run_manual_browser", fake_run)

    with pytest.raises(SystemExit):
        cli_main(["--manual-browser", "--browser-profile", str(tmp_path / "prof")])

    assert str(tmp_path / "prof") in str(recorded["profile_dir"])


def test_cli_exit_code_follows_the_command(monkeypatch, no_display):
    """A failed window (no display) must surface as a non-zero exit code."""
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["--manual-browser"])
    assert exit_info.value.code == 1
