"""Dump the structure of a Google vertical SERP for selector work.

    python scripts/probe_vertical.py news  electric vehicles
    python scripts/probe_vertical.py shop  mechanical keyboard
    python scripts/probe_vertical.py books taymiyyah

Saves /tmp/<vertical>_serp.html and prints how many elements each candidate
selector matches, so parsers can be written against the real DOM.
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import quote_plus

from google_search_mcp import anti_bot

VERTICALS = {
    "news": ("nws", """
        div.SoaBEf, div.g, .Ww4FFb, .vY6njf, .dURjMd, .n0jPhd, article,
        div[data-ved][jscontroller], .NiLAwe, .xrnccd, .MjjYud
    """),
    "shop": ("shop", """
        .sh-dgr__content, .KZmu8e, .i0X6df, .xcR77, [data-docid],
        .sh-pr__product-result, .MjjYud, div.g
    """),
    "books": ("bks", """
        .MjjYud, div.g, a[href*="books.google.com/books"], .byrV5b
    """),
}


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        return
    name = sys.argv[1]
    query = " ".join(sys.argv[2:])
    tbm, selectors = VERTICALS[name]
    url = f"https://www.google.com/search?q={quote_plus(query)}&hl=en&tbm={tbm}&num=20"

    print(f"url = {url}")
    async with anti_bot.browse_google(url, screenshot_label=f"probe_{name}") as page:
        if page is None:
            print("BLOCKED")
            return
        print(f"final url = {page.url}")
        html = await page.content()
        with open(f"/tmp/{name}_serp.html", "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"saved /tmp/{name}_serp.html ({len(html)} bytes)\n")

        counts = await page.evaluate(
            """(sel) => {
                const out = {};
                for (const s of sel.split(',').map(x => x.trim()).filter(Boolean)) {
                    try { out[s] = document.querySelectorAll(s).length; }
                    catch (e) { out[s] = 'ERR'; }
                }
                return out;
            }""",
            selectors,
        )
        for sel, count in counts.items():
            print(f"{count:>5}  {sel}")


if __name__ == "__main__":
    asyncio.run(main())
