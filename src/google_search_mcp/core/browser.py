"""Shared Playwright/Chromium pool with a concurrency cap.

Why this exists
---------------
Browser tools used to start a fresh Playwright instance and launch a Chromium
persistent context per call. That costs 1–3 s per call, uses a lot of RAM under
parallelism, and — because Chromium locks the ``BROWSER_DATA_DIR`` user-data
directory — makes concurrent calls *contend for the same directory*.

This pool starts Playwright and one persistent Chromium context **once** (lazily,
on first use) and hands out a fresh *page* per call from that shared context.
Concurrency is bounded by ``NOAPI_MCP_MAX_BROWSERS``.

The stealth/fingerprint setup is reused verbatim from ``browser.launch_browser``
so the anti-detection baseline is unchanged. Set ``NOAPI_MCP_BROWSER_POOL=0`` to
restore launch-per-call behaviour.

Verified APIs: ``playwright.async_api.async_playwright`` (``.start()``/``.stop()``),
``BrowserContext.new_page()``, ``Page.close()``, ``Browser.on("disconnected")``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any

from .. import config

log = logging.getLogger("google_search_mcp.browser_pool")


def enabled() -> bool:
    """Whether the shared pool is enabled (default: yes)."""
    return bool(config.BROWSER_POOL)


class BrowserPool:
    """Owns one Playwright instance + one persistent Chromium context."""

    def __init__(self, max_contexts: int | None = None):
        self._max = max_contexts or config.MAX_BROWSERS
        self._semaphore = asyncio.Semaphore(self._max)
        self._pw: Any = None
        self._context: Any = None
        self._lock = asyncio.Lock()
        self._dead = False
        self.starts = 0  # number of Chromium launches (for tests/observability)

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def _start(self) -> Any:
        """(Re)create the Playwright instance and persistent context."""
        from playwright.async_api import async_playwright

        from ..browser import launch_browser

        if self._pw is None:
            self._pw = await async_playwright().start()
        context = await launch_browser(self._pw)
        self._context = context
        self._dead = False
        self.starts += 1

        # Detect a crash/close so the next call can relaunch.
        def _mark_dead(*_args: Any) -> None:
            self._dead = True

        try:
            context.on("close", _mark_dead)
            browser = getattr(context, "browser", None)
            if browser is not None:
                browser.on("disconnected", _mark_dead)
        except Exception:  # noqa: BLE001 - event wiring is best-effort
            pass
        log.info("browser pool started (max_concurrent=%d)", self._max)
        return context

    async def _ensure(self) -> Any:
        async with self._lock:
            if self._context is None or self._dead:
                await self._start()
            return self._context

    @asynccontextmanager
    async def page(self, *, viewport: dict | None = None) -> AsyncIterator[Any]:
        """Yield a fresh page from the shared context, capped by the semaphore."""
        async with self._semaphore:
            context = await self._ensure()
            page = await context.new_page()
            try:
                yield page
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001 - page may already be gone
                    pass

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold a concurrency slot without taking a page.

        Used by callers (e.g. ``browse_google``) that manage their own page
        lifecycle but still must respect the global concurrency cap.
        """
        async with self._semaphore:
            yield

    async def context(self) -> Any:
        """Return the shared persistent context (starting it if needed)."""
        return await self._ensure()

    async def playwright_instance(self) -> Any:
        """Return the shared Playwright instance (starting it if needed)."""
        async with self._lock:
            if self._pw is None:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
            return self._pw

    def owns(self, context: Any) -> bool:
        """Whether *context* is the pool's shared context."""
        return context is not None and context is self._context

    async def aclose(self) -> None:
        """Close the context (if any) and stop Playwright."""
        async with self._lock:
            context, pw = self._context, self._pw
            self._context = None
            self._pw = None
            self._dead = True
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:  # noqa: BLE001
                pass

    @property
    def is_started(self) -> bool:
        return self._context is not None and not self._dead


# Module-level pool shared by all browser tools.
pool = BrowserPool()


async def aclose() -> None:
    """Shut the shared pool down (called from the server lifespan)."""
    await pool.aclose()
