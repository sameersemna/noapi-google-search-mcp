# PROGRESS LEDGER

> Long-horizon hardening task for `noapi-google-search-mcp`.
> **RULE: read this file first at the start of every session; update it before every commit.**
> Branch: `hardening/v0.4` · Conventional Commits · never commit to `main`.

## Current phase: 8 — Feeds Subsystem Optimization  ✅ COMPLETE  *(next: Phase 9 protocol polish / Phase 7+5 remainders)*
## Current step: 8.x — concurrent check_feeds, conditional GET (migration 3), batch inserts, bm25+snippet, since/until, retention
## Status: in-progress
## Last green commit: tip of `hardening/v0.4` (**358 pytest green**) — baseline was `cc37fd3` (143 green)

---

## ⚠️ PREMISE MISMATCH (read this first)

The task prompt was written against **commit `857a98c` / version `0.3.1`** and describes a
single **6,441-line `server.py` with 38 tools**. The repository has since advanced:

| Prompt premise | Actual state (verified 2026-10-14) |
|---|---|
| baseline `857a98c`, v`0.3.1` | HEAD `cc37fd3` on `dev`, **v`0.3.6`** (`857a98c` exists in history as the audit reference) |
| one 6,441-line `server.py` | **`server.py` = 726 lines**, code already split into `tools/` (6746 lines / 8 modules) + `utils/` (image, language, network, text, whisper_remote, ytdlp) + `config.py`, `browser.py`, `captcha.py`, `anti_bot.py`, `anti_detect.py`, `human_sim.py`, `stealth_js.py`, `logging_setup.py`, `metrics.py`, `health_server.py` |
| 38 tools | **42 tools** registered (verified via `mcp.list_tools()`) |
| no pytest | pytest IS configured (`pythonpath=["src"]`, `testpaths=["tests"]`); `tests/` has conftest + 7 files, **143 tests green in ~5s** |
| no logging | `logging_setup.py` with `install_tool_call_logging()` exists; `metrics.py` + `/health` exist |

**Consequences**
- **Phase 2 (modularize) is substantially DONE** → re-scope to minor cleanup only.
- Audit line numbers in the prompt (e.g. "~L6033") are **stale**; every bug was re-verified below.
- Snapshot/tripwire test must assert **42** tools, not 38.
- `server_selenium.py` (deprecated, 38 `@mcp.tool()`) is **NOT imported** by `server.py` — excluded.

---

## Phase checklist (re-scoped)

- [ ] **Phase 0** — Recon, baseline, safety net *(✅ done: recon, ledger, dev tooling, 42-tool snapshot, artifacts untracked, bug re-verification)*
- [ ] **Phase 1** — Fix verified bugs B1–B13 *(✅ done: all fixed with offline regression tests)*
- [x] ~~Phase 2 — Modularize~~ → **mostly done already**; remaining: remove/deprecate `server_selenium.py`, tidy `__init__` exports
- [ ] **Phase 3** — Config/logging/error model *(✅ errors + namespaced config + stdout-hygiene test + central ToolError conversion done; logging already to stderr)*
- [ ] **Phase 4** — Browser pool & concurrency *(✅ DONE: shared Chromium pool + `MAX_BROWSERS` cap + lifespan shutdown (ADR 0002, `01bff0b`); `browse_google` adopts it (`a6e1a0f`); all hot tool paths migrated via `pooled_browser` (`7799ecf`, `23d2bc4`). Measured **8.58x** faster per call — 2699 ms → 315 ms.)*
- [ ] **Phase 5** — Idempotency, ToolAnnotations, caching *(✅ annotations (42); ✅ TTL cache + singleflight wired into all read-only lookups; ✅ idempotency store wired into paste_text/shorten_url/archive_webpage + atomic writes. TODO: upload_to_s3, content-addressed local outputs)*
- [ ] **Phase 6** — Rate limiting, retries, circuit breaker *(✅ token bucket + breaker + transient-only retries, wired into browse_google; retry helper available but not yet applied to non-Google fetches)*
- [ ] **Phase 7** — Security hardening *(✅ netguard (SSRF) + fsguard; ✅ secrets via env for fetch_emails; ✅ email now PEEK (read-only); TODO: output caps for email/feed/doc text; sha256 over md5 in video cache)*
- [ ] **Phase 8** — Feeds subsystem optimization *(✅ DONE: concurrent `check_feeds` (bounded by `NOAPI_MCP_FEEDS_CONCURRENCY`) with per-source isolation; conditional GET via ETag/Last-Modified (migration 3); per-worker connections + batch inserts; FTS5 `bm25()` ranking + `snippet()`; `since`/`until` filters on `search_feeds` (additive); optional retention pruning (`NOAPI_MCP_FEEDS_MAX_ITEMS_PER_SUB`/`_MAX_AGE_DAYS`, default off). Migration v0→v3 verified non-destructively on a copy of the real DB, 35 rows preserved.)*
- [ ] **Phase 9** — MCP protocol polish (typed params, structured output, transports)
- [ ] **Phase 10** — Packaging & CI (optional extras, fork metadata)
- [ ] **Phase 11** — Documentation (README/CHANGELOG/ARCHITECTURE/SECURITY/ADRs)
- [ ] **Phase 12** — Final verification & handoff (v0.4.0)

