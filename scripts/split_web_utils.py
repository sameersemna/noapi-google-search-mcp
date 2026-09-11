#!/usr/bin/env python3
"""Extract the web-utilities section from server.py into tools/web_utils.py.

Reads server.py, pulls out the web-utilities section (lines 4592-5723,
1-indexed), writes it as tools/web_utils.py with the needed imports, and
replaces the section in server.py with an import of the new module.

Run it, then verify with `python test_tool_schema.py` that all 41 tools
still register.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "google_search_mcp", "server.py")
OUT = os.path.join(ROOT, "src", "google_search_mcp", "tools", "web_utils.py")

START = 4592  # 1-indexed first line ("async def _fetch_page_text")
END = 5723    # 1-indexed last line ("    )")


def main() -> None:
    with open(SERVER) as f:
        lines = f.readlines()

    section = lines[START - 1:END]

    header = '''"""Web utilities — page fetching, documents, email, pastebin, QR, archiving, Wikipedia, S3.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
import imaplib
import json
import os
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from email import policy as email_policy
from email.parser import BytesParser as EmailParser
from pathlib import Path

from mcp.server.fastmcp import Context, Image

from ..config import IMAP_SERVERS, MAX_PAGE_CHARS
from ..server import (
    mcp,
    _fetch_url_bytes,
    format_error,
    launch_browser,
    strip_html,
)
from ..utils.text import format_timestamp
from playwright.async_api import async_playwright


'''
    body = "".join(section)

    with open(OUT, "w") as f:
        f.write(header)
        f.write(body)

    new_server = lines[:START - 1] + [
        "# ---------------------------------------------------------------------------\n",
        "# Web utilities — moved to tools/web_utils.py\n",
        "# ---------------------------------------------------------------------------\n",
        "from .tools import web_utils as _web_utils  # noqa: F401  (registers web tools)\n",
        "\n",
    ] + lines[END:]

    with open(SERVER, "w") as f:
        f.writelines(new_server)

    print(f"Wrote {OUT} ({len(section)} lines)")
    print(f"Replaced web-utilities section in {SERVER}")


if __name__ == "__main__":
    main()
