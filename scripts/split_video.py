#!/usr/bin/env python3
"""Extract the video section from server.py into tools/video.py.

Reads server.py, pulls out the video section (lines 551-1125, 1-indexed),
writes it as tools/video.py with the needed imports, and replaces the
section in server.py with an import of the new module.

Run it, then verify with `python test_tool_schema.py` that all 41 tools
still register.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "google_search_mcp", "server.py")
OUT = os.path.join(ROOT, "src", "google_search_mcp", "tools", "video.py")

START = 551  # 1-indexed first line ("# transcribe_video ...")
END = 1125   # 1-indexed last line ("        return f\"Failed to extract clip: {e}\"")


def main() -> None:
    with open(SERVER) as f:
        lines = f.readlines()

    section = lines[START - 1:END]

    header = '''"""Google video tools — transcribe_video, search_transcript, extract_video_clip.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
import hashlib
import json
import os
import re
import subprocess
from urllib.parse import quote_plus

from mcp.server.fastmcp import Context, Image
from playwright.async_api import async_playwright

from ..config import (
    CLIPS_DIR,
    TRANSCRIBE_CACHE_DIR,
    TRANSCRIPT_CACHE_DIR,
    VIDEO_CACHE_DIR,
)
from ..server import (
    mcp,
    _download_audio,
    _transcribe_audio,
    _transcript_cache_path,
    browse_google,
    dismiss_consent,
    format_error,
    format_timestamp,
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
        "# Google video tools — moved to tools/video.py\n",
        "# ---------------------------------------------------------------------------\n",
        "from .tools import video as _video  # noqa: F401  (registers video tools)\n",
        "\n",
    ] + lines[END:]

    with open(SERVER, "w") as f:
        f.writelines(new_server)

    print(f"Wrote {OUT} ({len(section)} lines)")
    print(f"Replaced video section in {SERVER}")


if __name__ == "__main__":
    main()
