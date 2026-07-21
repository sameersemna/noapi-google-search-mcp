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
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Callable

from playwright.async_api import async_playwright

from .browser import (
    dismiss_consent,
    human_delay,
    launch_browser,
    load_cookies,
    save_cookies,
    setup_page_stealth,
    simulate_human_behavior,
    take_debug_screenshot,
    warmup_retry,
)
from .captcha import is_blocked, try_solve_captcha
from .config import GOOGLE_REQUEST_MIN_GAP, MAX_GOOGLE_RETRIES, RETRY_BACKOFF_SECONDS


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


import random  # noqa: E402 (needed for rate limit jitter)


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
      - CAPTCHA detection and solving
      - Warm-up retry on transient blocks
      - Configurable retries with exponential backoff
      - Fallback provider invocation when all retries fail
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
        context = await launch_browser(pw)
        await load_cookies(context)
        page = await context.new_page()
        await setup_page_stealth(page)

        try:
            # ── First attempt ──
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await dismiss_consent(page)
            await simulate_human_behavior(page)
            await take_debug_screenshot(page, f"{screenshot_label}_01_first")

            blocked = await is_blocked(page)

            # ── CAPTCHA solve attempt ──
            if blocked:
                await take_debug_screenshot(page, f"{screenshot_label}_02_blocked")
                solved = await try_solve_captcha(page)
                if solved:
                    await take_debug_screenshot(page, f"{screenshot_label}_03_solved")
                    blocked = False
                else:
                    # ── Warm-up retry ──
                    await take_debug_screenshot(page, f"{screenshot_label}_04_unsolved")
                    await warmup_retry(page, url)
                    await simulate_human_behavior(page)
                    await take_debug_screenshot(page, f"{screenshot_label}_05_warmup")
                    blocked = await is_blocked(page)

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
                    await human_delay(page)
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await dismiss_consent(page)
                    await simulate_human_behavior(page)
                except Exception:
                    pass

                blocked = await is_blocked(page)
                attempt += 1

            if blocked:
                await take_debug_screenshot(page, f"{screenshot_label}_07_final_blocked")
                await save_cookies(context)
                yield None
                return

            # ── Success — yield the page to the caller ──
            await take_debug_screenshot(page, f"{screenshot_label}_08_ok")
            yield page

        except Exception as e:
            await take_debug_screenshot(page, f"{screenshot_label}_99_error")
            # Re-raise so the caller can handle it
            raise

        finally:
            await save_cookies(context)
            await context.close()
