"""End-to-end test of every MCP tool through the live HTTP endpoint.

Unlike ``scripts/audit_tools.py`` (which calls the tool functions in-process), this
talks to the running service over streamable HTTP exactly as a client does — so it
also exercises the transport, tool registration, schema validation and response
shaping.

Outcomes are classified, because "did it work" is not binary for every tool:

* PASS          — returned data from its own domain
* BLOCKED       — Google bot detection / rate limit (environmental, retryable)
* NEEDS_CONFIG  — a credential-backed tool refusing cleanly instead of crashing
* EMPTY         — valid tool, but nothing to report (e.g. no subtitles in a clip)
* FAIL          — wrong, generic, empty-but-unexpected, or an exception

    python scripts/live_test_all_tools.py            # every group
    python scripts/live_test_all_tools.py local      # one group
    python scripts/live_test_all_tools.py google remote heavy

Groups: local, google, remote, heavy.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Fixtures the caller prepares first; see the module docstring.
# Target any deployment, e.g. a second instance started on another port to test
# a config change before it is rolled out:
#   MCP_TEST_URL=http://127.0.0.1:11404/mcp python scripts/live_test_all_tools.py heavy
URL = os.environ.get("MCP_TEST_URL", "http://127.0.0.1:11403/mcp")
FIXTURES = Path(tempfile.gettempdir()) / "mcp_selftest"
SAMPLE_IMAGE = str(Path(tempfile.gettempdir()) / "audit_sample.jpg")

# Markers that mean the tool reported a problem rather than data.
BLOCKED_MARKERS = (
    "blocked by bot detection",
    "rate-limiting",
    "rate limited",
    "429",
    "unusual traffic",
)
CONFIG_MARKERS = (
    "not configured",
    "not set",
    "missing",
    "requires",
    "credentials",
    "no imap",
    "access key",
    "api key",
    "install",
    "unavailable",
    "disabled",
)
EMPTY_MARKERS = (
    "no results",
    "no subtitles",
    "no items",
    "nothing found",
    "no subscriptions",
    "could not find",
    "empty",
)
# Upstream authentication/consent walls (yt-dlp's YouTube bot check, expired
# cookies). Not a defect, and retrying cannot fix it, so these are reported as
# NEEDS_CONFIG with the reason rather than as FAIL — and, just as important, not
# as PASS, which is what would happen if the message fell through to the
# expectation check because it contains no failure marker.
AUTH_MARKERS = (
    "rejected the request as automated",
    "refresh the cookies",
    "sign in to confirm",
    "not a bot",
)
# Markers that mean the tool itself reported a problem rather than data.
#
# Matched against the whole reply, so every entry must be a phrase that real
# content would not contain by accident. Bare English words were removed after
# `get_feed_items` was scored FAIL on a BBC headline reading "bus crashes in
# France": an aggregator's payload legitimately contains words like crash,
# cancelled and failed. A genuine server crash never reaches this point anyway —
# it surfaces as a raised exception or a timeout, both of which run_group already
# reports as FAIL.
FAILURE_MARKERS = (
    "raw page content",
    "could not extract",
    "lookup failed",
    "traceback",
    "unhandled",
    "iserror",
)
# Short tool-side failures ("Google Maps search failed.") are matched only in the
# opening line, so a long, legitimately failing-worded result is not mistaken for
# a broken tool.
OPENING_FAILURE_MARKERS = (
    "failed.",
    "failed to ",
    "error:",
    "exception:",
)
CHROME_WORDS = (
    "skip to main content",
    "accessibility help",
    "footer links",
    "send feedback",
    "try without personalisation",
)


@dataclass
class Case:
    tool: str
    args: dict[str, Any]
    group: str
    expect: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    note: str = ""
    timeout: float = 120.0
    skipped: str = ""  # set to skip with a reason
    extra: dict[str, Any] = field(default_factory=dict)


def _qr_path() -> Path:
    return FIXTURES / "selftest_qr.png"


CASES: list[Case] = [
    # ── local / no Google traffic ─────────────────────────────────────────
    Case("check_cookies", {}, "local", expect=("cookie",)),
    Case("manual_intervention_status", {}, "local", expect=("","")),
    Case("wikipedia", {"query": "Python", "sentences": 2}, "local", expect=("Python",)),
    Case("visit_page", {"url": "https://example.com"}, "local", expect=("Content from",)),
    Case("generate_qr", {"data": "https://example.com/selftest", "output_path": str(_qr_path())},
         "local", expect=("",)),
    Case("read_document", {"file_path": str(FIXTURES / "sample.txt")}, "local",
         expect=("quick brown fox",)),
    Case("list_images", {"directory": str(FIXTURES)}, "local", expect=("",)),
    Case("ocr_image", {"image_source": str(_qr_path())}, "local", expect=("",)),
    Case("list_subscriptions", {}, "local", expect=("",)),
    Case("get_feed_items", {"limit": 3}, "local", expect=("",)),
    Case("search_feeds", {"query": "test", "limit": 3}, "local", expect=("",)),
    Case("subscribe", {"source_type": "hackernews", "identifier": "top", "name": "selftest-hn"},
         "local", expect=("",)),
    Case("unsubscribe", {"source_type": "hackernews", "identifier": "top"}, "local", expect=("",)),
    Case("convert_media", {"input_path": str(FIXTURES / "tone.wav"), "output_format": "mp3"},
         "local", expect=("",), timeout=180),
    # ── Google browser tools ──────────────────────────────────────────────
    Case("google_search", {"query": "python asyncio tutorial", "num_results": 3}, "google",
         expect=("http",)),
    Case("google_search_tabs_urls", {"query": "electric vehicles"}, "google",
         expect=("Tabs:", "URL:")),
    Case("google_search_tabs_urls", {"query": "electric vehicles", "include_sections": True},
         "google", expect=("Tabs:", "Sections:"), note="include_sections=True"),
    Case("google_news", {"query": "electric vehicles", "num_results": 3}, "google",
         expect=("Source:",)),
    Case("google_scholar", {"query": "transformer attention", "num_results": 3}, "google",
         expect=("http",)),
    Case("google_images", {"query": "red panda", "num_results": 3}, "google", expect=("",)),
    Case("google_trends", {"query": "electric vehicles"}, "google", expect=("Google Trends for:",)),
    Case("google_maps", {"query": "coffee shops in Berlin", "num_results": 3}, "google",
         expect=("Link:",)),
    Case("google_maps_directions", {"origin": "Berlin", "destination": "Dresden"}, "google",
         expect=("Distance:", "Duration:")),
    Case("google_finance", {"query": "MSFT:NASDAQ"}, "google", expect=("Price:",)),
    Case("google_weather", {"location": "Berlin"}, "google", expect=("°",)),
    Case("google_shopping", {"query": "mechanical keyboard", "num_results": 3}, "google",
         expect=("Price:",)),
    Case("google_books", {"query": "taymiyyah", "num_results": 3}, "google",
         expect=("Author:", "books.google")),
    Case("google_translate", {"text": "Good morning, how are you?", "to_language": "de"},
         "google", expect=("Translation (",)),
    Case("google_flights", {"origin": "Berlin", "destination": "Paris"}, "google",
         expect=("Google Flights:",)),
    Case("google_hotels", {"query": "Hotels in Berlin under $200 per night", "num_results": 3},
         "google", expect=("Rating:", "Price:")),
    Case("google_lens", {"image_source": SAMPLE_IMAGE}, "google", expect=("Google Lens Results",)),
    Case("google_lens_detect", {"image_source": SAMPLE_IMAGE}, "google",
         expect=("Objects detected:",), timeout=240),
    # ── remote writes / credential-backed ────────────────────────────────
    Case("fetch_emails", {"limit": 1}, "remote", expect=("",)),
    Case("upload_to_s3", {"file_path": str(FIXTURES / "sample.txt"), "bucket": "selftest"},
         "remote", expect=("",)),
    Case("paste_text", {"content": "noapi mcp self-test", "title": "selftest"}, "remote",
         expect=("http",)),
    Case("shorten_url", {"url": "https://example.com/a/very/long/path/for/testing"}, "remote",
         expect=("http",)),
    Case("archive_webpage", {"url": "https://example.com"}, "remote", expect=("http",)),
    # ── heavy media (network + models) ───────────────────────────────────
    Case("list_subtitles", {"url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ"}, "heavy",
         expect=("",), timeout=180),
    Case("transcribe_local", {"file_path": str(FIXTURES / "tone.wav"), "language": "en"},
         "heavy", expect=("",), timeout=300),
    Case("extract_video_clip",
         {"url": str(FIXTURES / "clip.mp4"), "start_seconds": 0, "end_seconds": 1},
         "heavy", expect=("",), timeout=180),
    Case("transcribe_video", {"url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
                             "prefer_subtitles": True}, "heavy", expect=("",), timeout=420),
    # Depends on the transcript transcribe_video builds for the same URL.
    Case("search_transcript", {"url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
                               "query": "bunny"}, "heavy", expect=("",), timeout=420),
    # YouTube triage: metadata and caption tracks only — no audio, no GPU.
    Case("youtube_video_info", {"url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ"},
         "heavy", expect=("",), timeout=180),
    Case("get_captions", {"url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
                          "query": "bunny"}, "heavy", expect=("",), timeout=240),
    Case("youtube_search", {"query": "big buck bunny", "limit": 3}, "heavy",
         expect=("",), timeout=180),
    Case("list_channel_videos", {"target": "@3Blue1Brown", "limit": 3}, "heavy",
         expect=("",), timeout=180),
    Case("check_feeds", {}, "heavy", expect=("",), timeout=300),
    # Runs unattended would pop a GUI window on someone's desktop.
    Case("open_manual_browser", {}, "heavy",
         skipped="opens a headful browser window; not run unattended"),
]


def flatten(result: Any) -> str:
    """Render a CallToolResult's content as plain text."""
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            data = getattr(item, "data", b"")
            parts.append(f"[image {len(data)} bytes]")
    return "\n".join(parts)


