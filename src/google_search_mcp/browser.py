"""Browser lifecycle management — launch, cookies, stealth, consent dismissal.

Provides a single `launch_browser()` entry point that creates a hardened
Playwright browser context with stealth patches, cookie persistence, and
human-like interaction delays.
"""

import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone

from playwright.async_api import BrowserContext, Page

from .config import (
    BROWSER_DATA_DIR,
    COOKIE_JSON_PATH,
    COOKIE_DIR,
    ENABLE_MANUAL_INTERVENTION,
    MANUAL_INTERVENTION_POLL_SEC,
    MANUAL_INTERVENTION_REASONS,
    MANUAL_INTERVENTION_TIMEOUT_SEC,
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


async def launch_browser(pw, viewport: dict | None = None, headless: bool = True) -> BrowserContext:
    """Launch a Chromium browser with comprehensive stealth settings.

    Uses a persistent user data directory so browser fingerprint, localStorage,
    and session data remain consistent across restarts.

    Args:
        pw: Playwright instance.
        viewport: Optional viewport dict. Randomized if None.
        headless: If True (default), runs headless with stealth patches.
                  If False, runs headful (visible window) without stealth.
    """
    os.makedirs(BROWSER_DATA_DIR, exist_ok=True)

    if headless:
        context = await pw.chromium.launch_persistent_context(
            BROWSER_DATA_DIR,
            args=_STEALTH_ARGS + [
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
    else:
        # Headful mode — visible browser for manual login, no stealth
        context = await pw.chromium.launch_persistent_context(
            BROWSER_DATA_DIR,
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,800",
            ],
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
            bypass_csp=True,
        )
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


# ═══════════════════════════════════════════════════════════════════════════
# Startup cookie validation
# ═══════════════════════════════════════════════════════════════════════════

# Essential Google auth cookies that must be present for logged-in access
_REQUIRED_AUTH_COOKIES: set[str] = {
    "SID",
    "SAPISID",
    "APISID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
}


async def validate_google_cookies() -> dict:
    """Validate Google cookies at startup — check existence, expiry, and login state.

    Launches a temporary headless browser, loads cookies, and verifies that
    Google shows a logged-in session. YouTube is checked but only warns on failure.

    Returns a dict with:
        valid (bool): True if Google cookies are valid and show logged-in state
        google (dict): Google validation details
        youtube (dict): YouTube validation details
        errors (list[str]): Fatal errors (Google-related)
        warnings (list[str]): Non-fatal warnings (YouTube-related)
    """
    result: dict = {
        "valid": True,
        "google": {"file_found": False, "cookie_count": 0, "auth_cookies_present": [], "auth_cookies_missing": [], "expired_cookies": [], "logged_in": False, "blocked": False},
        "youtube": {"file_found": False, "cookie_count": 0, "logged_in": False, "blocked": False},
        "errors": [],
        "warnings": [],
    }

    now_ts = time.time()

    # ── Check google_cookies.txt ──
    google_path = os.path.join(COOKIE_DIR, "google_cookies.txt")
    if not os.path.isfile(google_path):
        result["errors"].append(
            f"Missing cookie file: {google_path}. "
            "Export cookies from Chrome using export_cookies.js or a browser extension."
        )
        result["valid"] = False
        # Can't proceed without the file
        return result

    result["google"]["file_found"] = True
    google_cookies = _parse_netscape_cookie_file(google_path)
    result["google"]["cookie_count"] = len(google_cookies)

    # Check expiry of each cookie
    auth_found: set[str] = set()
    for c in google_cookies:
        name = c.get("name", "")
        if name in _REQUIRED_AUTH_COOKIES:
            auth_found.add(name)
        expiry = c.get("expires", 0)
        if expiry > 0 and expiry < now_ts:
            result["google"]["expired_cookies"].append(
                f"{name} expired at {datetime.fromtimestamp(expiry).strftime('%Y-%m-%d %H:%M:%S')}"
            )

    result["google"]["auth_cookies_present"] = sorted(auth_found)
    result["google"]["auth_cookies_missing"] = sorted(_REQUIRED_AUTH_COOKIES - auth_found)

    if result["google"]["auth_cookies_missing"]:
        result["errors"].append(
            f"Missing required Google auth cookies: {', '.join(result['google']['auth_cookies_missing'])}. "
            "Re-export cookies from a logged-in Chrome session."
        )
        result["valid"] = False

    if result["google"]["expired_cookies"]:
        result["errors"].append(
            f"Expired Google cookies: {'; '.join(result['google']['expired_cookies'])}. "
            "Re-export fresh cookies from a logged-in Chrome session."
        )
        result["valid"] = False

    # ── Check youtube_cookies.txt (optional) ──
    youtube_path = os.path.join(COOKIE_DIR, "youtube_cookies.txt")
    if os.path.isfile(youtube_path):
        result["youtube"]["file_found"] = True
        youtube_cookies = _parse_netscape_cookie_file(youtube_path)
        result["youtube"]["cookie_count"] = len(youtube_cookies)
    else:
        result["warnings"].append(
            f"Missing cookie file: {youtube_path}. YouTube features may not work."
        )

    # ── Browser-based login state check ──
    # Only proceed if file-level checks passed
    if not result["valid"]:
        return result

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            context = await launch_browser(pw)
            await load_cookies(context)

            # ── Check Google login state ──
            google_page = await context.new_page()
            await setup_page_stealth(google_page)
            try:
                await google_page.goto(
                    "https://www.google.com",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await dismiss_consent(google_page)
                await human_delay(google_page, min_ms=1000, max_ms=2000)

                # Check for logged-in indicators
                logged_in = await google_page.evaluate(
                    """
                    () => {
                        const body = document.body?.innerText || '';

                        // Check for account avatar / profile button
                        const accountBtn = document.querySelector(
                            'a[aria-label*="Account"], ' +
                            'a[aria-label*="profile"], ' +
                            'a[aria-label*="Google"], ' +
                            'img[alt*="Google Account"], ' +
                            'button[aria-label*="Account"], ' +
                            '#gb_70, ' +  // Sign in button (present when NOT logged in)
                            'a[href*="SignOut"], ' +
                            'a[href*="signout"], ' +
                            'a[href*="Logout"]'
                        );

                        // If we see a "Sign in" button, we're NOT logged in
                        const signInBtn = document.querySelector(
                            'a[aria-label*="Sign in"], ' +
                            'a[aria-label*="Sign In"], ' +
                            '#gb_70'
                        );

                        // Check for account avatar image
                        const avatarImg = document.querySelector(
                            'img[src*="googleusercontent.com"], ' +
                            'img[class*="gb_C"], ' +
                            'img[class*="gb_ua"]'
                        );

                        // Check body text for logged-in indicators
                        const hasSignOut = body.includes('Sign out') || body.includes('Sign Out');
                        const hasAccount = body.includes('Google Account');

                        return {
                            loggedIn: (!!avatarImg || hasSignOut || hasAccount) && !signInBtn,
                            hasSignInButton: !!signInBtn,
                            hasAvatar: !!avatarImg,
                            hasSignOutText: hasSignOut,
                            hasAccountText: hasAccount,
                        };
                    }
                    """
                )

                result["google"]["logged_in"] = logged_in.get("loggedIn", False)
                if not result["google"]["logged_in"]:
                    result["errors"].append(
                        "Google does not show a logged-in session. "
                        "The browser saw a sign-in page or guest view. "
                        "Re-export cookies from a Chrome session where you are actively logged into Google."
                    )
                    result["valid"] = False
                else:
                    # Check if we're blocked by CAPTCHA
                    body_text = await google_page.evaluate("document.body?.innerText?.toLowerCase() || ''")
                    if "unusual traffic" in body_text or "captcha" in body_text:
                        result["google"]["blocked"] = True
                        result["errors"].append(
                            "Google is showing a CAPTCHA or 'unusual traffic' page even with cookies. "
                            "The cookies may be rate-limited or invalid. Re-export fresh cookies."
                        )
                        result["valid"] = False

            except Exception as e:
                result["errors"].append(f"Error checking Google login state: {e}")
                result["valid"] = False
            finally:
                await google_page.close()

            # ── Check YouTube login state (warn only) ──
            if result["youtube"]["file_found"]:
                yt_page = await context.new_page()
                await setup_page_stealth(yt_page)
                try:
                    await yt_page.goto(
                        "https://www.youtube.com",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await human_delay(yt_page, min_ms=1000, max_ms=2000)

                    yt_logged_in = await yt_page.evaluate(
                        """
                        () => {
                            const body = document.body?.innerText || '';

                            // YouTube avatar button (logged-in indicator)
                            const avatarBtn = document.querySelector(
                                '#avatar-btn, ' +
                                'yt-img-shadow#avatar, ' +
                                'img[src*="googleusercontent.com"], ' +
                                'button#avatar-button'
                            );

                            // Sign in button (present when NOT logged in)
                            const signInBtn = document.querySelector(
                                'a[aria-label*="Sign in"], ' +
                                '#buttons ytd-button-renderer a'
                            );

                            return {
                                loggedIn: !!avatarBtn && !signInBtn,
                                hasAvatar: !!avatarBtn,
                                hasSignInButton: !!signInBtn,
                            };
                        }
                        """
                    )

                    result["youtube"]["logged_in"] = yt_logged_in.get("loggedIn", False)
                    if not result["youtube"]["logged_in"]:
                        result["warnings"].append(
                            "YouTube does not show a logged-in session. "
                            "YouTube features (subscriptions, feed checks) may not work correctly. "
                            "Re-export youtube_cookies.txt from a logged-in Chrome session."
                        )

                    # Check for YouTube CAPTCHA
                    yt_body = await yt_page.evaluate("document.body?.innerText?.toLowerCase() || ''")
                    if "unusual traffic" in yt_body or "captcha" in yt_body:
                        result["youtube"]["blocked"] = True
                        result["warnings"].append(
                            "YouTube is showing a CAPTCHA or 'unusual traffic' page. "
                            "YouTube features may be blocked. Re-export fresh cookies."
                        )

                except Exception as e:
                    result["warnings"].append(f"Could not verify YouTube login state: {e}")
                finally:
                    await yt_page.close()

            await context.close()

    except Exception as e:
        result["errors"].append(f"Browser validation error: {e}")
        result["valid"] = False

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Headful login flow — open browser for manual login when cookies are invalid
# ═══════════════════════════════════════════════════════════════════════════

_LOGIN_POLL_INTERVAL_SEC = 10.0
_LOGIN_TIMEOUT_SEC = 300  # 5 minutes


def _write_netscape_cookie_file(filepath: str, cookies: list[dict], source_domain: str) -> None:
    """Write Playwright cookie dicts to a Netscape-format cookie file.

    This is the inverse of _parse_netscape_cookie_file.
    """
    now_ts = int(time.time())
    default_expiry = now_ts + 365 * 86400  # 1 year from now

    lines = [
        "# Netscape HTTP Cookie File",
        "# https://curl.se/rfc/cookie_spec.html",
        "# Exported by Google MCP Cookie Exporter (auto-login)",
        f"# Source: {source_domain}",
        f"# Date: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z",
        f"# Cookies: {len(cookies)}",
        "#",
        "# domain  domain_flag  path  secure  expiry  name  value",
        "",
    ]

    for c in cookies:
        domain = c.get("domain", "")
        if not domain.startswith("."):
            domain = "." + domain
        path = c.get("path", "/")
        secure = c.get("secure", False)
        expiry = c.get("expires", 0)
        if not expiry or expiry <= 0:
            expiry = default_expiry
        name = c.get("name", "")
        value = c.get("value", "")
        lines.append(
            "\t".join([
                domain,
                "TRUE",
                path,
                "TRUE" if secure else "FALSE",
                str(int(expiry)),
                name,
                value,
            ])
        )

    content = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w") as f:
        f.write(content)


async def login_and_save_cookies() -> bool:
    """Open a headful browser for the user to manually log into Google.

    The browser window appears visibly (not headless). The user logs into
    their Google account manually. Once login is detected, cookies are
    saved to google_cookies.txt and youtube_cookies.txt, plus the auto-save
    JSON path.

    Returns True if login was successful and cookies were saved.
    """
    print("=" * 60, file=sys.stderr, flush=True)
    print("  OPENING HEADFUL BROWSER FOR GOOGLE LOGIN", file=sys.stderr, flush=True)
    print("=" * 60, file=sys.stderr, flush=True)
    print("", file=sys.stderr, flush=True)
    print("A browser window will open. Please:", file=sys.stderr, flush=True)
    print("  1. Log into your Google account in the browser", file=sys.stderr, flush=True)
    print("  2. If prompted, complete any CAPTCHA/verification", file=sys.stderr, flush=True)
    print("  3. The system will detect your login automatically", file=sys.stderr, flush=True)
    print(f"  4. Timeout: {_LOGIN_TIMEOUT_SEC // 60} minutes", file=sys.stderr, flush=True)
    print("", file=sys.stderr, flush=True)
    print("Close the browser window to cancel.", file=sys.stderr, flush=True)
    print("", file=sys.stderr, flush=True)

    # On Linux, check that DISPLAY is set (headful browser needs it)
    if sys.platform == "linux" and not os.environ.get("DISPLAY"):
        print(
            "ERROR: No DISPLAY environment variable found. "
            "A headful browser cannot open without a graphical display. "
            "Run this on a machine with a desktop environment, or use SSH with -X.",
            file=sys.stderr, flush=True,
        )
        return False

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            # ── Launch headful browser using the shared launcher ──
            context = await launch_browser(pw, headless=False)
            page = await context.new_page()

            try:
                # ── Navigate to Google ──
                await page.goto(
                    "https://www.google.com",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )

                # ── Poll for login state ──
                start_time = time.time()
                logged_in = False

                while time.time() - start_time < _LOGIN_TIMEOUT_SEC:
                    await page.wait_for_timeout(int(_LOGIN_POLL_INTERVAL_SEC * 1000))

                    # Check if page is still alive
                    try:
                        state = await page.evaluate(
                            """
                            () => {
                                const body = document.body?.innerText || '';

                                // Logged-in indicators
                                const avatarImg = document.querySelector(
                                    'img[src*="googleusercontent.com"], ' +
                                    'img[class*="gb_C"], ' +
                                    'img[class*="gb_ua"]'
                                );
                                const signInBtn = document.querySelector(
                                    'a[aria-label*="Sign in"], ' +
                                    'a[aria-label*="Sign In"], ' +
                                    '#gb_70'
                                );
                                const hasSignOut = body.includes('Sign out') || body.includes('Sign Out');
                                const hasAccount = body.includes('Google Account');

                                const isLoggedIn = (!!avatarImg || hasSignOut || hasAccount) && !signInBtn;

                                return {
                                    loggedIn: isLoggedIn,
                                    hasAvatar: !!avatarImg,
                                    hasSignInButton: !!signInBtn,
                                    hasSignOutText: hasSignOut,
                                    hasAccountText: hasAccount,
                                };
                            }
                            """
                        )
                    except Exception:
                        # Page might have been closed
                        print("Browser window was closed. Cancelling login flow.", file=sys.stderr, flush=True)
                        return False

                    if state.get("loggedIn"):
                        logged_in = True
                        print("\n✅ Google login detected!", file=sys.stderr, flush=True)
                        break

                    elapsed = int(time.time() - start_time)
                    print(
                        f"  ⏳ Waiting for Google login... ({elapsed}s elapsed)",
                        file=sys.stderr, flush=True,
                    )

                if not logged_in:
                    print("\n❌ Login timeout reached. No login detected.", file=sys.stderr, flush=True)
                    return False

                # ── Small delay for cookies to settle ──
                await page.wait_for_timeout(2000)

                # ── Save Google cookies ──
                google_cookies = await context.cookies(urls=["https://www.google.com", "https://google.com"])
                google_path = os.path.join(COOKIE_DIR, "google_cookies.txt")
                _write_netscape_cookie_file(google_path, google_cookies, "www.google.com")
                print(f"  💾 Saved {len(google_cookies)} Google cookies to {google_path}", file=sys.stderr, flush=True)

                # ── Navigate to YouTube to capture YouTube-specific cookies ──
                print("  📺 Navigating to YouTube to capture YouTube cookies...", file=sys.stderr, flush=True)
                try:
                    await page.goto(
                        "https://www.youtube.com",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await page.wait_for_timeout(3000)

                    youtube_cookies = await context.cookies(urls=["https://www.youtube.com", "https://youtube.com"])
                    youtube_path = os.path.join(COOKIE_DIR, "youtube_cookies.txt")
                    _write_netscape_cookie_file(youtube_path, youtube_cookies, "www.youtube.com")
                    print(f"  💾 Saved {len(youtube_cookies)} YouTube cookies to {youtube_path}", file=sys.stderr, flush=True)
                except Exception as e:
                    print(f"  ⚠️  Could not capture YouTube cookies: {e}", file=sys.stderr, flush=True)

                # ── Also save to auto-save JSON path ──
                all_cookies = await context.cookies()
                try:
                    with open(COOKIE_JSON_PATH, "w") as f:
                        json.dump(all_cookies, f)
                    print(f"  💾 Saved {len(all_cookies)} total cookies to {COOKIE_JSON_PATH}", file=sys.stderr, flush=True)
                except Exception as e:
                    print(f"  ⚠️  Could not save JSON cookies: {e}", file=sys.stderr, flush=True)

                print("\n✅ Login complete! Cookies saved successfully.", file=sys.stderr, flush=True)
                return True

            finally:
                await context.close()

    except Exception as e:
        print(f"\n❌ Error during login flow: {e}", file=sys.stderr, flush=True)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Runtime manual intervention — open headful browser when bot detection fires
# ═══════════════════════════════════════════════════════════════════════════
# This is the "smart fallback" requested by users: when stealth + auto
# CAPTCHA solve + retries have all failed, instead of going straight to
# DuckDuckGo we open a *visible* browser window and pause the request.
# The user can solve the CAPTCHA, log into Google, or complete 2FA
# directly, and the original request automatically retries once the
# page no longer looks blocked.

# Concurrency: only one headful intervention at a time across the server.
_manual_intervention_lock = threading.Lock()
_manual_intervention_active: bool = False
_last_intervention_result: dict = {
    "timestamp": 0.0,
    "resolved": False,
    "reason": "",
    "url": "",
    "elapsed_sec": 0.0,
}


def is_manual_intervention_active() -> bool:
    """Return True if a headful intervention window is currently open."""
    return _manual_intervention_active


def get_last_intervention_result() -> dict:
    """Return metadata about the most recent intervention (for diagnostics)."""
    return dict(_last_intervention_result)


def _check_display_available() -> bool:
    """Check if a graphical display is available for a headful browser.

    On Linux we need DISPLAY (X11) or WAYLAND_DISPLAY. On macOS / Windows
    a display is always assumed (the user is on a desktop).
    """
    if sys.platform.startswith("linux"):
        return bool(
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        )
    # macOS, Windows, BSD
    return True


async def is_login_required(page) -> bool:
    """Detect whether the current page is a Google sign-in page.

    Returns True if the page looks like a Google login flow (email/password
    input, sign-in button, or accounts.google.com signin URL).
    """
    try:
        url = page.url.lower()
        if "accounts.google.com" in url and ("signin" in url or "v3/signin" in url or "identifier" in url):
            return True
        state = await page.evaluate(
            """
            () => {
                const body = (document.body?.innerText || '').toLowerCase();
                const hasEmail = !!document.querySelector(
                    'input[type="email"], input[name="identifier"], ' +
                    'input[autocomplete="username"], input#identifierId'
                );
                const hasPassword = !!document.querySelector(
                    'input[type="password"], input[name="password"]'
                );
                const isAccountsDomain = location.hostname === 'accounts.google.com';
                const hasSignInText = body.includes('sign in') || body.includes('signin');
                const hasChooseAccount = body.includes('choose an account') ||
                                         body.includes('choose account');
                return {
                    hasEmail, hasPassword, isAccountsDomain, hasSignInText, hasChooseAccount,
                };
            }
            """
        )
        # On accounts.google.com, treat any of these as "login required"
        if state.get("isAccountsDomain"):
            return bool(
                state.get("hasEmail")
                or state.get("hasPassword")
                or state.get("hasSignInText")
                or state.get("hasChooseAccount")
            )
        # Off-domain: only consider it a login page if it has clear sign-in UI
        return bool(
            (state.get("hasEmail") or state.get("hasPassword"))
            and state.get("hasSignInText")
        )
    except Exception:
        return False


async def detect_block_reason(page) -> str:
    """Classify why a page is blocked.

    Returns one of the strings in MANUAL_INTERVENTION_REASONS.
    """
    try:
        url = page.url.lower()
        state = await page.evaluate(
            """
            () => {
                const body = (document.body?.innerText || '').toLowerCase();
                return {
                    body,
                    hasRecaptcha: !!document.querySelector('iframe[src*="recaptcha"]'),
                    hasImageChallenge: !!document.querySelector(
                        'table.rc-imageselect-table, .rc-imageselect-target'
                    ),
                    hasUnusualTraffic: body.includes('unusual traffic') ||
                                       body.includes('our systems have detected'),
                    hasNotARobot: body.includes('not a robot') ||
                                  body.includes('i\\'m not a robot'),
                    hasChooseAccount: body.includes('choose an account') ||
                                      body.includes('choose account'),
                    hasSignIn: body.includes('sign in') && !body.includes('signing'),
                    hasEmail: !!document.querySelector(
                        'input[type="email"], input[name="identifier"], ' +
                        'input[autocomplete="username"], input#identifierId'
                    ),
                    hasPassword: !!document.querySelector(
                        'input[type="password"], input[name="password"]'
                    ),
                    hasVerification: body.includes('2-step verification') ||
                                     body.includes('verify it') ||
                                     body.includes('verify it\'s you') ||
                                     body.includes('enter the code'),
                    hasConsent: body.includes('before you continue') ||
                                body.includes('consent') ||
                                body.includes('i agree'),
                };
            }
            """
        )
        if "accounts.google.com" in url and (state.get("hasEmail") or state.get("hasPassword")):
            return "login"
        if state.get("hasRecaptcha") or state.get("hasImageChallenge"):
            return "captcha"
        if state.get("hasUnusualTraffic") or "/sorry/" in url:
            return "rate_limit"
        if state.get("hasVerification"):
            return "verification"
        if state.get("hasConsent") and "consent.google.com" in url:
            return "consent"
        if state.get("hasSignIn") and state.get("hasEmail"):
            return "login"
        if state.get("hasChooseAccount"):
            return "login"
        return "unknown"
    except Exception:
        return "unknown"


async def manual_intervention_for_block(
    pw,
    blocked_url: str,
    reason: str = "unknown",
    initial_cookies: list[dict] | None = None,
    timeout_sec: int | None = None,
) -> list[dict] | None:
    """Open a headful (visible) browser so the user can manually resolve a block.

    This is the runtime equivalent of `login_and_save_cookies` — invoked
    when anti-bot measures (stealth, auto CAPTCHA solve, retries) have
    all failed and we want the user to take over before falling back to
    a non-Google search provider.

    Reuses the same persistent browser profile, so any cookies/sessions
    already saved are loaded automatically. The user can:

      - Solve a CAPTCHA / image challenge that the neural net couldn't
      - Log into their Google account (if the session expired)
      - Complete 2FA / phone verification
      - Accept a consent dialog

    The function blocks (up to `timeout_sec` seconds) until the user
    resolves the page, closes the browser (cancel), or the timeout
    expires. Cookies are saved to disk and returned on success so the
    caller can apply them to its headless context and retry the request.

    Args:
        pw: The active Playwright instance (must outlive this call).
        blocked_url: The URL the user was blocked on.
        reason: Why we think we're blocked (one of MANUAL_INTERVENTION_REASONS).
        initial_cookies: Cookies from the failed headless context. Transferred
                         to the headful session so the user doesn't lose their state.
        timeout_sec: Override MANUAL_INTERVENTION_TIMEOUT_SEC for this call.

    Returns:
        A list of fresh cookies (from the headful session) if the user
        resolved the block, or None if cancelled / timed out.
    """
    global _manual_intervention_active, _last_intervention_result

    if not ENABLE_MANUAL_INTERVENTION:
        print(
            "MANUAL INTERVENTION: disabled via ENABLE_MANUAL_INTERVENTION=0. "
            "Falling back to alternative search provider.",
            file=sys.stderr, flush=True,
        )
        _last_intervention_result.update({
            "timestamp": time.time(),
            "resolved": False,
            "reason": reason,
            "url": blocked_url,
            "elapsed_sec": 0.0,
            "outcome": "disabled",
        })
        return None

    if not _check_display_available():
        print(
            "MANUAL INTERVENTION: no graphical display detected (DISPLAY / "
            "WAYLAND_DISPLAY unset). A headful browser cannot be opened. "
            "Falling back to alternative search provider. To enable, run the "
            "server on a machine with a desktop session or use X-forwarding.",
            file=sys.stderr, flush=True,
        )
        _last_intervention_result.update({
            "timestamp": time.time(),
            "resolved": False,
            "reason": reason,
            "url": blocked_url,
            "elapsed_sec": 0.0,
            "outcome": "no_display",
        })
        return None

    if not _manual_intervention_lock.acquire(blocking=False):
        print(
            "MANUAL INTERVENTION: another headful window is already open. "
            "Skipping to avoid multiple windows.",
            file=sys.stderr, flush=True,
        )
        _last_intervention_result.update({
            "timestamp": time.time(),
            "resolved": False,
            "reason": reason,
            "url": blocked_url,
            "elapsed_sec": 0.0,
            "outcome": "concurrent_lock",
        })
        return None

    _manual_intervention_active = True
    start_time = time.time()
    headful_context = None
    resolved = False
    new_cookies: list[dict] | None = None

    if timeout_sec is None:
        timeout_sec = MANUAL_INTERVENTION_TIMEOUT_SEC

    try:
        # ── Human-friendly reason text ──
        reason_text = {
            "captcha": "CAPTCHA challenge (reCAPTCHA / image selection)",
            "login": "Google sign-in required",
            "rate_limit": "rate limit / 'unusual traffic from your computer network'",
            "consent": "consent dialog",
            "verification": "2-step verification / phone / identity check",
            "unknown": "blocked page (specific reason not identified)",
        }.get(reason, "blocked page")

        # ── Big banner so the user can't miss it ──
        timeout_min = (timeout_sec // 60) if timeout_sec > 0 else "∞"
        print("", file=sys.stderr, flush=True)
        print("╔" + "═" * 68 + "╗", file=sys.stderr, flush=True)
        print("║  ⚠️  GOOGLE BOT DETECTION — MANUAL INTERVENTION REQUIRED" + " " * 7 + "║", file=sys.stderr, flush=True)
        print("╚" + "═" * 68 + "╝", file=sys.stderr, flush=True)
        print("", file=sys.stderr, flush=True)
        print(f"  Reason:    {reason_text}", file=sys.stderr, flush=True)
        print(f"  URL:       {blocked_url}", file=sys.stderr, flush=True)
        print(f"  Timeout:   {timeout_min} min (0 = wait forever)", file=sys.stderr, flush=True)
        print("", file=sys.stderr, flush=True)
        print("  A browser window is opening now. In it, please:", file=sys.stderr, flush=True)
        print("    • solve the CAPTCHA / image challenge, OR", file=sys.stderr, flush=True)
        print("    • log into your Google account, OR", file=sys.stderr, flush=True)
        print("    • complete 2-step verification / phone check, OR", file=sys.stderr, flush=True)
        print("    • accept any consent dialog that appears.", file=sys.stderr, flush=True)
        print("", file=sys.stderr, flush=True)
        print("  The system will detect when the page is unblocked and will", file=sys.stderr, flush=True)
        print("  automatically retry your request with the fresh cookies.", file=sys.stderr, flush=True)
        print("  To cancel, close the browser window.", file=sys.stderr, flush=True)
        print("", file=sys.stderr, flush=True)

        # ── Snapshot current cookies for transfer ──
        if initial_cookies is None:
            initial_cookies = []
        try:
            with open(COOKIE_JSON_PATH, "w") as f:
                json.dump(initial_cookies, f)
        except Exception:
            pass

        # ── Launch headful browser ──
        try:
            headful_context = await launch_browser(pw, headless=False)
        except Exception as e:
            print(f"  ❌ Could not launch headful browser: {e}", file=sys.stderr, flush=True)
            _last_intervention_result.update({
                "timestamp": time.time(),
                "resolved": False,
                "reason": reason,
                "url": blocked_url,
                "elapsed_sec": time.time() - start_time,
                "outcome": "launch_failed",
            })
            return None

        # Transfer cookies so the user doesn't lose their session
        if initial_cookies:
            try:
                await headful_context.add_cookies(initial_cookies)
            except Exception as e:
                print(f"  ⚠️  Could not transfer cookies: {e}", file=sys.stderr, flush=True)

        headful_page = await headful_context.new_page()

        # ── Navigate to the blocked URL ──
        print(f"  Opening {blocked_url} in headful browser...", file=sys.stderr, flush=True)
        try:
            await headful_page.goto(blocked_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠️  Navigation warning: {e}", file=sys.stderr, flush=True)

        # Give the page a moment to settle
        await human_delay(headful_page, min_ms=1500, max_ms=3000)

        # ── Poll for resolution ──
        print("  Watching for resolution...", file=sys.stderr, flush=True)
        effective_timeout = timeout_sec if timeout_sec and timeout_sec > 0 else float("inf")
        last_status: str | None = None
        last_logged: float = 0.0

        while time.time() - start_time < effective_timeout:
            await headful_page.wait_for_timeout(int(MANUAL_INTERVENTION_POLL_SEC * 1000))

            try:
                state = await headful_page.evaluate(
                    """
                    () => {
                        const body = (document.body?.innerText || '').toLowerCase();
                        const url = location.href.toLowerCase();
                        const isCaptcha =
                            url.includes('/sorry/') ||
                            body.includes('unusual traffic') ||
                            body.includes('not a robot') ||
                            !!document.querySelector('iframe[src*="recaptcha"]');
                        const isLogin =
                            (url.includes('accounts.google.com') &&
                             (body.includes('sign in') || body.includes('choose an account'))) ||
                            (!!document.querySelector('input[type="email"], input[name="identifier"]') &&
                             body.includes('sign in'));
                        const isVerification =
                            body.includes('2-step verification') ||
                            body.includes('verify it\'s you') ||
                            body.includes('enter the code we');
                        const hasSearchBox = !!document.querySelector(
                            'input[name="q"], textarea[name="q"], input[title="search"]'
                        );
                        const hasResults = document.querySelectorAll(
                            'div#search div.g, #rso div.g, #rso div.MjjYud, h3'
                        ).length > 0;
                        const hasAvatar = !!document.querySelector(
                            'img[src*="googleusercontent.com"], ' +
                            'button#avatar-button, ' +
                            'a[aria-label*="Google Account"]'
                        );
                        return {
                            isCaptcha, isLogin, isVerification,
                            hasSearchBox, hasResults, hasAvatar,
                        };
                    }
                    """
                )
            except Exception:
                # Page/browser closed by user
                print("", file=sys.stderr, flush=True)
                print("  ❌ Browser window was closed. Cancelling intervention.", file=sys.stderr, flush=True)
                break

            blocked = state.get("isCaptcha") or state.get("isLogin") or state.get("isVerification")
            looks_ok = state.get("hasSearchBox") or state.get("hasResults") or state.get("hasAvatar")

            if not blocked and looks_ok:
                resolved = True
                print("", file=sys.stderr, flush=True)
                print("  ✅ Block resolved! Saving cookies and retrying...", file=sys.stderr, flush=True)
                break

            # Log a status update if the status changed, or every 15s
            if blocked:
                status = (
                    "captcha" if state.get("isCaptcha")
                    else "login" if state.get("isLogin")
                    else "verification"
                )
            else:
                status = "loading"
            now = time.time()
            if status != last_status or (now - last_logged) >= 15.0:
                elapsed = int(now - start_time)
                print(f"  ⏳ Waiting for you to solve... ({elapsed}s, page status: {status})", file=sys.stderr, flush=True)
                last_status = status
                last_logged = now

        else:
            # Timeout
            print("", file=sys.stderr, flush=True)
            print(f"  ⏱  Intervention timed out after {int(time.time() - start_time)}s.", file=sys.stderr, flush=True)

        # ── Save cookies even if not resolved — user may have made progress ──
        try:
            new_cookies = await headful_context.cookies()
            with open(COOKIE_JSON_PATH, "w") as f:
                json.dump(new_cookies, f)
            if resolved:
                # Also refresh google_cookies.txt from the new state
                google_path = os.path.join(COOKIE_DIR, "google_cookies.txt")
                google_cookies = [
                    c for c in new_cookies
                    if "google" in c.get("domain", "").lower()
                    or c.get("domain", "").endswith(".google.com")
                    or c.get("domain", "").endswith(".youtube.com")
                ]
                if google_cookies:
                    _write_netscape_cookie_file(google_path, google_cookies, "www.google.com")
                    print(f"  💾 Updated {google_path} ({len(google_cookies)} cookies)", file=sys.stderr, flush=True)
                print(f"  💾 Saved {len(new_cookies)} cookies to {COOKIE_JSON_PATH}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"  ⚠️  Could not save cookies: {e}", file=sys.stderr, flush=True)
            new_cookies = None

        # Give the user a moment to read the message
        if resolved:
            await headful_page.wait_for_timeout(500)

        elapsed = time.time() - start_time
        _last_intervention_result.update({
            "timestamp": time.time(),
            "resolved": resolved,
            "reason": reason,
            "url": blocked_url,
            "elapsed_sec": elapsed,
            "outcome": "resolved" if resolved else "timeout_or_cancelled",
        })
        return new_cookies if resolved else None

    except Exception as e:
        elapsed = time.time() - start_time
        print(f"  ❌ Unexpected error during manual intervention: {e}", file=sys.stderr, flush=True)
        _last_intervention_result.update({
            "timestamp": time.time(),
            "resolved": False,
            "reason": reason,
            "url": blocked_url,
            "elapsed_sec": elapsed,
            "outcome": f"error: {e}",
        })
        return None
    finally:
        if headful_context is not None:
            try:
                await headful_context.close()
            except Exception:
                pass
        _manual_intervention_active = False
        _manual_intervention_lock.release()
