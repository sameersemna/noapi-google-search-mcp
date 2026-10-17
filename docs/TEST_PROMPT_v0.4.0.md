# v0.4.0 MCP test prompt — hardening release

A copy‑paste prompt (plus expectations and evidence checks) to verify the v0.4.0
changes against a **running** server, confirm previously‑existing tools still
behave, and exercise the tool that was added most recently.

Verified against the live registry on 2026‑10‑17: **42 tools**, all carrying MCP
annotations, served from `hardening/v0.4` @ `9751119c`, `/health` reporting
`version 0.4.0`.

---

## 1. What actually changed (so you test the right things)

**No new tools were added in v0.4.0** — the count is still 42. The most recently
*added* tool overall is **`list_subtitles`** (added in v0.3.6), so it is included
below as the "recently added tool" check.

v0.4.0 changed **existing** tools. These are the things worth testing:

| # | Change | Tools | Test |
|---|---|---|---|
| 1 | New optional `fresh` (cache bypass) | `wikipedia` | B4–B6 |
| 2 | New optional `idempotency_key` + duplicate suppression | `paste_text`, `shorten_url`, `archive_webpage`, `upload_to_s3` | B7–B12 |
| 3 | New optional `overwrite` | `upload_to_s3` | B13 |
| 4 | New optional `since` / `until` | `search_feeds` | D7 |
| 5 | Validated enums | `google_search.time_range`; `model_size` on `transcribe_video` / `transcribe_local` / `search_transcript` | A6, B3, E1–E3 |
| 6 | Read‑through cache + singleflight | all read‑only lookups | B1–B2, B4–B6 |
| 7 | Browser pool (Chromium launched once, max 3 concurrent) + token‑bucket pacing + circuit breaker | all browser tools | B1–B2, evidence in §5 |
| 8 | SSRF guard & filesystem roots guard | `visit_page`, `archive_webpage`, `shorten_url`, `read_document`, `transcribe_local`, `list_images`, `upload_to_s3` | A1–A4 |
| 9 | Failures now return MCP errors (`isError`) instead of plain text | all | A1–A4, A6 |
| 10 | Credentials via env (`NOAPI_MCP_IMAP_USER` / `_PASSWORD`) | `fetch_emails` | A5 |
| 11 | `fetch_emails` uses `BODY.PEEK[]` — reading mail no longer marks it *seen* | `fetch_emails` | A5 |
| 12 | Output caps; sha256 (not md5) transcript cache keys; lazy heavy imports | `read_document`, transcription | C16, E1–E3 |
| 13 | CLI: `--version`, `--check`, `--transport` | — | §3 |
| 14 | 20 bug fixes (B1–B20), incl. broken `google_translate`, local‑file clip extraction, images fallback | many | C7, C20, C9 |
| 15 | Backward compatibility: every tool must still work with **default** arguments | all 42 | Tier C |

---

## 2. Pre-flight (run in a terminal)

```bash
cd /home/sameer/Shared/Sync/Private/Apps/services/mcp/noapi-google-search-mcp

# Restart so the service definitely runs the new code.
sudo systemctl restart mcp-google.service
sleep 3 && systemctl is-active mcp-google.service

# Confirm the code being served is v0.4.0.
curl -s http://127.0.0.1:11499/health | python3 -c \
  'import sys,json; d=json.load(sys.stdin); print(d["version"], d["git"]["commit"][:8], d["git"]["branch"], "dirty=", d["git"]["dirty"])'

# Snapshot per-tool call metrics and pool state BEFORE the test.
curl -s http://127.0.0.1:11499/health | python3 -c \
  'import sys,json; m=json.load(sys.stdin)["metrics"]; print("calls:", m["total_calls"], "errors:", m["total_errors"])'
grep -c "browser pool started" service.log   # expect 0 right after a restart
```

Optional CLI checks (these run the module directly, no service needed):

```bash
/home/sameer/anaconda3/envs/mcp-google/bin/python -m google_search_mcp --version   # -> 0.4.0
/home/sameer/anaconda3/envs/mcp-google/bin/python -m google_search_mcp --check     # -> exit 0
```

