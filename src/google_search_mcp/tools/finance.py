"""Google finance/weather/shopping/books tools.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
import re
from urllib.parse import quote_plus

from mcp.server.fastmcp import Context, Image
from playwright.async_api import async_playwright

from ..core.cache import cached
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
)


# google_finance
# ---------------------------------------------------------------------------

def _clean_display_price(value: str) -> str:
    """Reject chart-axis text accidentally matched as a display price."""
    value = value.strip()
    if not re.fullmatch(r"(?:[$€£]\s*)?\d[\d,]*(?:\.\d+)?(?:\s+[A-Z]{3})?", value):
        return ""
    return value


def _extract_contextual_price(query: str, page_text: str) -> str:
    """Find a price immediately following the requested ticker/company name."""
    anchor = query.split(":", 1)[0].strip().casefold()
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]

    for index, line in enumerate(lines):
        normalized = line.casefold()
        if normalized.split(":", 1)[0] != anchor and not normalized.startswith(f"{anchor} "):
            continue
        for candidate in lines[index + 1 : index + 6]:
            price = _clean_display_price(candidate)
            if price:
                return price
    return ""


def _has_financial_data(data) -> bool:
    return any(data.get(key) for key in ("price", "display_price", "panel_text"))


@cached("google_finance")
async def do_google_finance(query: str) -> str:
    """Search Google Finance for stock/market data."""
    encoded_query = quote_plus(query)
    search_url = f"https://www.google.com/search?q={encoded_query}+stock+price&hl=en"

    async with browse_google(search_url, screenshot_label="finance") as page:
        if page is None:
            return f"Google Finance blocked by bot detection for: {query}\nTry again later."

        # The knowledge panel is injected after the SERP skeleton. A fixed 2 s
        # wait often read an empty panel, which silently pushed the call into the
        # /finance/quote fallback and discarded the name, change and stat rows.
        from contextlib import suppress

        with suppress(Exception):
            await page.wait_for_selector(
                '[data-attrid="title"], [data-last-price]', timeout=12000
            )
        await page.wait_for_timeout(1500)

        data = await page.evaluate(
            """
            () => {
                const data = {};
                const priceEl = document.querySelector('[data-last-price]');
                data.price = priceEl ? priceEl.getAttribute('data-last-price') : '';

                const nameEl = document.querySelector(
                    '.oPhL2e .PZPZlf, [data-attrid*="title"], .zzDege'
                );
                data.name = nameEl ? nameEl.innerText.trim() : '';

                const changeEl = document.querySelector(
                    '[data-attrid*="change"], .JwB6zf, .rPF6Lc'
                );
                data.change = changeEl ? changeEl.innerText.trim() : '';

                // Currency and exchange
                const currencyEl = document.querySelector('[data-currency-code]');
                data.currency = currencyEl ? currencyEl.getAttribute('data-currency-code') : 'USD';

                const exchangeEl = document.querySelector('[data-exchange]');
                data.exchange = exchangeEl ? exchangeEl.getAttribute('data-exchange') : '';

                // Key stats
                const stats = {};
                const statRows = document.querySelectorAll('.gyFHrc .P6K39c, .eYanAe .P6K39c, table.slpEwd tr');
                for (const row of statRows) {
                    const label = row.querySelector('.mfs7Fc, td:first-child');
                    const value = row.querySelector('.QXDnM, td:last-child');
                    if (label && value) {
                        const k = label.innerText.trim().split('\\n')[0];
                        const v = value.innerText.trim().split('\\n')[0];
                        if (k && v) stats[k] = v;
                    }
                }
                data.stats = stats;

                // ── Knowledge panel anchored on data-attrid ──
                // The current SERP panel no longer carries [data-last-price];
                // name/subtitle/price/change and the stat rows all hang off
                // data-attrid attributes, so anchor on those instead of guessing
                // at rotating class names.
                const attrid = (name) => {
                    const el = document.querySelector('[data-attrid="' + name + '"]');
                    return el ? el.innerText.trim() : '';
                };
                if (!data.name) data.name = attrid('title');
                if (!data.exchange) data.exchange = attrid('subtitle');

                const STAT_KEYS = new Set([
                    'Open', 'High', 'Low', 'Mkt cap', 'P/E ratio',
                    '52-wk high', '52-wk low', 'Dividend', 'Qtrly Div Amt',
                    'Avg vol', 'Volume', 'Beta', 'EPS', 'Shares outstanding',
                ]);
                data.stats = data.stats || {};
                for (const el of document.querySelectorAll('[data-attrid]')) {
                    const key = el.getAttribute('data-attrid');
                    if (!STAT_KEYS.has(key)) continue;
                    const value = (el.innerText || '').trim().split('\\n')[0];
                    if (value && !data.stats[key]) data.stats[key] = value;
                }

                const chart = document.querySelector('[data-attrid="Chart"]');
                if (chart) {
                    const chartText = chart.innerText || '';
                    if (!data.price) {
                        // e.g. "554,69 USD" (locale decimal separator varies).
                        const pm = chartText.match(/([\\d][\\d.,]*)\\s*([A-Z]{3})\\b/);
                        if (pm) {
                            data.price = pm[1];
                            data.currency = pm[2];
                        }
                    }
                    if (!data.change_abs || !data.change_pct) {
                        const cm = chartText.match(
                            /([+\\-\\u2212]\\s?[\\d][\\d.,]*)\\s*\\(?([+\\-\\u2212]?[\\d][\\d.,]*\\s?%)?\\)?/
                        );
                        if (cm) {
                            data.change_abs = cm[1];
                            data.change_pct = cm[2] || '';
                        }
                    }
                }

                // Get the knowledge panel text as fallback
                const panel = document.querySelector('.kp-wholepage, .knowledge-panel, .f5cPye');
                data.panel_text = panel ? panel.innerText.substring(0, 1500) : '';

                return data;
            }
            """
        )

        if not data.get("price") or not data.get("name"):
            # Fallback: try direct finance URL. Keep whatever the panel already
            # produced (name, change, stats) and only fill the gaps -- replacing
            # `data` outright is what lost the company name.
            panel = dict(data)
            direct_url = f"https://www.google.com/finance/quote/{encoded_query}"
            await page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            data = await page.evaluate(
                """
                () => {
                    const data = {};

                    const dataEl = document.querySelector('[data-last-price]');
                    if (dataEl) {
                        data.price = dataEl.getAttribute('data-last-price');
                    }

                    const currencyEl = document.querySelector('[data-currency-code]');
                    data.currency = currencyEl ? currencyEl.getAttribute('data-currency-code') : 'USD';

                    const exchangeEl = document.querySelector('[data-exchange]');
                    data.exchange = exchangeEl ? exchangeEl.getAttribute('data-exchange') : '';

                    const displayEl = document.querySelector('.fxKbKc, .kf1m0');
                    data.display_price = displayEl ? displayEl.innerText.trim() : '';

                    const nameEl = document.querySelector('.zzDege');
                    data.name = nameEl ? nameEl.innerText.trim() : '';

                    const rPF6Lc = document.querySelector('.rPF6Lc');
                    if (rPF6Lc) {
                        const text = rPF6Lc.innerText.trim();
                        const lines = text.split('\\n');
                        if (lines.length >= 2) {
                            data.change_pct = lines[1] ? lines[1].trim() : '';
                            data.change_abs = lines[2] ? lines[2].trim() : '';
                        }
                    }

                    const stats = {};
                    const statRows = document.querySelectorAll('.gyFHrc .P6K39c, .eYanAe .P6K39c, table.slpEwd tr');
                    for (const row of statRows) {
                        const label = row.querySelector('.mfs7Fc, td:first-child');
                        const value = row.querySelector('.QXDnM, td:last-child');
                        if (label && value) {
                            const k = label.innerText.trim().split('\\n')[0];
                            const v = value.innerText.trim().split('\\n')[0];
                            if (k && v) stats[k] = v;
                        }
                    }
                    data.stats = stats;

                    const aboutEl = document.querySelector('.bLLb2d, .Yfwt5');
                    data.about = aboutEl ? aboutEl.innerText.trim().substring(0, 500) : '';
                    data.page_text = document.body ? document.body.innerText : '';

                    return data;
                }
                """
            )
            data["display_price"] = _clean_display_price(
                data.get("display_price", "")
            )
            if not data["display_price"]:
                data["display_price"] = _extract_contextual_price(
                    query, data.get("page_text", "")
                )

            # Restore the knowledge-panel fields the quote page does not carry.
            for key, value in panel.items():
                if value and not data.get(key):
                    data[key] = value
            if panel.get("stats"):
                merged_stats = dict(panel["stats"])
                merged_stats.update(data.get("stats") or {})
                data["stats"] = merged_stats

        lines = [f"Google Finance: {query}\n"]

        if data.get("name"):
            lines.append(f"Company: {data['name']}")
        if data.get("price"):
            currency = data.get("currency", "USD")
            lines.append(f"Price: {data['price']} {currency}")
        elif data.get("display_price"):
            lines.append(f"Price: {data['display_price']}")
        if data.get("exchange"):
            lines.append(f"Exchange: {data['exchange']}")
        if data.get("change_pct") or data.get("change_abs"):
            change_parts = []
            if data.get("change_abs"):
                change_parts.append(data["change_abs"])
            if data.get("change_pct"):
                change_parts.append(f"({data['change_pct']})")
            lines.append(f"Change: {' '.join(change_parts)}")
        if data.get("stats"):
            lines.append("\nKey Stats:")
            for k, v in data["stats"].items():
                lines.append(f"  {k}: {v}")

        if data.get("about"):
            lines.append(f"\nAbout: {data['about']}")

        if data.get("panel_text") and not data.get("price"):
            lines.append(f"\n{data['panel_text']}")

        if not _has_financial_data(data):
            lines.append("Could not find financial data. Try a stock ticker like 'AAPL:NASDAQ' or 'TSLA:NASDAQ'.")

        return "\n".join(lines)


@mcp.tool()
async def google_finance(query: str) -> str:
    """Look up stock prices, market data, and company information on Google Finance.

    Sample prompts that trigger this tool:
        - "What's Apple's stock price?"
        - "How is Tesla stock doing?"
        - "Look up NVIDIA market cap"
        - "Get me the stock price for Microsoft"
        - "How is the S&P 500 doing today?"

    Args:
        query: Stock ticker with exchange (e.g. "AAPL:NASDAQ", "TSLA:NASDAQ", "MSFT:NASDAQ", ".INX:INDEXSP") or company name.
    """
    return await do_google_finance(query)


# ---------------------------------------------------------------------------
# google_weather
# ---------------------------------------------------------------------------

@cached("google_weather")
async def do_google_weather(location: str) -> str:
    """Get weather data from Google's weather card."""
    encoded_location = quote_plus(f"weather {location}")
    url = f"https://www.google.com/search?q={encoded_location}&hl=en"

    async with browse_google(url, screenshot_label="weather") as page:
        if page is None:
            return f"Weather lookup blocked by bot detection for: {location}\nTry again later."

        await page.wait_for_timeout(2000)

        data = await page.evaluate(
            """
            () => {
                const data = {};

                // Primary: Google's weather widget IDs
                const locEl = document.querySelector('#wob_loc');
                data.location = locEl ? locEl.innerText.trim() : '';

                const tempEl = document.querySelector('#wob_tm');
                data.temp_c = tempEl ? tempEl.innerText.trim() : '';

                const tempFEl = document.querySelector('#wob_ttm');
                data.temp_f = tempFEl ? tempFEl.innerText.trim() : '';

                const condEl = document.querySelector('#wob_dc');
                data.condition = condEl ? condEl.innerText.trim() : '';

                const precipEl = document.querySelector('#wob_pp');
                data.precipitation = precipEl ? precipEl.innerText.trim() : '';

                const humidEl = document.querySelector('#wob_hm');
                data.humidity = humidEl ? humidEl.innerText.trim() : '';

                const windEl = document.querySelector('#wob_ws');
                data.wind = windEl ? windEl.innerText.trim() : '';

                const timeEl = document.querySelector('#wob_dts');
                data.time = timeEl ? timeEl.innerText.trim() : '';

                // Forecast days
                data.forecast = [];
                const forecastDays = document.querySelectorAll('.wob_df');
                for (const day of forecastDays) {
                    const dayName = day.querySelector('.Z1VzSb, .QrNVmd');
                    const temps = day.querySelectorAll('.wob_t span:first-child');
                    let high = '', low = '';
                    if (temps.length >= 2) {
                        high = temps[0].innerText.trim();
                        low = temps[1].innerText.trim();
                    }
                    const iconEl = day.querySelector('img');
                    if (dayName) {
                        data.forecast.push({
                            day: dayName.innerText.trim(),
                            high: high,
                            low: low,
                            condition: iconEl ? iconEl.alt || '' : ''
                        });
                    }
                }

                // Fallback: try alternative weather widget selectors
                if (!data.temp_c && !data.location) {
                    const weatherWidget = document.querySelector(
                        'div#wob_wrap, [data-attrid*="weather"], .wob_wrap, ' +
                        '.kp-wholepage, .liYKde'
                    );
                    if (weatherWidget) {
                        data.raw_text = weatherWidget.innerText.substring(0, 2000);
                    }
                }

                // Last resort: search page text
                if (!data.temp_c && !data.location && !data.raw_text) {
                    const searchArea = document.querySelector('#search, #rso, [role="main"]');
                    if (searchArea) {
                        const text = searchArea.innerText.substring(0, 2000);
                        if (text.toLowerCase().includes('°') || text.toLowerCase().includes('weather')) {
                            data.raw_text = text;
                        }
                    }
                }

                return data;
            }
            """
        )

        if data.get("raw_text") and not data.get("temp_c"):
            raw = re.sub(r'\n{3,}', '\n\n', data["raw_text"]).strip()
            return f"Weather for: {location}\n\n(Could not extract structured weather data. Raw page content:)\n{raw}"

        if not data.get("temp_c") and not data.get("location"):
            return f"Could not find weather data for: {location}"

        # Use the provided location name if Google's #wob_loc is generic
        display_location = data.get("location", location)
        if not display_location or display_location.lower() in ("weather", ""):
            display_location = location

        lines = [f"Weather for: {display_location}\n"]

        if data.get("time"):
            lines.append(f"As of: {data['time']}")

        if data.get("temp_c"):
            temp_str = f"Temperature: {data['temp_c']}°C"
            if data.get("temp_f"):
                temp_str += f" ({data['temp_f']}°F)"
            lines.append(temp_str)

        if data.get("condition"):
            lines.append(f"Condition: {data['condition']}")
        if data.get("precipitation"):
            lines.append(f"Precipitation: {data['precipitation']}")
        if data.get("humidity"):
            lines.append(f"Humidity: {data['humidity']}")
        if data.get("wind"):
            lines.append(f"Wind: {data['wind']}")

        if data.get("forecast"):
            lines.append("\nForecast:")
            for f in data["forecast"][:7]:
                day_str = f"  {f['day']}"
                if f.get("high") and f.get("low"):
                    day_str += f": {f['high']}° / {f['low']}°"
                if f.get("condition"):
                    day_str += f" - {f['condition']}"
                lines.append(day_str)

        return "\n".join(lines)


