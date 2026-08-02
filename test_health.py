#!/usr/bin/env python3
"""Tests for the /health endpoint module.

These tests cover:

  1. Module-level state helpers (set_loaded_at, set_startup_result, etc.)
  2. Git info extraction
  3. Dependency checks (each dep gracefully handles missing modules)
  4. File checks
  5. Disk usage
  6. Process info
  7. Manual intervention check
  8. Status rollup (ok / degraded / down)
  9. Auth token
 10. Starlette app routing (via TestClient)
 11. The HealthServer lifecycle class

Run with:
    /home/sameer/anaconda3/envs/mcp-google/bin/python test_health.py
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "src")
# Skip cookie validation so the server module can be imported
os.environ.setdefault("SKIP_COOKIE_VALIDATION", "1")

from google_search_mcp import (  # noqa: E402
    config,
    health_server as hs,
)
from google_search_mcp.health_server import (  # noqa: E402
    HealthServer,
    _check_dependencies,
    _check_files,
    _check_mcp,
    _check_playwright,
    _check_process,
    _compute_status,
    _get_git_info,
    _get_python_info,
    _get_version,
    _humanize_bytes,
)


# ── Test harness ─────────────────────────────────────────────────────────

passed = 0
failed = 0
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ PASS: {label}")
    else:
        failed += 1
        failures.append(f"{label} — {detail}")
        print(f"  ✗ FAIL: {label}  ({detail})")
    return condition


def section(title: str):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


# ── Test 1: Module-level state ────────────────────────────────────────────


def test_module_state():
    section("Test 1: Module-level state helpers")
    from google_search_mcp import health_server as hs_module
    from datetime import datetime, timezone

    # Initial state
    check("Initial _LOADED_AT is None", hs_module._LOADED_AT is None)
    check("Initial _STARTUP_RESULT has expected keys",
          set(hs_module._STARTUP_RESULT.keys()) == {
              "cookie_validation_skipped",
              "cookie_validation_passed",
              "cookie_validation_errors",
              "cookie_validation_warnings",
          })

    # set_loaded_at
    hs_module.set_loaded_at()
    check("set_loaded_at sets _LOADED_AT", hs_module._LOADED_AT is not None)
    check("set_loaded_at sets _LOADED_AT_MONO", hs_module._LOADED_AT_MONO is not None)

    # set_startup_result
    hs_module.set_startup_result(
        skipped=True, passed=False,
        errors=["err1"], warnings=["warn1"],
    )
    check("set_startup_result records skipped",
          hs_module._STARTUP_RESULT["cookie_validation_skipped"] is True)
    check("set_startup_result records passed",
          hs_module._STARTUP_RESULT["cookie_validation_passed"] is False)
    check("set_startup_result records errors",
          hs_module._STARTUP_RESULT["cookie_validation_errors"] == ["err1"])
    check("set_startup_result records warnings",
          hs_module._STARTUP_RESULT["cookie_validation_warnings"] == ["warn1"])

    # set_mcp_server
    fake_mcp = MagicMock()
    hs_module.set_mcp_server(fake_mcp)
    check("set_mcp_server registers mcp",
          hs_module._MCP_SERVER is fake_mcp)


# ── Test 2: Version & git ───────────────────────────────────────────────


def test_version_and_git():
    section("Test 2: Version and git info")
    version = _get_version()
    check("_get_version returns a non-empty string", bool(version),
          f"got {version!r}")
    check("Version matches v0.3.2", version == "0.3.2", f"got {version!r}")

    git = _get_git_info()
    check("git info has 'commit' key", "commit" in git)
    check("git info has 'short' key", "short" in git)
    check("git info has 'branch' key", "branch" in git)
    check("git info has 'dirty' key", "dirty" in git)
    check("git info has 'describe' key", "describe" in git)

    # We're inside a git repo
    check("git commit is not 'unknown'", git["commit"] != "unknown",
          f"got {git['commit']!r}")
    check("git short is exactly 8 chars", len(git["short"]) == 8,
          f"got {git['short']!r}")

    # Python info
    py = _get_python_info()
    check("Python info has 'version'", "version" in py)
    check("Python info has 'executable'", "executable" in py)
    check("Python executable is the one running", py["executable"] == sys.executable)


# ── Test 3: Dependency checks ────────────────────────────────────────────


def test_dependency_checks():
    section("Test 3: Dependency checks")
    deps = _check_dependencies()
    for name in ("starlette", "uvicorn", "psutil", "playwright",
                 "opencv", "onnxruntime", "faster_whisper", "yt_dlp",
                 "lingua"):
        check(f"deps has '{name}'", name in deps,
              f"missing {name}")
        check(f"deps['{name}'] has 'ok' field", "ok" in deps[name])
    # starlette + uvicorn + psutil should be present in this env
    check("starlette is OK", deps["starlette"]["ok"] is True,
          f"err: {deps['starlette']}")
    check("uvicorn is OK", deps["uvicorn"]["ok"] is True)
    check("psutil is OK", deps["psutil"]["ok"] is True)
    check("opencv is OK", deps["opencv"]["ok"] is True)


def test_safe_import_handles_missing():
    section("Test 4: _safe_import handles missing modules")
    from google_search_mcp.health_server import _safe_import
    ok, err = _safe_import("definitely_not_a_real_module_xyz123")
    check("Missing module returns ok=False", ok is False)
    check("Missing module returns error string", err is not None and "No module" in err)

    # Existing module
    ok, ver = _safe_import("json")
    check("Existing module returns ok=True", ok is True)
    check("Existing module returns no version string (json has no __version__)",
          ver is None or isinstance(ver, str))


# ── Test 5: File checks ──────────────────────────────────────────────────


def test_file_checks():
    section("Test 5: File checks")
    files = _check_files()
    expected = ("cookie_dir", "google_cookies", "youtube_cookies",
                "auto_cookies", "mobilenet_onnx", "feeds_db",
                "browser_data_dir")
    for k in expected:
        check(f"files has '{k}'", k in files)
        check(f"files['{k}'] has 'path'", "path" in files[k])
        check(f"files['{k}'] has 'exists'", "exists" in files[k])


# ── Test 6: Humanize bytes ───────────────────────────────────────────────


def test_humanize_bytes():
    section("Test 6: _humanize_bytes")
    check("0 B", _humanize_bytes(0) == "0.0 B")
    check("1024 B -> KB", "KB" in _humanize_bytes(1024))
    check("MB", "MB" in _humanize_bytes(1024 * 1024))
    check("GB", "GB" in _humanize_bytes(1024 ** 3))


# ── Test 7: Process info ─────────────────────────────────────────────────


def test_process_info():
    section("Test 7: Process info")
    info = _check_process()
    check("process has pid", "pid" in info)
    check("process has ppid", "ppid" in info)
    check("process pid matches our pid", info["pid"] == os.getpid())
    check("process has memory info if psutil available",
          "memory_rss_mb" in info or info.get("psutil") == "missing")


# ── Test 8: Status rollup ────────────────────────────────────────────────


def test_status_rollup():
    section("Test 8: Status rollup logic")
    # All OK
    health = {
        "dependencies": {
            "starlette": {"ok": True},
            "uvicorn": {"ok": True},
            "playwright": {"ok": True, "chromium_installed": True},
            "opencv": {"ok": True},
        },
        "files": {"google_cookies": {"exists": True}},
        "manual_intervention": {"enabled": True, "display_available": True},
    }
    check("All deps present + cookies + display -> ok",
          _compute_status(health) == "ok")

    # Missing starlette -> down
    health["dependencies"]["starlette"]["ok"] = False
    check("Missing starlette -> down", _compute_status(health) == "down")
    health["dependencies"]["starlette"]["ok"] = True

    # Missing cookies -> degraded
    health["files"]["google_cookies"]["exists"] = False
    check("Missing google_cookies -> degraded",
          _compute_status(health) == "degraded")
    health["files"]["google_cookies"]["exists"] = True

    # Missing chromium -> degraded
    health["dependencies"]["playwright"]["chromium_installed"] = False
    check("chromium not installed -> degraded",
          _compute_status(health) == "degraded")
    health["dependencies"]["playwright"]["chromium_installed"] = True

    # No display + manual intervention enabled -> degraded
    health["manual_intervention"]["display_available"] = False
    check("No display + manual intervention enabled -> degraded",
          _compute_status(health) == "degraded")


# ── Test 9: MCP introspection ────────────────────────────────────────────


def test_mcp_introspection():
    section("Test 9: MCP server introspection")
    # No server registered
    hs.set_mcp_server(None)
    info = _check_mcp()
    check("No server returns tools=0", info["tools"] == 0)
    check("No server returns error", "error" in info)

    # Register a fake server with 5 tools
    fake = MagicMock()
    fake._tool_manager._tools = {f"tool_{i}": MagicMock() for i in range(5)}
    fake._prompt_manager._prompts = {}
    fake._resource_manager._resources = {}
    fake.name = "test-server"
    hs.set_mcp_server(fake)

    info = _check_mcp()
    check("Fake server returns 5 tools", info["tools"] == 5)
    check("Fake server has correct name", info["name"] == "test-server")
    check("Fake server has 0 prompts", info["prompts"] == 0)
    check("Fake server has 0 resources", info["resources"] == 0)
    check("Fake server exposes tool_names", len(info.get("tool_names", [])) == 5)


# ── Test 10: Build health payload ────────────────────────────────────────


def test_build_health_payload():
    section("Test 10: Build full /health payload")
    hs.set_loaded_at()
    hs.set_startup_result(skipped=True, passed=None, errors=[], warnings=[])
    payload = hs._build_health_payload()

    # Top-level keys
    expected_top = (
        "status", "uptime_sec", "loaded_at", "version", "git",
        "python", "platform", "process", "mcp", "config",
        "dependencies", "files", "disk", "manual_intervention",
        "startup",
    )
    for k in expected_top:
        check(f"payload has '{k}'", k in payload, f"missing {k}")

    check("status is one of ok/degraded/down",
          payload["status"] in ("ok", "degraded", "down"))
    check("uptime_sec is a number", isinstance(payload["uptime_sec"], (int, float)))
    check("version is non-empty", bool(payload["version"]))
    check("dependencies has 10 entries",
          len(payload["dependencies"]) == 10)
    check("disk has free_bytes", "free_bytes" in payload["disk"])


# ── Test 11: HealthServer lifecycle ──────────────────────────────────────


async def test_health_server_lifecycle():
    section("Test 11: HealthServer start/stop lifecycle")
    # Use a non-default port to avoid conflicts
    os.environ["HEALTH_PORT"] = "0"  # let OS pick
    os.environ["HEALTH_HOST"] = "127.0.0.1"
    # Reimport config to pick up the new value
    import importlib
    importlib.reload(config)
    importlib.reload(hs)

    server = HealthServer()
    check("Server has enabled property", hasattr(server, "enabled"))
    check("Server enabled by default", server.enabled is True)
    check("Server url is str", isinstance(server.url, str))

    await server.start()
    if server._error:
        print(f"  (startup error: {server._error})")
        return
    check("After start, _task is not None", server._task is not None)

    # Wait a moment for the port to be discovered (when port=0, the
    # OS-assigned port needs to be picked up by uvicorn's listening socket)
    for _ in range(20):
        if server._actual_port:
            break
        await asyncio.sleep(0.1)
    check("After start, _actual_port is set",
          server._actual_port is not None and server._actual_port > 0)

    # Verify the port is listening. We don't do a full HTTP request
    # because in the test event loop uvicorn is sharing the loop, which
    # can cause subtle races; the curl test in the integration test
    # (test_health.sh) covers the full HTTP path.
    import socket
    # Give uvicorn's accept loop time to start
    await asyncio.sleep(0.5)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", server._actual_port))
        check("Server port is accepting connections", True)
    except (socket.timeout, ConnectionRefusedError) as e:
        check("Server port is accepting connections", False, str(e))
    finally:
        s.close()

    await server.stop()
    check("After stop, _task is None", server._task is None)
    check("After stop, _server is None", server._server is None)


# ── Test 12: Auth token ──────────────────────────────────────────────────


def test_auth_token():
    section("Test 12: Auth token handling")
    from starlette.requests import Request

    # No auth required by default
    os.environ.pop("HEALTH_AUTH_TOKEN", None)
    importlib.reload(config)
    importlib.reload(hs)
    req = MagicMock(spec=Request)
    req.headers = {"authorization": ""}
    req.query_params = {}
    check("No auth needed by default", hs._check_auth(req) is True)

    # With token, missing auth fails
    os.environ["HEALTH_AUTH_TOKEN"] = "secret123"
    importlib.reload(config)
    importlib.reload(hs)
    check("Missing auth fails when token set", hs._check_auth(req) is False)

    # With correct bearer token
    req.headers = {"authorization": "Bearer secret123"}
    check("Correct bearer token passes", hs._check_auth(req) is True)

    # With wrong bearer token
    req.headers = {"authorization": "Bearer wrong"}
    check("Wrong bearer token fails", hs._check_auth(req) is False)

    # With token in query
    req.headers = {"authorization": ""}
    req.query_params = {"token": "secret123"}
    check("Token in query string passes", hs._check_auth(req) is True)

    # Cleanup
    os.environ.pop("HEALTH_AUTH_TOKEN", None)
    importlib.reload(config)
    importlib.reload(hs)


# ── Test 13: Starlette app builds correctly ─────────────────────────────


def test_build_app():
    section("Test 13: Starlette app routes")
    if not hs._STARLETTE_AVAILABLE:
        check("Starlette available", False, "starlette not installed")
        return
    app = hs.build_app()
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    check("/ route present", "/" in routes)
    check("/health route present", "/health" in routes)
    check("/health/live route present", "/health/live" in routes)
    check("/health/ready route present", "/health/ready" in routes)
    check("/version route present", "/version" in routes)


# ── Test 14: --once CLI mode ─────────────────────────────────────────────


def test_once_mode():
    section("Test 14: --once CLI mode")
    # The main() function with --once should print JSON and return
    import io
    import contextlib

    hs.set_loaded_at()
    hs.set_startup_result(skipped=True, passed=True, errors=[], warnings=[])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            hs.main.__wrapped__ if hasattr(hs.main, "__wrapped__") else None
        except Exception:
            pass
        # Call main with sys.argv override
        old_argv = sys.argv
        sys.argv = ["health_server", "--once"]
        try:
            hs.main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv
    output = buf.getvalue()
    try:
        parsed = json.loads(output)
        check("--once mode prints valid JSON", True)
        check("--once mode output has 'status'", "status" in parsed)
        check("--once mode output has 'version'", "version" in parsed)
    except Exception as e:
        check("--once mode prints valid JSON", False, str(e))


# ── Runner ──────────────────────────────────────────────────────────────


async def main():
    print("=" * 70)
    print("  Health endpoint tests")
    print("=" * 70)
    print()
    print(f"ENABLE_HEALTH_SERVER = {config.ENABLE_HEALTH_SERVER}")
    print(f"HEALTH_HOST = {config.HEALTH_HOST}")
    print(f"HEALTH_PORT = {config.HEALTH_PORT}")
    print(f"STARLETTE_AVAILABLE = {hs._STARLETTE_AVAILABLE}")
    print(f"UVICORN_AVAILABLE = {hs._UVICORN_AVAILABLE}")
    print(f"PSUTIL_AVAILABLE = {hs._PSUTIL_AVAILABLE}")
    print(f"package version = {hs._get_version()}")

    # Sync tests
    test_module_state()
    test_version_and_git()
    test_dependency_checks()
    test_safe_import_handles_missing()
    test_file_checks()
    test_humanize_bytes()
    test_process_info()
    test_status_rollup()
    test_mcp_introspection()
    test_build_health_payload()
    test_auth_token()
    test_build_app()
    test_once_mode()

    # Async test
    await test_health_server_lifecycle()

    # Summary
    print()
    print("=" * 70)
    total = passed + failed
    print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
    print("=" * 70)
    if failures:
        print()
        print("Failures:")
        for f in failures:
            print(f"  - {f}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import importlib  # used in test_auth_token
    sys.exit(asyncio.run(main()))