---

## 3. The prompt — SHORT SMOKE (≈2 minutes)

Paste this into the client that has this MCP server attached (LM Studio / Claude
Desktop / your mcp-proxy endpoint). It covers the highest‑value v0.4.0 changes
and proves old tools still answer.

```text
You are testing a locally running MCP server: noapi-google-search-mcp v0.4.0.
Execute every step by ACTUALLY CALLING the listed tool with the exact arguments.
Never answer from memory, never simulate a tool result.

For each step, output one table row:
  Step | Tool | Args | Observed (one line; quote any error verbatim) | Verdict (PASS/FAIL/SKIPPED) | Notes
Also record the elapsed time of each call.

Steps:
1.  check_cookies()                                  -> expect a cookie status line (not an error).
2.  manual_intervention_status()                      -> expect an "inactive"/not‑active status.
3.  visit_page("file:///etc/passwd")                  -> EXPECT AN ERROR: must mention the scheme is not allowed, and must NOT return file contents.
4.  visit_page("http://169.254.169.254/latest/meta-data/") -> EXPECT AN ERROR mentioning a cloud metadata address.
5.  read_document("/etc/hostname")                    -> EXPECT AN ERROR: outside the allowed roots.
6.  fetch_emails()                                    -> expect the message that credentials are missing (mentions NOAPI_MCP_IMAP_USER), not an error.
7.  google_search("current UTC time", num_results=3)  -> expect results. NOTE THE ELAPSED TIME (this is the first browser call: pool warm‑up).
8.  repeat step 7 with IDENTICAL arguments             -> expect the SAME results but MUCH FASTER (cache hit).
9.  google_search("python programming", num_results=5, time_range="past_week") -> header must say "(filtered: past week)".
10. google_search("test", time_range="last_week")      -> EXPECT REJECTION (invalid value for the time_range enum).
11. wikipedia("Python (programming language)", language="en", sentences=3)  -> note elapsed time.
12. repeat step 11 with identical arguments            -> expect it FASTER (cache hit).
13. wikipedia("Python (programming language)", language="en", sentences=3, fresh=true) -> expect a fresh fetch (slower than step 12).
14. paste_text("noapi-mcp v0.4.0 idempotency test", syntax="text")   -> record the URL.
15. repeat step 14 with IDENTICAL content              -> expect the SAME URL plus the note "(cached — identical request already completed)".
16. shorten_url("https://example.com/a/very/long/path?with=query&and=more") -> record the short URL.
17. repeat step 16 with the identical URL              -> expect the SAME short URL plus the "(cached …)" note.
18. google_news("artificial intelligence", num_results=3)
19. google_scholar("transformer architecture", num_results=3)
20. google_translate("Good morning, how are you?", to_language="german")   -> expect a German translation.
21. google_finance("AAPL")
22. google_weather("Berlin")
23. google_trends("electric vehicles")
24. list_subtitles("https://www.youtube.com/watch?v=dQw4w9WgXcQ")          -> expect a list of caption languages (this is the most recently added tool).
25. subscribe(source_type="hackernews", identifier="top")   -> expect "Subscribed to …".
26. subscribe(source_type="hackernews", identifier="top")   -> expect "Already subscribed to …" (idempotent).
27. check_feeds(source_type="hackernews")                    -> expect a per‑source line and a total; should finish in seconds.
28. get_feed_items(source_type="hackernews", limit=5)        -> expect ~5 items.
29. search_feeds("the", source_type="hackernews", limit=5)   -> expect results.
30. search_feeds("the", source_type="hackernews", since="2020-01-01", until="2030-01-01", limit=5) -> expect results (date filters accepted).
31. list_subscriptions()                                     -> expect 1 subscription with an item count.
32. unsubscribe(source_type="hackernews", identifier="top")   -> expect "Unsubscribed from …". (This is destructive and expected.)

Finally print a summary: number of PASS / FAIL / SKIPPED, then list every step whose Verdict is FAIL with the raw error text.
```

