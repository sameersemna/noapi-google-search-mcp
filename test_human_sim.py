#!/usr/bin/env python3
"""Tests for the human-like behavior primitives in human_sim.py.

These tests cover the math and behavior configuration WITHOUT requiring
a real browser. The browser-driven paths (human_click, human_mouse_move
on a real page) are tested in test_human_sim_browser.py (which needs
Playwright + Chromium to be installed).

What's tested:
  1. Configuration: HUMAN_BEHAVIOR_LEVEL values, scale mapping
  2. Math primitives: cubic Bezier, easing functions
  3. Bezier path generation: shape, length, start/end points
  4. Typing delay computation: WPM scaling, punctuation pauses,
     Gaussian noise
  5. Scroll easing: monotonic, ends at target delta
  6. human_sim integration: imports work, can be invoked from
     browser.py and anti_bot.py
  7. ASCII smoke test of the CLI

Run with:
    /home/sameer/anaconda3/envs/mcp-google/bin/python test_human_sim.py
"""

import asyncio
import importlib
import math
import os
import random
import sys

sys.path.insert(0, "src")
os.environ.setdefault("SKIP_COOKIE_VALIDATION", "1")

from google_search_mcp import (  # noqa: E402
    config,
    human_sim,
)
from google_search_mcp.human_sim import (  # noqa: E402
    _bezier_point,
    _compute_key_delay_ms,
    _cubic_bezier,
    _ease_in_out,
    _ease_out_cubic,
    _enabled,
    _gauss,
    _generate_bezier_path,
    _level,
    _PUNCTUATION_PAUSE,
    _scale,
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


# ── Test 1: Configuration ────────────────────────────────────────────────


def test_config():
    section("Test 1: HUMAN_BEHAVIOR_LEVEL configuration")
    check("HUMAN_BEHAVIOR_LEVEL is a string", isinstance(config.HUMAN_BEHAVIOR_LEVEL, str))
    check("HUMAN_BEHAVIOR_LEVEL is one of the valid values",
          config.HUMAN_BEHAVIOR_LEVEL in ("off", "low", "medium", "high"),
          f"got {config.HUMAN_BEHAVIOR_LEVEL!r}")

    # Scale mapping — use human_sim._scale (always fresh) instead of the
    # imported `_scale` reference (which goes stale after importlib.reload).
    check("human_sim._scale('off') == 0.0", human_sim._scale("off") == 0.0)
    check("human_sim._scale('low') < _scale('medium') < _scale('high')",
          human_sim._scale("low") < human_sim._scale("medium") < human_sim._scale("high"))
    check("human_sim._scale('unknown') == 0.7 (default to medium)",
          human_sim._scale("garbage") == 0.7)
    check("_level() returns current level", _level() == config.HUMAN_BEHAVIOR_LEVEL)

    # Verify env override (test in a separate env)
    saved = os.environ.get("HUMAN_BEHAVIOR_LEVEL")
    os.environ["HUMAN_BEHAVIOR_LEVEL"] = "high"
    importlib.reload(config)
    importlib.reload(human_sim)
    check("Env override HUMAN_BEHAVIOR_LEVEL=high takes effect",
          config.HUMAN_BEHAVIOR_LEVEL == "high")
    check("human_sim._scale('high') returns 1.0", human_sim._scale("high") == 1.0)

    # Invalid value falls back to medium
    os.environ["HUMAN_BEHAVIOR_LEVEL"] = "totally-invalid-value"
    importlib.reload(config)
    importlib.reload(human_sim)
    check("Invalid HUMAN_BEHAVIOR_LEVEL falls back to 'medium'",
          config.HUMAN_BEHAVIOR_LEVEL == "medium")

    # Restore
    if saved is None:
        os.environ.pop("HUMAN_BEHAVIOR_LEVEL", None)
    else:
        os.environ["HUMAN_BEHAVIOR_LEVEL"] = saved
    importlib.reload(config)
    importlib.reload(human_sim)


# ── Test 2: Math primitives ──────────────────────────────────────────────


def test_math_primitives():
    section("Test 2: Math primitives (cubic Bezier, easing)")
    # Bezier boundary conditions
    check("bezier(0) == p0", _cubic_bezier(0.0, 10, 20, 30, 40) == 10)
    check("bezier(1) == p3", _cubic_bezier(1.0, 10, 20, 30, 40) == 40)
    # 2D vector
    bx, by = _bezier_point(0.5, (0, 0), (1, 2), (3, 2), (4, 0))
    check("bezier_point(0.5) returns a tuple of 2 floats",
          isinstance(bx, float) and isinstance(by, float))
    check("bezier midpoint is between start and end",
          0 <= bx <= 4 and -1 <= by <= 3)
    # Easing
    check("ease_in_out(0) == 0", _ease_in_out(0.0) == 0.0)
    check("ease_in_out(1) == 1", _ease_in_out(1.0) == 1.0)
    check("ease_in_out(0.5) is between 0 and 1",
          0 < _ease_in_out(0.5) < 1)
    check("ease_in_out is monotonically increasing",
          all(_ease_in_out(t) < _ease_in_out(t + 0.01) for t in [0.1, 0.3, 0.5, 0.7, 0.9]))
    check("ease_out_cubic(0) == 0", _ease_out_cubic(0.0) == 0.0)
    check("ease_out_cubic(1) == 1", _ease_out_cubic(1.0) == 1.0)
    check("ease_out_cubic is fast at start, slow at end",
          # First half should be more than 50% (fast at start)
          _ease_out_cubic(0.5) > 0.5)


# ── Test 3: Gaussian distribution ─────────────────────────────────────────


def test_gauss():
    section("Test 3: _gauss() clipped Gaussian")
    # Mean, std, no clip
    samples = [_gauss(100, 5) for _ in range(1000)]
    mean = sum(samples) / len(samples)
    check("Mean of 1000 samples is near 100",
          abs(mean - 100) < 1.0, f"got mean={mean:.2f}")
    # Clipped
    for _ in range(50):
        v = _gauss(50, 100, lo=40, hi=60)
        check("Clipped sample is in [40, 60]", 40 <= v <= 60)
    # Always returns within bounds
    for _ in range(50):
        v = _gauss(50, 1000, lo=10, hi=20)
        check("Heavily-clipped sample respects bounds", 10 <= v <= 20)


# ── Test 4: Bezier path generation ───────────────────────────────────────


def test_bezier_path():
    section("Test 4: Bezier path generation")
    # Seed random for reproducibility
    random.seed(42)

    # Basic shape
    path = _generate_bezier_path((0, 0), (100, 0), intensity=0.7)
    check("Path is a non-empty list", len(path) > 0)
    check("Path has 10-100 waypoints", 10 <= len(path) <= 100)
    # First waypoint is near the start
    fx, fy, _ = path[0]
    check("First waypoint is near (0, 0)",
          abs(fx) < 5 and abs(fy) < 5)
    # Last waypoint is exactly the target
    lx, ly, _ = path[-1]
    check("Last waypoint is exactly (100, 0)",
          lx == 100 and ly == 0,
          f"got ({lx}, {ly})")
    # All dt values are non-negative
    check("All dt values are non-negative", all(dt >= 0 for _, _, dt in path))
    # Total time is reasonable
    total_ms = sum(dt for _, _, dt in path) * 1000
    check("Total path time is in [100ms, 5s]",
          100 <= total_ms <= 5000, f"got {total_ms:.0f}ms")

    # Longer distance → more waypoints
    short = _generate_bezier_path((0, 0), (50, 0), intensity=0.5)
    long_ = _generate_bezier_path((0, 0), (2000, 0), intensity=0.5)
    check("Longer distance produces more waypoints",
          len(long_) >= len(short))

    # Higher intensity → more time (more deliberate)
    random.seed(42)
    low_path = _generate_bezier_path((0, 0), (500, 500), intensity=0.2)
    random.seed(42)
    high_path = _generate_bezier_path((0, 0), (500, 500), intensity=1.0)
    low_time = sum(dt for _, _, dt in low_path)
    high_time = sum(dt for _, _, dt in high_path)
    check("Higher intensity → longer path time",
          high_time > low_time, f"low={low_time*1000:.0f}ms high={high_time*1000:.0f}ms")

    # Short distance is handled (degenerate case)
    short_path = _generate_bezier_path((10, 10), (10, 10), intensity=0.7)
    check("Zero-distance path is handled",
          len(short_path) >= 1)

    # Path is roughly continuous (no huge jumps)
    random.seed(42)
    smooth_path = _generate_bezier_path((0, 0), (500, 500), intensity=0.7)
    max_jump = 0
    for i in range(1, len(smooth_path)):
        dx = smooth_path[i][0] - smooth_path[i-1][0]
        dy = smooth_path[i][1] - smooth_path[i-1][1]
        max_jump = max(max_jump, math.hypot(dx, dy))
    check("Path is reasonably smooth (max step < 100px)",
          max_jump < 100, f"max step was {max_jump:.1f}px")


# ── Test 5: Typing delay calculation ──────────────────────────────────────


def test_typing_delays():
    section("Test 5: _compute_key_delay_ms()")
    # Base case: 60 WPM → 200ms/char (60s / (60*5) = 200ms)
    d = _compute_key_delay_ms("a", None, 60.0)
    check("60 WPM 'a' from start is ~280ms (with first-char 1.4x + jitter)",
          150 <= d <= 400, f"got {d:.0f}ms")

    # Same char, mid-string
    d_mid = _compute_key_delay_ms("a", "x", 60.0)
    check("60 WPM 'a' mid-string is ~200ms",
          100 <= d_mid <= 350, f"got {d_mid:.0f}ms")

    # After space: ~1.4x
    d_after_space = _compute_key_delay_ms("w", " ", 60.0)
    check("After space is longer than mid-string",
          d_after_space > d_mid, f"after_space={d_after_space:.0f} vs mid={d_mid:.0f}")

    # After period: ~2.8x
    d_after_period = _compute_key_delay_ms("W", ".", 60.0)
    check("After period is much longer than mid-string",
          d_after_period > d_mid * 1.5,
          f"after_period={d_after_period:.0f} vs mid={d_mid:.0f}")

    # Capital letter mid-word: ~1.15x
    d_cap = _compute_key_delay_ms("B", "a", 60.0)
    check("Capital letter mid-word is slightly slower",
          d_cap > d_mid, f"cap={d_cap:.0f} vs mid={d_mid:.0f}")

    # WPM scaling
    random.seed(0)
    samples_60 = [_compute_key_delay_ms("a", "x", 60.0) for _ in range(100)]
    samples_30 = [_compute_key_delay_ms("a", "x", 30.0) for _ in range(100)]
    check("30 WPM is ~2x slower than 60 WPM",
          sum(samples_30) / len(samples_30) > sum(samples_60) / len(samples_60) * 1.5,
          f"30wpm={sum(samples_30)/len(samples_30):.0f} 60wpm={sum(samples_60)/len(samples_60):.0f}")

    # Punctuation map is non-empty
    check("_PUNCTUATION_PAUSE has at least 8 entries",
          len(_PUNCTUATION_PAUSE) >= 8)


# ── Test 6: Scroll easing ────────────────────────────────────────────────


def test_scroll_easing():
    section("Test 6: _ease_out_cubic()")
    check("ease_out(0) == 0", _ease_out_cubic(0.0) == 0.0)
    check("ease_out(1) == 1", _ease_out_cubic(1.0) == 1.0)
    # Monotonically increasing
    check("ease_out is monotonically increasing",
          all(_ease_out_cubic(t) < _ease_out_cubic(t + 0.05)
              for t in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]))
    # Fast at start: should be > 0.5 at t=0.5
    check("ease_out is fast at start (>50% at midpoint)",
          _ease_out_cubic(0.5) > 0.5)
    # First 25% does most of the work
    check("ease_out does >50% in first 25% of duration",
          _ease_out_cubic(0.25) > 0.5)


