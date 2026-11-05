"""Enumerate the widgets on a Google Trends page and their rows.

``google_trends`` parses each ``trends-widget`` and dispatches on its title. This
prints every widget title with its first rows, so a mis-parse (rows from the wrong
widget landing in "Related queries") is visible directly.

    python scripts/probe_trends.py "electric vehicles"
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import quote_plus

from _probe_common import save_html

from google_search_mcp.browser import dismiss_consent, load_cookies
from google_search_mcp.core.browser import pooled_browser

JS = """
() => {
    const out = [];
    for (const w of document.querySelectorAll('trends-widget')) {
        const titleEl = w.querySelector('.widget-title, .fe-atoms-generic-title, h2');
        let title = titleEl ? titleEl.innerText : '';
        if (!title) title = (w.innerText || '').split('\\n')[0] || '';
        title = title.replace(/help_outline/g, '').trim();

        const lines = (w.innerText || '')
            .split('\\n').map(s => s.trim()).filter(Boolean);

        out.push({
            title,
            len: (w.innerText || '').length,
            hasTable: !!w.querySelector('table'),
            head: lines.slice(0, 14),
        });
    }
    return out;
}
"""


async def main() -> None:
    query = " ".join(sys.argv[1:]) or "electric vehicles"
    url = (
        "https://trends.google.com/trends/explore?q="
        f"{quote_plus(query)}&hl=en-US"
    )
    print(f"url = {url}\n")

    async with pooled_browser() as (ctx, page):
        await load_cookies(ctx)
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await dismiss_consent(page)

        for _ in range(10):
            await page.wait_for_timeout(2000)
            ready = await page.evaluate(
                """() => Array.from(document.querySelectorAll('trends-widget'))
                    .filter(w => (w.innerText || '').length > 150).length"""
            )
            if ready >= 3:
                break

        widgets = await page.evaluate(JS)
        print(f"widgets: {len(widgets)}\n")
        for i, w in enumerate(widgets, 1):
            print(f"===== widget {i}  title={w['title']!r}  len={w['len']} table={w['hasTable']}")
            for line in w["head"]:
                print(f"    {line!r}")
            print()

        html = await page.content()
        save_html("trends_widgets.html", html)


if __name__ == "__main__":
    asyncio.run(main())
