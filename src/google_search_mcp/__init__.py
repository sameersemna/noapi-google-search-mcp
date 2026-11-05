"""Google Search MCP Server — 43 tools for local LLMs.

Provides real Google search, live feeds, vision, OCR, video transcription,
email, document reading, and web utilities — all running locally through
headless Chromium and open-source ML models. No API keys required.

Includes a /health HTTP endpoint on a separate port (default 11499) for
monitoring and orchestration (k8s probes, Prometheus, etc.).

CLI
---
    noapi-google-search-mcp                 # run over stdio (default)
    noapi-google-search-mcp --version
    noapi-google-search-mcp --check         # dependency doctor
    noapi-google-search-mcp --transport streamable-http --host 127.0.0.1 --port 11498
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Single-source the version from the installed package metadata
    # (pyproject.toml). Falls back to a literal only for an uninstalled source
    # checkout. (B3: the literal previously drifted from pyproject.toml.)
    __version__ = _pkg_version("noapi-google-search-mcp")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "0.4.0"

from .server import mcp

_TRANSPORTS = ("stdio", "sse", "streamable-http")


def _has_module(name: str) -> bool:
    """True if *name* is importable (checks spec only — no import side effects)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _chromium_installed() -> bool:
    """True if a Playwright Chromium build is present in the browsers cache."""
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    candidates = (
        [root] if root
        else [os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")]
    )
    for base in candidates:
        try:
            if base and any(n.startswith("chromium") for n in os.listdir(base)):
                return True
        except OSError:
            continue
    return False


def _dependency_report() -> tuple[str, bool]:
    """Build a human-readable dependency doctor report.

    Returns ``(report, critical_ok)``. Only *critical* items make the exit code
    non-zero — the optional ML/media extras are reported with install hints but
    do not fail the check (they only disable specific tools).
    """
    rows: list[tuple[str, bool, str, bool]] = []  # (label, ok, hint, critical)

    rows.append(("mcp", _has_module("mcp"), "pip install mcp", True))
    rows.append(("playwright", _has_module("playwright"), "pip install playwright", True))
    rows.append((
        "chromium browser",
        _chromium_installed(),
        "python -m playwright install chromium",
        True,
    ))
    rows.append(("ffmpeg binary", shutil.which("ffmpeg") is not None, "install ffmpeg", False))
    rows.append(("yt-dlp", _has_module("yt_dlp"), "pip install 'yt-dlp[default]'", False))
    rows.append(("faster-whisper", _has_module("faster_whisper"), "pip install faster-whisper", False))
    rows.append(("opencv (cv2)", _has_module("cv2"), "pip install opencv-python-headless", False))
    rows.append(("rapidocr-onnxruntime", _has_module("rapidocr_onnxruntime"), "pip install rapidocr-onnxruntime", False))
    rows.append(("boto3 (S3 optional)", _has_module("boto3"), "pip install boto3", False))
    rows.append((
        "starlette+uvicorn (health)",
        _has_module("starlette") and _has_module("uvicorn"),
        "pip install starlette uvicorn",
        False,
    ))

    lines = [
        f"noapi-google-search-mcp {__version__}",
        f"python {sys.version.split()[0]} ({sys.executable})",
        "",
        "Dependencies:",
    ]
    critical_ok = True
    for label, ok, hint, critical in rows:
        mark = "OK     " if ok else "MISSING"
        suffix = "" if ok else f"  -> {hint}"
        crit = " [required]" if critical else ""
        lines.append(f"  [{mark}] {label}{crit}{suffix}")
        if critical and not ok:
            critical_ok = False

    lines.append("")
    lines.append(
        "Result: " + ("all required dependencies present" if critical_ok
                      else "REQUIRED DEPENDENCIES MISSING")
    )
    return "\n".join(lines), critical_ok


def main(argv: list[str] | None = None) -> None:
    """Run the MCP server (stdio by default).

    Options:
        --version       print the version and exit
        --check         run the dependency doctor and exit (non-zero if a
                        required dependency is missing)
        --transport     stdio (default) | sse | streamable-http
        --host / --port bind address for the HTTP transports (default
                        127.0.0.1:<NOAPI_MCP_HTTP_PORT>)

    The MCP server runs in the foreground (blocking). With ``--transport
    streamable-http`` (or ``sse``) it is served over HTTP by
    ``http_app.serve``; with the default ``stdio`` it speaks JSON-RPC on
    stdin/stdout. Either way the health server is reachable on
    ``HEALTH_HOST:HEALTH_PORT`` (default 0.0.0.0:11499).
    """
    from . import config

    parser = argparse.ArgumentParser(
        prog="noapi-google-search-mcp",
        description="Google Search MCP server (43 tools for local LLMs).",
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--check", action="store_true", help="check dependencies and exit")
    parser.add_argument(
        "--manual-browser",
        nargs="?",
        const="https://www.google.com",
        default=None,
        metavar="URL",
        help="open a visible Chromium window sharing the server's cookies, then exit "
        "(default URL: https://www.google.com). Use it to log in, clear a consent "
        "dialog or find a feed URL by hand; cookies are written back so the next "
        "tool call uses the new session.",
    )
    parser.add_argument(
        "--browser-profile",
        default=None,
        metavar="DIR",
        help="profile directory for --manual-browser (default: a dedicated \n"
        "'google-mcp-browser-manual' profile). Point it at the service's profile "
        "only while the service is stopped.",
    )
    parser.add_argument(
        "--browser-timeout",
        type=int,
        default=0,
        metavar="SEC",
        help="close the --manual-browser window after SEC seconds (0 = until you close it)",
    )
    parser.add_argument("--transport", choices=_TRANSPORTS, default=None,
                        help="MCP transport (default: stdio, or NOAPI_MCP_TRANSPORT)")
    parser.add_argument("--host", default=None, help="bind host for HTTP transports")
    parser.add_argument("--port", type=int, default=None, help="bind port for HTTP transports")
    args = parser.parse_args(argv)

    if args.version:
        print(f"noapi-google-search-mcp {__version__}")
        return

    if args.check:
        report, ok = _dependency_report()
        print(report)
        raise SystemExit(0 if ok else 1)

    if args.manual_browser:
        # A headful session is interactive: it must not start the MCP server, the
        # health endpoint or the browser pool.
        from .manual_browser import run_manual_browser

        raise SystemExit(
            run_manual_browser(
                args.manual_browser,
                profile_dir=args.browser_profile,
                timeout_sec=args.browser_timeout,
            )
        )

    transport = args.transport or config.TRANSPORT
    if transport not in _TRANSPORTS:
        transport = "stdio"

    host = args.host or config.HTTP_HOST
    port = config.HTTP_PORT if args.port is None else args.port

    # Only meaningful for the HTTP transports, but harmless for stdio.
    mcp.settings.host = host
    mcp.settings.port = port

    # Record what is actually served so /health does not report the configured
    # default (it said "stdio" for this HTTP deployment).
    from . import health_server as _health

    _health.set_active_transport(transport)

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    # The HTTP transports are served from ``http_app`` rather than
    # ``mcp.run(transport=...)`` for two reasons: FastMCP's lifespan is entered
    # once per session (so the health server and browser pool would be
    # started/torn down per client), and the Host-header allow-list has to be
    # set explicitly because FastMCP freezes it at construction time from the
    # loopback default. See docs/adr/0007-native-http-transport.md.
    from . import http_app

    http_app.serve(mcp, host=host, port=port)
