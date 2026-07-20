# noapi-google-search-mcp v0.3.0 — Full Regression Test Report

**Date:** 2026-07-21 00:41:26
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
| 3 | _parse_rss_atom — RSS 2.0 parsing | 7 | 0.00s | PASS |
| 4 | _parse_rss_atom — Atom parsing | 8 | 0.00s | PASS |
| 5 | SQLite + FTS5 database | 8 | 0.09s | PASS |
| 6 | News RSS — BBC News | 4 | 0.48s | PASS |
| 7 | Reddit — r/technology | 3 | 0.78s | PASS |
| 8 | Hacker News — Top Stories | 6 | 1.24s | PASS |
| 9 | GitHub — Releases | 4 | 0.68s | PASS |
| 10 | arXiv — cs.AI Papers | 5 | 0.57s | PASS |
| 11 | YouTube — 3Blue1Brown | 5 | 0.18s | PASS |
| 12 | Podcast — Lex Fridman | 3 | 1.89s | PASS |
| 13 | Full feed workflow | 13 | 4.23s | PASS |
| 14 | transcribe_local — local audio/video | 3 | 7.15s | PASS |
| 15 | convert_media — FFmpeg conversion | 2 | 1.68s | PASS |
| 16 | read_document — PDF/DOCX/text/HTML | 7 | 0.03s | PASS |
| 17 | fetch_emails — IMAP email pull | 5 | 0.22s | PASS |
| 18 | paste_text — dpaste.org pastebin | 2 | 0.88s | PASS |
| 19 | shorten_url — TinyURL | 1 | 0.25s | PASS |
| 20 | generate_qr — QR code generation | 2 | 0.06s | PASS |
| 21 | archive_webpage — Wayback Machine | 1 | 1.71s | PASS |
| 22 | wikipedia — article lookup | 4 | 1.41s | PASS |
| 23 | upload_to_s3 — MinIO/S3 upload | 2 | 0.00s | PASS |
| 24 | YouTube auto-transcription | 3 | 0.92s | PASS |
| | **Total** | **120/120** | **24.46s** | **24/24** |

---

```
ALL TESTS PASSED
```
