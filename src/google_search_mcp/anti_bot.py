"""Centralized anti-bot helper — browse_google() async context manager.

Eliminates the massive code duplication across 15+ tools where every tool
has its own copy of: browser launch → cookie load → CAPTCHA detect →
CAPTCHA solve → warm-up retry → block detection → fallback → cookie save.

Usage:

    async with browse_google(url) as page:
        if page is None:
            return "Blocked, fallback used or error message"
        # ... your scraping logic here ...

The helper handles all retry/block/fallback logic internally.

Smart bot-detection handling:
    1. Launch headless Chromium with stealth patches and existing cookies
    2. Navigate to the target Google URL
    3. Detect block: CAPTCHA, login page, rate limit, verification, etc.
    4. Attempt automatic CAPTCHA solve (neural net) for image challenges
    5. If still blocked → warm-up retry + a few automatic retries
    6. If still blocked → open a HEADFUL (visible) browser window so the
       user can solve the block manually (CAPTCHA, login, 2FA, ...).
       Wait up to MANUAL_INTERVENTION_TIMEOUT_SEC, then retry the request.
    7. Only if the user can't / won't resolve → fall back to alternative
       search provider (DuckDuckGo, etc.) and return its result.
"""

import asyncio
import random
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Callable

from playwright.async_api import async_playwright

from . import human_sim
from .browser import (
    detect_block_reason,
    dismiss_consent,
    human_delay,
    is_login_required,
    launch_browser,
    load_cookies,
    manual_intervention_for_block,
    save_cookies,
    setup_page_stealth,
    simulate_human_behavior,
    take_debug_screenshot,
    warmup_retry,
)
from .captcha import is_blocked, try_solve_captcha
from .config import (
    ENABLE_MANUAL_INTERVENTION,
    GOOGLE_REQUEST_MIN_GAP,
    MANUAL_INTERVENTION_REASONS,
    MAX_GOOGLE_RETRIES,
    RETRY_BACKOFF_SECONDS,
)


# ── Session-level rate limiter ──

_last_request_time: float = 0.0


async def _enforce_rate_limit() -> None:
    """Ensure minimum gap between consecutive Google requests."""
    global _last_request_time
    if _last_request_time > 0:
        elapsed = time.time() - _last_request_time
        if elapsed < GOOGLE_REQUEST_MIN_GAP:
            wait = GOOGLE_REQUEST_MIN_GAP - elapsed + random.uniform(0.1, 0.5)
            await asyncio.sleep(wait)
    _last_request_time = time.time()


async def _is_any_block(page) -> tuple[bool, str]:
    """Return (blocked, reason). Detects CAPTCHA, login, rate-limit, etc."""
    if await is_blocked(page):
        reason = await detect_block_reason(page)
        return True, reason
    if await is_login_required(page):
        return True, "login"
    return False, ""


async def _try_manual_intervention_flow(
    pw,
    context_holder: list,
    page_holder: list,
    url: str,
    reason: str,
    screenshot_label: str,
) -> bool:
    """Open a headful browser for manual resolution and retry the request.

    The current headless context lives in ``context_holder[0]`` and the
    current page in ``page_holder[0]``. We close that context (so the
    headful session can take over the persistent profile without file
    locks), open the headful window, wait for the user to resolve, then
    re-open a fresh headless context with the new cookies and retry the
    navigation. The new context/page are written back into the holders so
    the caller can use them transparently.

    Returns True if the user resolved the block (and the page is now
    usable), False otherwise. The caller decides what to do on False
    (typically fall back to DuckDuckGo).
    """
    if not ENABLE_MANUAL_INTERVENTION:
        return False

    context = context_holder[0]
    page = page_holder[0]

    # ── Capture current cookies and close headless context ──
    try:
        cookies_before = await context.cookies()
    except Exception:
        cookies_before = []
    try:
        await save_cookies(context)
    except Exception:
        pass
    try:
        await context.close()
    except Exception:
        pass
    context_holder[0] = None
    page_holder[0] = None

    # ── Open headful window for the user to resolve ──
    new_cookies = await manual_intervention_for_block(
        pw,
        blocked_url=url,
        reason=reason,
        initial_cookies=cookies_before,
    )

    if new_cookies is None:
        # User didn't resolve (timeout, closed window, or disabled)
        return False

    # ── Reopen headless context with the new cookies ──
    try:
        new_context = await launch_browser(pw, headless=True)
        try:
            await new_context.add_cookies(new_cookies)
        except Exception as e:
            print(
                f"  ⚠️  Could not apply new cookies to headless context: {e}",
                file=sys.stderr, flush=True,
            )
        new_page = await new_context.new_page()
        await setup_page_stealth(new_page)
        await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await dismiss_consent(new_page)
        await simulate_human_behavior(new_page)
        try:
            await take_debug_screenshot(new_page, f"{screenshot_label}_08_post_manual")
        except Exception:
            pass

        still_blocked, _ = await _is_any_block(new_page)
        if still_blocked:
            try:
                await take_debug_screenshot(new_page, f"{screenshot_label}_09_still_blocked_after_manual")
            except Exception:
                pass
            try:
                await new_context.close()
            except Exception:
                pass
            return False

        # Hand the new context + page back to the caller
        context_holder[0] = new_context
        page_holder[0] = new_page
        return True
    except Exception as e:
        print(
            f"  ❌ Could not reopen headless after manual intervention: {e}",
            file=sys.stderr, flush=True,
        )
        return False


