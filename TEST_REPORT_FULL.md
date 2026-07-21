# noapi-google-search-mcp v0.3.0 — Full Regression Test Report

**Date:** 2026-07-21 10:16:40
**Python:** 3.11.15
**Platform:** linux
**Tools:** 39
**Result:** 24/24 test groups PASSED, 120/120 checks PASSED (100%)

---

## Results

| # | Test | Checks | Time | Result |
|---|------|--------|------|--------|
| 1 | Tool count and registration | 17 | 0.00s | PASS |
| 2 | _strip_html — HTML tag removal | 5 | 0.00s | PASS |
| 3 | _parse_rss_atom — RSS 2.0 parsing | 7 | 0.01s | PASS |
| 4 | _parse_rss_atom — Atom parsing | 8 | 0.00s | PASS |
| 5 | SQLite + FTS5 database | 8 | 0.12s | PASS |
| 6 | News RSS — BBC News | 4 | 0.85s | PASS |
| 7 | Reddit — r/technology | 3 | 0.86s | PASS |
| 8 | Hacker News — Top Stories | 6 | 0.92s | PASS |
| 9 | GitHub — Releases | 4 | 0.68s | PASS |
| 10 | arXiv — cs.AI Papers | 5 | 0.64s | PASS |
| 11 | YouTube — 3Blue1Brown | 5 | 0.29s | PASS |
| 12 | Podcast — Lex Fridman | 3 | 2.15s | PASS |
| 13 | Full feed workflow | 13 | 2.93s | PASS |
| 14 | transcribe_local — local audio/video | 3 | 4.28s | PASS |
| 15 | convert_media — FFmpeg conversion | 2 | 1.26s | PASS |
| 16 | read_document — PDF/DOCX/text/HTML | 7 | 0.01s | PASS |
| 17 | fetch_emails — IMAP email pull | 5 | 0.18s | PASS |
| 18 | paste_text — dpaste.org pastebin | 2 | 0.83s | PASS |
| 19 | shorten_url — TinyURL | 1 | 0.17s | PASS |
| 20 | generate_qr — QR code generation | 2 | 0.14s | PASS |
| 21 | archive_webpage — Wayback Machine | 1 | 15.58s | PASS |
| 22 | wikipedia — article lookup | 4 | 6.04s | PASS |
| 23 | upload_to_s3 — MinIO/S3 upload | 2 | 0.00s | PASS |
| 24 | YouTube auto-transcription | 3 | 0.60s | PASS |
| | **Total** | **120/120** | **38.53s** | **24/24** |

---

```
ALL TESTS PASSED
```
