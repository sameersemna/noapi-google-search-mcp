#!/usr/bin/env python3
"""Extract the vision section from server.py into tools/vision.py.

Reads server.py, pulls out the vision section (lines 544-1397, 1-indexed),
writes it as tools/vision.py with the needed imports, and replaces the
section in server.py with an import of the new module.

Run it, then verify with `python test_tool_schema.py` that all 41 tools
still register.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "google_search_mcp", "server.py")
OUT = os.path.join(ROOT, "src", "google_search_mcp", "tools", "vision.py")

START = 544  # 1-indexed first line ("# google_lens (reverse image search)")
END = 1395   # 1-indexed last line ("            os.unlink(tmp_base64_path)")


def main() -> None:
    with open(SERVER) as f:
        lines = f.readlines()

    section = lines[START - 1:END]

    header = '''"""Google vision tools — lens, lens_detect, list_images, ocr_image.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
import base64
import os
import re
from urllib.parse import quote_plus

from mcp.server.fastmcp import Context, Image
from playwright.async_api import async_playwright

from ..config import (
    DEFAULT_IMAGE_DIR,
    IMAGE_EXTENSIONS,
    MAX_OBJECTS,
    MOBILENET_ONNX_PATH,
)
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
from ..utils.image import is_base64_image, is_local_file, save_base64_image


'''
    body = "".join(section)

    with open(OUT, "w") as f:
        f.write(header)
        f.write(body)

    new_server = lines[:START - 1] + [
        "# ---------------------------------------------------------------------------\n",
        "# Google vision tools — moved to tools/vision.py\n",
        "# ---------------------------------------------------------------------------\n",
        "from .tools import vision as _vision  # noqa: F401  (registers vision tools)\n",
        "\n",
    ] + lines[END:]

    with open(SERVER, "w") as f:
        f.writelines(new_server)

    print(f"Wrote {OUT} ({len(section)} lines)")
    print(f"Replaced vision section in {SERVER}")


if __name__ == "__main__":
    main()
