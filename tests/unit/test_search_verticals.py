"""Regression tests for the search-vertical plumbing.

Two bugs found in production, both of which produced *plausible-looking* wrong
output rather than an error:

* ``browse_google`` routes every ``google.com/search`` URL through the
  search-via-typing flow, which rebuilds the query from scratch and therefore
  drops the ``tbm=`` vertical. ``google_books`` asked for ``tbm=bks`` and got a
  plain web SERP back.
* ``search_via_typing`` never cleared the search box, so a query typed after
  the anti-detect warmup was *prepended* with the warmup query
  (``news todaytaymiyyah``). The SERP still rendered, so nothing failed loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from google_search_mcp import anti_detect, human_sim
from google_search_mcp.anti_detect import (
    is_google_search_url,
    parse_vertical_from_url,
)


# ── parse_vertical_from_url ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.google.com/search?q=x&tbm=bks", "bks"),
        ("https://www.google.com/search?q=x&hl=en&tbm=shop&num=10", "shop"),
        ("https://www.google.com/search?q=x&tbm=nws", "nws"),
        ("https://www.google.com/search?q=x&tbm=isch", "isch"),
        # tbm before q, and a repeated q — order must not matter
        ("https://www.google.com/search?tbm=bks&q=x&q=y", "bks"),
    ],
)
def test_parse_vertical_from_url(url, expected):
    assert parse_vertical_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/search?q=taymiyyah",  # plain web search
        "https://www.google.com/search?q=x&tbm=",  # empty value
        "https://scholar.google.com/scholar?q=x",  # not a /search URL
        "not a url",
    ],
)
def test_parse_vertical_from_url_returns_none_without_a_vertical(url):
    assert parse_vertical_from_url(url) is None


def test_books_url_is_recognised_and_yields_bks():
    """The exact URL ``do_google_books`` builds must survive both helpers."""
    url = "https://www.google.com/search?q=taymiyyah&hl=en&tbm=bks&num=10"
    assert is_google_search_url(url) is True
    assert parse_vertical_from_url(url) == "bks"


# ── search_via_typing: clear before typing, then apply the vertical ────────


class FakeLocator:
    """A search input that models what a browser does with typed characters.

    Typed text is appended to the existing value, which is what happens when the
    caret sits after the current text — the situation that turned ``taymiyyah``
    into ``news todaytaymiyyah``.
    """

    def __init__(self, page, *, prefill: str = ""):
        self.page = page
        self.value = prefill
        self.selected_all = False

    async def count(self) -> int:
        return 1

    async def bounding_box(self, **_kwargs):
        return {"width": 300, "height": 40}

    async def click(self, **_kwargs) -> None:
        self.selected_all = False

    async def evaluate(self, _script) -> None:
        return None

    async def input_value(self) -> str:
        return self.value

    async def fill(self, text) -> None:
        self.value = text
        self.selected_all = False

    async def press_sequentially(self, text, **_kwargs) -> None:
        await self.page.keyboard.type(text)

    async def press(self, key) -> None:
        if key == "Enter":
            await self.page.submit()
        else:
            await self.page.keyboard.press(key)


class FakeKeyboard:
    def __init__(self, locator: FakeLocator):
        self.locator = locator

    async def type(self, text, **_kwargs) -> None:
        self.locator.value += text

    async def press(self, key) -> None:
        if key == "Control+A":
            self.locator.selected_all = True
        elif key in ("Delete", "Backspace"):
            if self.locator.selected_all:
                self.locator.value = ""
                self.locator.selected_all = False
            else:
                self.locator.value = self.locator.value[:-1]


class FakeSuggestions:
    """No autocomplete suggestions — keeps the flow deterministic."""

    def __init__(self):
        self.first = self

    async def count(self) -> int:
        return 0

    async def is_visible(self) -> bool:
        return False


class FakeTypingPage:
    def __init__(self, *, prefill: str = "", start_url: str = "https://www.google.com/"):
        self.input = FakeLocator(self, prefill=prefill)
        self.keyboard = FakeKeyboard(self.input)
        self.url = start_url
        self.submitted_query: str | None = None
        self.first = self.input

    async def goto(self, url, **_kwargs) -> None:
        self.url = url

    async def wait_for_selector(self, *_a, **_k) -> None:
        return None

    async def wait_for_timeout(self, *_a, **_k) -> None:
        return None

    async def wait_for_url(self, *_a, **_k) -> None:
        return None

    async def wait_for_load_state(self, *_a, **_k) -> None:
        return None

    def locator(self, selector):
        if "presentation" in selector:
            return FakeSuggestions()
        return self.input

    async def submit(self) -> None:
        self.submitted_query = self.input.value
        self.url = f"https://www.google.com/search?q={self.input.value}"


@pytest.fixture()
def typing_env(monkeypatch):
    """Neutralise everything in ``search_via_typing`` except the typing itself."""

    async def noop(*_a, **_k):
        return None

    async def find(page, **_kwargs):
        return page.first

    monkeypatch.setattr(anti_detect, "_dismiss_google_overlays", noop)
    monkeypatch.setattr(anti_detect, "_find_google_search_input", find)
    monkeypatch.setattr(human_sim, "human_read", noop)
    monkeypatch.setattr(anti_detect.config, "ANTIDETECT_SEARCH_VIA_TYPING", True)


async def test_typing_replaces_a_prefilled_query(typing_env):
    """Regression: the warmup's query must not be prepended to the real one."""
    page = FakeTypingPage(
        prefill="news today",
        start_url="https://www.google.com/search?q=news+today",
    )

    ok = await anti_detect.search_via_typing(page, "taymiyyah", read_after=False)

    assert ok is True
    assert page.submitted_query == "taymiyyah"
    assert page.submitted_query != "news todaytaymiyyah"


