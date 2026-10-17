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
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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

        with contextlib.suppress(Exception):  # event wiring is best-effort
            context.on("close", _mark_dead)
            browser = getattr(context, "browser", None)
            if browser is not None:
                browser.on("disconnected", _mark_dead)
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
                # page may already be gone; closing is best-effort
                with contextlib.suppress(Exception):
                    await page.close()

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
            with contextlib.suppress(Exception):
                await context.close()
        if pw is not None:
            with contextlib.suppress(Exception):
                await pw.stop()

    @property
    def is_started(self) -> bool:
        return self._context is not None and not self._dead


# Module-level pool shared by all browser tools.
pool = BrowserPool()


class _SharedContextProxy:
    """Proxy for the pool's shared context whose ``close()`` is a no-op.

    Tool call sites close the context they are handed at the end of a call
    (``finally: await context.close()``). When that context is the *shared*
    pooled one, actually closing it would defeat the pool, so the proxy
    swallows ``close()`` while delegating every other attribute to the real
    context (``load_cookies``/``save_cookies``/``new_page``/... keep working).
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: Any):
        self._ctx = ctx

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)

    async def close(self) -> None:
        return None


@asynccontextmanager
async def pooled_browser(
    viewport: dict | None = None, *, headless: bool = True
) -> AsyncIterator[tuple[Any, Any]]:
    """Yield ``(context, page)`` from the shared pool, or a per-call launch.

    Drop-in replacement for the repeated boilerplate::

        async with async_playwright() as pw:
            context = await launch_browser(pw, viewport=...)
            page = await context.new_page()
            try:
                ...
            finally:
                await context.close()

    With the pool enabled (default) the shared Chromium context is reused and
    only the per-call page is closed — the yielded context proxy makes
    ``context.close()`` a no-op so existing ``finally`` blocks stay correct.
    Set ``NOAPI_MCP_BROWSER_POOL=0`` to restore per-call launches.
    """
    if enabled():
        async with pool.page(viewport=viewport) as page:
            yield _SharedContextProxy(page.context), page
        return

    from playwright.async_api import async_playwright

    from ..browser import launch_browser

    async with async_playwright() as pw:
        context = await launch_browser(pw, viewport=viewport, headless=headless)
        page = await context.new_page()
        try:
            yield context, page
        finally:
            # close is best effort
            with contextlib.suppress(Exception):
                await context.close()


async def aclose() -> None:
    """Shut the shared pool down (called from the server lifespan)."""
    await pool.aclose()
