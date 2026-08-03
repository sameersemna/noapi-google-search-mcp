"""Human-like behavior primitives for evading bot detection.

Modern bot detection (Google reCAPTCHA, "unusual traffic" filters) does
not just look at JavaScript fingerprints — it actively observes
*behavior* during the session. A bot that:

  * moves the cursor in a perfectly straight line,
  * clicks instantly on arrival without any hover or aim time,
  * scrolls in 1-frame chunks instead of with momentum,
  * types every character at the exact same speed with no pauses,
  * hovers over the same pixel for 30 seconds while "reading",

is trivially distinguishable from a real human.

This module provides primitives that mimic real human behavior:

  * ``human_mouse_move`` — Cubic Bezier curve mouse trajectory with
    variable speed (ease-in-out) and end-of-move micro-corrections.

  * ``human_click`` — hover near target → fine-aim → pause → click,
    with a post-click drift.

  * ``human_type`` — Variable WPM with Gaussian noise, special pauses
    after spaces / punctuation, occasional thinking bursts, and
    optional typo-and-correct cycles.

  * ``human_scroll`` — Smooth scroll with cubic ease-out and
    momentum decay, occasional overshoot, and rare back-scrolls.

  * ``human_read`` / ``human_idle`` — Dwell-time wait with subtle
    mouse jitter and the occasional micro-scroll, simulating a
    human reading the page.

  * ``human_navigate`` — Navigate + wait for load + small idle, so
    every page load feels "looked at" before any extraction.

Every primitive is a no-op or fast-path when
``HUMAN_BEHAVIOR_LEVEL=off`` is set, and intensity scales up through
``low`` → ``medium`` → ``high``.

All functions are async and accept a Playwright ``Page``. They never
throw on missing elements / closed pages — the worst they do is
silently no-op so the rest of the pipeline still works.
"""

from __future__ import annotations

import asyncio
import math
import random
import sys
from typing import TYPE_CHECKING, Any, Optional, Sequence

from . import config

if TYPE_CHECKING:
    from playwright.async_api import Page


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════

def _level() -> str:
    """Return the current HUMAN_BEHAVIOR_LEVEL (cached at config import)."""
    return config.HUMAN_BEHAVIOR_LEVEL


def _enabled() -> bool:
    """Return True if any human behavior should be applied."""
    return _level() != "off"


def _scale(level: str) -> float:
    """Return a 0-1 multiplier representing how 'human' to behave.

    Used to scale durations / step counts / overshoot amounts.
    """
    return {"off": 0.0, "low": 0.35, "medium": 0.7, "high": 1.0}.get(_level(), 0.7)


def _gauss(mean: float, std: float, lo: float = 0.0, hi: float = float("inf")) -> float:
    """Sample from a clipped Gaussian distribution."""
    for _ in range(8):
        v = random.gauss(mean, std)
        if lo <= v <= hi:
            return v
    return max(lo, min(hi, mean))