---

## 4. The prompt — FULL RUN (all 42 tools)

Same rules as above. Run the short prompt first; then continue with these tiers.
Mark anything slow or credential‑dependent as SKIPPED if you cannot run it.

```text
Continue the v0.4.0 MCP test. Same reporting rules (table row per step, real tool
calls only, verbatim errors, elapsed time).

TIER B — new parameters and caching (finish these even if you did the smoke test)
B1. google_search("northern lights photography", num_results=5) then repeat it identically.
    Expect: second call returns the same results but noticeably faster.
B2. For each of these, call it twice with identical arguments and report whether the
    second call is faster: google_news("world news", 3); google_scholar("diffusion models", 3);
    google_books("clean code", 3); google_trends("electric vehicles"); google_finance("AAPL");
    google_weather("Berlin"); visit_page("https://example.com/"); google_maps("coffee shops in Berlin", 3).
B3. paste_text("idempotency probe content", syntax="text", idempotency_key="probe-1") twice.
    Expect: same URL, second answer contains "(cached — identical request already completed)".
B4. upload_to_s3: if you have NO bucket/credentials, output SKIPPED with the reason.
    If you do have them, first upload a small file to key "mcp-test.txt", then repeat the upload
    with overwrite=false and expect: "already exists (overwrite=False)".

TIER C — backward compatibility with DEFAULT arguments (one call each)
C1.  google_search("python asyncio tutorial")
C2.  google_news("artificial intelligence", num_results=3)
C3.  google_scholar("transformer architecture", num_results=3)
C4.  google_trends("electric vehicles")
C5.  google_finance("AAPL")
C6.  google_weather("Berlin")
C7.  google_shopping("mechanical keyboard", num_results=3)
C8.  google_books("clean code", num_results=3)
C9.  google_translate("Good morning, how are you?", to_language="german")
     (This tool was completely broken before v0.4.0 — a NameError on its first line. It must now return a translation.)
C10. google_flights(origin="Berlin", destination="Lisbon", date="2026-11-10")
C11. google_hotels("hotels in Lisbon", num_results=3)
C12. google_images("northern lights", num_results=3)
C13. google_maps("coffee shops in Berlin", num_results=3)
C14. google_maps_directions(origin="Berlin Hauptbahnhof", destination="Alexanderplatz", mode="transit")
C15. google_lens("<a public image URL you can find>")           [SKIP if you have no image]
C16. google_lens_detect("<a local .png/.jpg inside your home directory>")  [SKIP if none]
C17. visit_page("https://example.com/")
C18. wikipedia("Albert Einstein")                                (default args, no fresh)
C19. list_images()                                               -> either a list, or the "Directory not found … mkdir -p ~/lens" hint.
C20. read_document("/home/sameer/Shared/Sync/Private/Apps/services/mcp/noapi-google-search-mcp/README.md")
C21. ocr_image("<a local image inside your home directory>")      [SKIP if none]
C22. generate_qr("https://example.com", size=200)                -> expect "QR code saved to: …"
C23. list_subtitles("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
C24. check_cookies()
C25. manual_intervention_status()
C26. extract_video_clip("<a local .mp4 inside your home directory>", start_seconds=0, end_seconds=2)
     [SKIP if none. NOTE: local‑file clip extraction was broken before v0.4.0 — it must not fail with a NameError.]
C27. convert_media("<a small media file inside your home directory>", output_format="mp3")  [SKIP if none]
C28. transcribe_local("<a short media file inside your home directory>", model_size="tiny") [SKIP if none]
C29. open_manual_browser(url="https://example.com", reason="unknown", timeout_sec=5) -> SKIP; only used when a block occurs.

TIER D — feeds lifecycle (idempotency + concurrency + date filters)
D1. subscribe(source_type="arxiv", identifier="cs.AI")     -> "Subscribed to arXiv cs.AI (arxiv)."
D2. subscribe(source_type="arxiv", identifier="cs.AI")     -> "Already subscribed to … (idempotent)."
D3. check_feeds(source_type="arxiv")                        -> expect a per‑source line + "Total: N new items across 1 sources".
                                                               Note the total wall time: sources are fetched CONCURRENTLY.
D4. check_feeds(source_type="arxiv")  a second time         -> expect mostly 0 new items (duplicates suppressed; conditional GET).
D5. get_feed_items(source_type="arxiv", limit=5)            -> ~5 items.
D6. search_feeds("learning", source_type="arxiv", limit=5)   -> results (ranked).
D7. search_feeds("learning", source_type="arxiv", since="2024-01-01", until="2030-01-01", limit=5) -> results.
D8. search_feeds("learning", source_type="arxiv", since="2030-01-01", limit=5) -> expect a date filter is HONOURED (likely no results), not an error.
D9. list_subscriptions()  -> shows the arxiv subscription with an item count and a "last checked" time.
D10. unsubscribe(source_type="arxiv", identifier="cs.AI")   -> "Unsubscribed from …". (destructive, expected)

TIER E — heavy / slow (optional; each may take minutes)
E1. transcribe_video("<a short YouTube URL>", model_size="small")
    -> must accept model_size="small" (enum) and not coerce it to tiny.
E2. search_transcript("<the URL transcribed in E1>", query="<a word from it>", model_size="base")
E3. extract_video_clip("<the URL from E1>", start_seconds=10, end_seconds=20)
E4. transcribe_video("<a short YouTube URL>", model_size="huge")  -> EXPECT REJECTION (invalid enum).

FINALLY: print the full results table, the PASS/FAIL/SKIPPED counts, and every raw error message.
```

