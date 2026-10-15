"""Phase 4 tests for the shared browser pool (fake Playwright backend).

Proves the pool launches Chromium once, caps concurrency, closes pages, and
relaunches after a disconnect — all without a real browser.
"""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp.core import browser as poolmod


class FakePage:
    def __init__(self, context):
        self.context = context
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.context.open_pages.discard(self)


class FakeContext:
    def __init__(self):
        self.open_pages: set[FakePage] = set()
        self.handlers: dict[str, object] = {}
        self.closed = False
        self.browser = None

    async def new_page(self) -> FakePage:
        page = FakePage(self)
        self.open_pages.add(page)
        return page

    def on(self, event, handler) -> None:
        self.handlers[event] = handler

    async def close(self) -> None:
        self.closed = True
        handler = self.handlers.get("close")
        if callable(handler):
            handler()


class FakePlaywright:
    def __init__(self):
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True
        return self

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def fake_backend(monkeypatch):
    launches: list[FakeContext] = []
    pws: list[FakePlaywright] = []

    async def fake_launch_browser(pw, viewport=None, headless=True):  # noqa: ARG001
        ctx = FakeContext()
        launches.append(ctx)
        return ctx

    def fake_async_playwright():
        pw = FakePlaywright()
        pws.append(pw)
        return pw

    import playwright.async_api as pw_api
    from google_search_mcp import browser as browser_mod

    monkeypatch.setattr(pw_api, "async_playwright", fake_async_playwright)
    monkeypatch.setattr(browser_mod, "launch_browser", fake_launch_browser)
    return {"launches": launches, "pws": pws}


def _fresh_pool(monkeypatch, max_contexts=3):
    pool = poolmod.BrowserPool(max_contexts=max_contexts)
    monkeypatch.setattr(poolmod, "pool", pool)
    return pool


def test_pool_launches_chromium_once_across_many_calls(fake_backend, monkeypatch):
    pool = _fresh_pool(monkeypatch)

    async def scenario():
        for _ in range(5):
            async with pool.page() as page:
                assert page is not None
        return pool

    asyncio.run(scenario())
    assert len(fake_backend["launches"]) == 1, "Chromium must launch once, not per call"
    assert pool.starts == 1
    assert len(fake_backend["pws"]) == 1


def test_page_is_closed_after_use(fake_backend, monkeypatch):
    pool = _fresh_pool(monkeypatch)
    captured = {}

    async def scenario():
        async with pool.page() as page:
            captured["page"] = page
            assert page in page.context.open_pages

    asyncio.run(scenario())
    assert captured["page"].closed is True
    assert captured["page"] not in captured["page"].context.open_pages


def test_concurrency_is_capped(fake_backend, monkeypatch):
    pool = _fresh_pool(monkeypatch, max_contexts=2)
    state = {"current": 0, "peak": 0}

    async def worker():
        async with pool.page() as page:  # noqa: ARG001
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            await asyncio.sleep(0.05)
            state["current"] -= 1

    async def scenario():
        await asyncio.gather(*[worker() for _ in range(6)])

    asyncio.run(scenario())
    assert state["peak"] <= 2, f"concurrency exceeded cap: {state['peak']}"
    assert len(fake_backend["launches"]) == 1


def test_relaunch_after_disconnect(fake_backend, monkeypatch):
    pool = _fresh_pool(monkeypatch)

    async def scenario():
        async with pool.page():
            pass
        # Simulate a browser crash.
        fake_backend["launches"][0].handlers["close"]()
        assert pool.is_started is False
        async with pool.page():
            pass

    asyncio.run(scenario())
    assert len(fake_backend["launches"]) == 2, "pool must relaunch after a disconnect"


def test_aclose_stops_playwright(fake_backend, monkeypatch):
    pool = _fresh_pool(monkeypatch)

    async def scenario():
        async with pool.page():
            pass
        await pool.aclose()

    asyncio.run(scenario())
    assert fake_backend["launches"][0].closed is True
    assert fake_backend["pws"][0].stopped is True
    assert pool.is_started is False


def test_enabled_reflects_config(monkeypatch):
    monkeypatch.setattr(poolmod.config, "BROWSER_POOL", True)
    assert poolmod.enabled() is True
    monkeypatch.setattr(poolmod.config, "BROWSER_POOL", False)
    assert poolmod.enabled() is False


def test_shutdown_helper_is_idempotent(fake_backend, monkeypatch):
    pool = _fresh_pool(monkeypatch)

    async def scenario():
        async with pool.page():
            pass
        await pool.aclose()
        await pool.aclose()  # must not raise

    asyncio.run(scenario())
