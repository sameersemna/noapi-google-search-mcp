#!/usr/bin/env python3
"""Extract the search-family section from server.py into tools/search.py.

Reads server.py, pulls out the search section (lines 512-1341, 1-indexed),
writes it as tools/search.py with the needed imports, and replaces the
section in server.py with an import of the new module.

Run it, then verify with `python test_tool_schema.py` that all 41 tools
still register.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "google_search_mcp", "server.py")
OUT = os.path.join(ROOT, "src", "google_search_mcp", "tools", "search.py")

START = 512  # 1-indexed first line ("# google_search")
END = 1341   # 1-indexed last line ("    return await do_google_trends(query)")


def main() -> None:
    with open(SERVER) as f:
        lines = f.readlines()

    section = lines[START - 1:END]

    header = '''"""Google search tools — web search, news, scholar, trends.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
from urllib.parse import quote_plus

from mcp.server.fastmcp import Context, Image
from playwright.async_api import async_playwright

from ..config import TIME_RANGE_MAP
from ..server import (
    mcp,
    browse_google,
    dismiss_consent,
    format_error,
    human_delay,
    is_blocked,
    launch_browser,
    load_cookies,
    save_cookies,
    try_solve_captcha,
    wait_for_google_results_ready,
)
from ..utils.network import (
    fallback_duckduckgo_news,
    fallback_web_search,
    format_fallback_results,
    resolve_urls,
)


'''
    body = "".join(section)

    with open(OUT, "w") as f:
        f.write(header)
        f.write(body)

    new_server = lines[:START - 1] + [
        "# ---------------------------------------------------------------------------\n",
        "# Google search tools — moved to tools/search.py\n",
        "# ---------------------------------------------------------------------------\n",
        "from .tools import search as _search  # noqa: F401  (registers search tools)\n",
        "\n",
    ] + lines[END:]

    with open(SERVER, "w") as f:
        f.writelines(new_server)

    print(f"Wrote {OUT} ({len(section)} lines)")
    print(f"Replaced search section in {SERVER}")


if __name__ == "__main__":
    main()