---

## 5. Expected results (for judging the model's report)

| Step | PASS looks like |
|---|---|
| A1 `visit_page("file://…")` | Error containing `scheme 'file' is not allowed`. **No file contents.** |
| A2 `visit_page("http://169.254.169.254/…")` | Error containing `cloud metadata address`. |
| A3 `read_document("/etc/hostname")` | Error containing `outside the allowed roots`. |
| A4 `archive_webpage("http://169.254.169.254/")` | Same metadata refusal. |
| A5 `fetch_emails()` | `Missing email credentials. Set NOAPI_MCP_IMAP_USER and NOAPI_MCP_IMAP_PASSWORD …` (a normal answer, not an error). If your env has IMAP creds set, it will fetch real mail instead — that is also a PASS, but reading mail must **not** mark it as read (verify in your mail client). |
| A6 / E4 invalid enum | The call is **rejected** (schema validation or a validation error). The important part: it is *not* silently ignored/coerced. |
| B1/B3/B4 `google_search` ×2 | Second identical call markedly faster; identical result text. |
| B3 valid `time_range="past_week"` | Header contains `(filtered: past week)`. |
| B4–B6 `wikipedia` | Call 2 faster than call 1; call 3 (`fresh=true`) is a fresh fetch (not faster). |
| B7–B12 paste/shorten/archive ×2 | Identical URL **and** the marker `(cached — identical request already completed)` on the second answer. |
| B13 `upload_to_s3(overwrite=false)` | `already exists (overwrite=False)` + a hint to pass `overwrite=True`. |
| C7 `google_translate` | A real translation (this tool was broken before v0.4.0). |
| C20 `read_document(README.md)` | Document text returned (allowed path). |
| C26 local clip extraction | No `NameError`; either a clip is produced or a clear dependency error. |
| D1–D2 `subscribe` | First: `Subscribed to …`; second: `Already subscribed to …` (idempotent, no duplicate row). |
| D3 `check_feeds` | One line per source plus `Total: N new items across 1 sources`; wall time ≈ slowest single source (concurrent), not the sum. |
| D4 second `check_feeds` | Mostly `0 new items` (duplicate suppression). |
| D7 `search_feeds(since,until)` | Results returned, filters accepted. |
| D8 `since="2030-01-01"` | No results (filter honoured) — **not** an error and **not** the full set. |
| All Tier C | Every tool returns its normal output shape; **no** `NameError`, `is not defined`, or bare traceback text. |