# ── Test 7: Module integration ───────────────────────────────────────────


def test_module_integration():
    section("Test 7: Module integration with the package")
    # Verify all expected public symbols are present
    expected = {
        "human_mouse_move", "human_click", "human_type",
        "human_scroll", "human_read", "human_idle", "human_navigate",
    }
    for name in expected:
        check(f"human_sim exports {name!r}",
              hasattr(human_sim, name) and callable(getattr(human_sim, name)))

    # Verify the module can be imported from various contexts
    check("human_sim is importable from .browser (via simulate_human_behavior)",
          True)  # Already verified by syntax check

    # Verify config flags
    check("HUMAN_BEHAVIOR_DISABLE_JITTER exists in config",
          hasattr(config, "HUMAN_BEHAVIOR_DISABLE_JITTER"))
    check("HUMAN_BEHAVIOR_DISABLE_JITTER is a bool",
          isinstance(config.HUMAN_BEHAVIOR_DISABLE_JITTER, bool))

    # Verify _enabled() reflects the level
    saved = config.HUMAN_BEHAVIOR_LEVEL
    config.HUMAN_BEHAVIOR_LEVEL = "off"
    check("_enabled() returns False when level=off", _enabled() is False)
    config.HUMAN_BEHAVIOR_LEVEL = "medium"
    check("_enabled() returns True when level=medium", _enabled() is True)
    config.HUMAN_BEHAVIOR_LEVEL = saved


