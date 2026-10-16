"""Phase 4 tests for the ``pooled_browser`` context manager.

Verifies the drop-in replacement used by the tool modules:

* pooled mode reuses one Chromium and closes only the per-call page;
* the yielded context proxy makes ``context.close()`` a no-op so existing
  ``finally: await context.close()`` blocks cannot kill the shared context;
* ``NOAPI_MCP_BROWSER_POOL=0`` restores per-call launches.
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

    def __getattr__(self, name):
        async def _noop(*a, **k):  # noqa: ARG001
            return None

        return _noop


class FakeContext:
    def __init__(self):
        self.pages: list[FakePage] = []
        self.closed = False

    async def new_page(self) -> FakePage:
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True

    def on(self, event, handler) -> None:  # noqa: ARG002
        return None


class FakePlaywright:
    async def start(self):
        return self

    async def stop(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture()
def fake_pool(monkeypatch):
    launches = {"n": 0}

    async def fake_launch_browser(pw, viewport=None, headless=True):  # noqa: ARG001
        launches["n"] += 1
        return FakeContext()

    import playwright.async_api as pw_api

    from google_search_mcp import browser as browser_mod

    monkeypatch.setattr(pw_api, "async_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(browser_mod, "launch_browser", fake_launch_browser)

    pool = poolmod.BrowserPool(max_contexts=2)
    monkeypatch.setattr(poolmod, "pool", pool)
    monkeypatch.setattr(poolmod.config, "BROWSER_POOL", True)
    return {"pool": pool, "launches": launches}


def test_pooled_browser_reuses_context_and_closes_page(fake_pool):
    pool = fake_pool["pool"]
    captured = {}

    async def scenario():
        for _ in range(3):
            async with poolmod.pooled_browser() as (context, page):
                captured["ctx"] = context
                captured["page"] = page
            # After the block, the per-call page must be closed...
            assert captured["page"].closed is True
            # ...but the shared context must still be alive.
            assert pool.is_started is True

    asyncio.run(scenario())
    assert fake_pool["launches"]["n"] == 1, "one Chromium for all three calls"


def test_context_proxy_close_is_a_noop(fake_pool):
    async def scenario():
        async with poolmod.pooled_browser() as (context, page):
            real = page.context
            await context.close()  # must NOT close the shared context
            assert real.closed is False

    asyncio.run(scenario())


def test_context_proxy_delegates_other_attributes(fake_pool):
    async def scenario():
        async with poolmod.pooled_browser() as (context, page):
            # load_cookies/save_cookies call e.g. context.add_cookies(...)
            assert context.pages == page.context.pages
            new = await context.new_page()
            assert new is not None

    asyncio.run(scenario())


def test_pooled_browser_falls_back_when_disabled(fake_pool, monkeypatch):
    monkeypatch.setattr(poolmod.config, "BROWSER_POOL", False)

    async def scenario():
        async with poolmod.pooled_browser() as (context, page):
            assert page is not None
        # Non-pooled path closes the per-call context.
        assert context.closed is True

    asyncio.run(scenario())
    assert fake_pool["launches"]["n"] == 1, "one launch for one call in legacy mode"
    assert fake_pool["pool"].is_started is False