**Immediate red flags:** any `name 'X' is not defined`, any tool that returns raw
Python traceback text, a second identical `paste_text` producing a **different**
URL, `visit_page("file://…")` returning file contents, or an invalid enum value
being silently accepted.

> Steps A1–A4 and the exact wording of the `(cached — identical request already
> completed)` marker were pre-verified against the code and through the real MCP
> call path on 2026‑10‑17 (all four return `ToolError` with the messages quoted
> above), so a FAIL on those steps means the deployment is wrong, not the
> expectation.

---

## 6. Evidence to check afterwards (terminal)

```bash
cd /home/sameer/Shared/Sync/Private/Apps/services/mcp/noapi-google-search-mcp

# Per-tool call counters (proves the calls really happened, and error counts).
curl -s http://127.0.0.1:11499/health | python3 -c \
  'import sys,json; m=json.load(sys.stdin)["metrics"]; print("calls:", m["total_calls"], "errors:", m["total_errors"]);
print(json.dumps(m["per_tool"], indent=2, sort_keys=True))'

# PHASE 4 PROOF: Chromium should have been launched ONCE for many calls, not per call.
grep -c "browser pool started" service.log        # expected: 1
grep -n "browser pool started" service.log

# Tool-call log lines (names + redacted args). Secrets must never appear here.
grep -n "tool=" service.log | tail -20

# STDOUT HYGIENE: stdout is the MCP protocol channel, so the log must contain
# no stray prints. Check that expected protocol lines look sane.
tail -30 service.log
```

**What to look for:** `browser pool started (max_concurrent=3)` appears **once**;
per‑tool counters match the number of calls you asked for; no `password=`/
`secret_key=` values appear in the log; the second `paste_text` shows no second
paste in the log.

---

## 7. Tool coverage matrix (all 42)

| Tier | Tools |
|---|---|
| Sanity | `check_cookies`, `manual_intervention_status` |
| Guards / errors | `visit_page`, `read_document`, `archive_webpage`, `fetch_emails`, `google_search` |
| New params + cache | `wikipedia`, `paste_text`, `shorten_url`, `archive_webpage`, `upload_to_s3` |
| Backward compat | `google_search`, `google_news`, `google_scholar`, `google_trends`, `google_finance`, `google_weather`, `google_shopping`, `google_books`, `google_translate`, `google_flights`, `google_hotels`, `google_images`, `google_maps`, `google_maps_directions`, `google_lens`, `google_lens_detect`, `visit_page`, `wikipedia`, `list_images`, `read_document`, `ocr_image`, `generate_qr`, `list_subtitles`, `extract_video_clip`, `convert_media`, `transcribe_local`, `open_manual_browser` |
| Feeds | `subscribe`, `check_feeds`, `get_feed_items`, `search_feeds`, `list_subscriptions`, `unsubscribe` |
| Heavy | `transcribe_video`, `search_transcript`, `extract_video_clip` |

---

## 8. Notes / known gaps

- `/health` → `config` still lists only the pre‑v0.4.0 settings; the new knobs
  (`MAX_BROWSERS`, `ENABLE_CACHE`, `GOOGLE_RPS`, `ALLOW_PRIVATE_URLS`, …) are
  **not** echoed there yet. To confirm one took effect, set it and observe
  behaviour (e.g. the `browser pool started (max_concurrent=N)` log line), or
  check `config.py` directly. Adding the new keys to the `/health` config dump is
  a small, worthwhile follow‑up.
- The circuit breaker only latches after a real Google block, so it cannot be
  triggered on demand from the client. To exercise it deliberately, set
  `NOAPI_MCP_CIRCUIT_COOLDOWN_SECONDS` low, force a block (many rapid searches),
  then confirm the next call fails fast with a message mentioning the remaining
  cooldown instead of retrying.
- `open_manual_browser` is intentionally not exercised; it is the headful
  fallback used only when a block is detected.
- `upload_to_s3` and the IMAP/mail checks depend on credentials; SKIPPED is an
  acceptable outcome for those.
