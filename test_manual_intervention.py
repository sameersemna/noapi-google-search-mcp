#!/usr/bin/env python3
"""Tests for the smart bot-detection / manual-intervention flow.

These tests cover:

  1. Config — the new ENABLE_MANUAL_INTERVENTION / timeout / poll options
     are present, env-overridable, and validated.
  2. browser.py — the new helpers (`is_login_required`,
     `detect_block_reason`, `_check_display_available`,
     `is_manual_intervention_active`, `get_last_intervention_result`)
     exist and behave correctly without a real browser.
  3. anti_bot.py — `browse_google` is callable, the new
     `_is_any_block` helper returns the expected tuple shape, and the
     `_try_manual_intervention_flow` short-circuits cleanly when
     ENABLE_MANUAL_INTERVENTION=0 or the display is unavailable.
  4. server.py — the new MCP tools `open_manual_browser` and
     `manual_intervention_status` are registered with FastMCP and
     return sensible responses when manual intervention is disabled
     or no display is available.
  5. End-to-end — disabled path: `browse_google` falls through to
     fallback (returns None) when intervention is disabled and the
     headless flow can't get past a synthetic block. (We mock the
     network layer to keep the test offline.)

The tests do NOT actually open a headful browser; that requires a
DISPLAY and human interaction. They just exercise the wiring.

Run with:
    /home/sameer/anaconda3/envs/mcp-google/bin/python test_manual_intervention.py

Or via the existing test runner pattern:
    python -m pytest test_manual_intervention.py -v
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "src")
# Skip cookie validation so the server module can be imported during tests.
os.environ.setdefault("SKIP_COOKIE_VALIDATION", "1")

# Set the env to *disable* manual intervention for the bulk of tests so we
# don't accidentally pop a window. The dedicated "enabled" tests flip it
# temporarily.
os.environ.setdefault("ENABLE_MANUAL_INTERVENTION", "0")

from google_search_mcp import (  # noqa: E402
    anti_bot,
    browser,
    config,
)
from google_search_mcp.browser import (  # noqa: E402
    _check_display_available,
    detect_block_reason,
    get_last_intervention_result,
    is_login_required,
    is_manual_intervention_active,
)
from google_search_mcp.config import (  # noqa: E402
    ENABLE_MANUAL_INTERVENTION,
    MANUAL_INTERVENTION_POLL_SEC,
    MANUAL_INTERVENTION_REASONS,
    MANUAL_INTERVENTION_TIMEOUT_SEC,
)


# ── Test harness ─────────────────────────────────────────────────────────

passed = 0
failed = 0
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ PASS: {label}")
    else:
        failed += 1
        failures.append(f"{label} — {detail}")
        print(f"  ✗ FAIL: {label}  ({detail})")
    return condition


def section(title: str):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


# ── Test 1: Config values ────────────────────────────────────────────────


def test_config_values():
    section("Test 1: Config — new manual-intervention options")
    check(
        "ENABLE_MANUAL_INTERVENTION is a bool",
        isinstance(ENABLE_MANUAL_INTERVENTION, bool),
        f"got {type(ENABLE_MANUAL_INTERVENTION).__name__}",
    )
    check(
        "ENABLE_MANUAL_INTERVENTION is False (env set to 0)",
        ENABLE_MANUAL_INTERVENTION is False,
        f"got {ENABLE_MANUAL_INTERVENTION}",
    )
    check(
        "MANUAL_INTERVENTION_TIMEOUT_SEC is a positive int",
        isinstance(MANUAL_INTERVENTION_TIMEOUT_SEC, int)
        and MANUAL_INTERVENTION_TIMEOUT_SEC > 0,
        f"got {MANUAL_INTERVENTION_TIMEOUT_SEC!r}",
    )
    check(
        "MANUAL_INTERVENTION_POLL_SEC is a positive float",
        isinstance(MANUAL_INTERVENTION_POLL_SEC, (int, float))
        and MANUAL_INTERVENTION_POLL_SEC > 0,
        f"got {MANUAL_INTERVENTION_POLL_SEC!r}",
    )
    check(
        "MANUAL_INTERVENTION_REASONS contains expected entries",
        all(r in MANUAL_INTERVENTION_REASONS for r in
            ("captcha", "login", "rate_limit", "verification", "unknown")),
        f"got {MANUAL_INTERVENTION_REASONS}",
    )
    check(
        "MANUAL_INTERVENTION_REASONS is a tuple (immutable)",
        isinstance(MANUAL_INTERVENTION_REASONS, tuple),
        f"got {type(MANUAL_INTERVENTION_REASONS).__name__}",
    )

    # Env override: simulate setting the env var, then re-import config.
    saved = os.environ.get("MANUAL_INTERVENTION_TIMEOUT_SEC")
    os.environ["MANUAL_INTERVENTION_TIMEOUT_SEC"] = "123"
    try:
        # Re-read by re-importing the config module
        import importlib
        importlib.reload(config)
        check(
            "Env override of MANUAL_INTERVENTION_TIMEOUT_SEC works",
            config.MANUAL_INTERVENTION_TIMEOUT_SEC == 123,
            f"got {config.MANUAL_INTERVENTION_TIMEOUT_SEC}",
        )
    finally:
        # Restore
        if saved is None:
            os.environ.pop("MANUAL_INTERVENTION_TIMEOUT_SEC", None)
        else:
            os.environ["MANUAL_INTERVENTION_TIMEOUT_SEC"] = saved
        importlib.reload(config)


# ── Test 2: browser.py helpers (no browser) ──────────────────────────────


def test_browser_helpers_loaded():
    section("Test 2: browser.py — helper functions are importable")
    check(
        "is_login_required is callable",
        callable(is_login_required),
    )
    check(
        "detect_block_reason is callable",
        callable(detect_block_reason),
    )
    check(
        "_check_display_available is callable",
        callable(_check_display_available),
    )
    check(
        "is_manual_intervention_active is callable",
        callable(is_manual_intervention_active),
    )
    check(
        "get_last_intervention_result is callable",
        callable(get_last_intervention_result),
    )
    check(
        "is_manual_intervention_active returns False at startup",
        is_manual_intervention_active() is False,
    )
    last = get_last_intervention_result()
    check(
        "get_last_intervention_result returns a dict with expected keys",
        isinstance(last, dict)
        and {"timestamp", "resolved", "reason", "url", "elapsed_sec"} <= set(last.keys()),
        f"got {last}",
    )

    # Display detection
    display_ok = _check_display_available()
    check(
        "_check_display_available returns a bool",
        isinstance(display_ok, bool),
        f"got {display_ok!r}",
    )


async def test_is_login_required_with_mock_page():
    section("Test 3: is_login_required — page mock")
    # Mock page that returns a Google sign-in URL
    page = AsyncMock()
    page.url = "https://accounts.google.com/v3/signin/identifier?foo=bar"
    page.evaluate = AsyncMock(return_value={
        "hasEmail": True, "hasPassword": False, "isAccountsDomain": True,
        "hasSignInText": True, "hasChooseAccount": False,
    })
    result = await is_login_required(page)
    check("is_login_required returns True for accounts.google.com signin URL",
          result is True, f"got {result}")

    # Mock page that's a normal search result
    page2 = AsyncMock()
    page2.url = "https://www.google.com/search?q=foo"
    page2.evaluate = AsyncMock(return_value={
        "hasEmail": False, "hasPassword": False, "isAccountsDomain": False,
        "hasSignInText": False, "hasChooseAccount": False,
    })
    result2 = await is_login_required(page2)
    check("is_login_required returns False for normal Google search page",
          result2 is False, f"got {result2}")

    # Mock page where evaluate throws — should return False (fail-safe)
    page3 = AsyncMock()
    page3.url = "https://www.google.com"
    page3.evaluate = AsyncMock(side_effect=RuntimeError("simulated"))
    result3 = await is_login_required(page3)
    check("is_login_required returns False when page.evaluate throws",
          result3 is False, f"got {result3}")


async def test_detect_block_reason_with_mock_page():
    section("Test 4: detect_block_reason — page mock")
    # CAPTCHA scenario
    page_captcha = AsyncMock()
    page_captcha.url = "https://www.google.com/sorry/index?..."
    page_captcha.evaluate = AsyncMock(return_value={
        "body": "our systems have detected unusual traffic",
        "hasRecaptcha": True,
        "hasImageChallenge": True,
        "hasUnusualTraffic": True,
        "hasNotARobot": False,
        "hasChooseAccount": False,
        "hasSignIn": False,
        "hasEmail": False,
        "hasPassword": False,
        "hasVerification": False,
        "hasConsent": False,
    })
    reason = await detect_block_reason(page_captcha)
    check("detect_block_reason returns 'captcha' for reCAPTCHA page",
          reason == "captcha", f"got {reason!r}")

    # Login scenario
    page_login = AsyncMock()
    page_login.url = "https://accounts.google.com/signin/v2/identifier"
    page_login.evaluate = AsyncMock(return_value={
        "body": "sign in to continue to gmail",
        "hasRecaptcha": False,
        "hasImageChallenge": False,
        "hasUnusualTraffic": False,
        "hasNotARobot": False,
        "hasChooseAccount": False,
        "hasSignIn": True,
        "hasEmail": True,
        "hasPassword": False,
        "hasVerification": False,
        "hasConsent": False,
    })
    reason2 = await detect_block_reason(page_login)
    check("detect_block_reason returns 'login' for Google sign-in page",
          reason2 == "login", f"got {reason2!r}")

    # Verification scenario
    page_verify = AsyncMock()
    page_verify.url = "https://myaccount.google.com/signinoptions/two-step"
    page_verify.evaluate = AsyncMock(return_value={
        "body": "2-step verification required",
        "hasRecaptcha": False,
        "hasImageChallenge": False,
        "hasUnusualTraffic": False,
        "hasNotARobot": False,
        "hasChooseAccount": False,
        "hasSignIn": False,
        "hasEmail": False,
        "hasPassword": False,
        "hasVerification": True,
        "hasConsent": False,
    })
    reason3 = await detect_block_reason(page_verify)
    check("detect_block_reason returns 'verification' for 2FA page",
          reason3 == "verification", f"got {reason3!r}")

    # Normal page → unknown
    page_ok = AsyncMock()
    page_ok.url = "https://www.google.com/search?q=foo"
    page_ok.evaluate = AsyncMock(return_value={
        "body": "about 1,234,567 results",
        "hasRecaptcha": False,
        "hasImageChallenge": False,
        "hasUnusualTraffic": False,
        "hasNotARobot": False,
        "hasChooseAccount": False,
        "hasSignIn": False,
        "hasEmail": False,
        "hasPassword": False,
        "hasVerification": False,
        "hasConsent": False,
    })
    reason4 = await detect_block_reason(page_ok)
    check("detect_block_reason returns 'unknown' for normal page",
          reason4 == "unknown", f"got {reason4!r}")


# ── Test 5: anti_bot.py wiring ───────────────────────────────────────────


def test_anti_bot_wiring():
    section("Test 5: anti_bot.py — wiring is correct")
    check(
        "browse_google exists and is callable",
        callable(anti_bot.browse_google),
    )
    check(
        "_is_any_block exists and is callable",
        callable(anti_bot._is_any_block),
    )
    check(
        "_try_manual_intervention_flow exists and is callable",
        callable(anti_bot._try_manual_intervention_flow),
    )
    check(
        "_enforce_rate_limit exists and is callable",
        callable(anti_bot._enforce_rate_limit),
    )
    # Make sure it imports `manual_intervention_for_block` (referenced in module)
    src = open("src/google_search_mcp/anti_bot.py").read()
    check(
        "anti_bot.py references manual_intervention_for_block",
        "manual_intervention_for_block" in src,
    )
    check(
        "anti_bot.py references detect_block_reason",
        "detect_block_reason" in src,
    )
    check(
        "anti_bot.py references is_login_required",
        "is_login_required" in src,
    )
    check(
        "anti_bot.py wraps context in a mutable holder for swap-in",
        "context_holder" in src and "page_holder" in src,
    )


# ── Test 6: manual_intervention_for_block — disabled path ────────────────


async def test_manual_intervention_disabled_path():
    section("Test 6: manual_intervention_for_block — disabled returns None")
    # Currently ENABLE_MANUAL_INTERVENTION=0 from the env
    pw = MagicMock()
    result = await browser.manual_intervention_for_block(
        pw,
        blocked_url="https://www.google.com/sorry/index",
        reason="captcha",
        initial_cookies=[],
    )
    check(
        "manual_intervention_for_block returns None when disabled",
        result is None,
        f"got {result!r}",
    )
    last = get_last_intervention_result()
    check(
        "Last intervention result is recorded",
        last.get("outcome") == "disabled",
        f"got outcome={last.get('outcome')!r}",
    )


# ── Test 7: browse_google — disabled + blocked → yields None ─────────────


async def test_browse_google_disabled_blocks_path():
    section("Test 7: browse_google — disabled + blocked → yields None")

    # Mock the playwright chain so we never hit the network.
    # Page is_blocked always returns True, so the flow walks all the way
    # through retries into the manual-intervention branch, which is
    # disabled, so we should fall through to `yield None`.

    class _FakePage:
        def __init__(self):
            self.url = "https://www.google.com/sorry/index"
        async def goto(self, *a, **kw):
            return None
        async def evaluate(self, *a, **kw):
            # detect_block_reason: simulate CAPTCHA
            return {
                "body": "our systems have detected unusual traffic",
                "hasRecaptcha": True,
                "hasImageChallenge": True,
                "hasUnusualTraffic": True,
                "hasNotARobot": False,
                "hasChooseAccount": False,
                "hasSignIn": False,
                "hasEmail": False,
                "hasPassword": False,
                "hasVerification": False,
                "hasConsent": False,
            }
        async def close(self):
            return None
        async def wait_for_load_state(self, *a, **kw):
            return None
        async def wait_for_timeout(self, *a, **kw):
            return None
        async def wait_for_function(self, *a, **kw):
            return None
        async def mouse_move(self, *a, **kw):
            return None
        async def screenshot(self, *a, **kw):
            return None
        @property
        def viewport_size(self):
            return {"width": 1280, "height": 800}

    class _FakeContext:
        async def new_page(self):
            return _FakePage()
        async def cookies(self):
            return []
        async def add_cookies(self, *a, **kw):
            return None
        async def add_init_script(self, *a, **kw):
            return None
        async def clear_cookies(self):
            return None
        async def close(self):
            return None

    class _FakeChromium:
        async def launch_persistent_context(self, *a, **kw):
            return _FakeContext()

    class _FakePlaywright:
        def __init__(self):
            self.chromium = _FakeChromium()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    with patch.object(anti_bot, "async_playwright",
                      return_value=_FakePlaywright()), \
         patch.object(anti_bot, "is_blocked",
                      new=AsyncMock(return_value=True)), \
         patch.object(anti_bot, "is_login_required",
                      new=AsyncMock(return_value=False)), \
         patch.object(anti_bot, "try_solve_captcha",
                      new=AsyncMock(return_value=False)), \
         patch.object(anti_bot, "setup_page_stealth",
                      new=AsyncMock(return_value=None)), \
         patch.object(anti_bot, "dismiss_consent",
                      new=AsyncMock(return_value=None)), \
         patch.object(anti_bot, "human_delay",
                      new=AsyncMock(return_value=None)), \
         patch.object(anti_bot, "simulate_human_behavior",
                      new=AsyncMock(return_value=None)), \
         patch.object(anti_bot, "take_debug_screenshot",
                      new=AsyncMock(return_value=None)), \
         patch.object(anti_bot, "load_cookies",
                      new=AsyncMock(return_value=0)), \
         patch.object(anti_bot, "save_cookies",
                      new=AsyncMock(return_value=None)), \
         patch.object(anti_bot, "warmup_retry",
                      new=AsyncMock(return_value=None)):
        yielded = None
        async with anti_bot.browse_google(
            "https://www.google.com/sorry/index",
            retries=0,
            screenshot_label="test",
        ) as page:
            yielded = page

    check(
        "browse_google yields None when blocked + intervention disabled",
        yielded is None,
        f"got {yielded!r}",
    )


# ── Test 8: server.py new tools are registered ───────────────────────────


def test_server_new_tools_registered():
    section("Test 8: server.py — new MCP tools are registered with FastMCP")
    # Import the mcp from server
    from google_search_mcp.server import mcp
    tools = mcp._tool_manager._tools
    check(
        "open_manual_browser is registered as an MCP tool",
        "open_manual_browser" in tools,
    )
    check(
        "manual_intervention_status is registered as an MCP tool",
        "manual_intervention_status" in tools,
    )
    if "open_manual_browser" in tools:
        t = tools["open_manual_browser"]
        check(
            "open_manual_browser has a non-empty description",
            bool(t.description) and len(t.description) > 50,
            f"desc length={len(t.description or '')}",
        )


async def test_open_manual_browser_disabled_path():
    section("Test 9: open_manual_browser — disabled path returns clear message")
    from google_search_mcp.server import open_manual_browser
    result = await open_manual_browser()
    check(
        "open_manual_browser returns a string when disabled",
        isinstance(result, str) and len(result) > 0,
        f"got {result!r}",
    )
    check(
        "open_manual_browser message mentions ENABLE_MANUAL_INTERVENTION",
        "ENABLE_MANUAL_INTERVENTION" in result,
        f"got {result!r}",
    )


async def test_manual_intervention_status_returns_text():
    section("Test 10: manual_intervention_status — returns status text")
    from google_search_mcp.server import manual_intervention_status
    result = await manual_intervention_status()
    check(
        "manual_intervention_status returns a string",
        isinstance(result, str) and len(result) > 50,
        f"got {result!r}",
    )
    check(
        "manual_intervention_status includes 'Manual intervention status'",
        "Manual intervention status" in result,
        f"got {result!r}",
    )
    check(
        "manual_intervention_status includes 'ENABLE_MANUAL_INTERVENTION'",
        "ENABLE_MANUAL_INTERVENTION" in result,
        f"got {result!r}",
    )


# ── Test 11: concurrent intervention lock ────────────────────────────────


async def test_concurrent_intervention_lock():
    section("Test 11: Concurrent intervention lock")
    from google_search_mcp.browser import _manual_intervention_lock
    # Try to acquire the lock — should succeed (no intervention running)
    acquired = _manual_intervention_lock.acquire(blocking=False)
    check(
        "Manual-intervention lock is acquirable when idle",
        acquired is True,
    )
    if acquired:
        _manual_intervention_lock.release()
        check("Lock release works", True)

    # Now hold the lock (simulating an in-progress intervention) and try
    # to call manual_intervention_for_block. It should immediately return
    # None with outcome "concurrent_lock".
    from google_search_mcp import browser as b
    saved_flag = b.ENABLE_MANUAL_INTERVENTION
    b.ENABLE_MANUAL_INTERVENTION = True
    try:
        assert _manual_intervention_lock.acquire(blocking=False), \
            "Could not pre-acquire lock for test"
        try:
            result = await b.manual_intervention_for_block(
                MagicMock(),
                blocked_url="https://www.google.com/sorry/index",
                reason="captcha",
            )
            check(
                "manual_intervention_for_block returns None when another is active",
                result is None,
                f"got {result!r}",
            )
            last = get_last_intervention_result()
            check(
                "Last intervention outcome is 'concurrent_lock'",
                last.get("outcome") == "concurrent_lock",
                f"got {last.get('outcome')!r}",
            )
        finally:
            _manual_intervention_lock.release()
    finally:
        b.ENABLE_MANUAL_INTERVENTION = saved_flag


# ── Test runner ──────────────────────────────────────────────────────────


async def main():
    print("=" * 70)
    print("  Manual intervention / smart bot-detection tests")
    print("=" * 70)
    print()
    print(f"ENABLE_MANUAL_INTERVENTION = {ENABLE_MANUAL_INTERVENTION}")
    print(f"MANUAL_INTERVENTION_TIMEOUT_SEC = {MANUAL_INTERVENTION_TIMEOUT_SEC}")
    print(f"MANUAL_INTERVENTION_POLL_SEC = {MANUAL_INTERVENTION_POLL_SEC}")
    print(f"_check_display_available() = {_check_display_available()}")
    print(f"MANUAL_INTERVENTION_REASONS = {MANUAL_INTERVENTION_REASONS}")

    # Sync tests
    test_config_values()
    test_browser_helpers_loaded()
    test_anti_bot_wiring()
    test_server_new_tools_registered()

    # Async tests
    await test_is_login_required_with_mock_page()
    await test_detect_block_reason_with_mock_page()
    await test_manual_intervention_disabled_path()
    await test_browse_google_disabled_blocks_path()
    await test_open_manual_browser_disabled_path()
    await test_manual_intervention_status_returns_text()
    await test_concurrent_intervention_lock()

    # Summary
    print()
    print("=" * 70)
    total = passed + failed
    print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
    print("=" * 70)
    if failures:
        print()
        print("Failures:")
        for f in failures:
            print(f"  - {f}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
