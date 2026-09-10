"""Network utilities — URL fetching, fallback search providers."""

import asyncio
import json
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict
from urllib.parse import quote_plus, unquote, urlparse, parse_qs

from ..config import (
    REDIRECT_RESOLVE_CACHE_SIZE,
    REDIRECT_RESOLVE_TIMEOUT,
    USER_AGENT,
)

logger = logging.getLogger(__name__)

# Google search-result redirect wrappers. Every organic SERP result's <a href>
# is one of these, NOT the final destination URL.
_GOOGLE_URL_REDIRECT_RE = re.compile(
    r"^https?://(?:www\.)?google\.[^/]+/url\?", re.IGNORECASE
)
_GOOGLE_GOTO_REDIRECT_RE = re.compile(
    r"^https?://(?:www\.)?google\.[^/]+/goto\?", re.IGNORECASE
)


def _is_google_redirect(url: str) -> bool:
    """Return True if the URL is a Google search-result redirect wrapper."""
    return bool(
        _GOOGLE_URL_REDIRECT_RE.match(url) or _GOOGLE_GOTO_REDIRECT_RE.match(url)
    )


def _decode_legacy_redirect(url: str) -> str:
    """Decode the legacy ``/url?q=...`` redirect format.

    The real destination is the ``q`` query parameter (URL-encoded). Returns
    the decoded destination, or ``""`` if it cannot be extracted.
    """
    try:
        params = parse_qs(urlparse(url).query)
        for key in ("q", "url", "u", "uddg"):
            val = params.get(key, [""])[0]
            if val:
                return unquote(val)
    except Exception:
        pass
    return ""


def _resolve_goto_redirect(url: str, timeout: int = 8) -> str:
    """Follow a modern ``/goto?url=...`` redirect via HTTP.

    The ``url`` param is a base64url-encoded protobuf token that cannot be
    decoded to a readable URL — it must be resolved by following the redirect.
    Uses HEAD first (lighter), falling back to GET for servers that reject HEAD.
    Returns the final ``response.url``, or ``""`` on failure.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        # HEAD first — lighter, and urllib follows redirects for HEAD too.
        # But some endpoints (Google's /goto) return 200 on HEAD without
        # redirecting, so only trust HEAD if it actually moved somewhere.
        try:
            req.method = "HEAD"
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                head_url = resp.geturl()
                if head_url and head_url != url:
                    return head_url
        except Exception:
            pass
        # Fall back to GET (some servers 405/403 on HEAD, or HEAD doesn't
        # redirect). GET reliably follows Google's /goto redirect.
        req.method = "GET"
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl()
    except Exception:
        return ""


# In-process LRU cache of resolved redirects. Google reuses the same /goto
# token across searches, so memoizing avoids re-following the same redirect
# (faster, and fewer requests to Google). Thread-safe enough for our use: the
# GIL protects the dict operations, and a worst-case duplicate resolution is
# harmless. Size is configurable via REDIRECT_RESOLVE_CACHE_SIZE (0 = off).
_resolve_cache: "OrderedDict[str, str]" = OrderedDict()


def _cache_get(url: str) -> str | None:
    if REDIRECT_RESOLVE_CACHE_SIZE <= 0:
        return None
    val = _resolve_cache.get(url)
    if val is not None:
        # Refresh LRU ordering.
        _resolve_cache.move_to_end(url)
    return val


def _cache_put(url: str, resolved: str) -> None:
    if REDIRECT_RESOLVE_CACHE_SIZE <= 0:
        return
    _resolve_cache[url] = resolved
    _resolve_cache.move_to_end(url)
    while len(_resolve_cache) > REDIRECT_RESOLVE_CACHE_SIZE:
        _resolve_cache.popitem(last=False)


def resolve_url(url: str, timeout: int | None = None) -> str:
    """Resolve a Google search-result redirect URL to its final destination.

    - Legacy ``/url?q=...``  -> URL-decode the ``q`` param.
    - Modern ``/goto?url=...`` -> follow the HTTP redirect.
    - Non-Google URLs pass through unchanged.
    - On failure, returns the original URL (never drops the result), logging a
      warning so the caller can still surface the result.

    ``timeout`` defaults to ``REDIRECT_RESOLVE_TIMEOUT``. Results are memoized
    in an in-process LRU cache (see ``REDIRECT_RESOLVE_CACHE_SIZE``).
    """
    if timeout is None:
        timeout = REDIRECT_RESOLVE_TIMEOUT

    if not url or not _is_google_redirect(url):
        return url

    cached = _cache_get(url)
    if cached is not None:
        return cached

    if _GOOGLE_URL_REDIRECT_RE.match(url):
        decoded = _decode_legacy_redirect(url)
        if decoded:
            _cache_put(url, decoded)
            return decoded

    # Modern goto format (or legacy decode failed) -> follow the redirect.
    resolved = _resolve_goto_redirect(url, timeout=timeout)
    if resolved:
        _cache_put(url, resolved)
        return resolved

    logger.warning("Failed to resolve Google redirect URL: %s", url)
    return url


async def resolve_urls(
    urls: list[str], timeout: int | None = None, max_concurrency: int = 8
) -> list[str]:
    """Resolve a list of URLs to their final destinations, concurrently.

    Runs the blocking ``resolve_url`` in a thread pool so the async event loop
    is never blocked, and bounds concurrency so a slow redirect can't stall the
    whole search. Returns a list aligned with the input.
    """
    if not urls:
        return []

    semaphore = asyncio.Semaphore(max_concurrency)

    async def _resolve_one(url: str) -> str:
        async with semaphore:
            return await asyncio.to_thread(resolve_url, url, timeout)

    return await asyncio.gather(*(_resolve_one(u) for u in urls))


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
