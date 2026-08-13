"""Health / metrics HTTP endpoint for the MCP server.

Exposes a small Starlette app on a separate port (default 11499) that
returns JSON status info about the running MCP service. Independent of
the MCP transport — works whether the server is launched with stdio
(behind mcp-proxy), streamable_http, or SSE.

Endpoints
---------

  GET  /              Welcome / index — list of endpoints
  GET  /health        Full health snapshot (JSON)
  GET  /health/live   Liveness probe — 200 OK if process is alive
  GET  /health/ready  Readiness probe — 200 if critical deps present
  GET  /version       Just the version + git commit info

The `/health` response is intentionally large so an operator (or
monitoring agent) can see the full picture of the service in one call:

    {
      "status": "ok" | "degraded" | "down",
      "uptime_sec": 123.4,
      "loaded_at": "2026-08-02T12:34:56.789+00:00",
      "version": "0.3.2",
      "git": {"commit": "abc123...", "short": "abc1234", "branch": "main",
              "dirty": false, "describe": "v0.3.2-3-gabc1234"},
      "build": {"py_compiled_at": "2026-08-02T11:00:00+00:00"},
      "python": {"version": "3.11.15 ...", "implementation": "CPython",
                 "executable": "/path/to/python"},
      "platform": {"system": "Linux", "release": "6.5.0", "machine": "x86_64",
                   "node": "latitude", "pid": 1234, "ppid": 1,
                   "threads": 12, "memory_rss_mb": 87.4,
                   "memory_vms_mb": 256.0, "cpu_percent": 1.2},
      "mcp": {"name": "google-search", "tools": 41, "prompts": 0,
              "resources": 0, "uptime_sec": 123.4},
      "config": {"ENABLE_MANUAL_INTERVENTION": true,
                 "MANUAL_INTERVENTION_TIMEOUT_SEC": 300,
                 "SKIP_COOKIE_VALIDATION": true, ...},
      "dependencies": {
          "playwright":  {"ok": true, "version": "1.45.0",
                          "chromium_installed": true,
                          "chromium_path": "/root/.cache/ms-playwright/..."},
          "opencv":      {"ok": true, "version": "4.10.0.84"},
          "onnxruntime": {"ok": true, "version": "1.19.2"},
          "faster_whisper": {"ok": true, "version": "1.0.3"},
          "yt_dlp":      {"ok": true, "version": "2024.05.27"},
          "rapidocr":    {"ok": true},
          "lingua":      {"ok": true, "version": "2.0.13"},
          "psutil":      {"ok": true, "version": "5.9.8"},
          "starlette":   {"ok": true, "version": "0.40.0"},
          "uvicorn":     {"ok": true, "version": "0.30.1"},
      },
      "files": {
          "cookie_dir":      {"path": "...", "exists": true,
                              "txt_file_count": 2, "total_size_bytes": 4096},
          "google_cookies":  {"exists": true, "size_bytes": 2048,
                              "modified_at": "2026-08-01T..."},
          "youtube_cookies": {"exists": true, "size_bytes": 1024,
                              "modified_at": "2026-08-01T..."},
          "auto_cookies":    {"exists": true, "size_bytes": 8192},
          "mobilenet_onnx":  {"exists": true, "size_bytes": 13631488},
          "feeds_db":        {"exists": true, "size_bytes": 524288},
      },
      "disk": {"cache_dir": "...", "free_bytes": 12345678901,
               "free_human": "11.5 GB", "total_bytes": 50000000000,
               "used_percent": 75.3},
      "manual_intervention": {
          "enabled": true,
          "display_available": true,
          "currently_active": false,
          "last_result": {"timestamp": ..., "resolved": true, ...}
      },
      "startup": {
          "cookie_validation_skipped": true,
          "cookie_validation_passed": null,
          ...
      }
    }

Design notes
------------

- The server is started in `app_lifespan` (server.py) and lives for the
  lifetime of the MCP process. On shutdown, the uvicorn server is
  gracefully stopped.
- All dependency checks are non-blocking / fast — we never launch a
  browser for a health check. We only check that Playwright is
  importable and that `chromium` has been installed via
  `playwright install chromium`.
- Each check is wrapped in a try/except so one broken dep doesn't
  break the whole health response.
- Health endpoint can be protected with `HEALTH_AUTH_TOKEN` (bearer
  token) for production deployments exposed publicly.
"""

