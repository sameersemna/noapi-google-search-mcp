"""Browser lifecycle management — launch, cookies, stealth, consent dismissal.

Provides a single `launch_browser()` entry point that creates a hardened
Playwright browser context with stealth patches, cookie persistence, and
human-like interaction delays.
"""

import json
import os
import random

from playwright.async_api import BrowserContext

from .config import (
    BROWSER_DATA_DIR,
    COOKIE_JSON_PATH,
    COOKIE_DIR,
    STEALTH_JS,
    USER_AGENT,
)


async def launch_browser(pw, viewport: dict | None = None) -> BrowserContext:
    """Launch a headless Chromium browser with stealth settings.

    Uses a persistent user data directory so browser fingerprint, localStorage,
    and session data remain consistent across restarts.
    """
    os.makedirs(BROWSER_DATA_DIR, exist_ok=True)

    context = await pw.chromium.launch_persistent_context(
        BROWSER_DATA_DIR,
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--window-size=1280,800",
            "--enable-webgl",
            "--use-gl=desktop",
        ],
        user_agent=USER_AGENT,
        viewport=viewport or {"width": 1280, "height": 800},
        locale="en-US",
    )
    await context.add_init_script(STEALTH_JS)
    return context


async def human_delay(page, min_ms: int = 500, max_ms: int = 1500) -> None:
    """Add a small random delay to mimic human interaction timing."""
    await page.wait_for_timeout(random.randint(min_ms, max_ms))


async def save_cookies(context: BrowserContext) -> None:
    """Persist browser cookies to disk so Google sees a returning user."""
    try:
        cookies = await context.cookies()
        with open(COOKIE_JSON_PATH, "w") as f:
            json.dump(cookies, f)
    except Exception:
        pass


def _parse_netscape_cookie_file(filepath: str) -> list[dict]:
    """Parse a Netscape-format cookie file into Playwright-compatible dicts."""
    cookies: list[dict] = []
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _, path, secure_str, expiry_str, name, value = parts[:7]
                secure = secure_str.lower() == "true"
                try:
                    expiry = int(expiry_str)
                except ValueError:
                    expiry = 0
                cookie: dict = {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "secure": secure,
                    "httpOnly": False,
                    "sameSite": "Lax",
                }
                if expiry > 0:
                    cookie["expires"] = expiry
                cookies.append(cookie)
    except Exception:
        return []
    return cookies


async def load_cookies(context: BrowserContext) -> int:
    """Load cookies into the browser context.

    Priority order (first found wins per cookie):
      1. Netscape-format .txt files in COOKIE_DIR
      2. Auto-saved JSON from COOKIE_JSON_PATH

    Returns the number of cookies loaded.
    """
    loaded = 0
    try:
        if os.path.isdir(COOKIE_DIR):
            for fname in sorted(os.listdir(COOKIE_DIR)):
                if fname.endswith(".txt"):
                    fpath = os.path.join(COOKIE_DIR, fname)
                    cookies = _parse_netscape_cookie_file(fpath)
                    if cookies:
                        await context.add_cookies(cookies)
                        loaded += len(cookies)
    except Exception:
        pass

    try:
        if os.path.isfile(COOKIE_JSON_PATH):
            with open(COOKIE_JSON_PATH) as f:
                cookies = json.load(f)
            if cookies:
                await context.add_cookies(cookies)
                loaded += len(cookies)
    except Exception:
        pass

    return loaded


async def dismiss_consent(page) -> None:
    """Dismiss Google consent banner if present (supports multiple languages)."""
    try:
        consent_btn = page.locator(
            "button:has-text('Accept all'), "
            "button:has-text('Accept All'), "
            "button:has-text('I agree'), "
            "button:has-text('Reject all'), "
            "button:has-text('Reject All'), "
            "button:has-text('Alle akzeptieren'), "
            "button:has-text('Alle ablehnen'), "
            "button:has-text('Tout accepter'), "
            "button:has-text('Tout refuser'), "
            "button:has-text('Aceptar todo'), "
            "button:has-text('Rechazar todo'), "
            "button:has-text('Accetta tutto'), "
            "button:has-text('Rifiuta tutto')"
        )
        if await consent_btn.count() > 0:
            await consent_btn.first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    await human_delay(page)


async def wait_for_google_results_ready(page, timeout_ms: int = 15000) -> None:
    """Wait until Google SERP has results or a terminal no-result/block state."""
    await page.wait_for_function(
        """
        () => {
            const bodyText = (document.body?.innerText || '').toLowerCase();

            if (bodyText.includes('our systems have detected unusual traffic') ||
                bodyText.includes('unusual traffic from your computer network') ||
                bodyText.includes('did not match any documents') ||
                bodyText.includes('no results found for')) {
                return true;
            }

            const hasResultCards = document.querySelectorAll(
                'div#search div.g, #rso div.g, #rso div.MjjYud, a h3'
            ).length > 0;

            const hasSearchContainer = !!document.querySelector('div#search, #rso');
            const hasEnoughLinks = document.querySelectorAll('a[href]').length > 20;

            return hasResultCards || (hasSearchContainer && hasEnoughLinks);
        }
        """,
        timeout=timeout_ms,
    )


async def warmup_retry(page, url: str) -> None:
    """Open Google home first, then navigate to target URL.

    Helps when the first direct SERP request gets a transient block.
    """
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