---

## 42-tool inventory (names + params verified from `mcp.list_tools()`)

Side-effect legend: **R**=read-only · **LW**=local write/cache · **RW**=remote write · **DB**=sqlite write · **B**=browser · heavy = `cv2/rapidocr/yt-dlp/faster-whisper/av`

| # | Tool | Params | Side effects | Net | B | heavy |
|---|---|---|---|---|---|---|
| 1 | `archive_webpage` | url | RW (archive.today) | ✅ | | |
| 2 | `check_cookies` | — | R (cookie files) | | | |
| 3 | `check_feeds` | source_type | DB + net fetch | ✅ | | |
| 4 | `convert_media` | input_path, output_format, output_path, quality | LW | | | ✅ ffmpeg |
| 5 | `extract_video_clip` | url, start_seconds, end_seconds, buffer_seconds, output_filename | LW + net | ✅ | | ✅ |
| 6 | `fetch_emails` | email_address, password, imap_server, folder, search, limit | R (IMAP) **secret arg** | ✅ | | |
| 7 | `generate_qr` | data, output_path, size | LW | | | |
| 8 | `get_feed_items` | source, source_type, limit | R (DB) | | | |
| 9 | `google_books` | query, num_results | R | ✅ | ✅ | |
| 10 | `google_finance` | query | R | ✅ | ✅ | |
| 11 | `google_flights` | origin, destination, date, return_date | R | ✅ | ✅ | |
| 12 | `google_hotels` | query, num_results | R (images) | ✅ | ✅ | |
| 13 | `google_images` | query, num_results | R (images) | ✅ | ✅ | |
| 14 | `google_lens` | image_source | R + local read | ✅ | ✅ | |
| 15 | `google_lens_detect` | image_source | R (local OCR) | | | ✅ |
| 16 | `google_maps` | query, num_results | R | ✅ | ✅ | |
| 17 | `google_maps_directions` | origin, destination, mode | R | ✅ | ✅ | |
| 18 | `google_news` | query, num_results | R | ✅ | ✅ | |
| 19 | `google_scholar` | query, num_results | R | ✅ | ✅ | |
| 20 | `google_search` | query, num_results, time_range, site, page, language, region | R | ✅ | ✅ | |
| 21 | `google_shopping` | query, num_results | R (images) | ✅ | ✅ | |
| 22 | `google_translate` | text, to_language, from_language | R | ✅ | ✅ | |
| 23 | `google_trends` | query | R | ✅ | ✅ | |
| 24 | `google_weather` | location | R | ✅ | ✅ | |
| 25 | `list_images` | directory | R (local fs) | | | |
| 26 | `list_subscriptions` | — | R (DB) | | | |
| 27 | `list_subtitles` | url | R (net) | ✅ | | ✅ yt-dlp |
| 28 | `manual_intervention_status` | — | R (local) | | | |
| 29 | `ocr_image` | image_source | R (local) | | | ✅ |
| 30 | `open_manual_browser` | url, reason, timeout_sec | browser (headful) | ✅ | ✅ | |
| 31 | `paste_text` | content, title, syntax, expiry_days | RW | ✅ | | |
| 32 | `read_document` | file_path | R (local fs) | | | |
| 33 | `search_feeds` | query, source_type, limit | R (DB) | | | |
| 34 | `search_transcript` | url, query, model_size, context_segments | LW + net | ✅ | | ✅ |
| 35 | `shorten_url` | url | RW | ✅ | | |
| 36 | `subscribe` | source_type, identifier, name | DB | ✅ | | |
| 37 | `transcribe_local` | file_path, model_size, language | LW + compute | | | ✅ |
| 38 | `transcribe_video` | url, model_size, language, prefer_subtitles | LW + net | ✅ | | ✅ |
| 39 | `unsubscribe` | source_type, identifier | DB (destructive) | | | |
| 40 | `upload_to_s3` | file_path, bucket, key, endpoint, access_key, secret_key | RW + local read **secret arg** | ✅ | | |
| 41 | `visit_page` | url | R | ✅ | ✅ | |
| 42 | `wikipedia` | query, language, sentences | R | ✅ | | |

