"""Google finance/weather/shopping/books tools.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
from urllib.parse import quote_plus

from mcp.server.fastmcp import Context, Image
from playwright.async_api import async_playwright

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

async def do_google_finance(query: str) -> str:
    """Search Google Finance for stock/market data."""
    encoded_query = quote_plus(query)
    search_url = f"https://www.google.com/search?q={encoded_query}+stock+price&hl=en"

    async with browse_google(search_url, screenshot_label="finance") as page:
        if page is None:
            return f"Google Finance blocked by bot detection for: {query}\nTry again later."

        await page.wait_for_timeout(2000)

        data = await page.evaluate(
            """
            () => {
                const data = {};
                const priceEl = document.querySelector(
                    '[data-attrid*="Price"], .YMlKec, .kCrYT .IsqQVc, ' +
                    '[data-last-price], .fxKbKc, .kf1m0'
                );
                data.price = priceEl ? priceEl.innerText.trim() : '';

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

                // Get the knowledge panel text as fallback
                const panel = document.querySelector('.kp-wholepage, .knowledge-panel, .f5cPye');
                data.panel_text = panel ? panel.innerText.substring(0, 1500) : '';

                return data;
            }
            """
        )

        if not data.get("price") and not data.get("name"):
            # Fallback: try direct finance URL
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

                    return data;
                }
                """
            )

        lines = [f"Google Finance: {query}\n"]

        if data.get("name"):
            lines.append(f"Company: {data['name']}")
        if data.get("display_price"):
            lines.append(f"Price: {data['display_price']}")
        elif data.get("price"):
            currency = data.get("currency", "USD")
            lines.append(f"Price: {data['price']} {currency}")
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

        if not data.get("price") and not data.get("panel_text"):
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

                // Google Shopping uses various container classes
                const items = document.querySelectorAll(
                    '.sh-dgr__content, .sh-dlr__list-result, ' +
                    '.KZmu8e, .i0X6df, .xcR77, ' +
                    '[data-docid], .sh-pr__product-result, ' +
                    'div[data-docid], div.sh-pr__product-result, ' +
                    'div.i0X6df, div.xcR77'
                );

                for (const el of items) {
                    if (results.length >= numResults) break;

                    const titleEl = el.querySelector('h3, h4, .tAxDx, .Xjkr3b, .EI11Pd');
                    const priceEl = el.querySelector('.a8Pemb, .HRLxBb, .kHxwFf, .T14wmb, b');
                    const storeEl = el.querySelector('.aULzUe, .IuHnof, .E5ocAb, .dD8iuc');
                    const ratingEl = el.querySelector('.Rsc7Yb, .QIrs8, .yi40Hd');

                    const title = titleEl ? titleEl.innerText.trim() : '';
                    if (!title) continue;

                    // Extract clean product URL from Google redirect wrappers
                    let productUrl = '';
                    // 1. Check data-merchant-url attribute on links
                    const merchantLink = el.querySelector('a[data-merchant-url]');
                    if (merchantLink) {
                        productUrl = merchantLink.getAttribute('data-merchant-url');
                    }
                    if (!productUrl) {
                        // 2. Try links with url?q= redirect pattern
                        const redirectLink = el.querySelector('a[href*="/url?"]');
                        if (redirectLink) {
                            try {
                                const u = new URL(redirectLink.href);
                                productUrl = u.searchParams.get('q') || u.searchParams.get('url') || '';
                            } catch(e) {}
                        }
                    }
                    if (!productUrl) {
                        // 3. Try links with aclk (Google Ads click tracker)
                        const aclkLink = el.querySelector('a[href*="aclk?"]');
                        if (aclkLink) {
                            try {
                                const u = new URL(aclkLink.href);
                                productUrl = u.searchParams.get('adurl') || '';
                            } catch(e) {}
                        }
                    }
                    if (!productUrl) {
                        // 4. Fallback: any link with an external href
                        const allLinks = el.querySelectorAll('a[href]');
                        for (const a of allLinks) {
                            const h = a.href;
                            if (h && h.startsWith('http') &&
                                !h.includes('google.com/aclk') &&
                                !h.includes('google.com/url') &&
                                !h.includes('google.com/search') &&
                                !h.includes('google.com/shopping')) {
                                productUrl = h;
                                break;
                            }
                        }
                    }
                    if (!productUrl) {
                        // 5. Last resort: use raw href
                        const linkEl = el.querySelector('a[href]');
                        productUrl = linkEl ? linkEl.href : '';
                    }

                    // Extract product thumbnail
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
                        title: title,
                        price: priceEl ? priceEl.innerText.trim() : '',
                        store: storeEl ? storeEl.innerText.trim() : '',
                        rating: ratingEl ? ratingEl.innerText.trim() : '',
                        url: productUrl,
                        thumbnail: thumbnail,
                    });
                }

                // Fallback: parse the visible text on shopping results
                if (results.length === 0) {
                    const body = document.querySelector('#search, #rso, main');
                    if (body) {
                        const text = body.innerText;
                        const pricePattern = /(?:[$£€]|CHF|USD|EUR)\s*[\d,.]+/g;
                        const matches = [...text.matchAll(pricePattern)];
                        if (matches.length > 0) {
                            return [{
                                title: '__raw__',
                                raw_text: text.substring(0, 3000),
                                price: '', store: '', rating: '', url: ''
                            }];
                        }
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
            return [f"Google Shopping Results for: {query}\n\n{raw}"]

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

                // Find all h3 elements that are book results
                const allH3 = document.querySelectorAll('h3');
                for (const h3 of allH3) {
                    if (results.length >= numResults) break;

                    const title = h3.innerText.trim();
                    if (!title || title.length < 3) continue;
                    // Skip navigation/header h3s
                    if (title === 'Search Results' || title === 'Filters and topics') continue;

                    // Walk up to find the result container
                    let container = h3.closest('.g, .MjjYud, .byrV5b, div[data-ved]') || h3.parentElement?.parentElement?.parentElement;
                    if (!container) continue;

                    // Get the link
                    const linkEl = container.querySelector('a[href*="books.google"], a[href^="http"]');
                    const url = linkEl ? linkEl.href : '';

                    // Get snippet
                    const snippetEl = container.querySelector('.VwiC3b, .cmlJmd, [data-sncf], .f5cPye');
                    const snippet = snippetEl ? snippetEl.innerText.trim() : '';

                    // Get author - look for text between the title and snippet
                    let author = '';
                    const metaEls = container.querySelectorAll('span, cite, .Y3v8qd');
                    for (const el of metaEls) {
                        const t = el.innerText.trim();
                        if (t && t !== title && !t.includes('http') &&
                            (t.includes(',') || t.includes('·') || /\d{4}/.test(t)) &&
                            t.length < 200) {
                            author = t;
                            break;
                        }
                    }

                    // Extract ISBN from container text or URL
                    let isbn = '';
                    const containerText = container.innerText || '';
                    const containerHtml = container.innerHTML || '';
                    const searchText = containerText + ' ' + containerHtml;
                    const isbn13Match = searchText.match(/97[89][\d-]{10,16}/);
                    if (isbn13Match) {
                        isbn = isbn13Match[0].replace(/-/g, '');
                        if (isbn.length !== 13) isbn = '';
                    }
                    if (!isbn) {
                        const isbn10Match = searchText.match(/ISBN[:\s]*([\d][\d\-]{8,12}[\dXx])/i);
                        if (isbn10Match) {
                            const cleaned = isbn10Match[1].replace(/-/g, '');
                            if (cleaned.length === 10 || cleaned.length === 13) isbn = cleaned;
                        }
                    }
                    if (!isbn && url) {
                        try {
                            const u = new URL(url);
                            const vid = u.searchParams.get('vid') || '';
                            const isbnFromVid = vid.match(/ISBN[:\s]*([\d-]{10,17})/i);
                            if (isbnFromVid) isbn = isbnFromVid[1].replace(/-/g, '');
                            if (!isbn) {
                                const isbnFromUrl = url.match(/isbn[=:]([\d-]{10,17})/i);
                                if (isbnFromUrl) isbn = isbnFromUrl[1].replace(/-/g, '');
                            }
                        } catch(e) {}
                    }

                    results.push({ title, url, author, snippet, isbn });
                }

                // Fallback: raw page text
                if (results.length === 0) {
                    const searchArea = document.querySelector('#search, #rso, [role="main"]');
                    if (searchArea) {
                        const text = searchArea.innerText.substring(0, 3000);
                        results.push({ title: '__raw__', url: '', author: '', snippet: text, isbn: '' });
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
            if r.get("author"):
                lines.append(f"   Author: {r['author']}")
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