def _cubic_bezier(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
    """Evaluate a 1-D cubic Bezier curve at parameter t in [0, 1]."""
    u = 1.0 - t
    return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3

def _bezier_point(t: float, p0: tuple[float, float], p1: tuple[float, float],
                 p2: tuple[float, float], p3: tuple[float, float]) -> tuple[float, float]:
    """Evaluate a 2-D cubic Bezier curve at parameter t in [0, 1]."""
    return (
        _cubic_bezier(t, p0[0], p1[0], p2[0], p3[0]),
        _cubic_bezier(t, p0[1], p1[1], p2[1], p3[1]),
    )

def _ease_in_out(t: float) -> float:
    """Cubic ease-in-out — slow start, fast middle, slow end."""
    return _cubic_bezier(t, 0.0, 0.4, 0.6, 1.0)


def _ease_out_cubic(t: float) -> float:
    """Cubic ease-out — fast start, slow end (momentum decay)."""
    return _cubic_bezier(t, 0.0, 0.2, 0.7, 1.0)


def _generate_bezier_path(
    start: tuple[float, float],
    end: tuple[float, float],
    intensity: float = 0.7,
) -> list[tuple[float, float, float]]:
    """Generate a list of (x, y, dt) waypoints along a Bezier curve.

    The curve is a cubic Bezier with two control points offset
    perpendicular to the line from start to end, simulating a natural
    hand arc. ``dt`` is the *delta* time in seconds since the previous
    waypoint (so the caller can sleep for the right amount). The
    speed is variable: ease-in-out for the main path, plus a small
    "micro-correction" at the end (over-shoot → correction).

    Args:
        start: (x, y) starting position.
        end: (x, y) target position.
        intensity: 0-1, how much to bend the curve and add corrections.

    Returns:
        List of (x, y, dt_seconds) waypoints, ending exactly at ``end``.
    """
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    dist = math.hypot(dx, dy)
    if dist < 1.0:
        # Too short to bother with a curve
        return [(ex, ey, 0.05)]

    # Perpendicular unit vector
    perp_x, perp_y = -dy / dist, dx / dist

    # Total path duration scales with distance (humans move ~600px/s
    # for a deliberate motion, faster for a flic). Cap it so we don't
    # wait 10 seconds for a long move.
    base_ms = max(150.0, min(1200.0, dist * 1.2))
    # Add random variation ±20%
    base_ms *= _gauss(1.0, 0.18, 0.6, 1.5)
    # Intensity controls slow-down (more intense = more deliberate)
    base_ms *= 1.0 + (intensity * 0.6)
    total_ms = base_ms

    # Control points: offset perpendicular by ~25% of the distance,
    # in random directions (one each side), so the curve bends.
    bend_amt = dist * 0.18 * intensity
    bend_dir1 = random.uniform(0.6, 1.0) * (1 if random.random() < 0.5 else -1)
    bend_dir2 = random.uniform(0.6, 1.0) * (1 if random.random() < 0.5 else -1)

    # 1/3 and 2/3 along the line, offset perpendicular
    p1x = sx + dx * 0.33 + perp_x * bend_amt * bend_dir1
    p1y = sy + dy * 0.33 + perp_y * bend_amt * bend_dir1
    p2x = sx + dx * 0.66 + perp_x * bend_amt * bend_dir2
    p2y = sy + dy * 0.66 + perp_y * bend_amt * bend_dir2

    # Number of waypoints scales with distance (more for long moves)
    n = max(12, min(60, int(dist / 12)))
    pts: list[tuple[float, float, float]] = []
    px, py = sx, sy
    for i in range(1, n + 1):
        t = i / n
        # Apply ease-in-out for natural speed
        # First 85% follows the main curve, last 15% is micro-corrections
        if t < 0.85:
            tt = t / 0.85
            eased = _ease_in_out(tt)
            bx = _cubic_bezier(eased, sx, p1x, p2x, ex)
            by = _cubic_bezier(eased, sy, p1y, p2y, ey)
            # Tiny sub-pixel jitter to break perfect smoothness
            bx += random.gauss(0, 0.4)
            by += random.gauss(0, 0.4)
        else:
            # End-of-move micro-corrections: overshoot ~3-8px then
            # correct back, scaled by intensity.
            tt = (t - 0.85) / 0.15
            overshoot = (1 - tt) * random.uniform(3, 8) * intensity
            angle = random.uniform(0, 2 * math.pi)
            bx = ex + math.cos(angle) * overshoot
            by = ey + math.sin(angle) * overshoot

        # dt: variable per-step time so the move has natural pacing.
        # The whole move takes `total_ms`; we add a small random jitter
        # to each step.
        dt_ms = (total_ms / n) * random.uniform(0.5, 1.6)
        pts.append((bx, by, dt_ms / 1000.0))
        px, py = bx, by

    # Always end exactly on the target
    pts.append((ex, ey, 0.04))
    return pts


async def _move_path(page: "Page", waypoints: list[tuple[float, float, float]]) -> None:
    """Execute a list of (x, y, dt) waypoints via page.mouse.move."""
    for x, y, dt in waypoints:
        try:
            await page.mouse.move(x, y)
        except Exception:
            # Mouse may be off-viewport or page closed; ignore
            return
        if dt > 0:
            await page.wait_for_timeout(int(dt * 1000))


async def _safe_page_size(page: "Page") -> tuple[int, int]:
    """Return (width, height) of the viewport, with a safe fallback."""
    try:
        vp = page.viewport_size
        if vp and vp.get("width") and vp.get("height"):
            return int(vp["width"]), int(vp["height"])
    except Exception:
        pass
    return 1280, 800


async def _current_mouse_pos(page: "Page") -> tuple[float, float]:
    """Best-effort: where is the mouse cursor right now?

    Playwright doesn't expose the cursor position directly via Python
    API, so we use a heuristic: keep a module-level last-known position
    and update it after every move.
    """
    return _LAST_MOUSE_POS.get(id(page), (640.0, 400.0))


_LAST_MOUSE_POS: dict[int, tuple[float, float]] = {}


def _set_last_mouse_pos(page: "Page", x: float, y: float) -> None:
    _LAST_MOUSE_POS[id(page)] = (x, y)


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

async def human_mouse_move(
    page: "Page",
    target_x: float,
    target_y: float,
    *,
    duration_ms: int | None = None,
    intensity: float | None = None,
) -> None:
    """Move the mouse to ``(target_x, target_y)`` along a human-like Bezier.

    Variable speed (cubic ease-in-out), slight perpendicular curve,
    sub-pixel jitter, and a final micro-correction overshoot.
    A no-op when ``HUMAN_BEHAVIOR_LEVEL=off``.
    """
    if not _enabled():
        # Even when "off", still place the cursor instantly so subsequent
        # code that reads its position works.
        try:
            await page.mouse.move(target_x, target_y)
        except Exception:
            pass
        _set_last_mouse_pos(page, target_x, target_y)
        return

    scale = intensity if intensity is not None else _scale(_level())
    if scale <= 0:
        try:
            await page.mouse.move(target_x, target_y)
        except Exception:
            pass
        _set_last_mouse_pos(page, target_x, target_y)
        return

    cur_x, cur_y = await _current_mouse_pos(page)
    path = _generate_bezier_path((cur_x, cur_y), (target_x, target_y), intensity=scale)
    if duration_ms is not None:
        # Caller wants a specific duration — rescale the per-step dts
        cur_total = sum(dt for _, _, dt in path) or 0.001
        target = duration_ms / 1000.0
        path = [(x, y, dt * target / cur_total) for (x, y, dt) in path]
    await _move_path(page, path)
    _set_last_mouse_pos(page, target_x, target_y)


async def human_click(
    page: "Page",
    selector_or_xy: str | tuple[float, float],
    *,
    hover_ms: int | None = None,
    button: str = "left",
    click_count: int = 1,
) -> bool:
    """Click an element (or coordinates) with a human-like approach pattern.

    The full sequence is:
      1. Move the cursor along a Bezier curve to a point ~25-40px away
         from the target.
      2. Hover briefly (200-500ms).
      3. Fine-tune: move to the exact click point with a short Bezier
         of its own.
      4. Hover again (100-300ms, the "aim" time).
      5. Click.
      6. ~30% chance of a tiny post-click drift.

    ``selector_or_xy`` may be:
      * a CSS selector string — the element is scrolled into view,
        its center is used as the click target.
      * an ``(x, y)`` tuple — clicked at those absolute coordinates.

    Returns ``True`` if the click was attempted, ``False`` if the
    selector didn't match (or the page was closed).
    """
    if not _enabled():
        # Fast path: just click immediately
        try:
            if isinstance(selector_or_xy, tuple):
                x, y = selector_or_xy
                await page.mouse.click(x, y, button=button, click_count=click_count)
            else:
                await page.click(selector_or_xy, button=button, click_count=click_count)
        except Exception:
            return False
        return True

    try:
        if isinstance(selector_or_xy, tuple):
            tx, ty = selector_or_xy
        else:
            loc = page.locator(selector_or_xy).first
            await loc.scroll_into_view_if_needed(timeout=2000)
            await page.wait_for_timeout(random.randint(80, 200))
            box = await loc.bounding_box()
            if not box:
                return False
            # Click slightly off-center (humans rarely hit the exact middle)
            cx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            cy = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            tx, ty = cx, cy

        # Step 1: move to a point ~25-40px from the target (offset by a
        # random angle). This is the "approach" — humans don't move
        # directly to the target, they swing past it.
        angle = random.uniform(0, 2 * math.pi)
        radius = random.uniform(25, 45)
        approach_x = tx + math.cos(angle) * radius
        approach_y = ty + math.sin(angle) * radius
        await human_mouse_move(page, approach_x, approach_y)

        # Step 2: hover briefly
        await page.wait_for_timeout(random.randint(180, 480))

        # Step 3: fine-tune to exact target
        await human_mouse_move(page, tx, ty, duration_ms=random.randint(140, 280))

        # Step 4: aim time
        aim_ms = hover_ms if hover_ms is not None else random.randint(100, 320)
        await page.wait_for_timeout(aim_ms)

        # Step 5: click
        await page.mouse.click(tx, ty, button=button, click_count=click_count)
        _set_last_mouse_pos(page, tx, ty)

        # Step 6: 30% chance of a small post-click drift (mouse drifts
        # after the click, like a real hand relaxing).
        if random.random() < 0.3:
            drift_x = tx + random.gauss(0, 4)
            drift_y = ty + random.gauss(0, 4)
            await page.mouse.move(drift_x, drift_y)
            _set_last_mouse_pos(page, drift_x, drift_y)

        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Natural typing
# ═══════════════════════════════════════════════════════════════════════════

# Pause multipliers after certain characters
_PUNCTUATION_PAUSE: dict[str, float] = {
    " ": 1.4,        # word boundary
    ".": 2.8,        # sentence end
    ",": 1.8,        # clause
    ";": 1.8,
    ":": 1.8,
    "!": 2.5,
    "?": 2.5,
    "\n": 3.5,       # newline
    "-": 1.3,
    "(": 1.4,
    ")": 1.4,
    '"': 1.4,
    "'": 1.2,
}


def _compute_key_delay_ms(char: str, prev_char: str | None, wpm: float) -> float:
    """Compute the delay (ms) before pressing ``char``.

    Real typing has:
      - A base inter-key delay that maps to ~WPM (60 WPM ≈ 200ms/char)
      - Per-character Gaussian jitter (σ ≈ 15-20ms)
      - Longer pauses after word boundaries, sentence ends, etc.
      - Occasional thinking bursts (handled at a higher level)
    """
    # Base delay: 60s/min ÷ (WPM * 5 chars/word) = ms/char
    base = 60000.0 / max(wpm * 5.0, 1.0)
    # Gaussian jitter — 0.7-1.3x base
    jitter = _gauss(1.0, 0.16, 0.6, 1.6)
    delay = base * jitter

    # Capital letter: slightly slower (you have to shift)
    if char.isupper() and prev_char is not None and prev_char != " ":
        delay *= 1.15

    # Number: slightly slower
    if char.isdigit():
        delay *= 1.1

    # First char: longer (orientation time)
    if prev_char is None:
        delay *= 1.4

    # Pause after previous punctuation
    if prev_char is not None and prev_char in _PUNCTUATION_PAUSE:
        delay *= _PUNCTUATION_PAUSE[prev_char]

    return delay


async def human_type(
    page: "Page",
    selector: str,
    text: str,
    *,
    wpm: float | None = None,
    clear_first: bool = True,
    typo_rate: float = 0.0,
) -> bool:
    """Type ``text`` into ``selector`` with natural human typing rhythm.

    Variable WPM (default 60 ± 20), Gaussian inter-key jitter, longer
    pauses after word/punctuation boundaries, and an occasional
    "thinking burst" every 5-12 characters. If ``typo_rate`` > 0,
    occasional typos are inserted and then corrected (a
    backspace-and-retype pattern).

    Returns True on success, False on failure (element not found,
    not editable, etc.).
    """
    if not _enabled():
        # Fast path: fill the field, no human behavior
        try:
            loc = page.locator(selector).first
            if clear_first:
                await loc.fill("")
            await loc.fill(text)
        except Exception:
            return False
        return True

    try:
        loc = page.locator(selector).first
        await loc.scroll_into_view_if_needed(timeout=2000)
        # Click into the field (humans do this)
        await human_click(page, selector)
        # Tiny pause after focusing the field
        await page.wait_for_timeout(random.randint(150, 350))

        if clear_first:
            # Select all + delete (a common human pattern)
            try:
                await page.keyboard.press("Control+A")
                await page.wait_for_timeout(random.randint(50, 120))
                await page.keyboard.press("Delete")
                await page.wait_for_timeout(random.randint(80, 180))
            except Exception:
                # Fallback: triple-click + delete
                try:
                    await loc.click(click_count=3)
                    await page.keyboard.press("Delete")
                except Exception:
                    pass

        # WPM
        actual_wpm = wpm if wpm is not None else _gauss(60.0, 12.0, 35.0, 95.0)
        # Higher HUMAN_BEHAVIOR_LEVEL = more realistic (slower) typing
        level_mult = {"low": 0.85, "medium": 1.0, "high": 1.15}.get(_level(), 1.0)
        actual_wpm /= level_mult

        prev_char: str | None = None
        i = 0
        while i < len(text):
            ch = text[i]
            # Should we insert a typo here?
            if typo_rate > 0 and ch.isalnum() and random.random() < typo_rate:
                # Pick a near-keyboard key as the typo
                nearby = (
                    "abcdefghijklmnopqrstuvwxyz"
                    if ch.isalpha() else "0123456789"
                )
                typo = random.choice([c for c in nearby if c != ch.lower()])
                if ch.isupper():
                    typo = typo.upper()
                await page.keyboard.type(typo)
                # Pause as if we noticed the mistake
                await page.wait_for_timeout(int(_gauss(450, 100, 200, 800)))
                # Backspace + retype the correct char
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(int(_gauss(120, 40, 50, 250)))
                await page.keyboard.type(ch)
            else:
                # Normal key press
                await page.keyboard.type(ch)
                # Wait for the per-char delay
                delay_ms = _compute_key_delay_ms(ch, prev_char, actual_wpm)
                # Add small jitter to each wait so it's not exact
                delay_ms *= random.uniform(0.85, 1.15)
                await page.wait_for_timeout(int(delay_ms))

            prev_char = ch
            i += 1

            # Every 5-12 chars, insert a "thinking burst" pause (250-600ms)
            if i % random.randint(5, 12) == 0 and i < len(text):
                await page.wait_for_timeout(int(_gauss(380, 100, 200, 700)))
    except Exception:
        return False

    # Small pause after the last char (humans don't instantly submit)
    await page.wait_for_timeout(int(_gauss(150, 60, 60, 350)))
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Smooth scroll
# ═══════════════════════════════════════════════════════════════════════════

async def human_scroll(
    page: "Page",
    delta_y: int,
    *,
    duration_ms: int | None = None,
    overshoot_chance: float = 0.15,
    backscroll_chance: float = 0.0,
) -> None:
    """Scroll the page by ``delta_y`` pixels with smooth momentum decay.

    Uses cubic ease-out so the scroll starts fast and decelerates at
    the end (mimicking a finger flick + momentum). If
    ``overshoot_chance > 0`` there's a small chance of overshooting by
    a few pixels and correcting back. ``backscroll_chance > 0`` adds
    a small chance of a tiny upward scroll after the main one (the
    common "scroll down, see something, scroll back up" pattern).

    A no-op when ``HUMAN_BEHAVIOR_LEVEL=off``.
    """
    if not _enabled() or delta_y == 0:
        # Instant scroll
        try:
            await page.evaluate(f"window.scrollBy({{top: {delta_y}, behavior: 'instant'}})")
        except Exception:
            pass
        return

    scale = _scale(_level())
    # Duration scales with distance and intensity
    base = max(220.0, min(900.0, abs(delta_y) * 1.4))
    base *= 1.0 + (scale * 0.4)
    if duration_ms is not None:
        base = duration_ms
    total_ms = base

    # Number of steps scales with intensity
    n = max(10, min(40, int(12 + scale * 14)))

    # Direction
    sign = 1 if delta_y > 0 else -1
    magnitude = abs(delta_y)

    # Apply overshoot?
    if random.random() < overshoot_chance * scale:
        magnitude = int(magnitude * random.uniform(1.05, 1.18))

    last_y = 0
    for i in range(1, n + 1):
        t = i / n
        if t < 0.85:
            tt = t / 0.85
            eased = _ease_out_cubic(tt)
            y = magnitude * eased
        else:
            # Last 15%: overshoot and correct (or just settle)
            tt = (t - 0.85) / 0.15
            # Eases back to final position
            y = magnitude * (1.0 + (1.0 - tt) * 0.04)
        step = int(y) - last_y
        if step != 0:
            try:
                await page.evaluate(
                    f"window.scrollBy({{top: {step}, behavior: 'instant'}})"
                )
            except Exception:
                return
            last_y = int(y)
        # Variable per-step sleep so the scroll isn't perfectly uniform
        step_ms = (total_ms / n) * random.uniform(0.6, 1.4)
        await page.wait_for_timeout(int(step_ms))

    # Optional: small back-scroll (humans do this when they see something
    # in their peripheral vision and want to re-read it)
    if backscroll_chance > 0 and random.random() < backscroll_chance * scale:
        back = -sign * random.randint(8, 35)
        try:
            await page.evaluate(
                f"window.scrollBy({{top: {back}, behavior: 'instant'}})"
            )
            await page.wait_for_timeout(int(_gauss(300, 100, 150, 500)))
            # And then re-scroll back to the intended position
            await page.evaluate(
                f"window.scrollBy({{top: {-back}, behavior: 'instant'}})"
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Reading / idle / navigation
# ═══════════════════════════════════════════════════════════════════════════

async def human_idle(
    page: "Page",
    duration_sec: float | None = None,
) -> None:
    """Simulate a human "just sitting there" with subtle mouse jitter.

    Adds small (1-3px) random mouse movements every 100-300ms to
    simulate a hand resting on the mouse. This is what a real user
    does when "reading" a page.

    A no-op when ``HUMAN_BEHAVIOR_LEVEL=off``.
    """
    if not _enabled():
        return
    if duration_sec is None:
        scale = _scale(_level())
        duration_sec = _gauss(0.8, 0.3, 0.3, 1.5) * scale
    if duration_sec <= 0:
        return
    end_at = asyncio.get_event_loop().time() + duration_sec
    while asyncio.get_event_loop().time() < end_at:
        # Small jitter: ±1-3px in a random direction
        cur_x, cur_y = await _current_mouse_pos(page)
        nx = cur_x + random.gauss(0, 1.5)
        ny = cur_y + random.gauss(0, 1.5)
        # Clamp to viewport
        w, h = await _safe_page_size(page)
        nx = max(2, min(w - 2, nx))
        ny = max(2, min(h - 2, ny))
        try:
            await page.mouse.move(nx, ny)
        except Exception:
            return
        _set_last_mouse_pos(page, nx, ny)
        await page.wait_for_timeout(random.randint(100, 300))


async def human_read(
    page: "Page",
    duration_sec: float | None = None,
    *,
    do_micro_scroll: bool = True,
) -> None:
    """Simulate a human reading the page: wait with idle micro-movements.

    This is what runs after a page loads and before any extraction
    starts — giving the user-agent a realistic "looked at this page
    for a while" dwell time. Optionally includes a tiny scroll to
    simulate the user adjusting their reading position.

    A no-op (or near-no-op) when ``HUMAN_BEHAVIOR_LEVEL=off``.
    """
    if not _enabled():
        return
    if duration_sec is None:
        scale = _scale(_level())
        # Default dwell time on a search results page: ~1.5-3.5s
        # At higher intensity we wait longer.
        duration_sec = _gauss(1.5, 0.6, 0.6, 3.5) * (0.5 + scale * 0.8)
    if duration_sec <= 0:
        return

    # Split the reading time into chunks with idle + occasional micro-scroll
    elapsed = 0.0
    while elapsed < duration_sec:
        chunk = min(duration_sec - elapsed, random.uniform(0.4, 0.9))
        await human_idle(page, duration_sec=chunk)
        elapsed += chunk
        if do_micro_scroll and random.random() < 0.35:
            # 35% chance of a tiny micro-scroll during the read
            delta = random.randint(-30, 30)
            if delta != 0:
                try:
                    await page.evaluate(
                        f"window.scrollBy({{top: {delta}, behavior: 'instant'}})"
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(int(_gauss(180, 60, 80, 320)))


async def human_navigate(
    page: "Page",
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    read_sec: float | None = None,
) -> None:
    """Navigate to a URL, wait for it to load, then ``human_read``.

    Convenience wrapper that combines page.goto + a short dwell so
    the page isn't scraped the instant it loads.
    """
    try:
        await page.goto(url, wait_until=wait_until, timeout=30000)
    except Exception:
        # Caller can handle the failure; we still try to read
        pass
    if read_sec is not None:
        await human_read(page, duration_sec=read_sec)
    else:
        await human_read(page)


# ═══════════════════════════════════════════════════════════════════════════
# CLI for ad-hoc testing
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:  # pragma: no cover
    """Quick sanity / smoke test of the math (no browser required)."""
    print(f"HUMAN_BEHAVIOR_LEVEL = {_level()}")
    print(f"scale = {_scale(_level())}")
    print()

    print("── Bezier path example ──")
    path = _generate_bezier_path((100, 100), (800, 600), intensity=0.7)
    print(f"start: (100, 100)  end: (800, 600)")
    print(f"total waypoints: {len(path)}")
    print(f"total time: {sum(dt for _, _, dt in path)*1000:.0f}ms")
    print(f"first 3: {path[:3]}")
    print(f"last 3: {path[-3:]}")

    print()
    print("── Typing delay example (60 WPM, 'Hello, world!') ──")
    total = 0.0
    prev = None
    for ch in "Hello, world!":
        d = _compute_key_delay_ms(ch, prev, 60.0)
        total += d
        print(f"  '{ch}': {d:.0f}ms")
        prev = ch
    if total > 0:
        print(f"  total: {total:.0f}ms = {total/1000:.2f}s for 13 chars (~{13*60/(total/60000):.0f} WPM)")
    else:
        print(f"  total: {total:.0f}ms")

    print()
    print("── Scroll easing (delta=300) ──")
    for t in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        print(f"  t={t:.1f} → y={300*_ease_out_cubic(t):.0f}px")


if __name__ == "__main__":  # pragma: no cover
    main()