from __future__ import annotations

import asyncio
import functools
import os
import platform as _platform
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Try to import starlette / uvicorn. They're standard in most environments
# (transitively via playwright or mcp) but we degrade gracefully if not.
try:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route
    _STARLETTE_AVAILABLE = True
except Exception:  # pragma: no cover - dependency missing
    Starlette = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]
    PlainTextResponse = None  # type: ignore[assignment]
    Route = None  # type: ignore[assignment]
    _STARLETTE_AVAILABLE = False

try:
    import uvicorn  # type: ignore
    _UVICORN_AVAILABLE = True
except Exception:  # pragma: no cover - dependency missing
    uvicorn = None  # type: ignore[assignment]
    _UVICORN_AVAILABLE = False

try:
    import psutil  # type: ignore
    _PSUTIL_AVAILABLE = True
except Exception:  # pragma: no cover - dependency missing
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════
# Module-level state shared between lifespan and the HTTP handlers
# ═══════════════════════════════════════════════════════════════════════════

# Service start time (set by `set_loaded_at()` in app_lifespan)
_LOADED_AT: datetime | None = None
_LOADED_AT_MONO: float | None = None  # monotonic time of process start

# Cookie validation result (set by lifespan, surfaced in /health)
_STARTUP_RESULT: dict[str, Any] = {
    "cookie_validation_skipped": None,
    "cookie_validation_passed": None,
    "cookie_validation_errors": [],
    "cookie_validation_warnings": [],
}

# Reference to the mcp server object (set by `set_mcp_server()`)
_MCP_SERVER: Any = None


def set_loaded_at(when: datetime | None = None) -> None:
    """Mark the service as loaded. Called from app_lifespan after cookie
    validation completes (so the loaded_at timestamp reflects when the
    service is actually ready, not just when the Python process started)."""
    global _LOADED_AT, _LOADED_AT_MONO
    _LOADED_AT = when or datetime.now(timezone.utc)
    _LOADED_AT_MONO = time.monotonic()


def set_startup_result(
    skipped: bool | None = None,
    passed: bool | None = None,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
) -> None:
    """Record cookie validation outcome for inclusion in /health."""
    if skipped is not None:
        _STARTUP_RESULT["cookie_validation_skipped"] = skipped
    if passed is not None:
        _STARTUP_RESULT["cookie_validation_passed"] = passed
    if errors is not None:
        _STARTUP_RESULT["cookie_validation_errors"] = errors
    if warnings is not None:
        _STARTUP_RESULT["cookie_validation_warnings"] = warnings


def set_mcp_server(server: Any) -> None:
    """Register the FastMCP server so /health can introspect its tools."""
    global _MCP_SERVER
    _MCP_SERVER = server


# ═══════════════════════════════════════════════════════════════════════════
# Version + git helpers
# ═══════════════════════════════════════════════════════════════════════════

def _get_version() -> str:
    """Return the package version from __init__.py."""
    try:
        from . import __version__
        return __version__
    except Exception:
        return "0.0.0-unknown"


def _get_git_info(workspace: Path | None = None) -> dict[str, Any]:
    """Return git metadata (commit, branch, dirty, describe).

    All fields are best-effort — returns "unknown" on any error.
    Never raises.
    """
    info: dict[str, Any] = {
        "commit": "unknown",
        "short": "unknown",
        "branch": "unknown",
        "dirty": None,
        "describe": "unknown",
    }
    try:
        cwd = str(workspace) if workspace else None

        def _git(*args: str) -> str | None:
            try:
                r = subprocess.run(
                    ["git", *args],
                    capture_output=True, text=True, timeout=2,
                    cwd=cwd,
                )
                if r.returncode == 0:
                    return r.stdout.strip()
            except Exception:
                return None
            return None

        commit = _git("rev-parse", "HEAD")
        if commit:
            info["commit"] = commit
            info["short"] = commit[:8]
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if branch:
            info["branch"] = branch
        describe = _git("describe", "--tags", "--always", "--dirty")
        if describe:
            info["describe"] = describe
        # Dirty check
        porcelain = _git("status", "--porcelain")
        if porcelain is not None:
            info["dirty"] = bool(porcelain)
    except Exception:
        pass
    return info