async def test_typing_applies_the_requested_vertical(typing_env):
    page = FakeTypingPage(start_url="https://www.google.com/ncr")

    await anti_detect.search_via_typing(
        page, "taymiyyah", read_after=False, vertical="bks"
    )

    assert "q=taymiyyah" in page.url
    assert "tbm=bks" in page.url


async def test_typing_without_a_vertical_stays_on_the_web_serp(typing_env):
    page = FakeTypingPage(start_url="https://www.google.com/ncr")

    await anti_detect.search_via_typing(page, "taymiyyah", read_after=False)

    assert "tbm=" not in page.url


async def test_typing_starts_from_the_homepage_when_on_a_results_page(typing_env):
    """Typing into a results page's box is what let a stale query survive."""
    page = FakeTypingPage(
        prefill="news today",
        start_url="https://www.google.com/search?q=news+today",
    )

    await anti_detect.search_via_typing(page, "taymiyyah", read_after=False)

    assert page.submitted_query == "taymiyyah"


async def test_typing_forces_the_value_when_typing_was_lost(typing_env):
    """A silently failed keystroke must not leave the old text in the box."""
    page = FakeTypingPage(start_url="https://www.google.com/ncr")

    async def lost(*_a, **_k):
        return None  # keystrokes go nowhere

    page.keyboard.type = lost

    await anti_detect.search_via_typing(page, "taymiyyah", read_after=False)

    assert page.submitted_query == "taymiyyah"


async def test_typing_returns_false_when_the_wrong_query_is_submitted(typing_env):
    """The backstop: browse_google must then navigate straight to the target URL
    instead of letting the caller scrape a SERP for the wrong search."""
    page = FakeTypingPage(start_url="https://www.google.com/ncr")

    async def submit_stale(self=None) -> None:
        page.submitted_query = "news today"
        page.url = "https://www.google.com/search?q=news+today"

    page.submit = submit_stale

    ok = await anti_detect.search_via_typing(page, "taymiyyah", read_after=False)

    assert ok is False


# ── books extractor structure ──────────────────────────────────────────────


def _books_extractor_source() -> str:
    """The JS that ``do_google_books`` evaluates, read straight from the tool."""
    path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "google_search_mcp"
        / "tools"
        / "finance.py"
    )
    src = path.read_text(encoding="utf-8")
    start = src.index("async def do_google_books")
    end = src.index("\n@mcp.tool()", start + 1)
    return src[start:end]


def test_books_extractor_anchors_on_book_links():
    """Regression: it used to iterate every <h3>, so any web SERP looked valid.

    Anchoring on ``books.google.com/books`` links is what makes the tool fail
    closed (no results) instead of returning unrelated web results.
    """
    js = _books_extractor_source()
    assert "books.google.com/books" in js
    assert "inauthor:" in js, "author must come from the inauthor: link"


def test_books_extractor_deduplicates_by_book_id():
    """One book is linked several times (cover, title, Preview)."""
    js = _books_extractor_source()
    assert "seen.has(" in js
    assert "seen.add(" in js
    assert "searchParams.get('id')" in js


def test_books_tool_requests_the_books_vertical():
    assert "tbm=bks" in _books_extractor_source()
