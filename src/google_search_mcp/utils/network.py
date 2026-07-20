"""Network utilities — URL fetching, fallback search providers."""

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus, unquote, urlparse, parse_qs

from ..config import USER_AGENT


def fetch_url_bytes(url: str, timeout: int = 15) -> bytes:
    """Fetch a URL using stdlib urllib. Returns raw bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Fallback search providers (used when Google blocks automated requests)
# ---------------------------------------------------------------------------


def fallback_duckduckgo_search(query: str, num_results: int = 5) -> list[dict]:
    """Fallback web search using DuckDuckGo HTML."""
    try:
        ddg_url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        req = urllib.request.Request(ddg_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return []

    matches = re.findall(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    results: list[dict] = []
    seen: set[tuple] = set()
    for href, title_html in matches:
        if len(results) >= num_results:
            break

        parsed_href = href
        if "duckduckgo.com/l/?" in href:
            try:
                q = parse_qs(urlparse(href).query)
                parsed_href = unquote((q.get("uddg", [""])[0] or "").strip())
            except Exception:
                parsed_href = href

        if not parsed_href.startswith("http"):
            continue

        title = re.sub(r"<[^>]+>", "", title_html)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue

        key = (title.lower(), parsed_href)
        if key in seen:
            continue
        seen.add(key)

        results.append({"title": title, "url": parsed_href, "snippet": ""})

    return results


def fallback_bing_rss_search(query: str, num_results: int = 5) -> list[dict]:
    """Second fallback using Bing RSS when both Google and DuckDuckGo are blocked."""
    try:
        rss_url = f"https://www.bing.com/search?format=rss&q={quote_plus(query)}"
        req = urllib.request.Request(rss_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml_bytes = resp.read()
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []

    results: list[dict] = []
    seen: set[tuple] = set()
    for item in root.findall("./channel/item"):
        if len(results) >= num_results:
            break
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if not title or not link.startswith("http"):
            continue
        key = (title.lower(), link)
        if key in seen:
            continue
        seen.add(key)
        results.append({"title": title, "url": link, "snippet": desc})

    return results


def fallback_web_search(query: str, num_results: int = 5) -> tuple[str, list[dict]]:
    """Try DuckDuckGo then Bing as fallback. Returns (provider_name, results)."""
    ddg = fallback_duckduckgo_search(query, num_results)
    if ddg:
        return "DuckDuckGo", ddg
    bing = fallback_bing_rss_search(query, num_results)
    if bing:
        return "Bing RSS", bing
    return "", []


def fallback_duckduckgo_news(query: str, num_results: int = 5) -> list[dict]:
    """Fallback news search using DuckDuckGo HTML."""
    try:
        ddg_url = f"https://duckduckgo.com/html/?q={quote_plus(query)}&t=h_&ia=news"
        req = urllib.request.Request(ddg_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return []

    results: list[dict] = []
    seen: set[tuple] = set()
    matches = re.findall(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for href, title_html in matches:
        if len(results) >= num_results:
            break
        parsed_href = href
        if "duckduckgo.com/l/?" in href:
            try:
                q = parse_qs(urlparse(href).query)
                parsed_href = unquote((q.get("uddg", [""])[0] or "").strip())
            except Exception:
                parsed_href = href
        if not parsed_href.startswith("http"):
            continue
        title = re.sub(r"<[^>]+>", "", title_html)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        key = (title.lower(), parsed_href)
        if key in seen:
            continue
        seen.add(key)
        results.append({"title": title, "url": parsed_href, "source": "DuckDuckGo News", "time": "", "snippet": ""})
    return results


def fallback_duckduckgo_images(query: str, num_results: int = 5) -> list[dict]:
    """Fallback image search using DuckDuckGo HTML."""
    try:
        ddg_url = f"https://duckduckgo.com/html/?q={quote_plus(query)}&t=h_&ia=images"
        req = urllib.request.Request(ddg_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return []

    results: list[dict] = []
    seen: set[str] = set()
    img_matches = re.findall(
        r'<img[^>]*class="[^"]*tile__img[^"]*"[^>]*src="([^"]+)"[^>]*alt="([^"]*)"',
        html,
        flags=re.IGNORECASE,
    )
    for src, alt in img_matches:
        if len(results) >= num_results:
            break
        if not src.startswith("http"):
            continue
        key = src.lower()
        if key in seen:
            continue
        seen.add(key)
        results.append({"title": alt or "Image", "thumbnail": src, "url": src})
    return results


def format_fallback_results(query: str, provider: str, results: list[dict]) -> str:
    """Format fallback search results into a readable string."""
    lines = [
        "Google blocked by bot detection for this request.",
        f"Showing fallback web results ({provider}):\n",
        f"Web Results for: {query}\n",
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')}")
        lines.append(f"   URL: {r.get('url', '')}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        lines.append("")
    return "\n".join(lines)
