"""Phase 4 tests: ``browse_google`` uses the shared pool when enabled."""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp import anti_bot
from google_search_mcp.core import browser as poolmod


class FakePage:
    def __init__(self, context):
        self.context = context
        self.closed = False
        self.goto_calls = 0

    async def goto(self, url, **kwargs):  # noqa: ARG002
        self.goto_calls += 1

    async def close(self) -> None:
        self.closed = True
        self.context.open_pages.discard(self)

    def __getattr__(self, name):
        # Any other Playwright page method (evaluate, wait_for_timeout, …)
        # becomes an async no-op returning None.
        async def _noop(*args, **kwargs):  # noqa: ARG001
            return None

        return _noop


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

    async def add_init_script(self, *a, **k):  # noqa: ARG002
        return None


class FakePlaywright:
    """Supports both ``.start()`` (pool) and ``async with`` (non-pool)."""

    async def start(self):
        return self

    async def stop(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture()
def stubbed_browse(monkeypatch):
    """Stub everything except the pool plumbing, so browse_google can run offline."""
    launches = {"n": 0, "contexts": []}

    async def fake_launch_browser(pw, viewport=None, headless=True):  # noqa: ARG001
        launches["n"] += 1
        ctx = FakeContext()
        launches["contexts"].append(ctx)
        return ctx

    import playwright.async_api as pw_api
    from google_search_mcp import browser as browser_mod

    monkeypatch.setattr(pw_api, "async_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(browser_mod, "launch_browser", fake_launch_browser)
    # anti_bot imported these names directly, so patch them there too.
    monkeypatch.setattr(anti_bot, "async_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(anti_bot, "launch_browser", fake_launch_browser)

    pool = poolmod.BrowserPool(max_contexts=2)
    monkeypatch.setattr(poolmod, "pool", pool)

    async def noop(*a, **k):  # noqa: ARG001
        return None

    monkeypatch.setattr(anti_bot, "load_cookies", noop)
    monkeypatch.setattr(anti_bot, "save_cookies", noop)
    monkeypatch.setattr(anti_bot, "setup_page_stealth", noop)
    monkeypatch.setattr(anti_bot, "simulate_human_behavior", noop)
    monkeypatch.setattr(anti_bot, "dismiss_consent", noop)
    monkeypatch.setattr(anti_bot, "take_debug_screenshot", noop)
    monkeypatch.setattr(anti_bot, "_enforce_rate_limit", noop)

    async def not_blocked(page):  # noqa: ARG001
        return False, ""

    monkeypatch.setattr(anti_bot, "_is_any_block", not_blocked)

    from google_search_mcp import anti_detect, human_sim

    monkeypatch.setattr(human_sim, "_enabled", lambda: False)
    monkeypatch.setattr(anti_detect, "warmup_session", noop)
    monkeypatch.setattr(anti_detect, "is_google_search_url", lambda url: False)
    monkeypatch.setattr(anti_detect, "simulate_tab_focus", noop)
    monkeypatch.setattr(anti_detect, "reading_mouse_track", noop)

    from google_search_mcp.core import circuit

    circuit.google_breaker.record_success()
    return {"launches": launches, "pool": pool}


def test_browse_google_uses_pool_and_launches_once(stubbed_browse):
    launches = stubbed_browse["launches"]
    pool = stubbed_browse["pool"]

    async def scenario():
        for _ in range(3):
            async with anti_bot.browse_google("https://www.google.com/search?q=x") as page:
                assert page is not None

    asyncio.run(scenario())
    # The pool launches Chromium exactly once and reuses it for all 3 calls —
    # it must NOT launch a browser per call.
    assert launches["n"] == 1, "pooled mode must launch Chromium only once"
    assert pool.starts == 1, "the shared Chromium must start exactly once"


def test_browse_google_closes_page_but_keeps_pooled_context(stubbed_browse):
    pool = stubbed_browse["pool"]

    async def scenario():
        async with anti_bot.browse_google("https://www.google.com/search?q=x") as page:
            captured = page
        assert captured.closed is True, "the per-call page must be closed"
        assert pool.is_started is True, "the shared context must stay alive"
        assert pool._context.closed is False

    asyncio.run(scenario())


def test_browse_google_falls_back_when_pool_disabled(monkeypatch, stubbed_browse):
    launches = stubbed_browse["launches"]
    monkeypatch.setattr(poolmod.config, "BROWSER_POOL", False)

    async def scenario():
        async with anti_bot.browse_google("https://www.google.com/search?q=x") as page:
            assert page is not None

    asyncio.run(scenario())
    assert launches["n"] == 1, "without the pool, a per-call launch still happens"
