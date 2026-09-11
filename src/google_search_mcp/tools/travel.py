"""Google translate/flights/hotels tools.

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


# google_translate
# ---------------------------------------------------------------------------


def _translate_via_google_http(text: str, sl: str, tl: str) -> str:
    """Fallback translator using Google HTTP endpoint (no browser scraping)."""
    chunks = split_translation_chunks(text)
    if not chunks:
        return ""

    out = []
    for chunk in chunks:
        try:
            endpoint = (
                "https://translate.googleapis.com/translate_a/single"
                f"?client=gtx&sl={quote_plus(sl)}&tl={quote_plus(tl)}&dt=t&q={quote_plus(chunk)}"
            )
            req = urllib.request.Request(endpoint, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(body)
            segs = data[0] if isinstance(data, list) and data else []
            translated = "".join((s[0] for s in segs if isinstance(s, list) and s and s[0]),)
            translated = translated.strip()
            if not translated:
                return ""
            out.append(translated)
        except Exception:
            return ""

    return "\n".join(out).strip()


def _translate_via_mymemory(text: str, sl: str, tl: str) -> str:
    """Secondary fallback translator via MyMemory public endpoint."""
    chunks = split_translation_chunks(text, max_chars=500)
    if not chunks:
        return ""

    # MyMemory does not accept "auto" as a source language.
    # Use English as a practical fallback source for this tertiary provider.
    mm_sl = (sl or "").strip() or "en"
    if mm_sl.lower() == "auto":
        mm_sl = "en"

    out = []
    for chunk in chunks:
        try:
            endpoint = (
                "https://api.mymemory.translated.net/get"
                f"?q={quote_plus(chunk)}&langpair={quote_plus(mm_sl)}|{quote_plus(tl)}"
            )
            req = urllib.request.Request(endpoint, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(body)

            status = data.get("responseStatus")
            details = (data.get("responseDetails") or "").strip()
            if status is not None and str(status) != "200":
                return ""

            translated = (data.get("responseData") or {}).get("translatedText", "").strip()
            if not translated:
                return ""
            upper_text = translated.upper()
            upper_details = details.upper()
            if "INVALID SOURCE LANGUAGE" in upper_text or "INVALID SOURCE LANGUAGE" in upper_details:
                return ""
            out.append(translated)
        except Exception:
            return ""

    return "\n".join(out).strip()


async def do_google_translate(text: str, to_language: str, from_language: str = "") -> str:
    """Translate text using Google Translate directly."""
    # Resolve language names to codes
    tl = LANGUAGE_CODES.get(to_language.lower(), to_language.lower())
    detection_note = ""
    if from_language:
        sl = LANGUAGE_CODES.get(from_language.lower(), from_language.lower())
    else:
        detected = detect_source_language(text)
        if detected and detected[1] >= LANG_DETECTION_CONFIDENCE_THRESHOLD:
            sl = detected[0]
            detection_note = f"Detected source language: {sl} (confidence {detected[1]:.2f})"
        else:
            sl = "auto"
            if detected:
                detection_note = (
                    "Detected source language confidence is low "
                    f"({detected[1]:.2f}); using auto-detect instead."
                )

    encoded_text = quote_plus(text)
    url = f"https://translate.google.com/?sl={sl}&tl={tl}&text={encoded_text}&op=translate"

    async with async_playwright() as pw:
        context = await launch_browser(pw)
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await dismiss_consent(page)
            # Wait for translation to load
            await page.wait_for_timeout(3000)

            data = await page.evaluate(
                r"""
                (payload) => {
                    const sourceText = payload?.sourceText || '';
                    const targetLang = payload?.targetLang || '';
                    const data = {};

                    const normalizedSource = (sourceText || '').trim();
                    const targetPrefix = (targetLang || '').toLowerCase().split('-')[0];
                    const candidates = [];

                    const addCandidate = (t) => {
                        const val = (t || '').trim();
                        if (!val) return;
                        if (normalizedSource && val === normalizedSource) return;
                        candidates.push(val);
                    };

                    const selectors = [
                        '[data-result-index] [lang]',
                        '[data-language-to-translate-into] [lang]',
                        'span[jsname="W297wb"]',
                        '[aria-live="polite"] span',
                        '.J0lOec',
                        '.lRu31',
                        '.HwtZe',
                        '.Y2IQFc',
                        '.ryNqvb'
                    ];

                    for (const sel of selectors) {
                        const nodes = document.querySelectorAll(sel);
                        for (const node of nodes) {
                            const lang = (node.getAttribute('lang') || '').toLowerCase();
                            if (lang && targetPrefix && !lang.startsWith(targetPrefix)) continue;
                            addCandidate(node.innerText || node.textContent || '');
                        }
                    }

                    if (candidates.length > 0) {
                        // Pick the longest non-source candidate (usually full translation)
                        candidates.sort((a, b) => b.length - a.length);
                        data.translation = candidates[0];
                    }

                    return data;
                }
                """,
                {"sourceText": text, "targetLang": tl},
            )

            translation = (data.get("translation") or "").strip()

            # API fallbacks when UI scraping fails or is blocked.
            if not translation or translation == text:
                translation = _translate_via_google_http(text, sl, tl)
                if translation and translation != text:
                    lines = ["Google Translate (fallback API)\n"]
                    if detection_note:
                        lines.append(detection_note)
                    lines.append(f"Original: {text}")
                    lines.append(f"Translation ({to_language}): {translation}")
                    return "\n".join(lines)

            if not translation or translation == text:
                translation = _translate_via_mymemory(text, sl, tl)
                if translation and translation != text:
                    lines = ["Google Translate (fallback API 2)\n"]
                    if detection_note:
                        lines.append(detection_note)
                    lines.append(f"Original: {text}")
                    lines.append(f"Translation ({to_language}): {translation}")
                    return "\n".join(lines)

            if not translation or translation == text:
                return f"Could not translate: {text}"

            lines = ["Google Translate\n"]
            if detection_note:
                lines.append(detection_note)
            lines.append(f"Original: {text}")
            lines.append(f"Translation ({to_language}): {translation}")

            return "\n".join(lines)

        except Exception as e:
            return format_error("Google Translate", e)

        finally:
            await context.close()


@mcp.tool()
async def google_translate(text: str, to_language: str, from_language: str = "") -> str:
    """Translate text from one language to another using Google Translate.

    Sample prompts that trigger this tool:
        - "Translate 'hello world' to Japanese"
        - "How do you say 'thank you' in French?"
        - "Translate this to Spanish: The weather is nice today"
        - "What does 'Guten Morgen' mean in English?"
        - "Translate 'I love programming' to Korean"

    Args:
        text: The text to translate.
        to_language: Target language (e.g. "Spanish", "Japanese", "French", "German", "Korean", "Chinese", "Arabic").
        from_language: Source language (optional, auto-detected if empty).
    """
    return await do_google_translate(text, to_language, from_language or "")


# ---------------------------------------------------------------------------
# google_flights
# ---------------------------------------------------------------------------

async def do_google_flights(
    origin: str, destination: str, date: str = "", return_date: str = ""
) -> str:
    """Search Google Flights for flight information."""
    query_parts = [f"flights from {origin} to {destination}"]
    if date:
        query_parts.append(f"on {date}")
    if return_date:
        query_parts.append(f"return {return_date}")

    search_query = " ".join(query_parts)
    encoded_query = quote_plus(search_query)
    url = f"https://www.google.com/search?q={encoded_query}&hl=en"

    async with async_playwright() as pw:
        context = await launch_browser(pw)
        await load_cookies(context)
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await dismiss_consent(page)

            # Detect and handle CAPTCHA/rate-limit blocks
            if await is_blocked(page):
                solved = await try_solve_captcha(page)
                if not solved:
                    try:
                        await page.goto(
                            "https://www.google.com/ncr",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        await dismiss_consent(page)
                        await human_delay(page)
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await dismiss_consent(page)
                    except Exception:
                        pass

                    if await is_blocked(page):
                        await save_cookies(context)
                        return f"Google Flights blocked by bot detection for: {origin} to {destination}\nTry again later."

            await page.wait_for_timeout(3000)

            data = await page.evaluate(
                """
                () => {
                    const data = { flights: [] };

                    // Google's flight card in search results
                    const flightCards = document.querySelectorAll(
                        '.OgdJid, ' +
                        '.zBTtmb, ' +
                        '[data-attrid*="flight"] .wUrVib, ' +
                        '.fltt-card, ' +
                        '.gws-flights__result, ' +
                        'div.VkpGBb, div[data-attrid*="flight"]'
                    );

                    for (const card of flightCards) {
                        const text = card.innerText.trim();
                        if (text && text.length > 10) {
                            data.flights.push({ raw: text });
                        }
                    }

                    // Try the flights widget
                    if (data.flights.length === 0) {
                        const widget = document.querySelector(
                            '[data-attrid*="flight"], ' +
                            '.gws-flights, ' +
                            '.VkpGBb[data-attrid*="flight"], ' +
                            'div[data-attrid*="flight"]'
                        );
                        if (widget) {
                            data.widget_text = widget.innerText.substring(0, 3000);
                        }
                    }

                    // Also grab the "View all flights" link if present
                    const viewAll = document.querySelector('a[href*="google.com/travel/flights"]');
                    data.flights_url = viewAll ? viewAll.href : '';

                    // Get the knowledge panel or featured snippet about flights
                    const panel = document.querySelector('.kp-wholepage, .liYKde, .ULSxyf, .f5cPye');
                    if (panel) {
                        const flightInfo = panel.innerText.substring(0, 2000);
                        if (flightInfo.toLowerCase().includes('flight') || flightInfo.includes('$') || flightInfo.includes('hr')) {
                            data.panel_text = flightInfo;
                        }
                    }

                    // Last resort: raw page text
                    if (!data.flights.length && !data.widget_text && !data.panel_text) {
                        const searchArea = document.querySelector('#search, #rso, [role="main"]');
                        if (searchArea) {
                            data.raw_text = searchArea.innerText.substring(0, 3000);
                        }
                    }

                    return data;
                }
                """
            )

            lines = [f"Google Flights: {origin} to {destination}\n"]
            if date:
                lines.append(f"Date: {date}")
            if return_date:
                lines.append(f"Return: {return_date}")
            lines.append("")

            has_data = False

            if data.get("flights"):
                for f in data["flights"][:5]:
                    raw = f.get("raw", "")
                    # Clean up and format
                    raw = re.sub(r'\n{2,}', '\n', raw).strip()
                    lines.append(raw)
                    lines.append("")
                has_data = True

            if data.get("widget_text"):
                text = re.sub(r'\n{3,}', '\n\n', data["widget_text"]).strip()
                lines.append(text)
                has_data = True

            if data.get("panel_text") and not has_data:
                text = re.sub(r'\n{3,}', '\n\n', data["panel_text"]).strip()
                lines.append(text)
                has_data = True

            if data.get("flights_url"):
                lines.append(f"\nView all flights: {data['flights_url']}")

            if not has_data and data.get("raw_text"):
                raw = re.sub(r'\n{3,}', '\n\n', data["raw_text"]).strip()
                lines.append(f"(Raw page content:)\n{raw}")
                has_data = True

            if not has_data and not data.get("flights_url"):
                lines.append(f"No flight data found. Try searching directly:")
                lines.append(f"https://www.google.com/travel/flights")

            return "\n".join(lines)

        except Exception as e:
            return format_error("Google Flights search", e)

        finally:
            await save_cookies(context)
            await context.close()


@mcp.tool()
async def google_flights(
    origin: str, destination: str, date: str = "", return_date: str = ""
) -> str:
    """Search Google Flights for flight options, prices, and travel times.

    Sample prompts that trigger this tool:
        - "Find flights from New York to London"
        - "Search for cheap flights from LA to Tokyo"
        - "Flights from San Francisco to Paris on March 15"
        - "Find round trip flights from Chicago to Miami"
        - "How much are flights from Dubai to Bangkok?"

    Args:
        origin: Departure city or airport (e.g. "New York", "LAX", "London").
        destination: Arrival city or airport (e.g. "Tokyo", "SFO", "Paris").
        date: Departure date (optional, e.g. "March 15", "2025-03-15").
        return_date: Return date for round trips (optional).
    """
    return await do_google_flights(origin, destination, date or "", return_date or "")


# ---------------------------------------------------------------------------
# google_hotels
# ---------------------------------------------------------------------------

async def do_google_hotels(query: str, num_results: int = 5) -> list:
    """Search Google for hotel information."""
    encoded_query = quote_plus(f"hotels {query}")
    url = f"https://www.google.com/search?q={encoded_query}&hl=en"

    async with async_playwright() as pw:
        context = await launch_browser(pw)
        await load_cookies(context)
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await dismiss_consent(page)

            # Detect and handle CAPTCHA/rate-limit blocks
            if await is_blocked(page):
                solved = await try_solve_captcha(page)
                if not solved:
                    try:
                        await page.goto(
                            "https://www.google.com/ncr",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        await dismiss_consent(page)
                        await human_delay(page)
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await dismiss_consent(page)
                    except Exception:
                        pass

                    if await is_blocked(page):
                        await save_cookies(context)
                        return [f"Google Hotels blocked by bot detection for: {query}\nTry again later."]

            await page.wait_for_timeout(3000)

            data = await page.evaluate(
                r"""
                (numResults) => {
                    const data = { hotels: [] };

                    // Strategy: .BTPx6e elements ARE the hotel name elements.
                    // Walk up to the row container to find price/rating/link/image.
                    // Images are in sibling elements with class "uhHOwf".
                    const nameEls = document.querySelectorAll(
                        '.BTPx6e, .cuQzEe, .p69s9e, ' +
                        'div[data-ved] a[href*="hotel"], ' +
                        'div[data-ved] a[href*="travel"]'
                    );

                    // Collect hotel thumbnail images separately — they sit in
                    // .uhHOwf containers as siblings/cousins of the name elements.
                    // Pair them with hotels by index.
                    const thumbImgs = document.querySelectorAll('.uhHOwf img, .taJbee img, .wI3pFd img');
                    const thumbSrcs = [];
                    for (const img of thumbImgs) {
                        const src = img.src || img.dataset?.src || '';
                        if (src && !thumbSrcs.includes(src)) thumbSrcs.push(src);
                    }

                    for (const nameEl of nameEls) {
                        if (data.hotels.length >= numResults) break;

                        const name = nameEl.innerText.trim();
                        if (!name || name.length < 2) continue;

                        // Walk up to find the row container (up to 6 levels)
                        let row = nameEl;
                        for (let i = 0; i < 6; i++) {
                            if (!row.parentElement) break;
                            row = row.parentElement;
                            // Stop when we find a container with a link or price
                            if (row.querySelector('a[href]') && row.querySelector('a[href]') !== nameEl) break;
                        }

                        // Extract price — look in the row and siblings
                        let price = '';
                        const priceEl = row.querySelector('.kixHKb, .qeiSWe, .priceText, .hVE8ee, .gJTvYb');
                        if (priceEl) {
                            price = priceEl.innerText.trim();
                        } else {
                            // Search row text for price pattern
                            const rowText = row.innerText || '';
                            const priceMatch = rowText.match(/(?:CHF|USD|\$|€|£)\s*[\d,.]+/i)
                                || rowText.match(/[\d,.]+\s*(?:CHF|USD|EUR|per night)/i);
                            if (priceMatch) price = priceMatch[0].trim();
                        }

                        // Extract rating
                        let rating = '';
                        const ratingEl = row.querySelector('.KFi5wf, .MW4etd, .yi40Hd, .F7XJmb');
                        if (ratingEl) rating = ratingEl.innerText.trim();

                        // Extract reviews
                        let reviews = '';
                        const reviewsEl = row.querySelector('.jdzyld, .RDApEe, .gRlVJ');
                        if (reviewsEl) reviews = reviewsEl.innerText.trim().replace(/[()]/g, '');

                        // Extract link
                        const bookLink = row.querySelector(
                            'a[href*="hotel"], a[href*="book"], a[href*="travel"], a[href*="maps"]'
                        );
                        const linkEl = bookLink || row.querySelector('a[href]');
                        let linkUrl = linkEl ? linkEl.href : '';
                        if (linkUrl.includes('/url?') || linkUrl.includes('google.com/url')) {
                            try {
                                const u = new URL(linkUrl);
                                linkUrl = u.searchParams.get('q') || u.searchParams.get('url') || linkUrl;
                            } catch(e) {}
                        }

                        // Image: try within the row first, then pair by index
                        let thumbnail = '';
                        // Check row for images
                        const rowImgs = row.querySelectorAll('img');
                        for (const img of rowImgs) {
                            const s = img.src || img.dataset?.src || '';
                            if (!s) continue;
                            if (s.startsWith('data:image') && s.length > 500) { thumbnail = s; break; }
                            if (s.startsWith('http') && !s.includes('gstatic.com/s/i/')) { thumbnail = s; break; }
                            // Protocol-relative URLs
                            if (s.startsWith('//')) { thumbnail = 'https:' + s; break; }
                        }
                        // Fallback: pair by index from the collected thumbnails
                        if (!thumbnail) {
                            const idx = data.hotels.length;
                            if (idx < thumbSrcs.length) {
                                let s = thumbSrcs[idx];
                                if (s.startsWith('//')) s = 'https:' + s;
                                thumbnail = s;
                            }
                        }

                        data.hotels.push({
                            name, price, rating, reviews, url: linkUrl, thumbnail,
                        });
                    }

                    // Fallback: get the hotel widget text
                    if (data.hotels.length === 0) {
                        const widget = document.querySelector(
                            '[data-attrid*="hotel"], .kp-wholepage, .liYKde, .f5cPye'
                        );
                        if (widget) {
                            const text = widget.innerText.substring(0, 3000);
                            if (text.toLowerCase().includes('hotel') || text.includes('$') || text.includes('/night')) {
                                data.widget_text = text;
                            }
                        }
                    }

                    // "View all hotels" link
                    const viewAll = document.querySelector('a[href*="google.com/travel/hotels"]');
                    data.hotels_url = viewAll ? viewAll.href : '';

                    // Last resort: raw page text
                    if (data.hotels.length === 0 && !data.widget_text) {
                        const searchArea = document.querySelector('#search, #rso, [role="main"]');
                        if (searchArea) {
                            data.raw_text = searchArea.innerText.substring(0, 3000);
                        }
                    }

                    return data;
                }
                """,
                num_results,
            )

            # Download thumbnail images for inline display
            import base64 as b64mod
            if data.get("hotels"):
                for h in data["hotels"][:num_results]:
                    thumb_url = h.get("thumbnail", "")
                    if not thumb_url:
                        continue
                    # Handle base64 data URIs from inline images
                    if thumb_url.startswith("data:image"):
                        try:
                            # data:image/jpeg;base64,/9j/4AAQ...
                            header, b64data = thumb_url.split(",", 1)
                            body = b64mod.b64decode(b64data)
                            if len(body) < 500 or len(body) > 5_000_000:
                                continue
                            h["image_bytes"] = body
                            ct = header.split(";")[0].replace("data:", "")
                            h["content_type"] = ct or "image/jpeg"
                        except Exception:
                            pass
                        continue
                    # Download HTTP URLs
                    if not thumb_url.startswith("http"):
                        continue
                    try:
                        resp = await context.request.get(thumb_url, timeout=8000)
                        if resp.ok:
                            body = await resp.body()
                            if len(body) < 1000 or len(body) > 5_000_000:
                                continue
                            h["image_bytes"] = body
                            ct = resp.headers.get("content-type", "image/jpeg")
                            h["content_type"] = ct.split(";")[0].strip()
                    except Exception:
                        continue

            # Build mixed content: text descriptions + inline images
            content: list = [f"Google Hotels: {query}\n"]
            has_data = False

            if data.get("hotels"):
                for i, h in enumerate(data["hotels"][:num_results], 1):
                    desc = f"{i}. {h['name']}"
                    if h.get("price"):
                        desc += f"\n   Price: {h['price']}"
                    if h.get("rating"):
                        rating_str = f"   Rating: {h['rating']}"
                        if h.get("reviews"):
                            rating_str += f" ({h['reviews']} reviews)"
                        desc += f"\n{rating_str}"
                    if h.get("url"):
                        desc += f"\n   URL: {h['url']}"
                    content.append(desc)

                    if h.get("image_bytes"):
                        try:
                            ct = h.get("content_type", "image/jpeg")
                            fmt_map = {
                                "image/jpeg": "jpeg", "image/png": "png",
                                "image/gif": "gif", "image/webp": "webp",
                            }
                            fmt = fmt_map.get(ct, "jpeg")
                            content.append(Image(data=h["image_bytes"], format=fmt))
                        except Exception:
                            pass
                has_data = True

            if data.get("widget_text") and not has_data:
                text = re.sub(r'\n{3,}', '\n\n', data["widget_text"]).strip()
                content.append(text)
                has_data = True

            if data.get("hotels_url"):
                content.append(f"\nView all hotels: {data['hotels_url']}")

            if not has_data and data.get("raw_text"):
                raw = re.sub(r'\n{3,}', '\n\n', data["raw_text"]).strip()
                content.append(f"(Raw page content:)\n{raw}")
                has_data = True

            if not has_data and not data.get("hotels_url"):
                content.append("No hotel data found. Try searching directly:")
                content.append("https://www.google.com/travel/hotels")

            return content

        except Exception as e:
            return format_error("Google Hotels search", e)

        finally:
            await save_cookies(context)
            await context.close()


@mcp.tool()
async def google_hotels(query: str, num_results: int = 5) -> list:
    """Search for hotels and accommodation with thumbnail images, prices, ratings, and booking URLs.

    Sample prompts that trigger this tool:
        - "Find hotels in Paris for next weekend"
        - "Search for cheap hotels in Tokyo"
        - "Best hotels near Times Square New York"
        - "Find 5-star hotels in Dubai"
        - "Hotels in London under $200 per night"

    Args:
        query: Hotel search query with location (e.g. "Paris", "Tokyo near Shibuya", "New York March 15-20").
        num_results: Number of results to return (default 5, max 10).
    """
    num_results = max(1, min(num_results, 10))
    return await do_google_hotels(query, num_results)
