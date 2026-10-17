# ADR 0002 — Shared browser pool with a concurrency cap

**Status:** accepted (2026-10-15)
**Phase:** 4

## Context

Every browser-based tool call previously did:

```python
async with async_playwright() as pw:
    context = await launch_browser(pw)      # launch_persistent_context(BROWSER_DATA_DIR)
    ...
    await context.close()
```

Consequences:

1. **Per-call Chromium launch** — typically 1–3 s of pure overhead and heavy RAM.
2. **A real concurrency bug.** `launch_browser` uses
   `pw.chromium.launch_persistent_context(BROWSER_DATA_DIR)`. Chromium enforces a
   single-instance lock on a user-data-dir, so two concurrent tool calls fight over the
   same directory. This matches the recorded field symptom: an LLM firing several
   `google_search` calls at once produced "Block persists after retries (reason=unknown)".
3. **No concurrency limit** — N parallel calls meant N Chromium processes.

The hardening brief requires reliability to come from sending **fewer** requests
(caching, rate limiting, back-off, circuit breaking) rather than better disguise, so
this change must **not** alter the stealth/fingerprint strategy.

## Decision

Add `core/browser.py` with a process-wide `BrowserPool`:

* **Lazy start.** The Playwright instance and one persistent Chromium context are created
  on the first `page()` call — server startup stays fast.
* **Page per call, context shared.** Each call gets a fresh `context.new_page()` (cheap,
  isolated document) from the single shared context, preserving the existing
  `BROWSER_DATA_DIR` fingerprint/cookie behaviour exactly.
* **Concurrency cap.** `asyncio.Semaphore(config.MAX_BROWSERS)` (default 3) wraps page
  acquisition, bounding Chromium contexts/processes.
* **Auto-relaunch.** If the context/browser disconnects (crash), the pool is marked dead
  and the next `page()` relaunches it.
* **Opt-out.** `NOAPI_MCP_BROWSER_POOL=0` restores launch-per-call behaviour if ever needed.
* **Shutdown.** The FastMCP lifespan closes the pool on exit.

Headful / manual-intervention flows keep launching their own visible context, because they
are explicitly user-facing and must be independent of the shared headless context.

Rejected alternatives:

* *A fresh isolated `BrowserContext` per call* — would discard the persistent
  `BROWSER_DATA_DIR` fingerprint and cookies, which is a behaviour change to the
  anti-detection baseline (out of scope, and forbidden to strengthen).
* *One page reused across calls* — leaks navigation/state between unrelated tool calls.

## Consequences

* ~1–3 s removed per browser tool call after warm-up; RAM bounded by `MAX_BROWSERS`.
* Fixes the concurrent persistent-context contention.
* New failure mode: if the shared context dies, the first call after the crash pays the
  relaunch cost (acceptable, and handled).
* `server_selenium.py` is untouched (dead code).

## Measured result

`scripts/bench_browser.py` (offline, `page.set_content` fixture, Chromium, 3 iterations
per mode). Pooled reuse removes the per-call Chromium launch and the context setup;
the absolute numbers vary between runs with machine load and page-cache warmth, so the
range across runs is given:

| mode | ms per call |
|---|---|
| per-call launch (baseline) | 2694 – 2699 |
| pooled (reuse) | 182 – 315 |
| **speedup** | **8.6× – 14.8×** (~2.4–2.5 s saved per call) |

## Adoption

All hot tool paths use `pooled_browser(viewport=...)` (a drop-in replacement that
yields `(context, page)` and makes `context.close()` a no-op so pre-existing
`finally` blocks cannot close the shared context):

`visit_page`, `google_maps`, `google_maps_directions`, `google_translate`,
`google_flights`, `google_hotels`, `google_lens`, `google_lens_detect`,
`google_trends` and the Twitter feed scraper.

Deliberately still per-call: `open_manual_browser` and the headful
manual-intervention flow (they need a *visible* browser), and
`validate_google_cookies` (a one-shot startup diagnostic).
