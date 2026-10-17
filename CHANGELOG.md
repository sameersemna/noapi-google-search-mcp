# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-10-17

Hardening release: correctness fixes, production-grade infrastructure (caching,
idempotency, resilience, security), a shared browser pool, and CI. **All 42 tool
names, parameter names and defaults remain backward compatible**; the changes are
additive (new optional parameters, new env flags) unless called out under
_Changed_ below.

### Added

- **`core/` infrastructure package** — `cache`, `idempotency`, `atomic`,
  `ratelimit`, `circuit`, `retry`, `browser` (pool), `netguard` (SSRF),
  `fsguard` (filesystem roots).
- **Read-through TTL cache + singleflight** for every read-only lookup
  (search/news/scholar/trends/finance/weather/shopping/books/maps/directions/
  translate/flights/hotels/visit_page/wikipedia). Per-tool TTLs, bounded
  in-memory LRU plus an optional SQLite tier that survives restarts. Errors and
  bot-detection/blocked results are **never** cached.
- **Idempotency for side-effecting remote writes** — `paste_text`,
  `shorten_url`, `archive_webpage`, `upload_to_s3`: a retried identical call
  within the window (default 24 h) returns the original result instead of
  creating a duplicate. SQLite-backed, two-phase (reserve/complete) so
  concurrent duplicates are also collapsed. New optional parameter
  `idempotency_key` on each.
- **`ToolAnnotations` on all 42 tools** — `title`, `readOnlyHint`,
  `destructiveHint`, `idempotentHint`, `openWorldHint`.
- **Typed error model** (`errors.py`): `UserInputError`, `UpstreamBlockedError`,
  `UpstreamError`, `DependencyMissingError`. Raised errors are converted to MCP
  `ToolError` (i.e. `isError: true`) centrally, so clients can finally
  distinguish a failure from content, while the human-readable wording is
  preserved.
- **CLI**: `--version`, `--check` (dependency doctor with install hints),
  `--transport {stdio,sse,streamable-http}`, `--host`, `--port`.
- **Shared Chromium browser pool** with a concurrency cap (`MAX_BROWSERS`,
  default 3) and automatic relaunch on disconnect. Measured **8.6× faster per
  call** (2699 ms → 315 ms) in `scripts/bench_browser.py`.
- **Resilience**: per-upstream token-bucket rate limiting (all `google.*` hosts
  share one bucket), a Google **circuit breaker** with exponential cooldown, and
  **transient-only retries** with exponential back-off + full jitter
  (`Retry-After` honoured; a detected block is never retried).
- **Feeds**: concurrent `check_feeds` with per-source isolation, HTTP
  **conditional GET** (ETag / Last-Modified), FTS5 `bm25()` ranking +
  `snippet()`, `since`/`until` filters on `search_feeds`, optional retention
  pruning, and a `PRAGMA user_version` **migration system** (v1 → v3).
- **Security guards**: SSRF/URL guard (`netguard`) for URL-taking tools and a
  filesystem allowed-roots guard (`fsguard`) for path-taking tools.
- Output caps: `NOAPI_MCP_MAX_DOCUMENT_CHARS`, `NOAPI_MCP_MAX_EMAIL_BODY_CHARS`.
- **CI** (`ci.yml`) with an offline test matrix and a coverage gate on `core/`;
  the release workflow now runs the tests before publishing. Pre-commit config.
- `[dev]` optional-dependency extra (pytest, pytest-asyncio, pytest-cov, ruff,
  mypy, coverage).
- ADRs under `docs/adr/`: `0002-browser-pool.md`,
  `0006-phase9-typing-and-structured-output.md`.
- Test suite grew from 143 to **415** offline tests.

### Changed (behaviour)

- **Google request pacing** is now a token bucket (`NOAPI_MCP_GOOGLE_RPS`,
  default **0.5**; `NOAPI_MCP_GOOGLE_BURST`, default 2) instead of the previous
  fixed 3-second gap. Set `NOAPI_MCP_GOOGLE_RPS=0.34` to approximate the old
  behaviour, or higher for more aggressive fetching.
- **Reading tools are cached** (per-tool TTL). Bypass per call with
  `fresh=true` (currently on `wikipedia`) or globally with
  `NOAPI_MCP_ENABLE_CACHE=0`.
- **`google_search.time_range`** and the `model_size` parameter of the
  transcription tools are now validated enums. An out-of-range value fails fast
  with a clear message instead of being silently ignored (`time_range`) or
  silently coerced to `tiny` (`model_size`). Every previously-valid value
  remains valid.
- **`fetch_emails`** now fetches with `BODY.PEEK[]` — previously it used
  `(RFC822)`, which silently marked every fetched message as **seen** (read).
  Credentials may now come from `NOAPI_MCP_IMAP_USER` /
  `NOAPI_MCP_IMAP_PASSWORD` when the arguments are empty (recommended; keeps
  secrets out of the model transcript). The `password: xxxx` sample prompt was
  removed.
