#!/usr/bin/env python3
"""Offline micro-benchmark: per-call Chromium launch vs the shared browser pool.

This runs entirely against local fixture HTML (``page.set_content``) so it needs
**no network**. It does require Playwright's Chromium to be installed.

Usage::

    python scripts/bench_browser.py [iterations]

It compares:

* **per-call** — what every tool used to do: start Playwright, launch a
  persistent Chromium context, open a page, render the fixture, close.
* **pooled** — the Phase 4 behaviour: reuse one Chromium context and only take a
  fresh page per call.

A temporary user-data-dir is used so the real profile is never touched and the
two runs cannot contend on the same directory lock.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

_FIXTURE = (
    "<html><body><h1>Hello</h1><article><p>" + ("word " * 400) + "</p></article></body></html>"
)


def _prepare() -> str:
    from google_search_mcp import browser as browser_mod

    tmpdir = tempfile.mkdtemp(prefix="bench-profile-")
    # Never touch the real profile / lock.
    browser_mod.BROWSER_DATA_DIR = tmpdir
    return tmpdir


async def bench_per_call(n: int) -> float:
    from playwright.async_api import async_playwright

    from google_search_mcp.browser import launch_browser

    total = 0.0
    for _ in range(n):
        start = time.perf_counter()
        async with async_playwright() as pw:
            context = await launch_browser(pw)
            page = await context.new_page()
            try:
                await page.set_content(_FIXTURE)
            finally:
                await context.close()
        total += time.perf_counter() - start
    return total / n


async def bench_pooled(n: int) -> float:
    from google_search_mcp.core import browser as pool_mod

    await pool_mod.pool.aclose()
    # Warm up (first call pays the one-time Chromium start; not timed).
    async with pool_mod.pooled_browser() as (_ctx, page):
        await page.set_content(_FIXTURE)

    total = 0.0
    for _ in range(n):
        start = time.perf_counter()
        async with pool_mod.pooled_browser() as (_ctx, page):
            await page.set_content(_FIXTURE)
        total += time.perf_counter() - start
    await pool_mod.pool.aclose()
    return total / n


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    _prepare()

    print(f"iterations per mode: {n}\n")
    per_call = await bench_per_call(n)
    pooled = await bench_pooled(n)

    print(f"per-call launch : {per_call * 1000:8.1f} ms/call")
    print(f"pooled (reuse)  : {pooled * 1000:8.1f} ms/call")
    if pooled > 0:
        print(f"speedup         : {per_call / pooled:8.2f}x")
    print(f"saved per call  : {(per_call - pooled) * 1000:8.1f} ms")


if __name__ == "__main__":
    asyncio.run(main())
