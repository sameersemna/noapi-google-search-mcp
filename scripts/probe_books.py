"""Ad-hoc probe: what does the Google Books vertical actually return?

Prints which vertical the browser ends up on and dumps the result container
structure so the parser can be written against reality rather than guesses.
"""

from __future__ import annotations

import asyncio
import sys

from google_search_mcp import anti_bot, anti_detect, config
from google_search_mcp.anti_detect import is_google_search_url, parse_query_from_url

QUERY = sys.argv[1] if len(sys.argv) > 1 else "taymiyyah"
URL = f"https://www.google.com/search?q={QUERY}&hl=en&tbm=bks&num=10"


async def main() -> None:
    print(f"url      = {URL}")
    print(f"is_search= {is_google_search_url(URL)}")
    print(f"query    = {parse_query_from_url(URL)!r}")
    print(f"typing   = {config.ANTIDETECT_SEARCH_VIA_TYPING}")

    async with anti_bot.browse_google(URL, screenshot_label="probe") as page:
        if page is None:
            print("BLOCKED (page is None)")
            return
        print(f"final url = {page.url}")
        print(f"title     = {await page.title()}")

        # Save the whole page for offline inspection.
        html = await page.content()
        with open("/tmp/books_serp.html", "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"html saved: /tmp/books_serp.html ({len(html)} bytes)")

        info = await page.evaluate(
            """
            () => {
                const count = (sel) => document.querySelectorAll(sel).length;
                const sample = (sel, n) => Array.from(document.querySelectorAll(sel))
                    .slice(0, n)
                    .map(e => (e.innerText || '').trim().slice(0, 120));
                return {
                    tbm: new URL(location.href).searchParams.get('tbm'),
                    h3: count('h3'),
                    booksLinks: count('a[href*="books.google"]'),
                    gBlocks: count('.g'),
                    byrV5b: count('.byrV5b'),
                    MjjYud: count('.MjjYud'),
                    bAliOe: count('.bAliOe'),
                    Y3v8qd: count('.Y3v8qd'),
                    h3Sample: sample('h3', 8),
                    booksLinkSample: Array.from(document.querySelectorAll('a[href*="books.google"]'))
                        .slice(0, 4).map(a => a.href),
                };
            }
            """
        )
        for key, value in info.items():
            print(f"{key:>16}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
