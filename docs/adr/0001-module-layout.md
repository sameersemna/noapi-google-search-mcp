# ADR 0001 — Module layout

- **Status:** Accepted (retro-record of a change made before this ADR existed)
- **Date:** 2026-10-17

## Context

The server began as a single ~6,400-line `server.py`. That had already been split
into a `tools/` package before this hardening project started, so the remaining
question was only *where new cross-cutting infrastructure should live* — the
cache, idempotency, rate limiting, retry, guards and browser pool had no natural
home among the tool modules.

## Decision

Introduce a `core/` package for **cross-cutting infrastructure** with no
knowledge of any specific tool:

```
core/cache.py        read-through TTL cache + singleflight
core/idempotency.py  dedupe store for remote writes
core/atomic.py       atomic file writes
core/ratelimit.py    token buckets per host group
core/circuit.py      cooldown circuit breaker
core/retry.py        transient-only retries with jitter
core/browser.py      shared Chromium pool
core/netguard.py     SSRF / URL guard
core/fsguard.py      allowed-roots filesystem guard
```

Layering, from bottom to top: `config` → `core/*` → `errors` → `tools/*` →
`server.py`. `core/*` must not import `tools/*`, and tool modules must not import
each other (they communicate through `server.py` re-exports, which also keeps the
legacy import paths working).

Deliberately **not** done: moving the existing `browser.py`, `captcha.py`,
`anti_bot.py`, `anti_detect.py`, `human_sim.py` and `stealth_js.py`. They work,
they hold the anti-detection baseline that must not change, and moving them
would have produced a large, risky diff for no functional gain.

## Consequences

- New infrastructure is unit-testable in isolation (this is what makes the 80%+
  coverage gate on `core/` meaningful).
- `core/` can be held to a strict lint standard while the legacy modules are
  cleaned up gradually.
- A small amount of duplication exists between the new pool
  (`core/browser.py`) and the legacy `browser.py` helpers; that is intentional,
  and the pool reuses `browser.launch_browser` verbatim so the stealth baseline
  is unchanged.
