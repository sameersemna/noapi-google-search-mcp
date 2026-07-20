"""Google Search MCP Server — 38 tools for local LLMs.

Provides real Google search, live feeds, vision, OCR, video transcription,
email, document reading, and web utilities — all running locally through
headless Chromium and open-source ML models. No API keys required.
"""

__version__ = "0.3.1"

from .server import mcp


def main() -> None:
    """Run the MCP server over stdio transport."""
    mcp.run(transport="stdio")