def _get_python_info() -> dict[str, Any]:
    return {
        "version": sys.version,
        "version_info": list(sys.version_info),
        "implementation": _platform.python_implementation(),
        "executable": sys.executable,
        "prefix": sys.prefix,
        "executable_mode": os.access(sys.executable, os.X_OK),
    }


def _get_platform_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "system": _platform.system(),
        "release": _platform.release(),
        "version": _platform.version(),
        "machine": _platform.machine(),
        "node": _platform.node(),
        "python_build": _platform.python_build(),
    }
    info["pid"] = os.getpid()
    info["ppid"] = os.getppid()
    info["cwd"] = os.getcwd()
    info["argv"] = sys.argv
    info["env"] = {
        k: v for k, v in os.environ.items()
        if k in (
            "DISPLAY", "WAYLAND_DISPLAY",
            "PYTHONUNBUFFERED", "SKIP_COOKIE_VALIDATION",
            "ENABLE_MANUAL_INTERVENTION", "MANUAL_INTERVENTION_TIMEOUT_SEC",
            "ANTIDETECT_LEVEL", "ANTIDETECT_SEARCH_VIA_TYPING",
            "ANTIDETECT_WARMUP_ON_FIRST_REQUEST",
        )
    }
    return info


# ═══════════════════════════════════════════════════════════════════════════
# Dependency checks
# ═══════════════════════════════════════════════════════════════════════════

def _safe_import(name: str, attr: str | None = None) -> tuple[bool, str | None]:
    """Try to import `name` (optionally attribute `attr`) and return its version."""
    try:
        mod = __import__(name, fromlist=["__version__"] if attr is None else [attr])
        if attr is not None and not hasattr(mod, attr):
            return True, None
        ver = getattr(mod, "__version__", None)
        return True, ver
    except Exception as e:
        return False, str(e)


def _check_chromium_install_path() -> tuple[bool, str | None]:
    """Return (installed, path) for Playwright's chromium binary.

    Uses a pure filesystem check so it works from inside an asyncio loop
    (where the sync Playwright API can't run). Globs
    `$PLAYWRIGHT_BROWSERS_PATH` (or the default
    `~/.cache/ms-playwright`) for known chromium binary names.
    """
    root = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
        or str(Path(os.path.expanduser("~")) / ".cache" / "ms-playwright")
    )
    # Try the modern layout (Playwright 1.40+): chromium-XXXX/chrome-linux64/chrome
    # and chromium_headless_shell-XXXX/chrome-headless-shell-linux64/headless_shell
    patterns: list[str] = [
        "chromium-*/chrome-linux64/chrome",
        "chromium-*/chrome-linux/chrome",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/headless_shell",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
        "chromium_headless_shell-*/chrome-linux/chrome",
        "chromium-*/chrome-linux/headless_shell",
    ]
    if Path(root).is_dir():
        for pat in patterns:
            matches = sorted(Path(root).glob(pat))
            for m in matches:
                try:
                    if m.is_file() and os.access(m, os.X_OK):
                        return True, str(m)
                except Exception:
                    continue
    return False, None


def _check_playwright() -> dict[str, Any]:
    info: dict[str, Any] = {"ok": False, "version": None, "error": None,
                            "chromium_installed": False, "chromium_path": None}
    ok, ver = _safe_import("playwright")
    info["ok"] = ok
    info["version"] = ver
    if not ok:
        info["error"] = ver
        return info
    # First try the sync API (works fine outside an asyncio loop)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
            info["chromium_installed"] = True
            info["chromium_path"] = path
            return info
    except Exception as e:
        err = str(e)
        info["chromium_error"] = err
        if "asyncio loop" in err or "Sync API" in err:
            # Running inside an asyncio loop — fall back to filesystem check
            installed, path = _check_chromium_install_path()
            info["chromium_installed"] = installed
            info["chromium_path"] = path
            info["chromium_check"] = "fallback (asyncio-safe filesystem check)"
        else:
            info["chromium_installed"] = False
    return info