---

## Bug status (B1–B10) — re-verified against current `0.3.6` code

| ID | Status | Evidence / fix |
|---|---|---|
| B1 `lstrip("r/")` strips chars | ✅ **FIXED** `8974573` | added `_normalize_subreddit()`; tests in `tests/unit/test_feeds_regressions.py` |
| B2 shared fixed temp `audio_temp` + wipe-all | ✅ **FIXED** `654432d` | `utils/ytdlp.py` now uses `tempfile.mkdtemp` per call + age-gated `_cleanup_stale_dirs`; tests in `tests/unit/test_ytdlp_regressions.py` |
| B3 version mismatch | ✅ **FIXED** `24270dc` | `__init__` reads `importlib.metadata.version()` w/ literal fallback; test `tests/unit/test_version.py`. Refreshed stale editable metadata (was 0.3.1) |
| B4 mixed date formats in `published_at` | ✅ **FIXED** `703ad3c` | `_normalize_published()` (RFC822/ISO/offset → ISO-8601 UTC) on insert + migration; tests `test_feeds_dedupe_dates.py`, `test_feeds_migrations.py` |
| B5 FTS5 missing UPDATE trigger | ✅ **FIXED** `703ad3c` | `fts_au` delete-then-insert trigger + `('rebuild')`; tests in `test_feeds_migrations.py` |
| B6 `LIKE` wildcard not escaped | ✅ **FIXED** `1d8c591` | `_like_escape()` + `ESCAPE '\\'` at 3 sites; tests `tests/unit/test_feeds_like_escape.py` |
| B7 URL-less items dropped / no `dedupe_key` | ✅ **FIXED** `703ad3c` | `dedupe_key` column + `UNIQUE(subscription_id, dedupe_key)`; content-hash fallback; tests `test_feeds_dedupe_dates.py` |
| B8 `http://export.arxiv.org` | ✅ **FIXED** `8974573` | both sites now `https://`; tests in `test_feeds_regressions.py` |
| B9 `google_search` pagination stride | ✅ **FIXED** `cedf20a` | extracted `_build_google_search_url()`; legacy stride kept as default, opt-in `NOAPI_MCP_SEARCH_PAGE_SIZE`; tests `test_search_url_builder.py` |
| B10 fixed `noapi_tmp` mc alias + secret on argv | ✅ **FIXED** `0a77bcc` | random per-call alias + `MC_HOST_<alias>` env (verified vs minio/docs); tests `tests/unit/test_s3_regressions.py` |
| B11 `tools/finance.py` missing `import re` | ✅ **FIXED** `35b826d` | added `import re`; test `tests/unit/test_finance_regressions.py` |
| B12 `transcribe_local` missing `TRANSCRIPT_CACHE_DIR`/`_transcript_cache_path`/`_transcribe_audio` | ✅ **FIXED** `175bc2f` | NameError confirmed at runtime; added imports in `tools/web_utils.py`; test `tests/unit/test_web_utils_regressions.py` |
| B13 `_read_docx_text` missing `ET` import | ✅ **FIXED** `175bc2f` | every `.docx` read raised NameError; added `import xml.etree.ElementTree as ET`; same test file |
| B14 `fetch_emails` used `(RFC822)` → marked mail as **seen** | ✅ **FIXED** `72eb3f5` | switched to `BODY.PEEK[]` so the tool is genuinely read-only; test `tests/unit/test_email_secrets.py` |

