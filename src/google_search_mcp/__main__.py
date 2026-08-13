"""Allow running as `python -m google_search_mcp`.

This module provides the entry point for the MCP server when invoked
via `python -m google_search_mcp`. It runs the FastMCP server using
the stdio transport (the default for MCP servers behind mcp-proxy).
"""

import sys

from .server import mcp


def main() -> None:
    """Run the MCP server with stdio transport."""
    print("Starting Google Search MCP server...", file=sys.stderr, flush=True)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

from google_search_mcp import main

main()
