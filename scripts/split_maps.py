#!/usr/bin/env python3
"""Extract the maps section from server.py into tools/maps.py.

Reads server.py, pulls out the maps section (lines 520-936, 1-indexed),
writes it as tools/maps.py with the needed imports, and replaces the
section in server.py with an import of the new module.

Run it, then verify with `python test_tool_schema.py` that all 41 tools
still register.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "google_search_mcp", "server.py")
OUT = os.path.join(ROOT, "src", "google_search_mcp", "tools", "maps.py")

START = 520  # 1-indexed first line ("# google_maps")
END = 936    # 1-indexed last line ("    return await do_google_maps_directions(...)")


def main() -> None:
    with open(SERVER) as f:
        lines = f.readlines()

    section = lines[START - 1:END]

    header = '''"""Google Maps tools — places search and directions.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
from urllib.parse import quote_plus

from mcp.server.fastmcp import Context, Image
from playwright.async_api import async_playwright

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
)


'''
    body = "".join(section)

    with open(OUT, "w") as f:
        f.write(header)
        f.write(body)

    new_server = lines[:START - 1] + [
        "# ---------------------------------------------------------------------------\n",
        "# Google Maps tools — moved to tools/maps.py\n",
        "# ---------------------------------------------------------------------------\n",
        "from .tools import maps as _maps  # noqa: F401  (registers maps tools)\n",
        "\n",
    ] + lines[END:]

    with open(SERVER, "w") as f:
        f.writelines(new_server)

    print(f"Wrote {OUT} ({len(section)} lines)")
    print(f"Replaced maps section in {SERVER}")


if __name__ == "__main__":
    main()
