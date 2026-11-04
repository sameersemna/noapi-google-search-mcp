"""Enumerate the tab links and result-type section headers on a Google SERP.

Google mixes result types (AI Overview, Top stories, Discussions and forums,
Hotel pack, Videos, Shopping…) into one page and exposes vertical switches as
``/search?...&udm=<n>`` or ``&tbm=<key>`` links. This prints exactly what a SERP
offers so ``google_search_tabs_urls`` can be written against reality.

    python scripts/probe_serp_sections.py "electric vehicles"
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import quote_plus

from _probe_common import save_html

from google_search_mcp import anti_bot

TABS_JS = """
() => {
    const abs = (h) => { try { return new URL(h, location.href).href; } catch (e) { return h; } };
    const out = [];
    const seen = new Set();
    for (const a of document.querySelectorAll('a[href*="udm="], a[href*="tbm="]')) {
        const href = a.getAttribute('href') || '';
        const text = (a.innerText || '').trim().replace(/\\s+/g, ' ');
        if (!text || text.length > 40) continue;
        if (seen.has(text + href)) continue;
        seen.add(text + href);
        const u = new URL(abs(href));
        out.push({
            text,
            udm: u.searchParams.get('udm') || '',
            tbm: u.searchParams.get('tbm') || '',
            path: u.pathname,
            aria: (a.getAttribute('aria-label') || '').slice(0, 50),
        });
    }
    return out;
}
"""

HEADINGS_JS = """
() => {
    const out = [];
    for (const el of document.querySelectorAll('h2, h3, [role="heading"], div[role="heading"]')) {
        const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
        if (!t || t.length > 70) continue;
        const lvl = el.getAttribute('aria-level') || el.tagName;
        // Skip headings that are result titles: those live inside a link with
        // an external href.
        const link = el.closest('a');
        const external = link && /^https?:\\/\\//.test(link.href || '') &&
                         !/google\\./.test(new URL(link.href).hostname);
        out.push({ text: t, level: lvl, external: !!external });
    }
    // Dedupe, keeping order.
    const seen = new Set(); const dedup = [];
    for (const h of out) {
        const k = h.text.toLowerCase();
        if (seen.has(k)) continue;
        seen.add(k); dedup.push(h);
    }
    return dedup.slice(0, 40);
}
"""

# Section containers Google uses for the mixed-in modules.
SECTION_JS = """
() => {
    const probes = [
        'g-section-with-header', 'g-accordion-expander', '.related-question-pair',
        '[data-attrid*="AIOverview"]', '[data-attrid*="hero"]',
        'g-scrolling-carousel', '.ULSxyf', '.MjjYud',
    ];
    const out = {};
    for (const p of probes) {
        try { out[p] = document.querySelectorAll(p).length; } catch (e) { out[p] = 'ERR'; }
    }
    return out;
}
"""


async def main() -> None:
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "electric vehicles"
    url = f"https://www.google.com/search?q={quote_plus(query)}&hl=en"

    print(f"query = {query!r}")
    async with anti_bot.browse_google(url, screenshot_label="probe_sections") as page:
        if page is None:
            print("BLOCKED")
            return
        print(f"final = {page.url}\n")

        tabs = await page.evaluate(TABS_JS)
        print(f"── TAB LINKS ({len(tabs)}) ──")
        for t in tabs:
            print(f"  {t['text']!r:28} udm={t['udm'] or '-':3} tbm={t['tbm'] or '-':5} path={t['path']}")

        heads = await page.evaluate(HEADINGS_JS)
        print(f"\n── HEADINGS ({len(heads)}) ──")
        for h in heads:
            mark = " (result-title)" if h["external"] else ""
            print(f"  [{h['level']:6}] {h['text']!r}{mark}")

        secs = await page.evaluate(SECTION_JS)
        print("\n── SECTION CONTAINERS ──")
        for k, v in secs.items():
            print(f"  {v:>4}  {k}")

        html = await page.content()
        print()
        save_html("serp_sections.html", html)


if __name__ == "__main__":
    asyncio.run(main())
