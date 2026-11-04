"""Probe Google's hotel surfaces and dump the DOM structure of a real result card.

The tool currently searches ``google.com/search?q=hotels+<query>``, which returns a
generic web SERP (AI mode reply + Images/News/Forums tabs). This probe compares
that against Google Travel and prints the selectors that actually hold hotel
data, so the extractor can be written against reality.

    python scripts/probe_hotels.py "hotels in Berlin under $200 per night"
"""

from __future__ import annotations

import asyncio
import sys

from google_search_mcp.browser import dismiss_consent, load_cookies
from google_search_mcp.core.browser import pooled_browser

CANDIDATES = [
    # What the tool uses today.
    "https://www.google.com/search?q=hotels+Hotels+in+Berlin&hl=en",
    # Google Travel search (the real hotel surface).
    "https://www.google.com/travel/search?q=hotels%20in%20Berlin&hl=en&gl=de",
    # Legacy travel URL shape still linked from the SERP.
    "https://www.google.com/travel/hotels/Berlin?hl=en&gl=de",
]

# Candidate hotel-card selectors seen across Google Travel revisions.
CARD_SELECTORS = [
    ".uaTTDe", "div.kCsrf", "c-wiz div.kCsrf", ".BTPx6e", ".cuQzEe",
    ".p69s9e", ".PVOOXe", ".l5cSP", "div[data-hveid][jsname]",
    ".fds1cd", ".eRl2bf", ".q2ZQ3", ".m5hFTe",
]

STRUCT_JS = """
(sel) => {
    const out = {};
    for (const s of sel) {
        try { out[s] = document.querySelectorAll(s).length; } catch (e) { out[s] = 'ERR'; }
    }
    return out;
}
"""

CARD_JS = """
() => {
    // Find the first element that looks like a hotel card: it holds a
    // price-like string AND a star-rating indicator.
    const all = Array.from(document.querySelectorAll('div'));
    const priceRe = /[\\$€£]\\s?\\d[\\d,]*/;
    const cands = all.filter(e => {
        const t = e.innerText || '';
        return priceRe.test(t) && t.length < 600 && e.querySelector('a[href]');
    });
    const card = cands.sort((a, b) => (a.innerText||'').length - (b.innerText||'').length)[0];
    if (!card) return { found: false };

    const chain = [];
    let n = card;
    for (let i = 0; i < 5 && n; i++) {
        chain.push({
            tag: n.tagName,
            cls: (n.className || '').toString().slice(0, 80),
            jsname: n.getAttribute && (n.getAttribute('jsname') || ''),
            aria: n.getAttribute && (n.getAttribute('aria-label') || '').slice(0, 60),
            len: (n.innerText || '').length,
        });
        n = n.parentElement;
    }

    const kids = Array.from(card.querySelectorAll('*')).slice(0, 30).map(e => ({
        tag: e.tagName,
        cls: (e.className || '').toString().slice(0, 50),
        aria: (e.getAttribute('aria-label') || '').slice(0, 60),
        text: (e.innerText || '').trim().slice(0, 70),
    }));

    return {
        found: true,
        cardText: (card.innerText || '').slice(0, 500),
        chain, kids,
        hrefs: Array.from(card.querySelectorAll('a[href]')).slice(0, 4).map(a => a.href.slice(0, 110)),
    };
}
"""


async def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "hotels in Berlin"
    from urllib.parse import quote_plus

    async with pooled_browser() as (ctx, page):
        await load_cookies(ctx)
        for url in CANDIDATES:
            print("=" * 78)
            print("URL   :", url)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                await dismiss_consent(page)
                await page.wait_for_timeout(4000)
                print("FINAL :", page.url)
                print("TITLE :", await page.title())
                html = await page.content()
                name = url.split("//", 1)[1].split("?", 1)[0].replace("/", "_")
                path = f"/tmp/hotels_{name}.html"
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(html)
                print(f"SAVED : {path} ({len(html)} bytes)")

                counts = await page.evaluate(STRUCT_JS, CARD_SELECTORS)
                hits = {k: v for k, v in counts.items() if v}
                print("SELECTOR HITS:", hits or "(none)")

                info = await page.evaluate(CARD_JS)
                if not info.get("found"):
                    print("CARD: none found")
                    continue
                print("CARD TEXT:", info["cardText"].replace("\n", " | ")[:400])
                for i, c in enumerate(info["chain"]):
                    print(f"   chain[{i}] <{c['tag']}> class={c['cls']!r} jsname={c['jsname']!r} len={c['len']}")
                print("   children:")
                for k in info["kids"][:14]:
                    print(f"     <{k['tag']:5}> {k['cls']!r:40} aria={k['aria']!r:30} {k['text']!r}")
                print("   hrefs:", info["hrefs"])
            except Exception as exc:  # noqa: BLE001
                print("ERROR :", exc)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
