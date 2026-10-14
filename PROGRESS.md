# PROGRESS LEDGER

> Long-horizon hardening task for `noapi-google-search-mcp`.
> **RULE: read this file first at the start of every session; update it before every commit.**
> Branch: `hardening/v0.4` · Conventional Commits · never commit to `main`.

## Current phase: 0 — Recon, Baseline & Safety Net
## Current step: 0.5 — dev tooling + 42-tool snapshot test DONE (awaiting user answers)
## Status: in-progress
## Last green commit: `9ed3fc6` (147 pytest green; ruff 585; mypy 128)

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

- [ ] **Phase 0** — Recon, baseline, safety net *(in progress)*
- [ ] **Phase 1** — Fix verified bugs B1–B10 (+ migration system for B4/B5/B7)
- [x] ~~Phase 2 — Modularize~~ → **mostly done already**; remaining: remove/deprecate `server_selenium.py`, tidy `__init__` exports
- [ ] **Phase 3** — Config/logging/error model *(logging partly exists; needs stderr-only test + error types)*
- [ ] **Phase 4** — Browser pool & concurrency **⚠️ CONFLICT — see open questions**
- [ ] **Phase 5** — Idempotency, ToolAnnotations, caching
- [ ] **Phase 6** — Rate limiting, retries, circuit breaker **⚠️ CONFLICT — see open questions**
- [ ] **Phase 7** — Security hardening (SSRF, allowed roots, secrets, output caps)
- [ ] **Phase 8** — Feeds subsystem optimization
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

| ID | Status | Evidence |
|---|---|---|
| B1 `lstrip("r/")` strips chars | **OPEN** | `tools/feeds.py:695` `identifier.lstrip("r/").strip("/")` (also deprecated `server_selenium.py:6447`) |
| B2 fixed shared temp `audio_temp` + wipe-all | **OPEN** | `utils/ytdlp.py:629-640` (`os.path.join(cache_dir,"audio_temp")`, `_cleanup_prefix(cache_dir,"audio_temp")`) — concurrency hazard |
| B3 version mismatch | **~DONE** | both `pyproject.toml` and `__init__.py` = `0.3.6`; still two hardcoded copies (not single-sourced) |
| B4 mixed date formats in `published_at` | **OPEN** | no ISO normalization found |
| B5 FTS5 missing UPDATE trigger | **OPEN** | no `PRAGMA user_version`, no migration system |
| B6 `LIKE` wildcard not escaped | **VERIFY** | read `tools/feeds.py` search/get paths |
| B7 URL-less items dropped / no `dedupe_key` | **OPEN** | no `dedupe_key` anywhere |
| B8 `http://export.arxiv.org` | **OPEN** | `tools/feeds.py:314,716` |
| B9 `google_search` pagination `start` vs `num+5` | **VERIFY** | read `tools/search.py` |
| B10 fixed `noapi_tmp` mc alias + secret on argv | **OPEN** | `tools/web_utils.py:1104` `alias = "noapi_tmp"` |

Also open (cross-cutting): no `ToolAnnotations` anywhere; `md5` still used (`tools/video.py:51,599`); no cache / rate limiter / circuit breaker / SSRF guard / fsguard.

---

## Decisions (ADRs to create under `docs/adr/`)
*(none yet — 0001 module layout, 0002 browser pool, 0003 idempotency+caching, 0004 resilience, 0005 feeds)*

## Verified API notes (context7)
- **pytest-asyncio**: `asyncio_mode = "auto"` under `[tool.pytest.ini_options]` (auto-marks async tests + fixtures). Marker registration via `markers = [...]`.
- **ruff**: `[tool.ruff]` for `line-length`; `[tool.ruff.lint]` for `select` / `ignore` / `extend-select`.
- **FastMCP**: `await mcp.list_tools()` returns `Tool` objects with `.name`, `.inputSchema` (used by snapshot test + inventory).
- **hatchling**: dev deps via PEP 621 `[project.optional-dependencies].dev` (universally supported; chosen over PEP 735 `[dependency-groups]` for broad tooling compatibility).

## Baseline metrics (commit `cc37fd3`)
- pytest: **143 passed** in ~5s (Python 3.11.15, pytest 9.1.0)
- ruff: **763** errors (default), **585** errors (project config: line-length 120, E501 ignored, `server_selenium.py` excluded, select E,F,W,I,B,UP,ASYNC,S,SIM) — not yet fixed
- Installed dev tools into env `mcp-google`: ruff 0.17.0, mypy 2.4.0, pytest-asyncio 1.4.0 (+pytest-timeout, coverage)
- After Phase 0.4/0.5: **147 passed** (added `tests/unit/test_tool_registry.py` — 4 tests)
- mypy: **128 errors in 14 files** (lenient config; `ignore_missing_imports=true`)

### New finding (from mypy, add to Phase 1)
- **B11 (NEW): `tools/finance.py:496,687` — `Name "re" is not defined`.** `re` is used but
  never imported in that module → `NameError` at runtime on that code path. Fix + regression test.

## Known issues / conflicts
1. **⚠️ RATE-LIMITING CONFLICT.** Repo memory records that on 2026-09-11 the user **explicitly did NOT want rate-limiting**, so a concurrency fix was deliberately not applied. The prompt's Phase 4 (concurrency cap) and Phase 6 (rate limiter + circuit breaker) contradict this. **Must confirm with user before implementing.**
2. `server_selenium.py` is dead code (not imported) but still contains a duplicate `@mcp.tool()` set — candidate for removal (ask user).
3. Committed artifacts still in tree (`test_all_log.txt`, `TEST_REPORT*.md`, `test_lmstudio_results*`) — removal from tree needs user OK.

## Next action (exact, for a fresh session)
1. ✅ Phase 0.4 (dev tooling in `pyproject.toml`: ruff/mypy/pytest config + `[dev]` extra).
2. ✅ Phase 0.5 (`tests/unit/test_tool_registry.py` — snapshot: exactly 42 tool names + param sets).
3. ✅ Commit `chore(phase0): baseline harness + ledger`.
4. **Questions asked to the user** (see "Known issues / conflicts" + the session message). Do NOT start Phase 4/5/6/7 until answered.
5. After answers: **Phase 1** — write a failing regression test per bug, then fix B1/B2/B4/B5/B7/B8/B10 (+ verify B6/B9), introduce `PRAGMA user_version` migration system, commit per bug (`fix(feeds): ... (B1)`).
6. Then **Phase 3** (config/logging/error model) → **Phase 5** (annotations/cache/idempotency) → others per user guidance on rate-limiting.