def _check_files() -> dict[str, Any]:
    """Check presence / size of important files and dirs."""
    from . import config

    def _stat(p: str | Path) -> dict[str, Any]:
        path = Path(p)
        info: dict[str, Any] = {"path": str(path), "exists": path.exists()}
        if info["exists"]:
            try:
                st = path.stat()
                info["size_bytes"] = st.st_size
                info["modified_at"] = datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc
                ).isoformat()
            except Exception as e:
                info["error"] = str(e)
        return info

    cookie_dir = Path(config.COOKIE_DIR)
    cookie_files: dict[str, Any] = {
        "path": str(cookie_dir),
        "exists": cookie_dir.is_dir(),
    }
    if cookie_files["exists"]:
        try:
            files = list(cookie_dir.iterdir())
            txt_files = [f for f in files if f.suffix == ".txt"]
            cookie_files["txt_file_count"] = len(txt_files)
            cookie_files["total_size_bytes"] = sum(
                f.stat().st_size for f in files if f.is_file()
            )
        except Exception as e:
            cookie_files["error"] = str(e)

    return {
        "cookie_dir": cookie_files,
        "google_cookies": _stat(Path(config.COOKIE_DIR) / "google_cookies.txt"),
        "youtube_cookies": _stat(Path(config.COOKIE_DIR) / "youtube_cookies.txt"),
        "auto_cookies": _stat(config.COOKIE_JSON_PATH),
        "mobilenet_onnx": _stat(config.MOBILENET_ONNX_PATH),
        "feeds_db": _stat(config.FEEDS_DB_PATH),
        "browser_data_dir": _stat(config.BROWSER_DATA_DIR),
    }


def _check_disk(path: str | None = None) -> dict[str, Any]:
    """Return free / total disk space for the cache directory."""
    from . import config
    target = path or config.CACHE_DIR
    try:
        usage = shutil.disk_usage(target)
        free = usage.free
        total = usage.total
        used = usage.used
        percent = (used / total * 100) if total else 0
        return {
            "path": target,
            "free_bytes": free,
            "free_human": _humanize_bytes(free),
            "total_bytes": total,
            "used_bytes": used,
            "used_percent": round(percent, 1),
        }
    except Exception as e:
        return {"path": target, "error": str(e)}


