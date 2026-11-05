# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-11-05

### Changed

- **The HTTP transports are now served natively; `mcp-proxy` is gone.**
  `python -m google_search_mcp --transport streamable-http` binds the port itself
  and serves `/mcp` (streamable HTTP), plus the deprecated `/sse` + `/messages/`
  transport for one release. The Node proxy was an extra process, an unpinned
  `npx -y` download on every restart, and a deprecated transport on the wire —
  and its `--transport` flag never existed (`--server` is the real one), so the
  unit file's stated intent had always been silently ignored. See
  [`docs/adr/0007-native-http-transport.md`](docs/adr/0007-native-http-transport.md).
- `mcp-google.service`, `start.sh` and `restart_server.sh` launch the Python
  server directly on `11403`. `restart_server.sh` lost the second half that
  killed the systemd-owned proxy and `nohup`ed a competing one. `-i` is no longer
  passed, so a crash exits and systemd can restart the process.
- `mcp` is now pinned to `>=1.27,<2`; the HTTP transport behaviour above is
  version-sensitive.
- **OCR is now an optional extra, so the package installs on Python 3.13.**
  `rapidocr-onnxruntime` declares `Requires-Python <3.13` upstream
  ([RapidAI/RapidOCR#579](https://github.com/RapidAI/RapidOCR/issues/579)), so
  as a hard dependency it made `pip install noapi-google-search-mcp`
  unsatisfiable on 3.13 — while the package claimed 3.13 support and CI only ran
  that job with `continue-on-error`. Install it with
  `pip install "noapi-google-search-mcp[ocr]"`. Both OCR entry points
  (`ocr_image`, and the scanned-PDF fallback in `read_document`) now return a
  one-line install hint.
- **`playwright>=1.48.0`** (was `>=1.40.0`) — the first release that officially
  added and tested Python 3.13.
- **`av` and `onnxruntime` are declared core dependencies.** Both were imported
  by shipped code but satisfied only by accident: `av` was missing entirely (so
  `extract_video_clip` failed on a clean install) and `onnxruntime` arrived
  transitively through `rapidocr`, which would have disappeared with the OCR
  move and silently disabled the neural-net CAPTCHA solver.
- The Python 3.13 CI job is **blocking** instead of `continue-on-error`.
- The `--check` dependency doctor reports `av`, `onnxruntime` and the OCR extra
  separately, with the right install hint for each.

### Added

- **`--manual-browser [URL]`** — opens a visible Chromium window that shares the
  server's cookies, for the cases only a human can handle: logging in, clearing a
  consent dialog, or finding a feed URL. Cookies are flushed every 5 s and on
  close to `~/.google_mcp_cookies.json`, which the server re-reads on its next
  browser tool call, so a login done here is immediately available to the tools.
  It uses a dedicated profile (`google-mcp-browser-manual`) rather than the
  service's: that profile is held by the service for its lifetime, is ~1.3 GB, and
  its `Cookies` file is SQLite, so a second writer risks corruption. Companion
  flags: `--browser-profile DIR` and `--browser-timeout SEC`.
- **`google_search_tabs_urls`** — reports the vertical tabs Google exposes on a
  SERP (`AI Mode`, `Images`, `News`, `Videos`, `Forums`, `Web`, `Books`, …) as
  `{label, url}` pairs, and with `include_sections=True` also the result-type
  section headers Google mixes into the page (`AI Overview`, `Top stories`,
  `Discussions and forums`, `Hotels`, `People also ask`, `Web results`, …).
  Tabs are de-duplicated by vertical (`Images` and `All images` are both
  `udm=2`) and page chrome (`View all`, `Next`, date-range pickers) is dropped.
  Read-only and cached for 30 min. Tool count is now **43**.
- `NOAPI_MCP_HTTP_STATELESS`, `NOAPI_MCP_HTTP_JSON_RESPONSE`,
  `NOAPI_MCP_HTTP_ENABLE_LEGACY_SSE`, `NOAPI_MCP_HTTP_DNS_REBINDING_PROTECTION`,
  `NOAPI_MCP_HTTP_ALLOWED_HOSTS`, `NOAPI_MCP_HTTP_ALLOWED_ORIGINS`.
- `google_search_mcp.http_app` — composes the streamable-HTTP and legacy SSE
  apps onto one Starlette app and owns process-wide startup/shutdown, because
  FastMCP's lifespan is entered once per session rather than once per process.
- **`visit_page` takes a per-call `max_characters` argument**, alongside the new
  `NOAPI_MCP_MAX_PAGE_CHARS` setting. The limit was a hardcoded 8000-character
  module constant, so the `env` block people reasonably reached for did nothing
  — and `NOAPI_MCP_MAX_TEXT_CHARS` was documented as a "generic text cap" while
  no code path read it. `NOAPI_MCP_MAX_CHARACTERS` / `max_characters` are also
  accepted as environment names, because that is the spelling people try first.
  Values are clamped to 200000: the text lands in a model's context window, and
  a tool that can silently return a megabyte turns one careless page visit into
  a blown context. A value that is not a positive integer is ignored rather than
  fatal, so a typo cannot stop the server from starting. The cap is part of the
  cache key, so asking for more text is never answered from a shorter cached
  entry.
- `[ocr]` extra (plus an `[all]` convenience extra) for the OCR tools.
- `CONTRIBUTING.md`, issue templates, and a pull-request template.

### Fixed

- **`/health` reported the wrong transport.** The field was hardcoded to
  `"transport": "stdio"`, so every HTTP deployment advertised stdio — the same
  class of stale label that made the retired `mcp-proxy --transport
  streamable-http` flag look effective. The CLI now records what it actually
  serves (`health_server.set_active_transport`), and `/health` reports that.
- **Domain-relevance audit across the Google tools.** An audit harness
  (`scripts/audit_tools.py`) calls every Google-facing tool and checks the output
  for markers that only exist in that domain, so a tool that silently answers a
  domain query with generic search results fails loudly. It also flags a
  page-chrome dump (Google's own navigation echoed as results), which is what
  first hid the `google_lens` defect.
- **`google_trends` reported the wrong widgets.** It matched any
  `[class*="related"]` element, so *interest by region* was emitted as *related
  queries* (and stray "trending now" rows leaked into every section). Widgets are
  now identified by their title (`trends-widget` → Related queries / Related
  topics / Interest by region / Interest over time) and each list is parsed by its
  row structure. A numeric value (a region share such as `53`) is no longer read
  as the next row's index, and the bidi marks Google wraps around trend dates are
  stripped before matching. Verified: regions `St. Helena 100, Singapore 53, South
  Korea 53, United States 45`, topics `Australia, TOYOTA, Toyota, Income, Fleet
  vehicle`, queries `comparing eight electric vehicles (Breakout), ford fathom
  (+950%)`.
- **`google_maps_directions` reported the wrong distance.** Its
  `#section-directions-trip-0` container no longer exists, so the code fell back
  to "the first distance anywhere on the page" and returned `25 mi` for a route
  whose real length is 191 km. Distance and duration are now read from the same
  route row (an ancestor containing both), so the pair always describes one
  option; the whole-page fallback is gone. Berlin → Dresden now reports
  `191 km | 2 hr 14 min`.
- **`google_finance` returned only a price.** `[data-last-price]` is gone from the
  current panel, and the code read the SERP before the panel rendered — then the
  `/finance/quote` fallback **replaced** the whole result, discarding the company
  name, change and stat rows. It now waits for the panel, reads `data-attrid`
  (`title`, `subtitle`, `Chart`, `Open`, `High`, `Low`, `Mkt cap`, `P/E ratio`,
  …), and *merges* the quote-page fallback instead of replacing it. `MSFT:NASDAQ`
  now returns the company, exchange and nine key stats.
- **`google_lens` presented page chrome as results.** Its extractors scanned every
  `div[role="heading"]`/`h3` on the page and, having matched nothing, appended the
  entire body text — which on an empty Lens page is Google's navigation
  ("Skip to main content / Accessibility help / Footer links"). That read as
  output and concealed the failure. Parsers are anchored to `#rso` now, the
  whole-body dump is removed, and an empty result is reported as such together
  with the Lens URL. Verified working: the sample image returns a description
  (`orange tabby cat … amber-gold eyes`) plus source links (Wikipedia, Wikimedia
  Commons, Flickr, Instagram, Pixabay). Note the Lens shell populates
  asynchronously and can still be empty transiently — that is now reported
  honestly instead of as chrome text.
- **Two inline ``page.evaluate`` scripts were invalid JavaScript.**
  `browser.detect_block_reason` and the manual-intervention poll loop both
  contained `'verify it\'s you'` inside a *non-raw* Python string: Python
  consumes the backslash, so JavaScript received a bare apostrophe, the script
  failed to parse, and the exception was swallowed by the surrounding
  `try/except`. Consequence: a block was never classified (`reason=unknown`, which
  is what the service log had been printing) and a resolved manual intervention
  was never noticed. The literals are double-quoted now, and
  `tests/unit/test_evaluate_scripts_syntax.py` compiles **every** inline script
  with `node --check` (parsing the module with `ast`, so raw/non-raw escaping is
  resolved exactly as the browser sees it).
- **`google_shopping` matched nothing and dumped raw page text.** Every selector
  it used (`.sh-dgr__content`, `.KZmu8e`, `.i0X6df`, `.xcR77`, `[data-docid]`,
  `.sh-pr__product-result`) has disappeared from the shopping vertical, so the
  extractor returned nothing and the caller appended `#rso`'s inner text without
  a label — which read as output. It now anchors on the product box
  (`.UC8ZCe`, else the nearest ancestor of a price span that also holds a
  title), requires a title *before* the price (a filter chip such as "Under €60"
  is not a product), and parses current price, list price, merchant and rating.
  The raw fallback is labelled "(Could not extract structured results …)" so a
  selector rot can never again look like data. Verified: five keyboards with
  `Price: €59.99`, `Store: Amazon.de - Amazon.de-Seller`, `Rating: 4,9 (956)` and
  inline images.
- **`google_trends` failed with "lookup failed".** The widget extractor had an
  unbalanced brace, which is a JavaScript syntax error — so *every* call threw
  and was reported as a transient failure. Fixed; regions and topics are now
  returned as separate lists (`St. Helena 100, Singapore 56, South Korea 55 …`;
  `National Health Service +500%, March +450% …`).
- **`google_hotels` returned search-tab links instead of hotels.**
  It searched `google.com/search?q=hotels <query>` (a generic web SERP) and
  parsed `div[data-ved] a[href*="hotel"]`. Those tab links (Images, News, Forums)
  *matched* because their query string contains the word "hotels", so they were
  emitted as results with nothing but a URL — and when Google rendered no hotel
  pack there were no listings at all. The tool now targets
  `google.com/travel/search` (Google's hotel surface) and reads the travel result
  cards: name, nightly price, rating, review count, stay dates, amenities, photo
  and detail link. The leading `"hotels "` prefix that doubled the word on the
  wire is gone, and the bot-detection guard is unchanged.
- **`google_books` returned plain web results instead of book results.** The
  search-via-typing anti-detect flow rebuilds the query from scratch, so the
  `tbm=` vertical in the request URL was dropped and the scraper parsed a normal
  SERP. `browse_google` now derives the vertical from the URL itself
  (`parse_vertical_from_url`), which also restores `google_news` (`nws`) and
  `google_shopping` (`shop`); both of those tools already carried vertical-specific
  selectors that could never match. `google_images` keeps its explicit `isch`.
- **Queries were prepended with the warm-up query.** `search_via_typing` typed
  into the Google search box without clearing it, and the anti-detect warmup
  leaves its benign query there. The search actually sent was
  `news today<taymiyyah>` — it still returned a plausible SERP, so nothing failed
  visibly. The flow now always starts from the homepage, clears the box
  (select-all/delete, with a `fill("")` fallback), verifies the box holds the
  query before submitting, and verifies the `q=` Google actually received. If any
  check fails it returns `False`, so `browse_google` navigates straight to the
  requested URL rather than scraping a SERP for the wrong search.
- **`google_news` fell back to raw page text.** Its result-container selectors
  (`div.SoaBEf`, `div.g`, …) no longer exist. It now targets the current cards
  (`a.aJWbwf` / `div.WCv1we` inside `#rso`, headline `.n0jPhd`, snippet
  `.UqSP2b`, date `.OSrXXb`) with the old selectors kept as fallbacks, and
  de-duplicates repeated articles.
- **`google_books` parsing.** The extractor now anchors on
  `books.google.com/books?id=…` links rather than every `<h3>`, so an unrelated
  SERP can no longer be mistaken for books; results are de-duplicated by book id
  (one book is linked several times); the author comes from the `inauthor:` link
  instead of any nearby span containing a comma or a digit; the publication year
  is captured; and URLs are canonicalised to
  `https://books.google.com/books?id=…`.
- Requests addressed to a LAN hostname (e.g. `latitude:11403`) are no longer
  rejected with `421 Invalid Host header`. `FastMCP` freezes its
  `transport_security` allow-list from the construction-time loopback host, so
  the HTTP entry point now sets it explicitly (protection stays off by default,
  matching the previous deployment).
- The PyInstaller spec pointed at `server.py`, which has no `__main__` guard, so
  the frozen binary imported the server and exited. It now builds
  `scripts/frozen_entry.py` and bundles `uvicorn`/`starlette`, which the HTTP
  path imports lazily.
- **A scanned PDF with no OCR installed looked like a blank document.** The
  `rapidocr` import sat inside `except Exception: pass`, so the missing extra was
  indistinguishable from a page with no text; the caller then reported "could not
  extract text". The path now checks for the extra explicitly, skips the
  `pdftoppm`/OCR work entirely when it is absent, and names the extra in the
  hint.
- **`yt-dlp` progress output is suppressed unconditionally.** The progress bar
  writes to stdout, which is the JSON-RPC channel on the stdio transport; a
  single call passing `quiet=False` would corrupt every message after it. The
  option was already set from `quiet=True`, but the shared builder allowed the
  unsafe combination, so it is now pinned in `build_ydl_opts`.

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
  default 3) and automatic relaunch on disconnect. Measured **8.6×–14.8× faster per
  call** (≈2695 ms → 182–315 ms) in `scripts/bench_browser.py`.
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

[Unreleased]: https://github.com/sameersemna/noapi-google-search-mcp/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/sameersemna/noapi-google-search-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/VincentKaufmann/noapi-google-search-mcp/releases/tag/v0.4.0