def classify(case: Case, result: Any, text: str) -> tuple[str, str]:
    """Map a call outcome onto PASS / BLOCKED / NEEDS_CONFIG / EMPTY / FAIL."""
    low = text.casefold()

    if getattr(result, "isError", False):
        # An MCP error is a legitimate answer for a tool that cannot run here,
        # as long as it says why. A bare failure is not.
        if any(m in low for m in CONFIG_MARKERS):
            return "NEEDS_CONFIG", "clean configuration error"
        if any(m in low for m in BLOCKED_MARKERS):
            return "BLOCKED", "bot detection"
        return "FAIL", f"isError: {text.strip()[:80]}"

    if any(m in low for m in AUTH_MARKERS):
        return "NEEDS_CONFIG", "upstream auth wall (refresh cookies)"

    opening = text.strip().splitlines()[0].casefold() if text.strip() else ""
    marker = next((m for m in FAILURE_MARKERS if m in low), None)
    if marker is None:
        marker = next((m for m in OPENING_FAILURE_MARKERS if m in opening), None)
    if marker is not None:
        if any(m in low for m in BLOCKED_MARKERS):
            return "BLOCKED", "bot detection"
        return "FAIL", f"failure marker {marker!r}"

    hits = sum(1 for word in CHROME_WORDS if word in low)
    if hits >= 3:
        return "FAIL", f"page-chrome dump ({hits} markers)"

    # A short reply naming missing configuration is a refusal, not a result —
    # even when the case has no ``expect`` marker to miss. Without this, a tool
    # that answers "IMAP not configured" instantly was scored as a pass.
    if len(low) < 300 and any(m in low for m in CONFIG_MARKERS):
        return "NEEDS_CONFIG", "clean configuration error"

    missing = [m for m in case.expect if m and m.casefold() not in low]
    if missing:
        if any(m in low for m in CONFIG_MARKERS):
            return "NEEDS_CONFIG", "configuration required"
        if any(m in low for m in EMPTY_MARKERS):
            return "EMPTY", f"nothing to report (missing {missing})"
        return "FAIL", f"missing {missing}"

    if len(text.strip()) < 5:
        return "FAIL", "empty output"
    return "PASS", "ok"