def _humanize_bytes(n: int | float) -> str:
    """Format bytes as a human-readable string."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _check_dependencies() -> dict[str, Any]:
    """Return status of all third-party deps the server relies on."""
    deps: dict[str, Any] = {}

    deps["playwright"] = _check_playwright()
    ok, ver = _safe_import("cv2")
    deps["opencv"] = {"ok": ok, "version": ver, "error": None if ok else ver}
    ok, ver = _safe_import("onnxruntime")
    deps["onnxruntime"] = {"ok": ok, "version": ver, "error": None if ok else ver}
    ok, ver = _safe_import("faster_whisper")
    deps["faster_whisper"] = {"ok": ok, "version": ver, "error": None if ok else ver}
    ok, ver = _safe_import("yt_dlp")
    deps["yt_dlp"] = {"ok": ok, "version": ver, "error": None if ok else ver}
    ok, ver = _safe_import("rapidocr")
    deps["rapidocr"] = {"ok": ok, "version": ver, "error": None if ok else ver}
    ok, ver = _safe_import("lingua")
    deps["lingua"] = {"ok": ok, "version": ver, "error": None if ok else ver}
    deps["psutil"] = {
        "ok": _PSUTIL_AVAILABLE,
        "version": getattr(psutil, "__version__", None) if _PSUTIL_AVAILABLE else None,
    }
    deps["starlette"] = {
        "ok": _STARLETTE_AVAILABLE,
        "version": getattr(__import__("starlette"), "__version__", None)
        if _STARLETTE_AVAILABLE else None,
    }
    deps["uvicorn"] = {
        "ok": _UVICORN_AVAILABLE,
        "version": getattr(uvicorn, "__version__", None) if _UVICORN_AVAILABLE else None,
    }
    return deps


def _check_config() -> dict[str, Any]:
    """Return the relevant subset of the runtime config."""
    from . import config
    return {
        "ENABLE_MANUAL_INTERVENTION": config.ENABLE_MANUAL_INTERVENTION,
        "MANUAL_INTERVENTION_TIMEOUT_SEC": config.MANUAL_INTERVENTION_TIMEOUT_SEC,
        "MANUAL_INTERVENTION_POLL_SEC": config.MANUAL_INTERVENTION_POLL_SEC,
        "SKIP_COOKIE_VALIDATION": config.SKIP_COOKIE_VALIDATION,
        "ENABLE_HEALTH_SERVER": config.ENABLE_HEALTH_SERVER,
        "HEALTH_HOST": config.HEALTH_HOST,
        "HEALTH_PORT": config.HEALTH_PORT,
        "HEALTH_AUTH_TOKEN_set": bool(config.HEALTH_AUTH_TOKEN),
        "GOOGLE_REQUEST_MIN_GAP": config.GOOGLE_REQUEST_MIN_GAP,
        "MAX_GOOGLE_RETRIES": config.MAX_GOOGLE_RETRIES,
        # Anti-detect (v0.3.4+)
        "ANTIDETECT_LEVEL": config.ANTIDETECT_LEVEL,
        "ANTIDETECT_SEARCH_VIA_TYPING": config.ANTIDETECT_SEARCH_VIA_TYPING,
        "ANTIDETECT_WARMUP_ON_FIRST_REQUEST": config.ANTIDETECT_WARMUP_ON_FIRST_REQUEST,
        "ANTIDETECT_TAB_FOCUS_EVENTS": config.ANTIDETECT_TAB_FOCUS_EVENTS,
        "ANTIDETECT_CLIENT_HINTS": config.ANTIDETECT_CLIENT_HINTS,
        "ANTIDETECT_RANDOMIZE_FINGERPRINT": config.ANTIDETECT_RANDOMIZE_FINGERPRINT,
    }


def _check_anti_detect() -> dict[str, Any]:
    """Return the current anti-detect status (per-session fingerprint etc.)."""
    try:
        from . import anti_detect
        return anti_detect.get_antidetect_status()
    except Exception as e:
        return {"error": str(e)}


def _check_mcp() -> dict[str, Any]:
    """Return information about the registered MCP tools / prompts / resources."""
    from . import config
    info: dict[str, Any] = {
        "name": "google-search",
        "tools": 0,
        "prompts": 0,
        "resources": 0,
        "uptime_sec": _uptime_sec(),
        "transport": "stdio",  # current default
    }
    if _MCP_SERVER is None:
        info["error"] = "mcp server not registered"
        return info
    try:
        # FastMCP exposes tool_manager with _tools dict
        tm = getattr(_MCP_SERVER, "_tool_manager", None)
        if tm is not None:
            tools = getattr(tm, "_tools", {}) or {}
            info["tools"] = len(tools)
            info["tool_names"] = sorted(tools.keys())
        pm = getattr(_MCP_SERVER, "_prompt_manager", None)
        if pm is not None:
            info["prompts"] = len(getattr(pm, "_prompts", {}) or {})
        rm = getattr(_MCP_SERVER, "_resource_manager", None)
        if rm is not None:
            info["resources"] = len(getattr(rm, "_resources", {}) or {})
        info["name"] = getattr(_MCP_SERVER, "name", "google-search")
    except Exception as e:
        info["introspection_error"] = str(e)
    return info


def _check_manual_intervention() -> dict[str, Any]:
    """Return manual intervention state."""
    from . import browser, config
    info: dict[str, Any] = {
        "enabled": config.ENABLE_MANUAL_INTERVENTION,
        "display_available": browser._check_display_available(),
        "currently_active": browser.is_manual_intervention_active(),
        "timeout_sec": config.MANUAL_INTERVENTION_TIMEOUT_SEC,
        "poll_sec": config.MANUAL_INTERVENTION_POLL_SEC,
    }
    last = browser.get_last_intervention_result()
    if last.get("timestamp", 0) > 0:
        info["last_result"] = {
            "timestamp": last.get("timestamp"),
            "timestamp_iso": (
                datetime.fromtimestamp(last["timestamp"], tz=timezone.utc).isoformat()
                if last.get("timestamp") else None
            ),
            "resolved": last.get("resolved"),
            "reason": last.get("reason"),
            "url": last.get("url"),
            "outcome": last.get("outcome"),
            "elapsed_sec": last.get("elapsed_sec"),
        }
    else:
        info["last_result"] = None
    return info


def _check_process() -> dict[str, Any]:
    """Return process-level info (pid, memory, cpu, threads)."""
    info: dict[str, Any] = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
    }
    if _PSUTIL_AVAILABLE:
        try:
            p = psutil.Process(os.getpid())
            mem = p.memory_info()
            info["memory_rss_mb"] = round(mem.rss / 1024 / 1024, 2)
            info["memory_vms_mb"] = round(mem.vms / 1024 / 1024, 2)
            try:
                info["cpu_percent"] = p.cpu_percent(interval=0.0)
            except Exception:
                info["cpu_percent"] = None
            info["threads"] = p.num_threads()
            info["nice"] = p.nice()
            try:
                info["uptime_sec"] = round(time.time() - p.create_time(), 1)
            except Exception:
                pass
        except Exception as e:
            info["error"] = str(e)
    else:
        info["psutil"] = "missing"
    return info


def _uptime_sec() -> float:
    if _LOADED_AT_MONO is None:
        return 0.0
    return round(time.monotonic() - _LOADED_AT_MONO, 3)


# ═══════════════════════════════════════════════════════════════════════════
# Health status rollup
# ═══════════════════════════════════════════════════════════════════════════

def _compute_status(health: dict[str, Any]) -> str:
    """Decide the overall status: 'ok' | 'degraded' | 'down'."""
    deps = health.get("dependencies", {})
    files_ = health.get("files", {})
    mi = health.get("manual_intervention", {})

    # Critical: starlette + uvicorn must be present (otherwise health
    # endpoint can't run)
    if not deps.get("starlette", {}).get("ok"):
        return "down"
    if not deps.get("uvicorn", {}).get("ok"):
        return "down"
    if not deps.get("playwright", {}).get("ok"):
        return "down"
    if not deps.get("opencv", {}).get("ok"):
        return "down"

    # Degraded conditions (non-critical)
    if not files_.get("google_cookies", {}).get("exists"):
        # No Google cookies — searches will be blocked. Degraded.
        return "degraded"
    if mi.get("enabled") and not mi.get("display_available"):
        # Manual intervention can't actually open a headful window
        return "degraded"
    pw = deps.get("playwright", {})
    if pw.get("chromium_installed") is False:
        # Playwright is importable but chromium isn't installed — searches
        # will fail. Degraded.
        return "degraded"

    return "ok"


def _build_health_payload() -> dict[str, Any]:
    """Assemble the full /health JSON response."""
    from . import config, browser

    workspace = Path(__file__).resolve().parent.parent.parent
    health: dict[str, Any] = {
        "status": "ok",  # placeholder, recomputed below
        "uptime_sec": _uptime_sec(),
        "loaded_at": _LOADED_AT.isoformat() if _LOADED_AT else None,
        "version": _get_version(),
        "git": _get_git_info(workspace=workspace),
        "python": _get_python_info(),
        "platform": _get_platform_info(),
        "process": _check_process(),
        "mcp": _check_mcp(),
        "config": _check_config(),
        "anti_detect": _check_anti_detect(),
        "dependencies": _check_dependencies(),
        "files": _check_files(),
        "disk": _check_disk(),
        "manual_intervention": _check_manual_intervention(),
        "startup": dict(_STARTUP_RESULT),
    }
    health["status"] = _compute_status(health)
    return health


# ═══════════════════════════════════════════════════════════════════════════
# HTTP routes
# ═══════════════════════════════════════════════════════════════════════════

def _check_auth(request: "Request") -> bool:
    """Return True if the request is allowed to hit the health endpoint."""
    from . import config
    if not config.HEALTH_AUTH_TOKEN:
        return True
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] == config.HEALTH_AUTH_TOKEN
    # Also accept ?token=... for simple monitoring setups
    return request.query_params.get("token") == config.HEALTH_AUTH_TOKEN


def _json(payload: Any, status: int = 200) -> "JSONResponse":
    return JSONResponse(payload, status_code=status)


async def root_handler(request: "Request") -> "JSONResponse":
    """GET / — index of available endpoints."""
    if not _check_auth(request):
        return _json({"error": "unauthorized"}, status=401)
    return _json({
        "service": "noapi-google-search-mcp",
        "version": _get_version(),
        "endpoints": {
            "GET /":            "this index",
            "GET /health":      "full health snapshot (JSON)",
            "GET /health/live": "liveness probe (200 if process is alive)",
            "GET /health/ready": "readiness probe (200 if critical deps present)",
            "GET /version":     "just version + git commit info",
        },
    })


async def health_handler(request: "Request") -> "JSONResponse":
    """GET /health — full snapshot."""
    if not _check_auth(request):
        return _json({"error": "unauthorized"}, status=401)
    payload = _build_health_payload()
    # 503 if status is "down", else 200
    status = 503 if payload["status"] == "down" else 200
    return _json(payload, status=status)


async def liveness_handler(request: "Request") -> "JSONResponse":
    """GET /health/live — minimal liveness probe. Always 200 if process alive."""
    if not _check_auth(request):
        return _json({"error": "unauthorized"}, status=401)
    return _json({
        "status": "alive",
        "uptime_sec": _uptime_sec(),
        "pid": os.getpid(),
    })


async def readiness_handler(request: "Request") -> "JSONResponse":
    """GET /health/ready — checks critical deps only."""
    if not _check_auth(request):
        return _json({"error": "unauthorized"}, status=401)
    deps = _check_dependencies()
    critical = ("starlette", "uvicorn", "playwright", "opencv")
    missing = [d for d in critical if not deps.get(d, {}).get("ok")]
    if missing:
        return _json(
            {"status": "not_ready", "missing": missing, "dependencies": deps},
            status=503,
        )
    return _json({"status": "ready", "dependencies": {d: deps[d] for d in critical}})


async def version_handler(request: "Request") -> "JSONResponse":
    """GET /version — version + git commit only."""
    if not _check_auth(request):
        return _json({"error": "unauthorized"}, status=401)
    workspace = Path(__file__).resolve().parent.parent.parent
    return _json({
        "version": _get_version(),
        "git": _get_git_info(workspace=workspace),
        "python": _get_python_info(),
    })


# ═══════════════════════════════════════════════════════════════════════════
# Starlette app factory
# ═══════════════════════════════════════════════════════════════════════════

def build_app() -> "Starlette":
    """Return a configured Starlette app with the /health routes.

    Raises RuntimeError if starlette isn't installed.
    """
    if not _STARLETTE_AVAILABLE:
        raise RuntimeError(
            "starlette is not installed — cannot start health server. "
            "Install with: pip install starlette uvicorn"
        )
    routes = [
        Route("/", root_handler, methods=["GET"]),
        Route("/health", health_handler, methods=["GET"]),
        Route("/health/live", liveness_handler, methods=["GET"]),
        Route("/health/ready", readiness_handler, methods=["GET"]),
        Route("/version", version_handler, methods=["GET"]),
    ]
    return Starlette(routes=routes, debug=False)


# ═══════════════════════════════════════════════════════════════════════════
# Background lifecycle
# ═══════════════════════════════════════════════════════════════════════════

class HealthServer:
    """Lifecycle wrapper for the uvicorn server.

    Usage in `app_lifespan`:

        health = HealthServer()
        await health.start()
        try:
            yield {}
        finally:
            await health.stop()

    If the health server cannot bind (e.g. port already in use from a
    previous process still in TIME_WAIT), it logs a warning and
    continues — the MCP tools remain fully functional without the
    health HTTP endpoint.
    """

    # Number of bind attempts with SO_REUSEADDR before giving up.
    _BIND_RETRIES: int = 3
    _BIND_RETRY_DELAY: float = 1.0

    def __init__(self, config_obj: Any | None = None) -> None:
        from . import config
        self.config = config_obj or config
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._host: str = self.config.HEALTH_HOST
        self._port: int = self.config.HEALTH_PORT
        self._actual_port: int | None = None  # set after start (if port=0)
        self._error: BaseException | None = None
        self._sock: socket.socket | None = None  # pre-bound socket with SO_REUSEADDR

    @property
    def enabled(self) -> bool:
        return bool(self.config.ENABLE_HEALTH_SERVER)

    @property
    def is_running(self) -> bool:
        """True if the health server successfully started and is accepting connections."""
        return self._server is not None and self._task is not None and not self._task.done()

    @property
    def url(self) -> str:
        port = self._actual_port or self._port
        host = "127.0.0.1" if self._host in ("0.0.0.0", "::") else self._host
        return f"http://{host}:{port}"

    def _create_bound_socket(self) -> socket.socket | None:
        """Create and bind a TCP socket with SO_REUSEADDR set.

        Returns the bound socket, or None if binding failed.
        """
        try:
            family = socket.AF_INET6 if self._host == "::" else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # On Linux, also try SO_REUSEPORT for kernel-level load balancing
            # across processes (available since Linux 3.9).
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass  # not all kernels support it
            sock.bind((self._host, self._port))
            sock.listen(128)
            sock.setblocking(False)
            return sock
        except OSError as e:
            print(
                f"[health_server] WARNING: cannot bind to "
                f"{self._host}:{self._port} — {e}",
                file=sys.stderr, flush=True,
            )
            return None

    async def start(self) -> None:
        """Start the health server in the current event loop.

        Tries to bind with SO_REUSEADDR up to _BIND_RETRIES times.
        If all attempts fail, logs a warning and continues without
        the health endpoint — the MCP tools remain fully functional.
        """
        if not self.enabled:
            return
        if not _STARLETTE_AVAILABLE or not _UVICORN_AVAILABLE:
            print(
                "[health_server] starlette/uvicorn not available — "
                "health endpoint disabled. Install with: "
                "pip install starlette uvicorn",
                file=sys.stderr, flush=True,
            )
            return

        # ── Bind with SO_REUSEADDR + retry ──
        for attempt in range(1, self._BIND_RETRIES + 1):
            self._sock = self._create_bound_socket()
            if self._sock is not None:
                break
            if attempt < self._BIND_RETRIES:
                print(
                    f"[health_server] Retrying bind in "
                    f"{self._BIND_RETRY_DELAY}s (attempt {attempt}/{self._BIND_RETRIES})...",
                    file=sys.stderr, flush=True,
                )
                await asyncio.sleep(self._BIND_RETRY_DELAY)

        if self._sock is None:
            print(
                "[health_server] WARNING: Health endpoint could not bind — "
                "continuing without health HTTP server. "
                "MCP tools remain fully functional. "
                "To free the port, run: fuser -k 11499/tcp",
                file=sys.stderr, flush=True,
            )
            self._server = None
            return

        app = build_app()
        config_uv = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config_uv)
        self._task = asyncio.create_task(self._serve(), name="health-server")
        # Wait until the server is actually accepting connections
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            print(
                "[health_server] WARNING: timed out waiting for health server to start",
                file=sys.stderr, flush=True,
            )
        if self._error is not None:
            print(
                f"[health_server] ERROR: {self._error}",
                file=sys.stderr, flush=True,
            )
            self._server = None
            return
        # Discover the actual bound port from our pre-created socket.
        try:
            addr = self._sock.getsockname()
            if addr and len(addr) >= 2:
                self._actual_port = int(addr[1])
        except Exception:
            pass
        if not self._actual_port:
            self._actual_port = self._port
        print(
            f"[health_server] Health endpoint live at {self.url}/health "
            f"(GET /, /health, /health/live, /health/ready, /version)",
            file=sys.stderr, flush=True,
        )

    async def _serve(self) -> None:
        """Run uvicorn in the background, using our pre-bound SO_REUSEADDR socket."""
        try:
            # Pass our pre-bound socket so uvicorn doesn't try to bind again.
            self._ready.set()
            await self._server.serve(sockets=[self._sock])
        except BaseException as e:
            self._error = e
            self._ready.set()

    async def stop(self) -> None:
        """Gracefully stop the health server."""
        if self._server is not None:
            try:
                self._server.should_exit = True
            except Exception:
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except Exception:
                    pass
            except Exception:
                pass
        self._task = None
        self._server = None
        # Close our pre-bound socket.
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


# ═══════════════════════════════════════════════════════════════════════════
# CLI for ad-hoc testing
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:  # pragma: no cover
    """Run the health server standalone for testing/debugging.

    Example:
        python -m google_search_mcp.health_server
    """
    import argparse
    parser = argparse.ArgumentParser(description="Standalone health server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--once", action="store_true",
                        help="Print /health once and exit (don't run server)")
    args = parser.parse_args()

    if args.host:
        os.environ["HEALTH_HOST"] = args.host
    if args.port is not None:
        os.environ["HEALTH_PORT"] = str(args.port)

    set_loaded_at()

    if args.once:
        import json as _json
        print(_json.dumps(_build_health_payload(), indent=2, default=str))
        return

    if not _STARLETTE_AVAILABLE or not _UVICORN_AVAILABLE:
        print("starlette/uvicorn not installed; cannot start server", file=sys.stderr)
        sys.exit(1)
    app = build_app()
    uvicorn.run(
        app,
        host=os.environ.get("HEALTH_HOST", "0.0.0.0"),
        port=int(os.environ.get("HEALTH_PORT", "11499")),
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