**Migration system** (`703ad3c`): `PRAGMA user_version` + `_MIGRATIONS` (v1 base schema at v2 shape,
v2 in-place upgrade of pre-hardening DBs). Verified non-destructively on a copy of the real DB
(`~/.cache/noapi-google-search-mcp/feeds.db`, 35 rows preserved, `dedupe_key` backfilled, FTS rebuilt).

Also open (cross-cutting): no `ToolAnnotations` anywhere; `md5` still used (`tools/video.py:51,599`); no cache / rate limiter / circuit breaker / SSRF guard / fsguard.

---

## Decisions (ADRs to create under `docs/adr/`)
Owner answers (2026-10-14):
- **D1 — Premise.** Treat current tree (`dev` @ `cc37fd3`, v0.3.6, 42 tools, already modular) as the
  baseline. Re-scope moot phases (Phase 2 effectively done).
- **D2 — Resilience (supersedes 2026-09-11 "no rate-limiting").** Implement **all** of: concurrency
  cap + RPS throttle + circuit breaker (prompt Phases 4 & 6). → ADR 0004.
- **D3 — Artifacts.** Untrack generated test logs/reports; keep on disk. *Done (`27fd302`).*
- **D4 — Packaging/fork metadata.** Leave unchanged for now (no optional extras, no URL/maintainer
  changes, no PyPI rename). Revisit in Phase 10.
- **D5 — Security defaults.** `ALLOWED_ROOTS` = user home; **private/LAN URLs ALLOWED by default**
  (LAN-friendly, protects the whisper/GPU + local service setup). Opt-out via env flag.

*(ADR files not yet written — 0001 module layout, 0002 browser pool, 0003 idempotency+caching,
0004 resilience, 0005 feeds.)*

## Verified API notes (context7 / introspection)
- **pytest-asyncio**: `asyncio_mode = "auto"` under `[tool.pytest.ini_options]` (auto-marks async tests + fixtures). Marker registration via `markers = [...]`.
- **ruff**: `[tool.ruff]` for `line-length`; `[tool.ruff.lint]` for `select` / `ignore` / `extend-select`.
- **FastMCP**: `await mcp.list_tools()` returns `Tool` objects with `.name`, `.inputSchema` (used by snapshot test + inventory).
- **hatchling**: dev deps via PEP 621 `[project.optional-dependencies].dev` (universally supported; chosen over PEP 735 `[dependency-groups]` for broad tooling compatibility).
- **Error API (verified by introspection)**: `from mcp.server.fastmcp.exceptions import ToolError`. Raising `ToolError` in a tool → `isError=true` with our message in `content` (model can read it); any other exception → generic `Error executing tool <name>` (no internal leak).
- **Annotations (verified by introspection)**: `from mcp.types import ToolAnnotations` — fields `title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`. `FastMCP.tool(name, title, description, annotations, icons, meta, structured_output)`. `list_tools()` rebuilds Tool objects each call, BUT mutating `mcp._tool_manager._tools[name].annotations` **does** persist into `list_tools()` (the project already mutates the same private registry for metrics) — that is what `tools/annotations.py` uses.

## Baseline metrics (commit `cc37fd3`)
- pytest: **143 passed** in ~5s (Python 3.11.15, pytest 9.1.0)
- ruff: **763** errors (default), **585** errors (project config: line-length 120, E501 ignored, `server_selenium.py` excluded, select E,F,W,I,B,UP,ASYNC,S,SIM) — not yet fixed
- Installed dev tools into env `mcp-google`: ruff 0.17.0, mypy 2.4.0, pytest-asyncio 1.4.0 (+pytest-timeout, coverage)
- After Phase 0.4/0.5: **147 passed** (added `tests/unit/test_tool_registry.py` — 4 tests)
- After Phase 1: **190 passed**.
- After Phases 3 + 5(partial): **251 passed**.
- After Phases 5/6/7 (2026-10-15): **326 passed**. New test files this session:
  `test_ratelimit.py`, `test_circuit_retry.py`, `test_resilience_integration.py`,
  `test_netguard.py`, `test_fsguard.py`, `test_guard_wiring.py`, `test_email_secrets.py`,
  `test_idempotent_writes.py`.