async def run_group(session: ClientSession, group: str) -> list[tuple[str, str, str, float, str]]:
    rows = []
    for case in [c for c in CASES if c.group == group]:
        label = case.tool + (f" ({case.note})" if case.note else "")
        if case.skipped:
            rows.append((label, "SKIP", case.skipped, 0.0, ""))
            print(f"SKIP   {label:44} {case.skipped}", flush=True)
            continue

        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                session.call_tool(case.tool, case.args), timeout=case.timeout
            )
            text = flatten(result)
            verdict, why = classify(case, result, text)
        except asyncio.TimeoutError:
            text = ""
            verdict, why = "FAIL", f"timed out after {case.timeout:.0f}s"
        except Exception:
            text = traceback.format_exc(limit=2)
            verdict, why = "FAIL", "raised"
        elapsed = time.monotonic() - started
        rows.append((label, verdict, why, elapsed, text))
        print(f"{verdict:6} {label:44} {elapsed:6.1f}s  {why}", flush=True)
    return rows


async def main() -> None:
    groups = sys.argv[1:] or ["local", "google", "remote", "heavy"]
    all_rows: list[tuple[str, str, str, float, str]] = []

    async with (
        streamablehttp_client(URL) as (read, write, _),
        ClientSession(read, write) as session,
    ):
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"connected to {URL} — {len(tools)} tools registered\n")

            for group in groups:
                print(f"───── {group} " + "─" * 60)
                all_rows.extend(await run_group(session, group))
                print()

    print("=" * 78)
    order = {"PASS": 0, "EMPTY": 1, "NEEDS_CONFIG": 2, "BLOCKED": 3, "SKIP": 4, "FAIL": 5}
    for label, verdict, why, _elapsed, _text in sorted(
        all_rows, key=lambda r: (order.get(r[1], 9), r[0])
    ):
        print(f"{verdict:12} {label:44} {why}")

    counts: dict[str, int] = {}
    for _label, verdict, _why, _elapsed, _text in all_rows:
        counts[verdict] = counts.get(verdict, 0) + 1
    print("\n" + "=" * 78)
    print("  ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: order.get(kv[0], 9))))

    fails = [r for r in all_rows if r[1] == "FAIL"]
    if fails:
        print("\nFAILURES:\n")
        for label, _verdict, why, _elapsed, text in fails:
            print(f"##### {label} — {why}")
            print(text[:700])
            print()

    FIXTURES.joinpath("live_test_report.txt").write_text(
        "\n".join(
            f"───── {label}  [{verdict}] {why}  ({elapsed:.1f}s)\n{text}"
            for label, verdict, why, elapsed, text in all_rows
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(main())