- **`upload_to_s3`** gained `overwrite: bool = True`. The default preserves the
  previous behaviour; `overwrite=False` refuses to replace an existing object.
- **Heavy native dependencies** (`cv2`, `numpy`, `onnxruntime`) are now imported
  lazily, so server startup no longer loads ~100 MB of native libraries unless a
  CAPTCHA actually needs solving.
- Private/LAN URLs and local file paths remain **allowed by default**
  (LAN-friendly, for self-hosted/GPU setups). Tighten with
  `NOAPI_MCP_ALLOW_PRIVATE_URLS=0` and `NOAPI_MCP_ALLOWED_ROOTS` (default: the
  user's home directory plus the system temp directory).
- `__main__.py` no longer starts the server merely on import (it previously
  called `main()` outside the `__name__` guard).

### Fixed

- **B1** `subscribe(reddit, ...)` stripped the *characters* `r`/`/` instead of
  the `r/` prefix (`"rust"` → `"ust"`).
- **B2** concurrent `transcribe_video` calls shared a fixed temp filename in a
  shared directory and deleted each other's downloads (now a unique per-call
  directory).
- **B3** `__version__` could drift from `pyproject.toml` (now single-sourced
  from package metadata).
- **B4** `published_at` was stored in mixed formats (RFC 822 / ISO 8601 / epoch)
  so `ORDER BY published_at` sorted incorrectly (now normalised to ISO-8601 UTC,
  with a backfill migration).
- **B5** the FTS5 index had no `UPDATE` trigger (now added, plus a rebuild).
- **B6** user input was interpolated into `LIKE '%…%'` unescaped, so `%`/`_`
  acted as wildcards (now escaped).
- **B7** items without a URL were silently dropped (now deduped by a content
  hash via a new `dedupe_key`).
- **B8** the arXiv API was called over `http://` (now `https://`).
- **B9** `google_search` pagination used `start=(page-1)*num_results` while
  requesting `num=num_results+2`, so changing `num_results` between pages
  skipped/duplicated results. Behaviour is unchanged by default; set
  `NOAPI_MCP_SEARCH_PAGE_SIZE` for a fixed stride.
- **B10** `upload_to_s3` used a fixed `mc` alias (concurrent uploads raced) and
  passed the secret key on the command line (visible via `ps`). Now a per-call
  random alias with credentials passed via `MC_HOST_<alias>` only.
- **B11** `tools/finance.py` used `re` without importing it (`NameError` in the
  finance/weather/books paths).
- **B12** `transcribe_local` used `TRANSCRIPT_CACHE_DIR`,
  `_transcript_cache_path` and `_transcribe_audio` without importing them
  (`NameError` on every call).
- **B13** `_read_docx_text` used `ET` without importing it (`NameError` on every
  `.docx` read).
- **B14** `fetch_emails` marked mail as read (see _Changed_).
- **B15** `extract_video_clip` used `Path` without importing it, so local-file
  clip extraction always failed.
- **B16** `tools/travel.py`: the translate HTTP helpers used `urllib`, `json`,
  `re`, `USER_AGENT` and `split_translation_chunks` without importing them, and
  the browser path used `LANGUAGE_CODES` / `detect_source_language` /
  `LANG_DETECTION_CONFIDENCE_THRESHOLD` — `google_translate` raised `NameError`
  on its first line.
- **B17** `tools/search.py`: the `google_images` DuckDuckGo fallback used
  `fallback_duckduckgo_images` and `google_trends` used `re`, both unimported —
  i.e. the fallback broke exactly when Google blocked.
- **B18** `tools/vision.py`: `google_lens`, `google_lens_detect` and
  `list_images` used `Path` and `datetime` without importing them, so
  local-image flows failed.
- **B19** `browser.py`: `simulate_human_behavior` used the `config` module
  without importing it (hit at the default human-behaviour level).
- **B20** `__main__.py` started the server at import time.

### Security

- **SSRF guard**: `file:`, `data:`, `javascript:` and other non-`http(s)`
  schemes are always rejected; link-local/cloud-metadata addresses
  (`169.254.169.254`) are always rejected; loopback/private/reserved are
  rejected in strict mode (`NOAPI_MCP_ALLOW_PRIVATE_URLS=0`). Hostnames are
  resolved and *every* address is checked (anti-rebinding).
- **Filesystem guard**: path-taking tools are confined to
  `NOAPI_MCP_ALLOWED_ROOTS`; symlinks are resolved so they cannot escape.
- **Secrets**: credentials can be supplied via environment variables
  (`NOAPI_MCP_IMAP_USER`/`_PASSWORD`, `AWS_*`) instead of tool arguments; secrets
  are never placed on a subprocess command line and are redacted in tool-call
  logs.
- Transcript cache keys now use `sha256` instead of `md5` (old md5-keyed cache
  files are still read as a fallback).

### Deprecated

- `src/google_search_mcp/server_selenium.py` is dead code (not imported by the
  server). It is slated for removal in a future release.

[0.4.0]: https://github.com/VincentKaufmann/noapi-google-search-mcp/releases/tag/v0.4.0