@mcp.tool()
async def google_weather(location: str) -> str:
    """Get current weather conditions and forecast for any location.

    Sample prompts that trigger this tool:
        - "What's the weather in Dubai?"
        - "Is it going to rain in London today?"
        - "What's the temperature in New York?"
        - "Weather forecast for Tokyo this week"
        - "How hot is it in Dubai right now?"

    Args:
        location: The city or location to get weather for (e.g. "Dubai", "New York", "London, UK", "Tokyo").
    """
    return await do_google_weather(location)


# ---------------------------------------------------------------------------
# google_shopping
# ---------------------------------------------------------------------------

@cached("google_shopping")
async def do_google_shopping(query: str, num_results: int = 5) -> list:
    """Search Google Shopping for products and prices."""
    encoded_query = quote_plus(query)
    url = f"https://www.google.com/search?q={encoded_query}&hl=en&tbm=shop&num={num_results + 5}"

    async with browse_google(url, screenshot_label="shopping") as page:
        if page is None:
            return [f"Google Shopping blocked by bot detection for: {query}\nTry again later."]

        await page.wait_for_timeout(2000)

        results = await page.evaluate(
            r"""
            (numResults) => {
                const results = [];
                const seen = new Set();
                const PRICE_RE = /[€$£¥₹]\s?\d[\d.,]*/;
                const RATING_RE = /^([\d.,]+)\s*\(\s*([\d.,]+[KM]?)\s*\)$/;
                const NOISE = new Set([
                    'sale', 'sponsored', 'ad', 'about this result',
                    'report a violation', 'in stores nearby', 'in store',
                    '& more prices', '& more', '30-day returns',
                    'free delivery', 'special offer', 'price drop', 'deal',
                ]);

                const abs = (href) => {
                    if (!href) return '';
                    try { return new URL(href, location.href).href; } catch (e) { return href; }
                };

                // Product boxes.
                //
                // The shopping vertical lists one product per box whose text runs
                //   [SALE] / <title lines> / <price> / [list price] / <merchant> / <rating>
                // The price span (.lmQWe and friends) is the stable anchor, but its
                // class names churn, so boxes are collected from the known container
                // class *and* by walking up from anything that looks like a price.
                // Every class the old extractor used (.sh-dgr__content, [data-docid],
                // .KZmu8e, …) is gone from the current page; it matched nothing and
                // the caller dumped raw page text.
                const boxes = new Set();
                for (const el of document.querySelectorAll('.UC8ZCe')) boxes.add(el);
                for (const el of document.querySelectorAll('span, div')) {
                    const t = (el.innerText || '').trim();
                    if (!PRICE_RE.test(t) || t.length > 12) continue;
                    let n = el;
                    for (let i = 0; i < 8 && n; i++) {
                        n = n.parentElement;
                        if (!n) break;
                        const len = (n.innerText || '').length;
                        if (len > 60 && len < 420) { boxes.add(n); break; }
                    }
                }

                for (const box of boxes) {
                    if (results.length >= numResults) break;

                    const lines = (box.innerText || '')
                        .split('\n')
                        .map((s) => s.trim())
                        .filter(Boolean);
                    if (!lines.length) continue;

                    // A product needs a title *before* its price.
                    const priceIdx = lines.findIndex(
                        (l) => PRICE_RE.test(l) && l.length <= 16
                    );
                    if (priceIdx < 1) continue;

                    const titleParts = [];
                    for (let i = 0; i < priceIdx; i++) {
                        const low = lines[i].toLowerCase();
                        if (NOISE.has(low) || PRICE_RE.test(lines[i])) continue;
                        titleParts.push(lines[i]);
                    }
                    const title = titleParts.join(' ').trim();
                    if (title.length < 3) continue;

                    const price = (lines[priceIdx].match(PRICE_RE) || [''])[0].trim();
                    if (!price) continue;

                    let store = '';
                    let oldPrice = '';
                    let rating = '';
                    for (let i = priceIdx + 1; i < lines.length; i++) {
                        const line = lines[i];
                        const low = line.toLowerCase();
                        const rm = line.match(RATING_RE);
                        if (rm && !rating) { rating = rm[1] + ' (' + rm[2] + ')'; continue; }
                        if (PRICE_RE.test(line) && line.length <= 16) {
                            if (!oldPrice && line !== price) oldPrice = line;
                            continue;
                        }
                        if (!store && !NOISE.has(low) && line.length <= 80) store = line;
                    }

                    const key = (title + '|' + price).toLowerCase();
                    if (seen.has(key)) continue;
                    seen.add(key);

                    // Prefer an outbound merchant link; the box's own anchor is
                    // usually a Google comparison page.
                    let linkUrl = '';
                    for (const a of box.querySelectorAll('a[href]')) {
                        const h = abs(a.getAttribute('href'));
                        let host = '';
                        try { host = new URL(h).hostname; } catch (e) { host = ''; }
                        if (/^https?:\/\//.test(h) && host && !/google\./.test(host)) {
                            linkUrl = h;
                            break;
                        }
                    }
                    if (!linkUrl) {
                        const own = box.querySelector('a[href]');
                        if (own) linkUrl = abs(own.getAttribute('href'));
                    }

                    let thumbnail = '';
                    const img = box.querySelector('img');
                    if (img) thumbnail = img.src || img.dataset?.src || '';

                    results.push({
                        title, price, old_price: oldPrice, store, rating,
                        url: linkUrl, thumbnail,
                    });
                }

                if (results.length === 0) {
                    const area = document.querySelector('#rso, #search, [role="main"]');
                    if (area) {
                        results.push({
                            title: '__raw__',
                            raw_text: area.innerText.substring(0, 3000),
                            price: '', store: '', rating: '', url: '',
                        });
                    }
                }

                return results;
            }
            """,
            num_results,
        )

        if not results:
            return f"No shopping results found for: {query}"

        # Handle raw text fallback
        if len(results) == 1 and results[0].get("title") == "__raw__":
            raw = results[0].get("raw_text", "")
            raw = re.sub(r'\n{3,}', '\n\n', raw).strip()
            # Say so explicitly. An unlabelled dump reads like real output and
            # hid the fact that every product selector had stopped matching.
            return [
                f"Google Shopping Results for: {query}\n\n"
                f"(Could not extract structured results. Raw page content:)\n{raw}"
            ]

        # Download product thumbnail images via page context
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

        # Build mixed content: text + inline images
        content: list = [f"Google Shopping Results for: {query}\n"]
        for i, r in enumerate(results[:num_results], 1):
            desc = f"{i}. {r['title']}"
            if r.get("price"):
                desc += f"\n   Price: {r['price']}"
            if r.get("store"):
                desc += f"\n   Store: {r['store']}"
            if r.get("rating"):
                desc += f"\n   Rating: {r['rating']}"
            if r.get("url"):
                desc += f"\n   URL: {r['url']}"
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
async def google_shopping(query: str, num_results: int = 5) -> list:
    """Search Google Shopping for products with prices, stores, ratings, and product images.

    Sample prompts that trigger this tool:
        - "Find the cheapest MacBook Air"
        - "Compare prices for Sony WH-1000XM5 headphones"
        - "How much does a Nintendo Switch cost?"
        - "Search for running shoes under $100"
        - "Find deals on mechanical keyboards"

    Args:
        query: The product search query string.
        num_results: Number of results to return (default 5, max 10).
    """
    num_results = max(1, min(num_results, 10))
    return await do_google_shopping(query, num_results)


