"""Domain tool modules for the Google Search MCP server.

Each module registers its tools on the shared ``mcp`` instance (imported from
``..server``) via the ``@mcp.tool()`` decorator. ``server.py`` imports these
modules at the bottom (after ``mcp`` is defined) so all tools register.
"""
