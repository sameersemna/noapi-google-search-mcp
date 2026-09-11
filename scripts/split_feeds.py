#!/usr/bin/env python3
"""Extract the feeds section from server.py into tools/feeds.py.

Reads server.py, pulls out the feeds section (lines 5756-6838, 1-indexed),
writes it as tools/feeds.py with the needed imports, and replaces the
section in server.py with an import of the new module.

This is a one-shot migration script. Run it, then verify with
`python test_tool_schema.py` that all 41 tools still register.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "google_search_mcp", "server.py")
FEEDS = os.path.join(ROOT, "src", "google_search_mcp", "tools", "feeds.py")

START = 5756  # 1-indexed first line of feeds section ("# Database")
END = 6836    # 1-indexed last line of feeds section ("        conn.close()")


def main() -> None:
    with open(SERVER) as f:
        lines = f.readlines()

    # Extract the feeds section (0-indexed slice).
    feeds_lines = lines[START - 1:END]

    # Build the new module.
    header = '''"""Feed subscription tools — subscribe, check, search, and browse feeds.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from mcp.server.fastmcp import Context
from playwright.async_api import async_playwright

from ..config import (
    ARXIV_CATEGORIES,
    FEEDS_DB_PATH,
    PRESET_NEWS_FEEDS,
    TRANSCRIBE_CACHE_DIR,
    TRANSCRIPT_CACHE_DIR,
)
from ..server import (
    mcp,
    _download_audio,
    _fetch_url_bytes,
    _transcribe_audio,
    _transcript_cache_path,
    format_timestamp,
    launch_browser,
    strip_html,
)


'''
    body = "".join(feeds_lines)

    with open(FEEDS, "w") as f:
        f.write(header)
        f.write(body)

    # Replace the section in server.py with an import.
    new_server = lines[:START - 1] + [
        "# ---------------------------------------------------------------------------\n",
        "# Feed subscription tools — moved to tools/feeds.py\n",
        "# ---------------------------------------------------------------------------\n",
        "from .tools import feeds as _feeds  # noqa: F401  (registers feed tools)\n",
        "\n",
    ] + lines[END:]

    with open(SERVER, "w") as f:
        f.writelines(new_server)

    print(f"Wrote {FEEDS} ({len(feeds_lines)} lines)")
    print(f"Replaced feeds section in {SERVER}")


if __name__ == "__main__":
    main()
