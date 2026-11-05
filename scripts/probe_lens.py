"""Probe Google Lens' result DOM so its extractor can be anchored properly.

``google_lens`` currently matches nothing and falls back to dumping page chrome
("Skip to main content / Accessibility help / Footer links …"). This prints the
selectors that actually hold the results.

    python scripts/probe_lens.py
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote_plus

from _probe_common import save_html

from google_search_mcp.browser import dismiss_consent, load_cookies
from google_search_mcp.core.browser import pooled_browser

IMAGES = {
    "tower": (
        "https://upload.wikimedia.org/wikipedia/commons/thumb/"
        "a/a9/Eiffel_Tower_from_Champ_de_Mars.jpg/640px-Eiffel_Tower_from_Champ_de_Mars.jpg"
    ),
    "cat": (
        "https://upload.wikimedia.org/wikipedia/commons/thumb/"
        "3/3a/Cat03.jpg/640px-Cat03.jpg"
    ),
    "logo": (
        "https://upload.wikimedia.org/wikipedia/commons/thumb/"
        "9/95/Instagram_logo_2022.svg/640px-Instagram_logo_2022.svg.png"
    ),
}
IMAGE_URL = IMAGES.get(__import__("sys").argv[1] if len(__import__("sys").argv) > 1 else "tower", IMAGES["tower"])

SELECTORS = [
    "div[role='heading']",
    "h3",
    "a[href^='http']",
    ".g",
    "#rso",
    "[data-attrid]",
    "g-section-with-header",
    ".Vd9M6",
    ".F1hUFe",
    ".m4JzQe",
    ".kC8rJf",
    ".qR5WPe",
    "c-wiz",
    "[jsname]",
    "img",
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

# Find the element that holds a visual-match card: a link to a non-Google host
# with a heading.
CARD_JS = """
() => {
    const isExternal = (h) => {
        try { const u = new URL(h, location.href);
              return /^https?:/.test(u.protocol) && !/google\\./.test(u.hostname); }
        catch (e) { return false; }
    };
    const links = Array.from(document.querySelectorAll('a[href^="http"]'))
        .filter(a => isExternal(a.href) && (a.innerText || '').trim().length > 8);
    const out = { count: links.length, samples: [] };
    for (const a of links.slice(0, 6)) {
        const chain = [];
        let n = a;
        for (let i = 0; i < 4 && n; i++) {
            chain.push({ tag: n.tagName, cls: (n.className||'').toString().slice(0,60),
                          id: n.id || '', len: (n.innerText||'').length });
            n = n.parentElement;
        }
        const heading = a.querySelector('h3, [role="heading"]');
        out.samples.push({
            href: a.href.slice(0, 90),
            text: (a.innerText || '').trim().slice(0, 80).replace(/\\n/g, ' | '),
            hasHeading: !!heading,
            headingText: heading ? heading.innerText.trim().slice(0, 60) : '',
            chain,
        });
    }
    return out;
}
"""

HEADINGS_JS = """
() => Array.from(document.querySelectorAll('div[role="heading"]'))
    .slice(0, 25)
    .map(e => (e.innerText || '').trim().slice(0, 60).replace(/\\n/g, ' | '))
    .filter(Boolean);
"""


async def main() -> None:
    url = f"https://lens.google.com/uploadbyurl?url={quote_plus(IMAGE_URL)}&hl=en"
    print("url:", url)

    async with pooled_browser() as (ctx, page):
        await load_cookies(ctx)
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await dismiss_consent(page)

        # Lens renders an AI "Thinking" interstitial first and fills the results
        # in later, so poll for real content instead of a fixed wait.
        LEN_JS = "() => document.body.innerText.length"
        for elapsed in range(0, 46, 5):
            await page.wait_for_timeout(5000)
            length = await page.evaluate(LEN_JS)
            print(f"  t+{elapsed + 5:>2}s  body text {length} chars", flush=True)
            if length > 700:
                break

        print("final:", page.url)
        print("title:", await page.title())

        html = await page.content()
        save_html("lens_results.html", html)

        counts = await page.evaluate(STRUCT_JS, SELECTORS)
        print("\n── selector hits ──")
        for sel, n in counts.items():
            if n:
                print(f"  {n:>5}  {sel}")

        print("\n── div[role=heading] samples ──")
        for text in await page.evaluate(HEADINGS_JS):
            print(f"  {text!r}")

        print("\n── external links (candidate result cards) ──")
        cards = await page.evaluate(CARD_JS)
        print("  total:", cards["count"])
        for s in cards["samples"]:
            print(f"\n  text={s['text']!r}")
            print(f"  href={s['href']}")
            print(f"  heading={s['hasHeading']} {s['headingText']!r}")
            for i, c in enumerate(s["chain"]):
                print(f"    chain[{i}] <{c['tag']}> cls={c['cls']!r} id={c['id']!r} len={c['len']}")

        body = await page.evaluate("() => document.body.innerText.substring(0, 900)")
        print("\n── body text head ──")
        print(body)


if __name__ == "__main__":
    asyncio.run(main())