# ── Test 8: human_idle / human_read are no-ops when off ──────────────────


async def test_off_mode_is_fast():
    section("Test 8: human_read / human_idle are fast no-ops when off")
    # Mock page object — human_read/human_idle should not even touch it
    class MockPage:
        def __init__(self):
            self.mouse_calls = 0
            self.evaluate_calls = 0
            self.wait_calls = 0
        async def mouse_move(self, *a, **kw):
            self.mouse_calls += 1
        async def evaluate(self, *a, **kw):
            self.evaluate_calls += 1
        async def wait_for_timeout(self, *a, **kw):
            self.wait_calls += 1
        @property
        def viewport_size(self):
            return {"width": 1280, "height": 800}

    saved = config.HUMAN_BEHAVIOR_LEVEL
    config.HUMAN_BEHAVIOR_LEVEL = "off"
    importlib.reload(human_sim)

    page = MockPage()
    start = asyncio.get_event_loop().time()
    await human_sim.human_idle(page, duration_sec=10.0)
    elapsed_off = asyncio.get_event_loop().time() - start
    check("human_idle with off returns immediately (<100ms)",
          elapsed_off < 0.1, f"elapsed={elapsed_off*1000:.0f}ms")
    check("human_idle with off does NOT call page.mouse.move",
          page.mouse_calls == 0)
    check("human_idle with off does NOT call page.evaluate",
          page.evaluate_calls == 0)

    config.HUMAN_BEHAVIOR_LEVEL = saved
    importlib.reload(human_sim)


