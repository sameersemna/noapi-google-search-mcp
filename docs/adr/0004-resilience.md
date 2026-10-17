# ADR 0004 — Resilience: rate limiting, retries, circuit breaker

- **Status:** Accepted
- **Date:** 2026-10-17

## Context

Google blocks a client that requests too fast or too often in parallel. When a
block happens, the previous behaviour was to try again immediately on the next
tool call — which deepens the block. There was also no back-off for ordinary
transient network failures.

An earlier project decision had *rejected* rate limiting. The owner has since
reversed that (recorded as decision D2 in `PROGRESS.md`), explicitly approving a
concurrency cap, an RPS throttle and a circuit breaker.

Note the design constraint: reliability must come from **sending fewer requests**,
not from disguising them better. The stealth/CAPTCHA code is left functionally
untouched.

## Decision

Three composable primitives in `core/`:

1. **`ratelimit.py` — per-host-group token buckets.** All `google.*` hosts share
   one bucket (they share one block budget); other upstreams get their own more
   generous buckets. `acquire()` *awaits* a token rather than failing, and the
   clock is injectable so tests need no real sleeping. Configurable via
   `NOAPI_MCP_GOOGLE_RPS` (default 0.5) and `NOAPI_MCP_GOOGLE_BURST` (default 2).

2. **`circuit.py` — cooldown circuit breaker for Google.** On a detected block
   the circuit opens and calls fail fast for a cooldown
   (`NOAPI_MCP_CIRCUIT_COOLDOWN_SECONDS`, default 600 s, escalating
   exponentially to `NOAPI_MCP_CIRCUIT_MAX_COOLDOWN_SECONDS`, default 3600 s on
   repeated blocks). While open, `CircuitOpenError` (an `UpstreamBlockedError`)
   tells the model how long to wait and suggests cache or non-Google tools.
   Cache hits are served *before* the breaker is consulted, so cached answers keep
   working during a block.

3. **`retry.py` — transient-only retries.** Exponential back-off with full
   jitter, `Retry-After` honoured, and a hard rule: **never** retry an
   `UpstreamBlockedError`/`CircuitOpenError`, and never retry a non-transient
   error (e.g. a `ValueError`).

Wiring: `browse_google` throttles, checks the breaker *before* acquiring a
browser, records a block when it fails and records success when it succeeds. The
gate lives in one place, and the ~19 tool call sites need no changes.

The previous fixed "minimum gap between Google requests" became the *default
derivation* for the new rate, so existing tuning (`GOOGLE_REQUEST_MIN_GAP=3.0`)
still produces equivalent pacing.

## Consequences

- A block no longer cascades: subsequent calls fail fast and cheaply.
- Throttling happens before the expensive resource (a browser context) is taken.
- Concurrency is bounded by the browser pool (`MAX_BROWSERS`), so parallel tool
  calls cannot fan out into N Chromium instances.
- Operators who preferred the old pacing can set
  `NOAPI_MCP_GOOGLE_RPS=0.34`; those who want more throughput can raise it.
