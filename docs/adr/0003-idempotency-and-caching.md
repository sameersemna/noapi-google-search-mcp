# ADR 0003 — Idempotency and caching

- **Status:** Accepted
- **Date:** 2026-10-17

## Context

Two different problems, sharing one root cause — **clients and models retry**:

1. **Duplicate side effects.** A retried `paste_text` / `shorten_url` /
   `archive_webpage` / `upload_to_s3` creates a second remote object.
2. **Repeated reads.** A model often calls the same read-only tool with the same
   arguments repeatedly. Each repeat re-hits Google: slow, *and* the main cause
   of rate-limit blocks.

## Decision

### Caching (read-only tools)

A read-through TTL cache with **singleflight**, in `core/cache.py`:

- Cache key = `sha256(tool + canonical_json(normalised args) + schema_version)`.
  Normalisation casefolds/strips strings and treats `""`/`None` as absent, so
  semantically identical calls share an entry.
- Two tiers: a bounded in-memory LRU, plus an optional SQLite tier so restarts
  stay warm.
- Per-tool TTLs (search/scholar/books 1 h, news 10 min, weather 15 min,
  finance 1 min, trends 6 h, maps 24 h, translate/wikipedia 7 d, …).
- **Errors are never cached**, and neither is anything matching a
  blocked/rate-limited marker: a cached block would outlive the block itself.
- **Singleflight**: concurrent identical calls share one in-flight future, so N
  identical calls become one upstream request.
- Applied transparently by decorating the *implementation* function
  (`do_google_search`, `_fetch_page_text`, `_wikipedia_lookup`, …), which keeps
  tool signatures and text output byte-identical. `fresh=True` (additive) bypasses
  a read; `NOAPI_MCP_ENABLE_CACHE=0` disables caching entirely.

### Idempotency (side-effecting tools)

A small SQLite store (`core/idempotency.py`) with a **two-phase** protocol:

1. `reserve(key)` — only the first caller wins; the row starts `in_progress`. This
   also protects against *concurrent* duplicates.
2. `complete(key, result)` on success, or `abandon(key)` on failure so a later
   retry can proceed.

A hit within the window (default 24 h, `NOAPI_MCP_IDEMPOTENCY_WINDOW_SECONDS`)
returns the stored result marked `(cached — identical request already completed)`.

Keys are derived from the operation's identity: content+syntax+expiry for a
paste, the URL for shorten/archive, and the file's **sha256 plus bucket and key**
for an upload — so changing the content correctly produces a new upload.

Each affected tool gains an additive `idempotency_key` parameter for callers that
want explicit control.

## Consequences

- Retries are safe, and repeated reads send strictly fewer requests upstream.
- `upload_to_s3` also gained `overwrite: bool = True` (default preserves old
  behaviour) so `overwrite=False` can refuse to replace an existing object.
- A tool's *first* call still does the real work; caching is not a substitute for
  correctness.