@asynccontextmanager
async def browse_google(
    url: str,
    *,
    retries: int = MAX_GOOGLE_RETRIES,
    fallback_fn: Callable[[], str | list | None] | None = None,
    screenshot_label: str = "page",
) -> AsyncIterator:
    """Async context manager for browsing Google with full anti-bot protection.

    Handles:
      - Browser launch with stealth settings
      - Cookie loading
      - Rate limiting between requests
      - Block detection (CAPTCHA, login page, rate limit, verification)
      - Automatic CAPTCHA solving (neural net for image challenges)
      - Warm-up retry on transient blocks
      - Configurable retries with exponential backoff
      - **Manual intervention** — if everything else fails, opens a headful
        (visible) browser so the user can solve the block manually. Once
        resolved, the request retries automatically.
      - Fallback provider invocation when all attempts fail
      - Debug screenshots (if SCREENSHOTS_DIR is configured)
      - Cookie saving and browser cleanup

    Args:
        url: The Google URL to navigate to.
        retries: Number of retry attempts after initial block (default: 2).
        fallback_fn: Optional callable that returns a fallback result string/list
                     when all retries are exhausted. If None, a standard block
                     message is returned.
        screenshot_label: Label prefix for debug screenshots.

    Yields:
        The Playwright Page object if successful, or None if blocked and
        fallback was used / all retries exhausted.
    """
    await _enforce_rate_limit()

    async with async_playwright() as pw:
        # Mutable holders so the manual-intervention flow can swap in a
        # new context/page mid-function without rebinding nonlocal vars.
        context_holder: list = [None]
        page_holder: list = [None]

        try:
            context_holder[0] = await launch_browser(pw)
            await load_cookies(context_holder[0])
            page_holder[0] = await context_holder[0].new_page()
            await setup_page_stealth(page_holder[0])
            page = page_holder[0]
            context = context_holder[0]

            # ── Anti-detect warmup (first request in this session) ──
            # On a fresh browser context, do a benign search first to
            # build up cookie diversity and trust tokens. This is the
            # single biggest signal reduction for Google reCAPTCHA.
            try:
                from . import anti_detect
                await anti_detect.warmup_session(context, page)
            except Exception as e:
                print(
                    f"[anti_detect] Warmup failed (continuing): {e}",
                    file=sys.stderr, flush=True,
                )

            # ── First attempt ──
            # Use search-via-typing when the URL is a Google /search?q=
            # URL — this is the single biggest anti-bot signal we can fix
            # (real humans go to google.com and type, they don't hit
            # /search?q=... directly).
            from . import anti_detect
            used_typing_flow = False
            if (
                anti_detect.is_google_search_url(url)
                and anti_detect.config.ANTIDETECT_SEARCH_VIA_TYPING
            ):
                query = anti_detect.parse_query_from_url(url)
                if query:
                    print(
                        f"[anti_detect] Using search-via-typing for: {query!r}",
                        file=sys.stderr, flush=True,
                    )
                    typed_ok = await anti_detect.search_via_typing(page, query)
                    used_typing_flow = typed_ok
                    if not typed_ok:
                        # Fall back to direct goto
                        print(
                            "[anti_detect] Typing flow failed, "
                            "falling back to direct goto",
                            file=sys.stderr, flush=True,
                        )
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                else:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            else:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            await dismiss_consent(page)

            # Dwell on the page before checking for blocks / scraping
            # results. Without this, the page is "scraped" 50ms after it
            # loads, which is a strong bot signal (real users take
            # 1-3 seconds to orient themselves on a new page).
            if used_typing_flow:
                # Already did a short read inside the typing flow
                await human_sim.human_read(page, duration_sec=random.uniform(0.5, 1.5))
            else:
                await human_sim.human_read(page)

            # Reading mouse track — hover over result titles like a
            # real user browsing search results.
            if used_typing_flow:
                try:
                    await anti_detect.reading_mouse_track(page)
                except Exception as e:
                    print(
                        f"[anti_detect] reading_mouse_track failed: {e}",
                        file=sys.stderr, flush=True,
                    )

            await simulate_human_behavior(page)
            await take_debug_screenshot(page, f"{screenshot_label}_01_first")

            blocked, reason = await _is_any_block(page)

            # ── Tab focus simulation (occasional) ──
            # Real users switch tabs and come back. Bots never do.
            # This is a small but consistent signal of "real user".
            if not blocked:
                try:
                    await anti_detect.simulate_tab_focus(page)
                except Exception:
                    pass

            # ── CAPTCHA solve attempt ──
            if blocked:
                await take_debug_screenshot(page, f"{screenshot_label}_02_blocked")
                if reason == "captcha" or not reason:
                    solved = await try_solve_captcha(page)
                else:
                    # Non-CAPTCHA blocks (login, rate_limit, verification) are
                    # not solvable by the neural net — skip straight to retries
                    # / manual intervention.
                    solved = False
                if solved:
                    await take_debug_screenshot(page, f"{screenshot_label}_03_solved")
                    blocked = False
                else:
                    # ── Warm-up retry ──
                    await take_debug_screenshot(page, f"{screenshot_label}_04_unsolved")
                    await warmup_retry(page, url)
                    await simulate_human_behavior(page)
                    await take_debug_screenshot(page, f"{screenshot_label}_05_warmup")
                    blocked, reason = await _is_any_block(page)

            # ── Retry loop ──
            attempt = 0
            while blocked and attempt < retries:
                await take_debug_screenshot(page, f"{screenshot_label}_06_retry{attempt}")
                backoff = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                await asyncio.sleep(backoff)

                # Navigate to Google home first, then to target
                try:
                    await page.goto(
                        "https://www.google.com/ncr",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await dismiss_consent(page)
                    await human_sim.human_idle(page, duration_sec=random.uniform(0.4, 1.2))

                    # On retry, also try typing if applicable
                    if (
                        anti_detect.is_google_search_url(url)
                        and anti_detect.config.ANTIDETECT_SEARCH_VIA_TYPING
                    ):
                        retry_query = anti_detect.parse_query_from_url(url)
                        if retry_query:
                            await anti_detect.search_via_typing(page, retry_query)
                        else:
                            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    else:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                    await dismiss_consent(page)
                    # Wait + simulate human interaction before checking
                    # whether the block persists. This gives the page time
                    # to load results and lets us look like a real user.
                    await human_sim.human_read(page, duration_sec=random.uniform(0.8, 2.0))
                    # Reading mouse track on retry too
                    if (
                        anti_detect.is_google_search_url(url)
                        and anti_detect.config.ANTIDETECT_SEARCH_VIA_TYPING
                    ):
                        try:
                            await anti_detect.reading_mouse_track(page)
                        except Exception:
                            pass
                    await simulate_human_behavior(page)
                except Exception:
                    pass

                blocked, reason = await _is_any_block(page)
                attempt += 1

            # ── Manual intervention (NEW) ──
            # If all automatic measures failed, give the user a chance to
            # resolve the block via a visible browser window.
            if blocked:
                try:
                    await take_debug_screenshot(page, f"{screenshot_label}_07_final_blocked")
                except Exception:
                    pass
                if reason not in MANUAL_INTERVENTION_REASONS:
                    reason = "unknown"
                print(
                    f"\n[browse_google] Block persists after retries "
                    f"(reason={reason}). Triggering manual intervention...",
                    file=sys.stderr, flush=True,
                )
                resolved = await _try_manual_intervention_flow(
                    pw, context_holder, page_holder, url, reason, screenshot_label
                )
                if resolved:
                    # New context/page are now in the holders
                    page = page_holder[0]
                    context = context_holder[0]
                    blocked = False
                else:
                    try:
                        await take_debug_screenshot(page, f"{screenshot_label}_07b_manual_failed")
                    except Exception:
                        pass

            if blocked:
                # ── All automatic + manual attempts failed → fall back ──
                if context is not None:
                    try:
                        await save_cookies(context)
                    except Exception:
                        pass
                yield None
                return

            # ── Success — yield the page to the caller ──
            try:
                await take_debug_screenshot(page, f"{screenshot_label}_08_ok")
            except Exception:
                pass
            yield page

        except Exception as e:
            try:
                await take_debug_screenshot(page_holder[0], f"{screenshot_label}_99_error")
            except Exception:
                pass
            # Re-raise so the caller can handle it
            raise

        finally:
            ctx = context_holder[0]
            if ctx is not None:
                try:
                    await save_cookies(ctx)
                except Exception:
                    pass
                try:
                    await ctx.close()
                except Exception:
                    pass