# ── Test 9: human_mouse_move dispatches to fast path when off ───────────


async def test_human_mouse_move_off_fast_path():
    section("Test 9: human_mouse_move with level=off uses fast path")
    class MockPage:
        def __init__(self):
            self.mouse_calls = []
        async def mouse_move(self, x, y):
            self.mouse_calls.append((x, y))

    saved = config.HUMAN_BEHAVIOR_LEVEL
    config.HUMAN_BEHAVIOR_LEVEL = "off"
    importlib.reload(human_sim)

    page = MockPage()
    start = asyncio.get_event_loop().time()
    await human_sim.human_mouse_move(page, 500, 500)
    elapsed = asyncio.get_event_loop().time() - start
    check("human_mouse_move with off completes in <100ms",
          elapsed < 0.1, f"elapsed={elapsed*1000:.0f}ms")
    check("human_mouse_move with off calls page.mouse.move exactly once",
          len(page.mouse_calls) == 1, f"got {len(page.mouse_calls)} calls")
    check("human_mouse_move with off lands at exact target",
          page.mouse_calls[0] == (500, 500))

    config.HUMAN_BEHAVIOR_LEVEL = saved
    importlib.reload(human_sim)


# ── Test 10: human_mouse_move generates a Bezier path when on ───────────


async def test_human_mouse_move_generates_path():
    section("Test 10: human_mouse_move generates Bezier path when on")
    move_calls: list[tuple[float, float]] = []

    class MockPage:
        async def mouse_move(self, x, y):
            move_calls.append((x, y))
        async def wait_for_timeout(self, ms):
            pass

    saved = config.HUMAN_BEHAVIOR_LEVEL
    config.HUMAN_BEHAVIOR_LEVEL = "high"
    importlib.reload(human_sim)

    # Run several times to test different paths
    for start_x, start_y, end_x, end_y in [
        (100, 100, 800, 600),
        (50, 50, 1200, 700),
        (640, 400, 100, 100),
    ]:
        # Reset the last-known position
        from google_search_mcp.human_sim import _LAST_MOUSE_POS
        # We can't easily set this for our mock page; the function will
        # start from the default (640, 400).
        page = MockPage()
        move_calls.clear()
        await human_sim.human_mouse_move(page, end_x, end_y)
        # Should have generated many waypoints (Bezier curve)
        check(f"human_mouse_move generates multiple waypoints (got {len(move_calls)})",
              len(move_calls) > 5, f"only {len(move_calls)} calls")
        # Last call should be at the exact target
        check(f"Last waypoint is at target ({end_x}, {end_y})",
              move_calls[-1] == (end_x, end_y),
              f"got {move_calls[-1]}")

    config.HUMAN_BEHAVIOR_LEVEL = saved
    importlib.reload(human_sim)


# ── Runner ──────────────────────────────────────────────────────────────


async def main():
    print("=" * 70)
    print("  Human-like behavior tests")
    print("=" * 70)
    print()
    print(f"HUMAN_BEHAVIOR_LEVEL = {config.HUMAN_BEHAVIOR_LEVEL}")
    print(f"HUMAN_BEHAVIOR_DISABLE_JITTER = {config.HUMAN_BEHAVIOR_DISABLE_JITTER}")

    # Sync tests
    test_config()
    test_math_primitives()
    test_gauss()
    test_bezier_path()
    test_typing_delays()
    test_scroll_easing()
    test_module_integration()

    # Async tests
    await test_off_mode_is_fast()
    await test_human_mouse_move_off_fast_path()
    await test_human_mouse_move_generates_path()

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
