"""Browser lifecycle management — launch, cookies, stealth, consent dismissal.

Provides a single `launch_browser()` entry point that creates a hardened
Playwright browser context with stealth patches, cookie persistence, and
human-like interaction delays.
"""

import json
import os
import random
from datetime import datetime

from playwright.async_api import BrowserContext, Page

from .config import (
    BROWSER_DATA_DIR,
    COOKIE_JSON_PATH,
    COOKIE_DIR,
    SCREENSHOTS_DIR,
    STEALTH_JS,
    USER_AGENT,
)


# ── Viewport randomization ──

_BASE_VIEWPORT = {"width": 1280, "height": 800}


def _randomized_viewport() -> dict:
    """Return a viewport with slight randomization to avoid identical fingerprints."""
    return {
        "width": _BASE_VIEWPORT["width"] + random.randint(-30, 30),
        "height": _BASE_VIEWPORT["height"] + random.randint(-20, 20),
    }


# ── Stealth-enhancing Chromium launch args ──

_STEALTH_ARGS: list[str] = [
    # Core stealth
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    # Disable features that leak automation status
    "--disable-features=IsolateOrigins,site-per-process,TranslateUI,BlinkGenPropertyTrees",
    "--disable-site-isolation-trials",
    "--disable-component-extensions-with-background-pages",
    "--disable-client-side-phishing-detection",
    "--disable-sync",
    "--disable-default-apps",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-breakpad",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--disable-domain-reliability",
    "--disable-ipc-flooding-protection",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-extensions",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--force-color-profile=srgb",
    # Window size
    "--window-size=1280,800",
    # WebGL
    "--enable-webgl",
    "--use-gl=desktop",
]

# ── Extra HTTP headers to set on every page ──

_EXTRA_HEADERS: dict[str, str] = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "DNT": "1",
}


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


async def launch_browser(pw, viewport: dict | None = None) -> BrowserContext:
    """Launch a headless Chromium browser with comprehensive stealth settings.

    Uses a persistent user data directory so browser fingerprint, localStorage,
    and session data remain consistent across restarts.
    """
    os.makedirs(BROWSER_DATA_DIR, exist_ok=True)

    context = await pw.chromium.launch_persistent_context(
        BROWSER_DATA_DIR,
        args=_STEALTH_ARGS + [
            # Use the new headless mode (less detectable than old headless)
            "--headless=new",
        ],
        ignore_default_args=["--enable-automation"],
        user_agent=USER_AGENT,
        viewport=viewport or _randomized_viewport(),
        locale="en-US",
        timezone_id="America/New_York",
        bypass_csp=True,
    )
    await context.add_init_script(STEALTH_JS)
    return context


async def setup_page_stealth(page: Page) -> None:
    """Apply per-page stealth: extra HTTP headers and behavioral setup."""
    await page.set_extra_http_headers(_EXTRA_HEADERS)


async def human_delay(page, min_ms: int = 500, max_ms: int = 1500) -> None:
    """Add a small random delay to mimic human interaction timing."""
    await page.wait_for_timeout(random.randint(min_ms, max_ms))


async def simulate_human_behavior(page: Page) -> None:
    """Simulate subtle human-like behavior: micro-scrolls and mouse movement."""
    try:
        # Small random scroll
        scroll_y = random.randint(10, 80)
        await page.evaluate(f"window.scrollBy(0, {scroll_y})")
        await page.wait_for_timeout(random.randint(100, 300))

        # Random mouse movement to a non-interactive area
        vp = page.viewport_size
        if vp:
            x = random.randint(100, vp["width"] - 100)
            y = random.randint(100, vp["height"] - 100)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
    except Exception:
        pass


async def take_debug_screenshot(page: Page, label: str) -> str | None:
    """Take a debug screenshot if SCREENSHOTS_DIR is configured.

    Returns the file path or None if screenshots are disabled.
    """
    if not SCREENSHOTS_DIR:
        return None
    try:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S%f")[:12]
        fname = f"{label}_{ts}.png"
        fpath = os.path.join(SCREENSHOTS_DIR, fname)
        await page.screenshot(path=fpath, full_page=False)
        return fpath
    except Exception:
        return None


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
