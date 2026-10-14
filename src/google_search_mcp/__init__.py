"""Google Search MCP Server — 42 tools for local LLMs.

Provides real Google search, live feeds, vision, OCR, video transcription,
email, document reading, and web utilities — all running locally through
headless Chromium and open-source ML models. No API keys required.

Includes a /health HTTP endpoint on a separate port (default 11499) for
monitoring and orchestration (k8s probes, Prometheus, etc.).
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Single-source the version from the installed package metadata
    # (pyproject.toml). Falls back to a literal only for an uninstalled source
    # checkout. (B3: the literal previously drifted from pyproject.toml.)
    __version__ = _pkg_version("noapi-google-search-mcp")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "0.3.6"

from .server import mcp


def main() -> None:
    """Run the MCP server over stdio transport.

    The MCP server runs in the foreground (blocking). The health server
    is started in the background by `app_lifespan` and is reachable on
    `HEALTH_HOST:HEALTH_PORT` (default 0.0.0.0:11499).
    """
    mcp.run(transport="stdio")
