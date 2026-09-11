"""Google search tools — web search, news, scholar, trends.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
from urllib.parse import quote_plus

from mcp.server.fastmcp import Context, Image
from playwright.async_api import async_playwright

from ..config import TIME_RANGE_MAP
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
    wait_for_google_results_ready,
)
from ..utils.network import (
    fallback_duckduckgo_news,
    fallback_web_search,
    format_fallback_results,
    resolve_urls,
)


# ---------------------------------------------------------------------------
# google_search
# ---------------------------------------------------------------------------

async def do_google_search(
    query: str,
    num_results: int = 5,
    time_range: str | None = None,
    site: str | None = None,
    page: int = 1,
    language: str | None = None,
    region: str | None = None,
) -> str:
    """Launch headless Chromium, search Google, and scrape results."""
    search_query = query
    if site:
        search_query = f"site:{site} {search_query}"

    encoded_query = quote_plus(search_query)
    start = (page - 1) * num_results
    url = f"https://www.google.com/search?q={encoded_query}&num={num_results + 2}"

    # Language and region
    if language:
        url += f"&lr=lang_{language}&hl={language}"
    else:
        url += "&hl=en"
    if region:
        url += f"&gl={region}"

    if start > 0:
        url += f"&start={start}"
    if time_range and time_range in TIME_RANGE_MAP:
        url += f"&tbs={TIME_RANGE_MAP[time_range]}"

    def _fallback() -> str:
        provider, fallback = fallback_web_search(query, num_results)
        if fallback:
            return format_fallback_results(query, provider, fallback)
        return (
            "Search blocked by Google bot detection. "
            "Your IP may be temporarily rate-limited. "
            "Try again in a few minutes or from a different network."
        )

    async with browse_google(url, fallback_fn=_fallback, screenshot_label="search") as browser_page:
        # browse_google yields the fallback result (str) when blocked, or None if no fallback
        if browser_page is None or isinstance(browser_page, str):
            return _fallback()

        await wait_for_google_results_ready(browser_page, timeout_ms=15000)

        results = await browser_page.evaluate(
            """
            (numResults) => {
                const results = [];
                const seen = new Set();

                const normalizeHref = (href) => {
                    if (!href) return '';
                    let out = href;
                    if (out.startsWith('/url?')) {
                        try {
                            const u = new URL(out, location.origin);
                            out =
                                u.searchParams.get('q') ||
                                u.searchParams.get('url') ||
                                u.searchParams.get('u') ||
                                u.searchParams.get('uddg') ||
                                out;
                        } catch (_) {}
                    } else if (out.includes('/url?')) {
                        try {
                            const u = new URL(out, location.origin);
                            out =
                                u.searchParams.get('q') ||
                                u.searchParams.get('url') ||
                                u.searchParams.get('u') ||
                                u.searchParams.get('uddg') ||
                                out;
                        } catch (_) {}
                    }
                    try {
                        out = decodeURIComponent(out);
                    } catch (_) {}
                    if (!out.startsWith('http')) return '';
                    if (out.includes('google.com/search') || out.includes('/preferences?')) return '';
                    return out;
                };

                const canonicalKey = (url, title) => {
                    try {
                        const u = new URL(url);
                        const path = (u.pathname || '/').replace(/\\/+$/, '') || '/';
                        const host = u.hostname.replace(/^www\\./, '');
                        const t = (title || '').trim().toLowerCase();
                        return `${host}${path}|${t}`;
                    } catch (_) {
                        return `${url}|${(title || '').trim().toLowerCase()}`;
                    }
                };

                const addResult = (title, href, snippet) => {
                    if (results.length >= numResults) return;
                    const cleanTitle = (title || '').trim();
                    const cleanUrl = normalizeHref(href || '');
                    if (!cleanTitle || !cleanUrl) return;
                    const key = canonicalKey(cleanUrl, cleanTitle);
                    if (seen.has(key)) return;
                    seen.add(key);
                    results.push({
                        title: cleanTitle,
                        url: cleanUrl,
                        snippet: (snippet || '').trim(),
                    });
                };

                const containers = document.querySelectorAll(
                    'div#search div.g, #rso div.g, #rso div.MjjYud, div.g, div.MjjYud, div[data-hveid]'
                );
                for (const el of containers) {
                    if (results.length >= numResults) break;
                    const titleEl = el.querySelector('h3');
                    if (!titleEl) continue;
                    const linkEl = titleEl.closest('a[href]') || el.querySelector('a:has(h3), a[href]');
                    const snippetEl = el.querySelector(
                        'div[data-sncf], div.VwiC3b, span.aCOpRe, div[style*="-webkit-line-clamp"], div[data-content-feature]'
                    );
                    addResult(titleEl.innerText, linkEl?.href || '', snippetEl?.innerText || '');
                }

                if (results.length < numResults) {
                    const headings = document.querySelectorAll('#search h3, #rso h3, h3');
                    for (const h3 of headings) {
                        if (results.length >= numResults) break;
                        const a = h3.closest('a[href]');
                        if (!a) continue;
                        const parent = a.closest('div.g, div.MjjYud, div[data-hveid]') || a.parentElement?.parentElement;
                        const snippetEl = parent?.querySelector(
                            'div[data-sncf], div.VwiC3b, span.aCOpRe, div[style*="-webkit-line-clamp"], div[data-content-feature]'
                        );
                        addResult(h3.innerText, a.href || '', snippetEl?.innerText || '');
                    }
                }

                return results;
            }
            """,
            num_results,
        )

        if not results:
            provider, fallback = fallback_web_search(query, num_results)
            if fallback:
                return format_fallback_results(query, provider, fallback)
            return f"No results found for: {query}"

        # Resolve Google redirect URLs (e.g. /url?q=..., /goto?url=...) to their
        # final destinations so citations point at the real source, not Google.
        resolved_urls = await resolve_urls([r.get("url", "") for r in results])
        for r, final_url in zip(results, resolved_urls):
            r["url"] = final_url

        header = f"Google Search Results for: {query}"
        if time_range:
            header += f" (filtered: {time_range.replace('_', ' ')})"
        if site:
            header += f" (site: {site})"
        if language:
            header += f" (lang: {language})"
        if region:
            header += f" (region: {region})"
        if page > 1:
            header += f" (page {page})"

        lines = [header + "\n"]
        offset = (page - 1) * num_results
        for i, r in enumerate(results[:num_results], offset + 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   URL: {r['url']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
            lines.append("")

        return "\n".join(lines)


@mcp.tool()
async def google_search(
    query: str,
    num_results: int = 5,
    time_range: str = "",
    site: str = "",
    page: int = 1,
    language: str = "",
    region: str = "",
) -> str:
    """Search Google and return results with titles, URLs, and snippets.

    Sample prompts that trigger this tool:
        - "Search for the best Python web frameworks"
        - "Find Reddit discussions about home lab setups from the past week"
        - "Search Stack Overflow for async Python examples"
        - "Look up recent news about SpaceX in German"
        - "Get page 2 of results for machine learning tutorials"
        - "Search Hacker News for posts about Rust programming"
        - "Find Japanese results about Tokyo restaurants"

    Args:
        query: The search query string.
        num_results: Number of results to return (default 5, max 10).
        time_range: Filter by time. One of: "past_hour", "past_day", "past_week", "past_month", "past_year". Leave empty for no filter.
        site: Limit results to a specific domain (e.g. "reddit.com", "stackoverflow.com", "github.com", "arxiv.org", "news.ycombinator.com"). Leave empty for all sites.
        page: Results page number (default 1). Use 2, 3, etc. to get more results.
        language: Language code for results (e.g. "en", "de", "fr", "es", "ja", "zh"). Leave empty for English.
        region: Country/region code (e.g. "us", "gb", "de", "fr", "jp"). Leave empty for default.
    """
    num_results = max(1, min(num_results, 10))
    page = max(1, min(page, 10))
    return await do_google_search(
        query,
        num_results,
        time_range=time_range or None,
        site=site or None,
        page=page,
        language=language or None,
        region=region or None,
    )


# ---------------------------------------------------------------------------
# google_news
# ---------------------------------------------------------------------------

async def do_google_news(query: str, num_results: int = 5) -> list:
    """Launch headless Chromium, search Google News, and scrape results."""
    encoded_query = quote_plus(query)
    url = f"https://www.google.com/search?q={encoded_query}&hl=en&tbm=nws&num={num_results + 5}"

    def _fallback() -> list:
        ddg_news = fallback_duckduckgo_news(query, num_results)
        if ddg_news:
            content = [f"Google News blocked by bot detection. Showing fallback results (DuckDuckGo News):\n"]
            content.append(f"News Results for: {query}\n")
            for i, r in enumerate(ddg_news[:num_results], 1):
                desc = f"{i}. {r['title']}"
                desc += f"\n   URL: {r['url']}"
                if r.get("source"):
                    desc += f"\n   Source: {r['source']}"
                content.append(desc)
            return content
        return [f"Google News blocked by bot detection for: {query}\nTry again later or use google_search with site:news.google.com"]

    async with browse_google(url, fallback_fn=_fallback, screenshot_label="news") as page:
        if page is None:
            return _fallback()

        await wait_for_google_results_ready(page, timeout_ms=15000)

        results = await page.evaluate(
            """
            (numResults) => {
                const results = [];
                // Primary: modern Google News containers
                const containers = document.querySelectorAll(
                    'div#search div.SoaBEf, div#search div.g, #rso div.SoaBEf, #rso div.g, div.SoaBEf, div.g, ' +
                    'div[data-sokoban-container], div.Ww4FFb, div.vY6njf, div.dURjMd'
                );
                for (const el of containers) {
                    if (results.length >= numResults) break;
                    const linkEl = el.querySelector('a[href^="http"]');
                    const titleEl = el.querySelector('div[role="heading"], h3, a.Ww4FFb, a.JheGif');
                    const sourceEl = el.querySelector('.NUnG9d, .CEMjEf, .UPmit, .Y3v8qd, .gH_JQd');
                    const timeEl = el.querySelector('.OSrXXb, .WG9SHc, .ZE0LJd span, time, [datetime], .LfYrUe');
                    const snippetEl = el.querySelector('.GI74Re, .Y3v8qd, div.VwiC3b, .f5cPye');
                    if (linkEl && titleEl) {
                        let thumbnail = '';
                        const imgs = el.querySelectorAll('img');
                        for (const img of imgs) {
                            const s = img.src || img.dataset?.src || '';
                            if (!s) continue;
                            if (s.startsWith('data:image') && s.length > 500) { thumbnail = s; break; }
                            if (s.startsWith('http') && !s.includes('gstatic.com/s/i/')) { thumbnail = s; break; }
                            if (s.startsWith('//')) { thumbnail = 'https:' + s; break; }
                        }
                        results.push({
                            title: titleEl.innerText.trim(),
                            url: linkEl.href,
                            source: sourceEl ? sourceEl.innerText.trim() : '',
                            time: timeEl ? timeEl.innerText.trim() : '',
                            snippet: snippetEl ? snippetEl.innerText.trim() : '',
                            thumbnail: thumbnail,
                        });
                    }
                }
                // Fallback: any h3 + link in the search area
                if (results.length === 0) {
                    const allLinks = document.querySelectorAll('#search a[href^="http"], #rso a[href^="http"], a[href^="http"]');
                    for (const a of allLinks) {
                        if (results.length >= numResults) break;
                        const heading = a.querySelector('div[role="heading"], h3, span[role="heading"]');
                        if (heading) {
                            results.push({
                                title: heading.innerText.trim(),
                                url: a.href,
                                source: '', time: '', snippet: ''
                            });
                        }
                    }
                }
                // Last resort: raw page text
                if (results.length === 0) {
                    const searchArea = document.querySelector('#search, #rso, [role="main"]');
                    if (searchArea) {
                        const text = searchArea.innerText.substring(0, 3000);
                        results.push({ title: 'Raw page content', url: '', source: '', time: '', snippet: text, raw_text: true });
                    }
                }
                return results;
            }
            """,
            num_results,
        )

        if not results:
            return [f"No news results found for: {query}"]

        # Check if we got raw text fallback
        if results[0].get("raw_text"):
            return [f"Google News Results for: {query}\n\n(Could not extract structured results. Raw page content:)\n{results[0]['snippet']}"]

        # Download article thumbnail images via page context
        import base64 as b64mod
        for r in results[:num_results]:
            thumb_url = r.get("thumbnail", "")
            if not thumb_url:
                continue
            if thumb_url.startswith("data:image"):
                try:
                    header, b64data = thumb_url.split(",", 1)
                    body = b64mod.b64decode(b64data)
                    if len(body) < 500 or len(body) > 5_000_000:
                        continue
                    r["image_bytes"] = body
                    ct = header.split(";")[0].replace("data:", "")
                    r["content_type"] = ct or "image/jpeg"
                except Exception:
                    pass
                continue
            if not thumb_url.startswith("http"):
                continue
            try:
                resp = await page.context.request.get(thumb_url, timeout=8000)
                if resp.ok:
                    body = await resp.body()
                    if len(body) < 1000 or len(body) > 5_000_000:
                        continue
                    r["image_bytes"] = body
                    ct = resp.headers.get("content-type", "image/jpeg")
                    r["content_type"] = ct.split(";")[0].strip()
            except Exception:
                continue

        # Resolve Google redirect URLs to their final destinations.
        resolved_urls = await resolve_urls([r.get("url", "") for r in results])
        for r, final_url in zip(results, resolved_urls):
            r["url"] = final_url

        # Build mixed content: text + inline images
        content: list = [f"Google News Results for: {query}\n"]
        for i, r in enumerate(results[:num_results], 1):
            desc = f"{i}. {r['title']}"
            desc += f"\n   URL: {r['url']}"
            source_info = []
            if r.get("source"):
                source_info.append(r["source"])
            if r.get("time"):
                source_info.append(r["time"])
            if source_info:
                desc += f"\n   Source: {' - '.join(source_info)}"
            if r.get("snippet"):
                desc += f"\n   {r['snippet']}"
            content.append(desc)

            if r.get("image_bytes"):
                try:
                    ct = r.get("content_type", "image/jpeg")
                    fmt_map = {
                        "image/jpeg": "jpeg", "image/png": "png",
                        "image/gif": "gif", "image/webp": "webp",
                    }
                    fmt = fmt_map.get(ct, "jpeg")
                    content.append(Image(data=r["image_bytes"], format=fmt))
                except Exception:
                    pass

        return content


@mcp.tool()
async def google_news(query: str, num_results: int = 5) -> list:
    """Search Google News for recent headlines, articles, and article images.

    Sample prompts that trigger this tool:
        - "What are the latest AI news?"
        - "Get me today's top headlines"
        - "Any recent news about the stock market?"
        - "What happened in the US election?"
        - "Latest news about climate change"

    Args:
        query: The news search query string.
        num_results: Number of results to return (default 5, max 10).
    """
    num_results = max(1, min(num_results, 10))
    return await do_google_news(query, num_results)


# ---------------------------------------------------------------------------
# google_scholar
# ---------------------------------------------------------------------------

async def do_google_scholar(query: str, num_results: int = 5) -> str:
    """Launch headless Chromium, search Google Scholar, and scrape results."""
    encoded_query = quote_plus(query)
    url = f"https://scholar.google.com/scholar?q={encoded_query}&hl=en&num={num_results + 5}"

    async with browse_google(url, screenshot_label="scholar") as page:
        if page is None:
            return f"Google Scholar blocked by bot detection for: {query}\nTry again later."

        await page.wait_for_selector("#gs_res_ccl", timeout=15000)

        results = await page.evaluate(
            """
            (numResults) => {
                const results = [];
                const entries = document.querySelectorAll('.gs_r.gs_or.gs_scl, .gs_ri');
                for (const el of entries) {
                    if (results.length >= numResults) break;

                    const titleEl = el.querySelector('.gs_rt a, .gs_rt');
                    const linkEl = el.querySelector('.gs_rt a');
                    const authorsEl = el.querySelector('.gs_a');
                    const snippetEl = el.querySelector('.gs_rs');
                    const citedEl = el.querySelector('.gs_fl a');

                    let citedBy = '';
                    const flLinks = el.querySelectorAll('.gs_fl a');
                    for (const fl of flLinks) {
                        if (fl.textContent.includes('Cited by')) {
                            citedBy = fl.textContent.trim();
                            break;
                        }
                    }

                    if (titleEl) {
                        results.push({
                            title: titleEl.innerText.trim(),
                            url: linkEl ? linkEl.href : '',
                            authors: authorsEl ? authorsEl.innerText.trim() : '',
                            snippet: snippetEl ? snippetEl.innerText.trim() : '',
                            cited_by: citedBy
                        });
                    }
                }
                return results;
            }
            """,
            num_results,
        )

        if not results:
            return f"No scholar results found for: {query}"

        # Resolve Google redirect URLs to their final destinations.
        resolved_urls = await resolve_urls([r.get("url", "") for r in results])
        for r, final_url in zip(results, resolved_urls):
            r["url"] = final_url

        lines = [f"Google Scholar Results for: {query}\n"]
        for i, r in enumerate(results[:num_results], 1):
            lines.append(f"{i}. {r['title']}")
            if r.get("url"):
                lines.append(f"   URL: {r['url']}")
            if r.get("authors"):
                lines.append(f"   Authors: {r['authors']}")
            if r.get("cited_by"):
                lines.append(f"   {r['cited_by']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
            lines.append("")

        return "\n".join(lines)


@mcp.tool()
async def google_scholar(query: str, num_results: int = 5) -> str:
    """Search Google Scholar for academic papers, citations, and research.

    Sample prompts that trigger this tool:
        - "Find me papers on transformer attention mechanisms"
        - "Look up academic research about quantum computing"
        - "Search for citations on CRISPR gene editing"
        - "Find recent studies about large language models"
        - "What does the research say about intermittent fasting?"

    Args:
        query: The academic search query string.
        num_results: Number of results to return (default 5, max 10).
    """
    num_results = max(1, min(num_results, 10))
    return await do_google_scholar(query, num_results)


# ---------------------------------------------------------------------------
# google_images
# ---------------------------------------------------------------------------

@mcp.tool()
async def google_images(query: str, num_results: int = 5) -> list:
    """Search Google Images and return images inline in chat.

    Returns image thumbnails directly in the conversation so you can see them.
    Also provides source URLs for each image.

    Sample prompts that trigger this tool:
        - "Show me images of the Northern Lights"
        - "Find pictures of modern kitchen designs"
        - "Search for diagrams of neural network architecture"
        - "Show me what a DGX Spark looks like"

    Args:
        query: The image search query string.
        num_results: Number of image results to return (default 5, max 10).
    """
    import base64 as b64mod

    num_results = max(1, min(num_results, 10))
    encoded_query = quote_plus(query)
    url = f"https://www.google.com/search?q={encoded_query}&hl=en&tbm=isch"

    def _fallback() -> list:
        ddg_imgs = fallback_duckduckgo_images(query, num_results)
        if ddg_imgs:
            content = [f"Google Images blocked by bot detection. Showing fallback results (DuckDuckGo Images):\n"]
            content.append(f"Image Results for: {query}\n")
            for i, r in enumerate(ddg_imgs[:num_results], 1):
                desc = f"{i}. {r.get('title', 'Image')}"
                if r.get("url"):
                    desc += f"\n   URL: {r['url']}"
                content.append(desc)
            return content
        return [f"Google Images blocked by bot detection for: {query}\nTry again later or use a different query."]

    async with browse_google(
        url, fallback_fn=_fallback, screenshot_label="images", vertical="isch"
    ) as page:
        if page is None:
            return _fallback()

        context = page.context
        await page.wait_for_timeout(2000)

        results = await page.evaluate(
            """
            (numResults) => {
                    const results = [];

                    // Primary: modern Google Images result links
                    const imgLinks = document.querySelectorAll(
                        'div[data-id] a[href^="/imgres"], a[jsname], ' +
                        'div[data-ri] a, div.isv-r a, a[jsaction*="click"]'
                    );
                    for (const a of imgLinks) {
                        if (results.length >= numResults) break;

                        const img = a.querySelector('img[src^="http"], img[data-src^="http"]');
                        if (!img) continue;

                        const thumbnail = img.src || img.dataset.src || '';
                        if (!thumbnail || thumbnail.startsWith('data:')) continue;

                        let fullUrl = '';
                        try {
                            const href = a.href || '';
                            const params = new URLSearchParams(href.split('?')[1] || '');
                            fullUrl = params.get('imgurl') || '';
                        } catch(e) {}

                        results.push({
                            title: img.alt || '',
                            thumbnail: thumbnail,
                            url: fullUrl || thumbnail,
                        });
                    }

                    // Fallback: any visible image in the search results area
                    if (results.length === 0) {
                        const allImgs = document.querySelectorAll(
                            '#search img[src^="http"], #islrg img[src^="http"], ' +
                            '#search img[data-src^="http"], #islrg img[data-src^="http"], ' +
                            'div[data-ri] img[src^="http"]'
                        );
                        for (const img of allImgs) {
                            if (results.length >= numResults) break;
                            if (img.width < 50 || img.height < 50) continue;
                            results.push({
                                title: img.alt || '',
                                thumbnail: img.src || img.dataset.src || '',
                                url: img.src || img.dataset.src || '',
                            });
                        }
                    }

                    // Last resort: raw page text
                    if (results.length === 0) {
                        const searchArea = document.querySelector('#search, #islrg, [role="main"]');
                        if (searchArea) {
                            const text = searchArea.innerText.substring(0, 3000);
                            results.push({ title: 'Raw page content', url: '', thumbnail: '', raw_text: true, snippet: text });
                        }
                    }

                    return results;
                }
                """,
                num_results,
            )

        if not results:
            return [f"No image results found for: {query}"]

        # Check if we got raw text fallback
        if results[0].get("raw_text"):
            return [f"Google Image Results for: {query}\n\n(Could not extract structured results. Raw page content:)\n{results[0]['snippet']}"]

        # Download full-size images for inline display (fall back to thumbnail)
        for r in results[:num_results]:
            full_url = r.get("url", "")
            thumb_url = r.get("thumbnail", "")
            for img_url in [full_url, thumb_url]:
                if not img_url or not img_url.startswith("http"):
                    continue
                try:
                    resp = await context.request.get(img_url, timeout=8000)
                    if resp.ok:
                        body = await resp.body()
                        # Skip if too small (likely broken) or too large (>5MB)
                        if len(body) < 1000 or len(body) > 5_000_000:
                            continue
                        r["image_bytes"] = body
                        ct = resp.headers.get("content-type", "image/jpeg")
                        r["content_type"] = ct.split(";")[0].strip()
                        break
                except Exception:
                    continue

        # Build mixed content: text descriptions + inline images
        content = [f"Google Image Results for: {query}\n"]

        for i, r in enumerate(results[:num_results], 1):
            desc = f"{i}. {r.get('title', 'Untitled')}"
            if r.get("url"):
                desc += f"\n   Source: {r['url']}"
            content.append(desc)

            if r.get("image_bytes"):
                try:
                    ct = r.get("content_type", "image/jpeg")
                    fmt_map = {
                        "image/jpeg": "jpeg", "image/png": "png",
                        "image/gif": "gif", "image/webp": "webp",
                    }
                    fmt = fmt_map.get(ct, "jpeg")
                    content.append(Image(data=r["image_bytes"], format=fmt))
                except Exception:
                    pass

        return content


# ---------------------------------------------------------------------------
# google_trends
# ---------------------------------------------------------------------------

async def do_google_trends(query: str) -> str:
    """Launch headless Chromium, check Google Trends, and scrape interest data."""
    encoded_query = quote_plus(query)
    url = f"https://trends.google.com/trends/explore?q={encoded_query}&hl=en"

    async with async_playwright() as pw:
        context = await launch_browser(pw)
        await load_cookies(context)
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Detect and handle CAPTCHA/rate-limit blocks
            if await is_blocked(page):
                solved = await try_solve_captcha(page)
                if not solved:
                    # Warm-up retry: open Google home first, then re-run query
                    try:
                        await page.goto(
                            "https://www.google.com/ncr",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        await dismiss_consent(page)
                        await human_delay(page)
                        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        pass

                    if await is_blocked(page):
                        await save_cookies(context)
                        return (
                            f"Google Trends for: {query}\n\n"
                            f"Google Trends is currently rate-limiting this request (HTTP 429).\n"
                            f"Try again in a few minutes, or visit the page directly:\n"
                            f"https://trends.google.com/trends/explore?q={encoded_query}"
                        )

            # Trends takes longer to load its widgets
            await page.wait_for_timeout(5000)

            data = await page.evaluate(
                """
                () => {
                    const data = { interest: [], related_topics: [], related_queries: [] };

                    // Interest over time - try to get the widget content
                    const timeWidget = document.querySelector('fe-line-chart-directive, .fe-line-chart');
                    if (timeWidget) {
                        data.interest_note = 'Interest over time data available (see Google Trends for chart)';
                    }

                    // Related topics
                    const topicWidgets = document.querySelectorAll('fe-related-queries .comparison-item, .fe-atoms-generic-list .item');
                    for (const el of topicWidgets) {
                        const label = el.querySelector('.label-text, .item-text, a');
                        const value = el.querySelector('.progress-bar-wrapper, .bar');
                        if (label) {
                            data.related_topics.push({
                                topic: label.innerText.trim(),
                                value: value ? value.getAttribute('aria-label') || value.innerText.trim() : ''
                            });
                        }
                    }

                    // Related queries - look for the queries widget
                    const queryCards = document.querySelectorAll('.fe-related-queries-wrapper .comparison-item, [class*="related"] .item');
                    for (const el of queryCards) {
                        const label = el.querySelector('.label-text, .item-text, a');
                        const value = el.querySelector('.progress-bar-wrapper, .bar');
                        if (label) {
                            data.related_queries.push({
                                query: label.innerText.trim(),
                                value: value ? value.getAttribute('aria-label') || value.innerText.trim() : ''
                            });
                        }
                    }

                    // Fallback: get all visible text from the trends page
                    const mainContent = document.querySelector('.trends-wrapper, main, [role="main"]');
                    if (mainContent) {
                        data.page_text = mainContent.innerText.substring(0, 3000);
                    }

                    return data;
                }
                """
            )

            lines = [f"Google Trends for: {query}\n"]

            if data.get("interest_note"):
                lines.append(f"Note: {data['interest_note']}\n")

            if data.get("related_topics"):
                lines.append("Related Topics:")
                for t in data["related_topics"][:10]:
                    val = f" ({t['value']})" if t.get("value") else ""
                    lines.append(f"  - {t['topic']}{val}")
                lines.append("")

            if data.get("related_queries"):
                lines.append("Related Queries:")
                for q in data["related_queries"][:10]:
                    val = f" ({q['value']})" if q.get("value") else ""
                    lines.append(f"  - {q['query']}{val}")
                lines.append("")

            # If structured data extraction didn't work well, fall back to page text
            if not data.get("related_topics") and not data.get("related_queries"):
                page_text = data.get("page_text", "")
                if page_text:
                    # Clean up the text
                    page_text = re.sub(r'\n{3,}', '\n\n', page_text).strip()
                    lines.append(page_text)
                else:
                    lines.append("Could not extract structured trends data.")
                    lines.append(f"Visit: https://trends.google.com/trends/explore?q={encoded_query}")

            return "\n".join(lines)

        except Exception as e:
            return format_error("Google Trends lookup", e)

        finally:
            await save_cookies(context)
            await context.close()


@mcp.tool()
async def google_trends(query: str) -> str:
    """Check Google Trends for a topic to see interest over time, related topics, and related queries.

    Sample prompts that trigger this tool:
        - "What's trending in tech right now?"
        - "Is Python more popular than JavaScript?"
        - "Check the trend for electric vehicles"
        - "What are people searching for about AI?"

    Args:
        query: The topic or search term to check trends for.
    """
    return await do_google_trends(query)