- After Phase 4 (2026-10-16): **340 passed**. New test files: `test_browser_pool.py`,
  `test_browse_google_pool.py`, `test_pooled_browser.py`.
- Browser benchmark (`scripts/bench_browser.py`, offline): per-call **2699.3 ms** vs pooled
  **314.7 ms** → **8.58x**, ~2384.7 ms saved per browser tool call. Test files in `tests/unit/`:
  `test_tool_registry.py`, `test_feeds_regressions.py`, `test_finance_regressions.py`,
  `test_ytdlp_regressions.py`, `test_version.py`, `test_feeds_like_escape.py`,
  `test_s3_regressions.py`, `test_feeds_migrations.py`, `test_feeds_dedupe_dates.py`,
  `test_search_url_builder.py`, `test_errors.py`, `test_config_env.py`, `test_stdout_hygiene.py`,
  `test_tool_annotations.py`, `test_cache.py`, `test_wikipedia_cache_wiring.py`,
  `test_idempotency.py`, `test_web_utils_regressions.py`.
- mypy: **128 errors in 14 files** (lenient config; `ignore_missing_imports=true`)

### New finding (from mypy, add to Phase 1)
- **B11 (NEW): `tools/finance.py:496,687` — `Name "re" is not defined`.** `re` is used but
  never imported in that module → `NameError` at runtime on that code path. Fix + regression test.

## Known issues / conflicts
1. ~~RATE-LIMITING CONFLICT~~ **RESOLVED 2026-10-14 (D2):** owner now approves concurrency cap + RPS
   throttle + circuit breaker. Phases 4 & 6 unblocked. (Supersedes the 2026-09-11 "no rate-limiting" note.)
2. `server_selenium.py` is dead code (not imported) but still contains a duplicate `@mcp.tool()` set —
   candidate for removal (needs owner OK; currently ruff-excluded).
3. Generated test logs/reports untracked and gitignored (D3) — files still on disk.

## Next action (exact, for a fresh session)
1. **Phase 7 remainder** (small, high value):
   - Output caps for email bodies / feed content / document text (`config.MAX_TEXT_CHARS`) with a
     `[truncated …]` marker.
   - Replace `md5` cache keys with `sha256` in `tools/video.py` (keep reading old md5-keyed
     transcript caches as a fallback so existing caches aren't lost).
2. **Phase 5 remainder**:
   - `upload_to_s3` idempotency: `HEAD` the object before upload, skip when size/ETag match, add
     `overwrite: bool = True`.
   - Content-addressed atomic outputs for `generate_qr` / `extract_video_clip` / `convert_media`
     (never silently clobber a user path; append `-1`, `-2`… unless `overwrite=True`).
3. **Phase 9**: `Literal` enum params (must keep every previously-valid value valid), structured
   output for the high-value read tools (keep the existing text rendering too — verify with
   context7 how FastMCP emits both `structuredContent` and text), `--transport streamable-http`
   (bind 127.0.0.1), `--version`/`--check` CLI.
4. **Phases 10–12**: optional extras (D4 = unchanged for now), CI gate, docs/CHANGELOG/ARCHITECTURE/
   SECURITY/ADRs, v0.4.0 bump + PR.
5. Deferred/low-priority: `browser.py:validate_google_cookies` still launches its own Chromium
   (one-shot startup diagnostic); `server_selenium.py` is dead code (needs owner OK to delete).
6. Baseline: **358 pytest green**. Bench: `python scripts/bench_browser.py`.
   Env python: `/home/sameer/anaconda3/envs/mcp-google/bin/python`.
   Pre-existing ruff findings remain in `web_utils.py`/`maps.py`/`travel.py`/`vision.py`/`feeds.py`/
   `search.py` (unused imports) and `server.py`/`anti_bot.py` — untouched, documented.
   NOTE: `browse_google` and the pool are covered; `check_feeds` concurrency is covered by an
   *overlap* assertion (not a wall-clock threshold) — keep it that way (robust to CI noise).