# ---------------------------------------------------------------------------
# google_books
# ---------------------------------------------------------------------------

@cached("google_books")
async def do_google_books(query: str, num_results: int = 5) -> str:
    """Search Google Books for books and publications."""
    encoded_query = quote_plus(query)
    url = f"https://www.google.com/search?q={encoded_query}&hl=en&tbm=bks&num={num_results + 5}"

    async with browse_google(url, screenshot_label="books") as page:
        if page is None:
            return f"Google Books blocked by bot detection for: {query}\nTry again later."

        await page.wait_for_timeout(2000)

        results = await page.evaluate(
            r"""
            (numResults) => {
                const results = [];
                const seen = new Set();

                // Anchor on book result links rather than on every <h3>.
                //
                // On the books vertical each result is
                //   <a href="…/books?id=XXX"><h3>Title</h3><cite>books.google.com</cite></a>
                //   <div><a href="…&q=inauthor:%22Author%22"><span>Author</span></a> · 2019</div>
                //   <div><span><span>Snippet…</span></span></div>
                // A single book is often linked several times (cover, title,
                // "Preview"), so results must be de-duplicated by book id.
                const anchors = document.querySelectorAll(
                    'a[href*="books.google.com/books"], a[href*="/books?id="]'
                );

                const bookRow = (container, title, author) => {
                    if (!container) return '';
                    const drop = new Set([
                        title.toLowerCase(),
                        'preview', 'more', 'save', 'add to my library',
                        'books.google.com', 'books.google.com › books',
                    ]);
                    const lines = (container.innerText || '')
                        .split('\n')
                        .map((s) => s.trim())
                        .filter(Boolean);
                    const kept = [];
                    for (const line of lines) {
                        const low = line.toLowerCase();
                        if (drop.has(low)) continue;
                        if (author && low === author.toLowerCase()) continue;
                        if (/^books\.google\.com/.test(low)) continue;
                        if (/^\d{4}$/.test(line)) continue;
                        if (kept.length && kept[kept.length - 1] === line) continue;
                        kept.push(line);
                    }
                    // The snippet is the remaining long prose line.
                    kept.sort((a, b) => b.length - a.length);
                    return (kept[0] || '').replace(/\s+/g, ' ').slice(0, 400);
                };

                for (const a of anchors) {
                    if (results.length >= numResults) break;

                    const h3 = a.querySelector('h3');
                    if (!h3) continue;
                    const title = (h3.innerText || '').trim();
                    if (title.length < 2) continue;

                    // Book id → canonical URL + de-duplication key.
                    // Parse the href defensively: `new URL(href, location.origin)`
                    // throws when the base is unusable, which silently loses the
                    // id and leaves the long tracking URL in the output.
                    let bookId = '';
                    const rawHref = a.getAttribute('href') || a.href || '';
                    try {
                        bookId = new URL(rawHref, location.href).searchParams.get('id') || '';
                    } catch (e) {
                        const idMatch = rawHref.match(/[?&]id=([A-Za-z0-9_-]{6,})/);
                        if (idMatch) bookId = idMatch[1];
                    }
                    const key = bookId || title.toLowerCase();
                    if (seen.has(key)) continue;
                    seen.add(key);

                    // Nearest ancestor that also holds the author link.
                    let container = null;
                    let node = a;
                    for (let i = 0; i < 10 && node; i++) {
                        node = node.parentElement;
                        if (!node) break;
                        if (node.querySelector('a[href*="inauthor:"]')) {
                            container = node;
                            break;
                        }
                    }
                    if (!container) container = a.closest('.MjjYud') || a.parentElement;

                    // Author + publication year.
                    let author = '';
                    let year = '';
                    const authorLink = container
                        ? container.querySelector('a[href*="inauthor:"]')
                        : null;
                    if (authorLink) {
                        author = (authorLink.innerText || '').trim();
                        const row = authorLink.closest('div');
                        const yearMatch = ((row && row.innerText) || '').match(/[·•]\s*(\d{4})/);
                        if (yearMatch) year = yearMatch[1];
                    }

                    // ISBN is rarely on the SERP; keep the best-effort scan.
                    let isbn = '';
                    const containerText = (container && container.innerText) || '';
                    const isbn13 = containerText.match(/97[89][\d-]{10,16}/);
                    if (isbn13) {
                        const cleaned = isbn13[0].replace(/-/g, '');
                        if (cleaned.length === 13) isbn = cleaned;
                    }

                    results.push({
                        title: title,
                        url: bookId ? 'https://books.google.com/books?id=' + bookId : a.href,
                        author: author,
                        year: year,
                        isbn: isbn,
                        snippet: bookRow(container, title, author),
                    });
                }

                // Fallback: raw page text
                if (results.length === 0) {
                    const searchArea = document.querySelector('#search, #rso, [role="main"]');
                    if (searchArea) {
                        const text = searchArea.innerText.substring(0, 3000);
                        results.push({
                            title: '__raw__', url: '', author: '', year: '',
                            snippet: text, isbn: '',
                        });
                    }
                }

                return results;
            }
            """,
            num_results,
        )

        if not results:
            return f"No book results found for: {query}"

        # Handle raw text fallback
        if len(results) == 1 and results[0].get("title") == "__raw__":
            raw = re.sub(r'\n{3,}', '\n\n', results[0]["snippet"]).strip()
            return f"Google Books Results for: {query}\n\n(Could not extract structured results. Raw page content:)\n{raw}"

        lines = [f"Google Books Results for: {query}\n"]
        for i, r in enumerate(results[:num_results], 1):
            lines.append(f"{i}. {r['title']}")
            byline = r.get("author", "")
            if r.get("year"):
                byline = f"{byline} · {r['year']}" if byline else r["year"]
            if byline:
                lines.append(f"   Author: {byline}")
            if r.get("isbn"):
                lines.append(f"   ISBN: {r['isbn']}")
            if r.get("url"):
                lines.append(f"   URL: {r['url']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
            lines.append("")

        return "\n".join(lines)


@mcp.tool()
async def google_books(query: str, num_results: int = 5) -> str:
    """Search Google Books for books, textbooks, and publications.

    Sample prompts that trigger this tool:
        - "Find books about machine learning"
        - "Search for books by Stephen King"
        - "What are the best books on Python programming?"
        - "Find textbooks on linear algebra"
        - "Look up books about the history of AI"

    Args:
        query: The book search query string.
        num_results: Number of results to return (default 5, max 10).
    """
    num_results = max(1, min(num_results, 10))
    return await do_google_books(query, num_results)
