"""Validate a vertical's extractor against a saved SERP, without the network.

Loads a page captured by ``scripts/probe_vertical.py`` into a blank page and runs
the *shipped* extractor, so parser iterations cost seconds instead of a browser
round-trip (and do not hit Google).

    python scripts/probe_parser.py books /tmp/books_serp.html 5
    python scripts/probe_parser.py news  /tmp/news_serp.html 3
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "src" / "google_search_mcp" / "tools"

# vertical -> (module file, function that owns the extractor)
TARGETS = {
    "books": ("finance.py", "async def do_google_books"),
    "news": ("search.py", "async def do_google_news"),
}


def extract_js(vertical: str) -> str:
    """Pull the ``page.evaluate`` script out of the tool, so this always runs the
    shipped code rather than a copy of it."""
    module, marker = TARGETS[vertical]
    src = (TOOLS / module).read_text(encoding="utf-8")
    start = src.index(marker)

    # End of the function: the first top-level boundary after the marker.
    ends = [
        idx
        for idx in (
            src.find("\n@mcp.tool()", start + 1),
            src.find("\n@cached", start + 1),
            src.find("\n# ---", start + 1),
        )
        if idx != -1
    ]
    if not ends:
        raise SystemExit(f"cannot find the end of {marker}")
    body = src[start : min(ends)]

    match = re.search(r'page\.evaluate\(\s*\n\s*r?"""(.*?)"""', body, re.S)
    if not match:
        raise SystemExit(f"could not locate the evaluate script for {vertical}")
    return match.group(1)


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        return
    vertical = sys.argv[1]
    path = Path(sys.argv[2])
    num_results = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    js = extract_js(vertical)
    # Read the capture off the event loop: the file can be several MB.
    html = await asyncio.to_thread(path.read_text, encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html, wait_until="domcontentloaded")
        results = await page.evaluate(js, num_results)
        await browser.close()

    print(f"{vertical}: extracted {len(results)} result(s) from {path.name}\n")
    for i, r in enumerate(results, 1):
        if r.get("raw_text") or r.get("title") == "__raw__":
            print(f"{i}. RAW FALLBACK - parser matched nothing")
            print(f"   {str(r.get('snippet', ''))[:200]}\n")
            continue
        print(f"{i}. {r.get('title')}")
        for label, key in (
            ("Author", "author"),
            ("Year", "year"),
            ("Source", "source"),
            ("Time", "time"),
            ("ISBN", "isbn"),
            ("URL", "url"),
        ):
            if r.get(key):
                print(f"   {label}: {r[key]}")
        if r.get("snippet"):
            print(f"   {str(r['snippet'])[:220]}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
