# ADR 0005 — Feeds subsystem

- **Status:** Accepted
- **Date:** 2026-10-17

## Context

The feeds subsystem (`tools/feeds.py`) stores subscribed sources in SQLite and
indexes items with FTS5. It had correctness and scalability problems:

- `check_feeds` fetched sources **sequentially**, on the event loop, using a
  single connection.
- No conditional GET, so every check re-downloaded unchanged feeds.
- `published_at` was stored in whatever format the feed emitted, so
  `ORDER BY published_at` sorted lexically across mixed formats.
- The FTS index had no `UPDATE` trigger, so a future update would desync it.
- Items without a URL were silently dropped.

There was also no schema-versioning mechanism, which blocked any of these fixes.

## Decision

1. **Introduce a migration system** — `PRAGMA user_version` plus an ordered list
   of migration functions, each run inside a transaction. Migration 1 creates the
   current schema; migration 2 upgrades pre-existing databases in place (rebuild
   `feed_items` to add `dedupe_key`, normalize dates, rebuild FTS); migration 3
   adds `etag`/`last_modified`. Migrations are idempotent, and the upgrade path
   was verified **non-destructively on a copy of the real database** (35 rows
   preserved).

2. **De-duplicate on `dedupe_key`** (`UNIQUE(subscription_id, dedupe_key)`), which
   is the URL when present and otherwise `sha256(source_type|title|published|
   content)`. Items without a URL are now stored instead of dropped.

3. **Normalize `published_at` to ISO-8601 UTC** on insert (and backfill existing
   rows), accepting RFC 822, ISO 8601 and offset forms. Unparseable values are
   preserved rather than discarded, and the raw value is kept in `metadata`.

4. **Concurrent, isolated fetching.** `check_feeds` fetches sources concurrently
   with a bounded semaphore (`NOAPI_MCP_FEEDS_CONCURRENCY`, default 8) and
   isolates per-source failures: one bad feed no longer fails the batch. SQLite
   work moves off the event loop via `asyncio.to_thread`, with WAL and per-worker
   connections to avoid cross-thread connection reuse.

5. **Conditional GET.** Store `etag`/`last_modified` per subscription, send
   `If-None-Match`/`If-Modified-Since`, and treat `304` as "no new items".

6. **Better search.** `search_feeds` ranks with FTS5 `bm25()` and uses
   `snippet()` for excerpts, falling back to escaped `LIKE` (the `%`/`_` wildcard
   bug is fixed). Additive `since`/`until` ISO filters were added.

7. **Optional retention.** `NOAPI_MCP_FEEDS_MAX_ITEMS_PER_SUB` and
   `NOAPI_MCP_FEEDS_MAX_AGE_DAYS` prune after a check. **Default: unlimited**, i.e.
   current behaviour is preserved.

## Consequences

- A check now takes roughly as long as the slowest source rather than the sum.
- Steady-state bandwidth drops sharply (304s carry no body).
- Dates sort correctly across all source types.
- Existing databases upgrade automatically and safely on first open.
- No behaviour change unless the operator opts into retention.
