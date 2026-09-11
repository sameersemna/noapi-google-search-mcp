"""Google Maps tools — places search and directions.

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


# google_maps
# ---------------------------------------------------------------------------

async def do_google_maps(query: str, num_results: int = 5) -> list:
    """Search Google Maps for places and return results with a map screenshot."""
    encoded_query = quote_plus(query)
    # Navigate directly to Google Maps search (shows map with pins)
    url = f"https://www.google.com/maps/search/{encoded_query}/?hl=en"

    async with async_playwright() as pw:
        context = await launch_browser(pw, viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await dismiss_consent(page)
            # Wait for results panel to appear
            await page.wait_for_timeout(3000)
            # Wait for the map canvas to render (tiles need time to load)
            try:
                await page.wait_for_selector(
                    "canvas, .widget-scene-canvas", timeout=8000
                )
            except Exception:
                pass
            # Extra time for map tiles to fully render
            await page.wait_for_timeout(4000)

            # Extract place data from Google Maps results panel
            results = await page.evaluate(
                r"""
                (numResults) => {
                    const results = [];
                    const seen = new Set();

                    // Use div.Nv2PK (the main card container) to avoid
                    // duplicates from nested a.hfpxzc links.
                    const cards = document.querySelectorAll('div.Nv2PK');

                    for (const card of cards) {
                        if (results.length >= numResults) break;

                        // --- Name ---
                        const nameEl = card.querySelector(
                            '.qBF1Pd, .fontHeadlineSmall, [role="heading"]'
                        );
                        let name = nameEl ? nameEl.innerText.trim() : '';
                        if (!name) {
                            const link = card.querySelector('a.hfpxzc');
                            if (link) name = (link.getAttribute('aria-label') || '').trim();
                        }
                        if (!name || name.length < 2 || seen.has(name)) continue;
                        seen.add(name);

                        // --- Rating from selector ---
                        let rating = '';
                        const rEl = card.querySelector('.MW4etd, .yi40Hd');
                        if (rEl) rating = rEl.innerText.trim();

                        // --- Parse card text lines for all fields ---
                        // Card text layout:
                        //   Cantinetta Antinori
                        //   4.4(2,486) · $$$       ← reviews + price here
                        //   Italian · (icon) · Augustinergasse 25
                        //   Seasonal Tuscan cuisine with fine wines
                        //   Closed · Opens 11:30 am
                        //   "Review quote..."
                        const allText = card.innerText || '';
                        const lines = allText.split('\n').map(s => s.trim())
                            .filter(s => s && s !== '\xa0');

                        let reviews = '', priceRange = '';
                        let category = '', address = '';
                        let description = '', status = '';

                        for (const line of lines) {
                            if (line === name) continue;
                            if (line.length <= 2) continue;

                            // Rating line: "4.4(2,486) · $$$" or just "4.6"
                            if (/^\d\.\d/.test(line)) {
                                // Reviews in parentheses: (2,486)
                                const revMatch = line.match(/\(([\d,]+)\)/);
                                if (revMatch && !reviews) reviews = revMatch[1];
                                // Price: $, $$, $$$, $$$$
                                const pm = line.match(/([\$\u0024€£]{1,4})\s*$/);
                                if (pm && !priceRange) priceRange = pm[1];
                                if (!priceRange) {
                                    const pm2 = line.match(/([\$€£]{1,4})/);
                                    if (pm2) priceRange = pm2[1];
                                }
                                // CHF price pattern
                                if (!priceRange) {
                                    const chf = line.match(/CHF\s*[\d,.]+/i);
                                    if (chf) priceRange = chf[0];
                                }
                                continue;
                            }

                            // Status: "Closed · Opens ..." or "Open · Closes ..."
                            if (/^(Closed|Open\b|Temporarily closed)/i.test(line)) {
                                status = line;
                                continue;
                            }

                            // Quote lines
                            if (line.startsWith('"') || line.startsWith('\u201c')) continue;
                            // Action buttons
                            if (/^(Reserve|Order online|Dine-in|Takeout|Delivery)/i.test(line)) continue;

                            // Category · address line (contains separator)
                            // "Italian · (icon) · Augustinergasse 25"
                            if (line.includes('\u00B7') || line.includes('·')) {
                                if (!category) {
                                    const segs = line.split(/[·\u00B7]/).map(s => s.trim())
                                        .filter(s => s && s.length > 1);
                                    for (const seg of segs) {
                                        if (/^[\$€£]{1,4}$/.test(seg)) {
                                            if (!priceRange) priceRange = seg;
                                        } else if (!category && !/\d/.test(seg) &&
                                                   seg.length < 50) {
                                            category = seg;
                                        } else if (!address && /\d/.test(seg) &&
                                                   seg.length < 80) {
                                            address = seg;
                                        }
                                    }
                                }
                                continue;
                            }

                            // Description/tagline
                            if (!description && line.length > 10 &&
                                line.length < 150 && !/^\d/.test(line)) {
                                description = line;
                            }
                        }

                        // Place URL
                        let placeUrl = '';
                        const link = card.querySelector('a.hfpxzc, a[data-item-id]');
                        if (link && link.href) placeUrl = link.href;

                        results.push({
                            name, rating, reviews, priceRange,
                            category, address, description, status,
                            url: placeUrl,
                        });
                    }

                    // Fallback: parse raw text from results panel
                    if (results.length === 0) {
                        const panel = document.querySelector(
                            '[role="feed"], [role="main"], .m6QErb'
                        );
                        if (panel) {
                            return [{
                                name: '__raw__',
                                raw_text: panel.innerText.substring(0, 3000),
                                rating: '', reviews: '', category: '',
                                priceRange: '', address: '', description: '',
                                status: '', url: ''
                            }];
                        }
                    }

                    return results;
                }
                """,
                num_results,
            )

            # Take a viewport screenshot showing the map with pins
            screenshot_bytes = await page.screenshot(full_page=False, type="png")
            if not results:
                content = [f"Google Maps Results for: {query}\n\nNo places found."]
                content.append(Image(data=screenshot_bytes, format="png"))
                return content

            # Handle raw text fallback
            if len(results) == 1 and results[0].get("name") == "__raw__":
                raw = results[0].get("raw_text", "")
                content = [f"Google Maps Results for: {query}\n\n{raw}"]
                content.append(Image(data=screenshot_bytes, format="png"))
                return content

            # Build mixed content: text descriptions first, then map screenshot
            content: list = [f"Google Maps Results for: {query}\n"]
            for i, r in enumerate(results[:num_results], 1):
                desc = f"{i}. {r['name']}"
                if r.get("rating"):
                    rating_str = f"   Rating: {r['rating']}"
                    if r.get("reviews"):
                        rating_str += f" ({r['reviews']} reviews)"
                    desc += f"\n{rating_str}"
                if r.get("priceRange"):
                    desc += f"\n   Price: {r['priceRange']}"
                if r.get("category"):
                    desc += f"\n   Type: {r['category']}"
                if r.get("address"):
                    desc += f"\n   Address: {r['address']}"
                if r.get("description"):
                    desc += f"\n   Note: {r['description']}"
                if r.get("status"):
                    desc += f"\n   Hours: {r['status']}"
                if r.get("url"):
                    desc += f"\n   Link: {r['url']}"
                content.append(desc)

            # Map screenshot at the end (shows all pins)
            content.append(Image(data=screenshot_bytes, format="png"))

            return content

        except Exception as e:
            return [format_error("Google Maps search", e)]

        finally:
            await context.close()


@mcp.tool()
async def google_maps(query: str, num_results: int = 5) -> list:
    """Search Google Maps for places, restaurants, businesses, and locations with ratings, prices, addresses, and a map screenshot showing pinned locations.

    Sample prompts that trigger this tool:
        - "Find Italian restaurants near Times Square"
        - "Where are the best coffee shops in Berlin?"
        - "Search for hotels in Tokyo"
        - "Find EV charging stations in San Francisco"
        - "What are the top-rated gyms in London?"

    Args:
        query: The place search query (e.g. "pizza near Central Park", "hotels in Paris").
        num_results: Number of results to return (default 5, max 10).
    """
    num_results = max(1, min(num_results, 10))
    return await do_google_maps(query, num_results)


# ---------------------------------------------------------------------------
# google_maps_directions
# ---------------------------------------------------------------------------


async def do_google_maps_directions(
    origin: str, destination: str, mode: str = "driving"
) -> list:
    """Get directions between two locations with a map screenshot."""
    # Google Maps uses "bicycling" not "cycling"
    mode_map = {"cycling": "bicycling"}
    gm_mode = mode_map.get(mode, mode)

    encoded_origin = quote_plus(origin)
    encoded_dest = quote_plus(destination)
    url = (
        f"https://www.google.com/maps/dir/{encoded_origin}/{encoded_dest}"
        f"/?travelmode={gm_mode}&hl=en"
    )

    async with async_playwright() as pw:
        context = await launch_browser(pw, viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await dismiss_consent(page)
            # Wait for the map canvas and route to render
            await page.wait_for_timeout(5000)

            # Scrape route info from the directions panel
            route_data = await page.evaluate(
                """
                () => {
                    const data = {distance: '', duration: '', steps: [], summary: ''};

                    // Try to get distance and duration from the trip info
                    const tripEl = document.querySelector(
                        '#section-directions-trip-0, ' +
                        '[data-trip-index="0"], ' +
                        '.MespJc'
                    );

                    if (tripEl) {
                        const text = tripEl.innerText;
                        // Extract distance and duration patterns
                        const distMatch = text.match(/(\\d[\\d,.]+\\s*(?:km|mi|m|miles|ft))/i);
                        const durMatch = text.match(/(\\d+\\s*(?:hr|hour|min|h|d|day)s?(?:\\s*\\d+\\s*(?:min|hr|h)s?)?)/i);
                        if (distMatch) data.distance = distMatch[1];
                        if (durMatch) data.duration = durMatch[1];
                    }

                    // Broader fallback: search entire page for distance/duration
                    if (!data.distance || !data.duration) {
                        const allText = document.body.innerText;
                        if (!data.distance) {
                            const dm = allText.match(/(\\d[\\d,.]+\\s*(?:km|mi|miles))\\b/i);
                            if (dm) data.distance = dm[1];
                        }
                        if (!data.duration) {
                            const tm = allText.match(/(\\d+\\s*(?:hr|hour|h)s?\\s*\\d*\\s*(?:min)?s?)/i);
                            if (!tm) {
                                const tm2 = allText.match(/(\\d+\\s*min)/i);
                                if (tm2) data.duration = tm2[1];
                            } else {
                                data.duration = tm[1];
                            }
                        }
                    }

                    // Try to get route summary (e.g. "via A9")
                    const summaryEl = document.querySelector(
                        '.r4nke, .LjGbjd, span[jstcache]'
                    );
                    if (summaryEl) {
                        const st = summaryEl.innerText.trim();
                        if (st.toLowerCase().startsWith('via')) {
                            data.summary = st;
                        }
                    }

                    // Get step-by-step directions
                    const stepEls = document.querySelectorAll(
                        '[data-legid] .directions-mode-step, ' +
                        '.directions-mode-step, ' +
                        'div[jstcache] span.XoKrad, ' +
                        '.T2yjMc'
                    );
                    for (const step of stepEls) {
                        const t = step.innerText.trim();
                        if (t && t.length > 2 && t.length < 300) {
                            data.steps.push(t);
                        }
                    }

                    // Fallback: get the directions panel raw text
                    if (data.steps.length === 0) {
                        const panel = document.querySelector(
                            '#directions-searchbox-0, ' +
                            '.directions-renderer, ' +
                            '#section-directions-trip-0, ' +
                            '[role="main"]'
                        );
                        if (panel) {
                            const lines = panel.innerText.split('\\n')
                                .map(l => l.trim())
                                .filter(l => l.length > 2 && l.length < 300);
                            // Take first 30 non-empty lines as raw directions
                            data.raw_panel = lines.slice(0, 30).join('\\n');
                        }
                    }

                    return data;
                }
                """
            )

            # Take a full page screenshot
            screenshot_bytes = await page.screenshot(full_page=False, type="png")

            # Build result
            content = []

            header = f"Directions: {origin} → {destination} ({mode})\n"
            if route_data.get("distance") or route_data.get("duration"):
                header += f"Distance: {route_data.get('distance', 'N/A')}"
                header += f" | Duration: {route_data.get('duration', 'N/A')}"
                if route_data.get("summary"):
                    header += f" | {route_data['summary']}"
                header += "\n"

            content.append(header)

            if route_data.get("steps"):
                steps_text = "Route steps:\n"
                for i, step in enumerate(route_data["steps"][:25], 1):
                    steps_text += f"  {i}. {step}\n"
                content.append(steps_text)
            elif route_data.get("raw_panel"):
                content.append(f"Route details:\n{route_data['raw_panel']}\n")

            # Add map screenshot
            content.append(Image(data=screenshot_bytes, format="png"))

            return content

        except Exception as e:
            return [format_error("Google Maps directions lookup", e)]

        finally:
            await context.close()


@mcp.tool()
async def google_maps_directions(
    origin: str, destination: str, mode: str = "driving"
) -> list:
    """Get driving/walking/transit/cycling directions between two locations with route info and a map screenshot.

    Sample prompts that trigger this tool:
        - "Get directions from Berlin to Munich"
        - "How do I drive from New York to Boston?"
        - "Walking directions from the Eiffel Tower to the Louvre"
        - "Transit route from Shibuya to Akihabara"
        - "Cycling route from Golden Gate Bridge to Fisherman's Wharf"
        - "Show me the route from London to Edinburgh"

    Args:
        origin: Starting location (address, city, or place name).
        destination: Ending location (address, city, or place name).
        mode: Travel mode - one of "driving" (default), "walking", "transit", or "cycling".
    """
    mode = mode.lower().strip()
    valid_modes = {"driving", "walking", "transit", "cycling"}
    if mode not in valid_modes:
        mode = "driving"
    return await do_google_maps_directions(origin, destination, mode)
