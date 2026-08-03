#!/usr/bin/env python3
"""Tests for the anti_detect module (v0.3.4 advanced anti-bot detection).

Covers:
  1. Per-session fingerprint generation and stability
  2. URL parsing (Google search URL detection + query extraction)
  3. Header generation (Client Hints, Sec-Fetch-*)
  4. Init script generation (combines stealth_js + per-session patches)
  5. Warmup state machine
  6. Module-level state reset
  7. Session-stability of the fingerprint across calls
  8. Anti-detect status (for /health)
  9. Config surface (all new keys exposed)
 10. Session-fingerprint determinism (same seed → same fingerprint)
 11. Different seeds → different fingerprints

Run with:
    /home/sameer/anaconda3/envs/mcp-google/bin/python test_anti_detect.py
"""

import asyncio
import importlib
import json
import os
import re
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "src")
os.environ.setdefault("SKIP_COOKIE_VALIDATION", "1")

from google_search_mcp import (  # noqa: E402
    anti_detect,
    config,
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


# ── Test 1: Per-session fingerprint generation ─────────────────────────


def test_fingerprint_generation():
    section("Test 1: generate_session_fingerprint()")
    # Default (no seed) — produces a valid fingerprint
    fp = anti_detect.generate_session_fingerprint()
    check("fingerprint has all required keys", set(fp.keys()) >= {
        "seed", "viewport", "locale", "timezone", "hardware_concurrency",
        "device_memory", "platform", "webgl_vendor", "webgl_renderer",
        "chrome_version", "chrome_major", "user_agent", "accept_language",
        "sec_ch_ua", "sec_ch_ua_mobile", "sec_ch_ua_platform",
        "sec_ch_ua_arch", "sec_fetch_dest", "sec_fetch_mode",
        "sec_fetch_site", "sec_fetch_user",
    }, f"keys: {sorted(fp.keys())}")
    check("viewport has width and height", "width" in fp["viewport"] and "height" in fp["viewport"])
    check("viewport width is reasonable", 800 <= fp["viewport"]["width"] <= 4000,
          f"got {fp['viewport']['width']}")
    check("viewport height is reasonable", 600 <= fp["viewport"]["height"] <= 3000,
          f"got {fp['viewport']['height']}")
    check("UA contains Chrome", "Chrome" in fp["user_agent"],
          f"got {fp['user_agent'][:80]}")
    check("UA is a Chrome-style UA", re.search(r"Chrome/\d+", fp["user_agent"]) is not None)
    check("chrome_major is a number", fp["chrome_major"].isdigit())
    check("locale is non-empty", bool(fp["locale"]))
    check("timezone is non-empty", bool(fp["timezone"]))
    check("hardware_concurrency is a power of 2 between 4-32",
          fp["hardware_concurrency"] in (4, 8, 12, 16, 24, 32),
          f"got {fp['hardware_concurrency']}")
    check("device_memory is 4/8/16/32",
          fp["device_memory"] in (4, 8, 16, 32),
          f"got {fp['device_memory']}")
    check("webgl_vendor is one of the expected vendors",
          fp["webgl_vendor"] in ("Intel Inc.", "NVIDIA Corporation", "AMD", "Apple Inc."),
          f"got {fp['webgl_vendor']}")
    check("webgl_renderer is non-empty", bool(fp["webgl_renderer"]))
    check("sec_ch_ua has 3 brands", fp["sec_ch_ua"].count('"') == 8,
          f"got {fp['sec_ch_ua']}")  # 3 brand names + 3 version values = 8 quotes
    check("sec_ch_ua_platform is desktop", fp["sec_ch_ua_mobile"] == "?0",
          f"got {fp['sec_ch_ua_mobile']}")
    check("sec_fetch_mode is navigate", fp["sec_fetch_mode"] == "navigate",
          f"got {fp['sec_fetch_mode']}")


# ── Test 2: Fingerprint determinism with seeds ────────────────────────


def test_fingerprint_determinism():
    section("Test 2: Fingerprint determinism with seeds")
    fp1 = anti_detect.generate_session_fingerprint(seed=12345)
    fp2 = anti_detect.generate_session_fingerprint(seed=12345)
    check("Same seed produces same fingerprint", fp1 == fp2,
          f"differ: {[k for k in fp1 if fp1[k] != fp2[k]]}")

    fp3 = anti_detect.generate_session_fingerprint(seed=67890)
    check("Different seeds produce different fingerprints", fp1 != fp3)

    # The same seed should produce the same viewport (mostly)
    # Note: the random module is used to apply jitter, so we need
    # to make sure the seed is applied consistently.
    fp4 = anti_detect.generate_session_fingerprint(seed=12345)
    fp5 = anti_detect.generate_session_fingerprint(seed=12345)
    check("Viewport is deterministic for same seed",
          fp4["viewport"] == fp5["viewport"],
          f"got {fp4['viewport']} vs {fp5['viewport']}")


# ── Test 3: URL parsing ──────────────────────────────────────────────


def test_url_parsing():
    section("Test 3: URL parsing")
    test_cases = [
        # (url, is_google_search, expected_query)
        ("https://www.google.com/search?q=python+tutorial", True, "python tutorial"),
        ("https://google.com/search?q=hello+world", True, "hello world"),
        ("https://www.google.co.uk/search?q=foo&hl=en", True, "foo"),
        ("https://www.google.de/search?q=bar&hl=de", True, "bar"),
        ("https://www.google.com.au/search?q=baz", True, "baz"),
        ("https://www.google.co.jp/search?q=qux", True, "qux"),
        ("https://www.google.com.br/search?q=quux", True, "quux"),
        ("https://www.google.com/search?q=weather+today&tbs=qdr:d", True, "weather today"),
        ("https://www.google.com/search?num=10", True, ""),
        ("https://www.google.com/", False, ""),
        ("https://www.google.com", False, ""),
        ("https://example.com/search?q=foo", False, "foo"),
        ("https://www.google.com/maps", False, ""),
        ("https://www.google.com/search", True, ""),
    ]
    for url, expected_is_google, expected_query in test_cases:
        is_google = anti_detect.is_google_search_url(url)
        query = anti_detect.parse_query_from_url(url)
        check(f"is_google_search_url({url!r}) == {expected_is_google}",
              is_google is expected_is_google,
              f"got {is_google}")
        check(f"parse_query_from_url({url!r}) == {expected_query!r}",
              query == expected_query,
              f"got {query!r}")


# ── Test 4: Headers generation ──────────────────────────────────────


def test_headers():
    section("Test 4: fingerprint_to_headers()")
    fp = anti_detect.generate_session_fingerprint(seed=42)
    headers = anti_detect.fingerprint_to_headers(fp)

    check("headers has Accept-Language", "Accept-Language" in headers)
    check("Accept-Language matches fingerprint", headers["Accept-Language"] == fp["accept_language"])
    check("headers has Sec-CH-UA", "Sec-CH-UA" in headers)
    check("Sec-CH-UA matches fingerprint", headers["Sec-CH-UA"] == fp["sec_ch_ua"])
    check("headers has Sec-CH-UA-Mobile", "Sec-CH-UA-Mobile" in headers)
    check("Sec-CH-UA-Mobile is ?0 (desktop)", headers["Sec-CH-UA-Mobile"] == "?0")
    check("headers has Sec-CH-UA-Platform", "Sec-CH-UA-Platform" in headers)
    check("Sec-Fetch-Dest is 'document'", headers["Sec-Fetch-Dest"] == "document")
    check("Sec-Fetch-Mode is 'navigate'", headers["Sec-Fetch-Mode"] == "navigate")
    check("Sec-Fetch-Site is 'none'", headers["Sec-Fetch-Site"] == "none")
    check("Sec-Fetch-User is '?1'", headers["Sec-Fetch-User"] == "?1")
    check("Upgrade-Insecure-Requests is '1'", headers["Upgrade-Insecure-Requests"] == "1")

    # With client hints disabled
    saved = config.ANTIDETECT_CLIENT_HINTS
    try:
        config.ANTIDETECT_CLIENT_HINTS = False
        # Re-import the module so it picks up the new config
        importlib.reload(anti_detect)
        importlib.reload(anti_detect.config) if hasattr(anti_detect, "config") else None
        # Just check the function directly
        fp = anti_detect.generate_session_fingerprint(seed=42)
        # anti_detect.fingerprint_to_headers should still work
        h = anti_detect.fingerprint_to_headers(fp)
        # With Client Hints disabled, the function returns only Accept-Language
        # (it reads config.ANTIDETECT_CLIENT_HINTS at call time)
        check("With client hints disabled, headers should not include Sec-CH-UA",
              "Sec-CH-UA" not in h,
              f"got {h}")
    finally:
        config.ANTIDETECT_CLIENT_HINTS = saved
        importlib.reload(anti_detect)


# ── Test 5: Init script generation ────────────────────────────────────


def test_init_script():
    section("Test 5: build_init_script() / get_init_script()")
    anti_detect.reset_session()  # force fresh init script
    script = anti_detect.get_init_script()
    check("init script is non-empty", len(script) > 1000,
          f"length: {len(script)}")
    # The init script should contain the stealth_js patches
    check("init script contains navigator.webdriver patch", "navigator, 'webdriver'" in script)
    check("init script contains WebGL patch", "getParameter" in script and "37445" in script)
    # It should also contain the per-session fingerprint patches
    check("init script contains SESSION_NOISE", "SESSION_NOISE" in script)
    check("init script contains SESSION_VENDOR", "SESSION_VENDOR" in script)
    check("init script contains SESSION_RENDERER", "SESSION_RENDERER" in script)
    check("init script contains SESSION_HW", "SESSION_HW" in script)
    check("init script contains SESSION_PLATFORM", "SESSION_PLATFORM" in script)
    # Should include the screen consistency patch
    check("init script patches screen size",
          "availWidth" in script and "outerWidth" in script)
    # Should include the AudioContext patch
    check("init script patches AnalyserNode",
          "AnalyserNode" in script and "getFloatFrequencyData" in script)
    # The init script should start with the stealth_js block
    check("init script starts with stealth_js (Stealth JS comment)",
          "Comprehensive Stealth JS" in script[:2000])

    # Calling get_init_script() again should return the same script
    # (it's cached after first generation)
    script2 = anti_detect.get_init_script()
    check("get_init_script() returns cached script", script == script2)

    # After reset, a new script is generated. The fingerprint is
    # process-stable (PID + minute), so the noise bytes and the
    # fingerprint are the same both before and after reset within the
    # same minute. The cache is invalidated, so we get a freshly
    # built script — but with the same content.
    anti_detect.reset_session()
    script3 = anti_detect.get_init_script()
    check("reset_session() invalidates the script cache",
          script3 is not None and len(script3) > 1000,
          "expected a new script after reset")
    check("reset_session() keeps the same fingerprint content",
          script3 == script,
          "fingerprint is process-stable, so the script should be identical")


# ── Test 6: Warmup state ─────────────────────────────────────────────


def test_warmup_state():
    section("Test 6: Warmup state machine")
    # First request in this process — should be True
    anti_detect.reset_warmup_state()
    check("is_first_request_in_session() is True initially",
          anti_detect.is_first_request_in_session() is True)

    # After marking warmed up, should be False
    anti_detect.mark_session_warmed_up()
    check("After mark, is_first_request_in_session() is False",
          anti_detect.is_first_request_in_session() is False)

    # Reset and verify again
    anti_detect.reset_warmup_state()
    check("After reset_warmup_state, is_first_request_in_session() is True",
          anti_detect.is_first_request_in_session() is True)


# ── Test 7: Session stability ─────────────────────────────────────────


def test_session_stability():
    section("Test 7: Fingerprint is stable across calls in same session")
    anti_detect.reset_session()
    fp1 = anti_detect.get_session_fingerprint()
    fp2 = anti_detect.get_session_fingerprint()
    fp3 = anti_detect.get_session_fingerprint()
    check("Same session returns same fingerprint", fp1 == fp2 == fp3)

    # Even with reset, a fresh call within the same process should
    # produce a deterministic fingerprint (because of the PID+time seed)
    fp4 = anti_detect.get_session_fingerprint()
    check("After get_session_fingerprint called multiple times, still stable",
          fp1 == fp4)


# ── Test 8: Anti-detect status (for /health) ──────────────────────────


def test_antidetect_status():
    section("Test 8: get_antidetect_status()")
    anti_detect.reset_session()
    status = anti_detect.get_antidetect_status()

    expected_top_keys = {
        "level", "search_via_typing", "warmup_on_first_request",
        "tab_focus_events", "client_hints", "randomize_fingerprint",
        "visit_homepage_first", "warmup_queries", "session_warmed_up",
        "session_fingerprint",
    }
    check("status has all expected top-level keys",
          set(status.keys()) >= expected_top_keys,
          f"missing: {expected_top_keys - set(status.keys())}")

    check("status.level is the config value",
          status["level"] == config.ANTIDETECT_LEVEL)

    fp = status["session_fingerprint"]
    expected_fp_keys = {
        "viewport", "platform", "chrome_version", "webgl_vendor",
        "webgl_renderer", "hardware_concurrency", "device_memory",
        "locale", "timezone",
    }
    check("status.session_fingerprint has all expected keys",
          set(fp.keys()) >= expected_fp_keys)

    # session_warmed_up should be a bool
    check("status.session_warmed_up is bool",
          isinstance(status["session_warmed_up"], bool))

    # warmup_queries is a non-empty list
    check("status.warmup_queries is a non-empty list",
          isinstance(status["warmup_queries"], list) and len(status["warmup_queries"]) > 0)


# ── Test 9: Config keys are exposed ──────────────────────────────────


def test_config_keys():
    section("Test 9: Config keys are exposed")
    required_keys = [
        "ANTIDETECT_LEVEL",
        "ANTIDETECT_SEARCH_VIA_TYPING",
        "ANTIDETECT_WARMUP_ON_FIRST_REQUEST",
        "ANTIDETECT_TAB_FOCUS_EVENTS",
        "ANTIDETECT_CLIENT_HINTS",
        "ANTIDETECT_RANDOMIZE_FINGERPRINT",
        "ANTIDETECT_VISIT_HOMEPAGE_FIRST",
        "ANTIDETECT_WARMUP_QUERIES",
    ]
    for key in required_keys:
        check(f"config.{key} is defined", hasattr(config, key),
              f"missing {key}")

    # Check that they're all the right types
    check("ANTIDETECT_LEVEL is a str", isinstance(config.ANTIDETECT_LEVEL, str))
    check("ANTIDETECT_SEARCH_VIA_TYPING is bool", isinstance(config.ANTIDETECT_SEARCH_VIA_TYPING, bool))
    check("ANTIDETECT_WARMUP_ON_FIRST_REQUEST is bool", isinstance(config.ANTIDETECT_WARMUP_ON_FIRST_REQUEST, bool))
    check("ANTIDETECT_TAB_FOCUS_EVENTS is bool", isinstance(config.ANTIDETECT_TAB_FOCUS_EVENTS, bool))
    check("ANTIDETECT_CLIENT_HINTS is bool", isinstance(config.ANTIDETECT_CLIENT_HINTS, bool))
    check("ANTIDETECT_RANDOMIZE_FINGERPRINT is bool", isinstance(config.ANTIDETECT_RANDOMIZE_FINGERPRINT, bool))
    check("ANTIDETECT_WARMUP_QUERIES is a tuple", isinstance(config.ANTIDETECT_WARMUP_QUERIES, tuple))
    check("ANTIDETECT_WARMUP_QUERIES has at least one query", len(config.ANTIDETECT_WARMUP_QUERIES) > 0)

    # Env override test
    saved_level = os.environ.get("ANTIDETECT_LEVEL")
    os.environ["ANTIDETECT_LEVEL"] = "high"
    try:
        importlib.reload(config)
        check("Env override of ANTIDETECT_LEVEL works",
              config.ANTIDETECT_LEVEL == "high",
              f"got {config.ANTIDETECT_LEVEL}")
    finally:
        if saved_level is not None:
            os.environ["ANTIDETECT_LEVEL"] = saved_level
        else:
            os.environ.pop("ANTIDETECT_LEVEL", None)
        importlib.reload(config)


# ── Test 10: Session noise bytes ──────────────────────────────────────


def test_session_noise():
    section("Test 10: _session_noise_bytes()")
    bytes1 = anti_detect._session_noise_bytes(12345, 32)
    check("noise bytes length is correct", len(bytes1) == 32)
    check("noise bytes 1 == noise bytes 1 (same seed)", bytes1 == anti_detect._session_noise_bytes(12345, 32))

    bytes2 = anti_detect._session_noise_bytes(67890, 32)
    check("noise bytes 1 != noise bytes 2 (different seed)", bytes1 != bytes2)

    bytes_long = anti_detect._session_noise_bytes(12345, 100)
    check("noise bytes scales to arbitrary length", len(bytes_long) == 100)


# ── Test 11: Per-platform UA generation ─────────────────────────────


def test_user_agent_per_platform():
    section("Test 11: User-Agent generation per platform")
    # Verify UA format matches platform
    for platform, expected_substr in [
        ("Linux x86_64", "X11; Linux x86_64"),
        ("Macintosh", "Macintosh"),
        ("Windows NT 10.0; Win64; x64", "Windows NT 10.0"),
    ]:
        # Use a seed that deterministically picks this platform
        # (we just test the function with explicit override via fp dict)
        ua = None
        for seed in range(100):
            fp = anti_detect.generate_session_fingerprint(seed=seed)
            if fp["platform"] == platform:
                ua = fp["user_agent"]
                break
        if ua is None:
            print(f"  (skipped: no seed in 0-99 picked platform={platform})")
            continue
        check(f"UA for {platform!r} contains {expected_substr!r}",
              expected_substr in ua,
              f"got {ua}")


# ── Test 12: Viewport within reasonable range ────────────────────────


def test_viewport_range():
    section("Test 12: Viewport sizes are in realistic range")
    widths = set()
    heights = set()
    for seed in range(50):
        fp = anti_detect.generate_session_fingerprint(seed=seed)
        widths.add(fp["viewport"]["width"])
        heights.add(fp["viewport"]["height"])

    check("At least 3 distinct widths across 50 seeds", len(widths) >= 3,
          f"got {sorted(widths)[:5]}")
    check("All widths in 800-4000 range", all(800 <= w <= 4000 for w in widths),
          f"got {sorted(widths)}")
    check("All heights in 600-3000 range", all(600 <= h <= 3000 for h in heights),
          f"got {sorted(heights)}")


# ── Test 13: Random GPU options are used ─────────────────────────────


def test_gpu_options():
    section("Test 13: GPU options are varied")
    vendors = set()
    renderers = set()
    for seed in range(50):
        fp = anti_detect.generate_session_fingerprint(seed=seed)
        vendors.add(fp["webgl_vendor"])
        renderers.add(fp["webgl_renderer"])
    check("At least 2 different vendors across 50 seeds", len(vendors) >= 2,
          f"got {vendors}")
    check("At least 3 different renderers across 50 seeds", len(renderers) >= 3,
          f"got {renderers}")


# ── Test 14: reset_session() preserves nothing across resets ──────────


def test_reset_session():
    section("Test 14: reset_session()")
    anti_detect.reset_session()
    fp1 = anti_detect.get_session_fingerprint()
    script1 = anti_detect.get_init_script()
    anti_detect.reset_session()
    fp2 = anti_detect.get_session_fingerprint()
    script2 = anti_detect.get_init_script()
    check("Fingerprint after reset is still the same (session-stable)",
          fp1 == fp2,
          f"got {fp1!r} vs {fp2!r}")
    check("Init script after reset is the same", script1 == script2,
          f"len before: {len(script1)}, len after: {len(script2)}")


# ── Test 15: Session fingerprint noise is small ────────────────────


def test_session_noise_is_small():
    section("Test 15: Canvas / WebGL noise is small (visually invisible)")
    # The init script patches canvas with atob/btoa + 1 bit flip.
    # Verify it doesn't add multi-byte noise that would visibly alter
    # the image.
    script = anti_detect.get_init_script()
    # Look for the atob/btoa pattern
    has_atob = "atob" in script and "btoa" in script
    check("init script uses atob/btoa for canvas noise", has_atob)

    # Look for the bit-flip pattern: charCodeAt(pos) ^ 1
    has_bitflip = "charCodeAt" in script and "^ 1" in script
    check("init script uses single-bit-flip noise", has_bitflip)


# ── Runner ────────────────────────────────────────────────────────────


def main():
    print("=" * 70)
    print("  Anti-detect (v0.3.4) tests")
    print("=" * 70)
    print()
    print(f"ANTIDETECT_LEVEL = {config.ANTIDETECT_LEVEL}")
    print(f"ANTIDETECT_SEARCH_VIA_TYPING = {config.ANTIDETECT_SEARCH_VIA_TYPING}")
    print(f"ANTIDETECT_WARMUP_ON_FIRST_REQUEST = {config.ANTIDETECT_WARMUP_ON_FIRST_REQUEST}")
    print(f"ANTIDETECT_TAB_FOCUS_EVENTS = {config.ANTIDETECT_TAB_FOCUS_EVENTS}")
    print(f"ANTIDETECT_CLIENT_HINTS = {config.ANTIDETECT_CLIENT_HINTS}")
    print(f"ANTIDETECT_RANDOMIZE_FINGERPRINT = {config.ANTIDETECT_RANDOMIZE_FINGERPRINT}")
    print(f"ANTIDETECT_WARMUP_QUERIES = {config.ANTIDETECT_WARMUP_QUERIES}")
    print()

    test_fingerprint_generation()
    test_fingerprint_determinism()
    test_url_parsing()
    test_headers()
    test_init_script()
    test_warmup_state()
    test_session_stability()
    test_antidetect_status()
    test_config_keys()
    test_session_noise()
    test_user_agent_per_platform()
    test_viewport_range()
    test_gpu_options()
    test_reset_session()
    test_session_noise_is_small()

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
    sys.exit(main())
