"""Domain-relevance audit: does each tool return its own vertical's data?

The failure this catches (see ``google_books`` / ``google_hotels``): a tool that
targets the generic web SERP, or whose selector is loose enough to match page
navigation, silently returns *general search results* under a domain-specific
name. Nothing raises; the output simply is not what the tool promises.

Each case is called for real, then checked against markers that only exist in
that domain (e.g. news yields a source and a relative time, books yields an
author and a ``books.google.com/books`` URL). A tool that falls back to "raw page
content" fails: that is the tool admitting it matched nothing.

    python scripts/audit_tools.py            # everything
    python scripts/audit_tools.py news books  # a subset
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import traceback
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _probe_common import save_text

from google_search_mcp.core.circuit import CircuitOpenError
from google_search_mcp.tools import finance, maps, search, travel, vision, web_utils

# Signals that a tool failed to extract rather than returning domain data.
FAILURE_MARKERS = (
    "raw page content",
    "could not extract",
    "could not identify",
    "blocked by bot detection",
    "no results found",
    "no book results",
    "no hotel data",
    "lookup failed",
    "please try again",
    "rate-limiting",
    "returned no results",
    "failed:",
    "file not found",
    "no image at the url",
)

# Google's own navigation text. A result that is mostly these words means the
# extractor matched nothing and the tool dumped the page chrome -- the failure
# mode ``google_lens`` had, which no FAILURE_MARKER caught because it appends the
# raw text without a label.
CHROME_WORDS = (
    "skip to main content",
    "accessibility help",
    "footer links",
    "send feedback",
    "privacy",
    "terms",
    "try without personalisation",
    "from your ip address",
)

IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/3/3a/Cat03.jpg"


def _local_image() -> str:
    """Download the sample image once and return its local path."""
    path = Path(tempfile.gettempdir()) / "audit_sample.jpg"
    if not path.exists():
        # Wikimedia rejects the default urllib agent with 403.
        req = urllib.request.Request(IMAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed URL
            path.write_bytes(resp.read())
    return str(path)


def flat(result: Any) -> str:
    """Render a tool result (str | list of str/Image) as plain text."""
    if isinstance(result, str):
        return result
    parts = []
    for item in result or []:
        if isinstance(item, str):
            parts.append(item)
        else:
            parts.append(f"[image {len(getattr(item, 'data', b''))} bytes]")
    return "\n".join(parts)


@dataclass
class Case:
    name: str
    run: Callable[[], Any]
    must_contain: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = FAILURE_MARKERS
    note: str = ""
    #: Set when the upstream surface is currently unavailable for reasons
    #: outside this codebase. The case is reported as KNOWN instead of PASS or
    #: FAIL, so a silent regression cannot hide behind it, and an upstream
    #: recovery shows up as the case no longer being "known".
    known: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# Every case asserts a marker that only exists in its own vertical. An empty
# ``must_contain`` would let a tool pass on generic output, which is the whole
# failure mode this script exists to catch.
CASES: list[Case] = [
    Case("google_search", lambda: search.do_google_search("python", 3),
         must_contain=("http",)),
    Case("google_news", lambda: search.do_google_news("electric vehicles", 3),
         must_contain=("Source:", "URL:")),
    Case("google_scholar", lambda: search.do_google_scholar("transformer attention", 3),
         must_contain=("URL:",)),
    Case("google_images", lambda: search.google_images("red panda", 3),
         must_contain=("Results for",)),
    Case("google_trends", lambda: search.do_google_trends("electric vehicles"),
         must_contain=("Google Trends for:",)),
    Case("google_maps", lambda: maps.do_google_maps("coffee shops in Berlin", 3),
         must_contain=("Link:",)),
    # A route needs *both* halves: the old bug returned a distance borrowed from
    # an unrelated element, so a lone distance is not evidence of a route.
    Case("google_maps_directions",
         lambda: maps.do_google_maps_directions("Berlin", "Dresden"),
         must_contain=("Distance:", "Duration:")),
    Case("google_finance", lambda: finance.do_google_finance("MSFT:NASDAQ"),
         must_contain=("Price:",)),
    Case("google_weather", lambda: finance.do_google_weather("Berlin"),
         must_contain=("°",)),
    Case("google_shopping", lambda: finance.do_google_shopping("mechanical keyboard", 3),
         must_contain=("Price:",)),
    Case("google_books", lambda: finance.do_google_books("taymiyyah", 3),
         must_contain=("Author:", "books.google")),
    Case("google_translate",
         lambda: travel.do_google_translate("Good morning, how are you?", "de"),
         must_contain=("Translation (",)),
    Case("google_flights", lambda: travel.do_google_flights("Berlin", "Paris"),
         must_contain=("Google Flights:",)),
    Case("google_hotels",
         lambda: travel.do_google_hotels("Hotels in Berlin under $200 per night", 3),
         must_contain=("Rating:", "Price:")),
    Case("google_lens", lambda: vision.do_google_lens(IMAGE_URL),
         must_contain=("Google Lens Results",)),
    # google_lens_detect is a local object detector: it takes a file path, not a
    # URL, so the image is fetched to disk first.
    Case("google_lens_detect", lambda: vision.do_google_lens_detect(_local_image()),
         must_contain=("Objects detected:",)),
    Case("wikipedia", lambda: web_utils.wikipedia("Python", "en", 3),
         must_contain=("Python",)),
    Case("visit_page", lambda: web_utils.visit_page("https://example.com"),
         must_contain=("Content from",)),
]


def judge(case: Case, text: str) -> tuple[bool | None, str]:
    """Return ``(ok, why)``; ``ok is None`` means a known upstream limitation."""
    low = text.casefold()
    for marker in case.must_not_contain:
        if marker.casefold() in low:
            if case.known:
                return None, f"known: {case.known}"
            return False, f"failure marker: {marker!r}"
    for marker in case.must_contain:
        if marker and marker.casefold() not in low:
            if case.known:
                return None, f"known: {case.known}"
            return False, f"missing {marker!r}"
    if len(text.strip()) < 20:
        return False, "output too short"
    # Page-chrome dump: the extractor matched nothing and echoed the nav text.
    hits = sum(1 for word in CHROME_WORDS if word in low)
    if hits >= 3:
        return False, f"page-chrome dump ({hits} chrome markers)"
    if case.known:
        # The surface started working — drop the exception, not the check.
        return True, "ok (was known-unavailable; remove the `known` field)"
    return True, "ok"


def _label(ok: bool | None, why: str) -> str:
    """Report a case as PASS / FAIL / KNOWN / SKIP.

    KNOWN (the upstream surface is unavailable) is kept distinct from SKIP (the
    circuit breaker was open, so the tool was never really exercised): conflating
    them would let a broken tool look like an unavailable one.
    """
    if ok is None:
        return "KNOWN" if why.startswith("known") else "SKIP"
    return "PASS" if ok else "FAIL"


async def main() -> None:
    wanted = set(sys.argv[1:])
    cases = [c for c in CASES if not wanted or c.name in wanted or c.name.replace("google_", "") in wanted]

    results: list[tuple[str, bool | None, str, str]] = []
    for case in cases:
        started = time.monotonic()
        ok: bool | None
        try:
            text = flat(await case.run())
            ok, why = judge(case, text)
        except CircuitOpenError as exc:
            # Not a tool defect: the breaker is open, so every Google call fails
            # fast. Re-run these after the cooldown.
            text = str(exc)
            ok, why = None, "skipped: circuit open"
        except Exception:
            text = traceback.format_exc(limit=3)
            ok, why = False, "raised"
        elapsed = time.monotonic() - started
        results.append((case.name, ok, why, text))
        print(f"{_label(ok, why):5}  {case.name:26} {elapsed:5.1f}s  {why}", flush=True)

    print("\n" + "=" * 78)
    for name, ok, why, text in results:
        print(f"\n───── {name}  [{_label(ok, why)}] {why}")
        print(text[:600])

    passed = sum(1 for _, ok, _, _ in results if ok)
    known = sum(1 for _, ok, why, _ in results if ok is None and why.startswith("known"))
    skipped = sum(1 for _, ok, why, _ in results if ok is None and not why.startswith("known"))
    print("\n" + "=" * 78)
    summary = f"{passed}/{len(results) - skipped - known} tools return domain-relevant output"
    if known:
        summary += f" ({known} known-unavailable)"
    if skipped:
        summary += f" ({skipped} skipped: circuit open)"
    print(summary)

    # Written from a plain helper: doing file I/O inside a coroutine trips
    # ruff's ASYNC230 and is a needless block on the event loop.
    save_text(
        "audit_tools.txt",
        "".join(
            f"───── {name}  [{_label(ok, why)}] {why}\n{text}\n\n"
            for name, ok, why, text in results
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
