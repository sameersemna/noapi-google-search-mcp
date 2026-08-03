"""Google Search MCP Server — 41 tools for local LLMs.

Provides real Google search, live feeds, vision, OCR, video transcription,
email, document reading, and web utilities — all running locally through
headless Chromium and open-source ML models. No API keys required.

Includes a /health HTTP endpoint on a separate port (default 11499) for
monitoring and orchestration (k8s probes, Prometheus, etc.).
"""

__version__ = "0.3.4"

from .server import mcp


def main() -> None:
    """Run the MCP server over stdio transport.

    The MCP server runs in the foreground (blocking). The health server
    is started in the background by `app_lifespan` and is reachable on
    `HEALTH_HOST:HEALTH_PORT` (default 0.0.0.0:11499).
    """
    mcp.run(transport="stdio")
