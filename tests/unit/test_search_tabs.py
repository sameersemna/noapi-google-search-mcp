"""Tests for ``google_search_tabs_urls``.

The tool reports the vertical switches Google exposes on a SERP
(``/search?...&udm=<key>`` or ``&tbm=<key>``) and, behind ``include_sections``,
the mixed-in result-type headers (AI Overview, Top stories, Discussions and
forums, Hotels, People also ask, Web results…).

Everything here is offline: the extractor is exercised against a stub page, and
its structure is asserted against the shipped JavaScript.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from google_search_mcp.core import cache as cache_mod
from google_search_mcp.tools import search

SEARCH_PY = Path(__file__).resolve().parents[2] / "src/google_search_mcp/tools/search.py"


def _tool_source() -> str:
    src = SEARCH_PY.read_text(encoding="utf-8")
    start = src.index("async def do_google_search_tabs_urls")
    end = src.index("\n@mcp.tool()", start + 1)
    return src[start:end]


def _extractor_js() -> str:
    match = re.search(r'\.evaluate\(\s*\n\s*r"""(.*?)"""', _tool_source(), re.S)
    assert match, "the tool no longer evaluates an inline script"
    return match.group(1)


# ── label filters ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("label", ["AI Mode", "Images", "News", "Forums", "Web"])
def test_tab_labels_are_accepted(label):
    assert search._tab_label_allowed(label) is True


@pytest.mark.parametrize("label", ["View all", "All images", "All news", "More", ""])
def test_tab_chrome_is_rejected(label):
    """'All images' duplicates 'Images' (both are udm=2)."""
    assert search._tab_label_allowed(label) is False


@pytest.mark.parametrize(
    "label",
    ["Web results", "People also ask", "Top stories", "Discussions and forums"],
)
def test_section_labels_are_accepted(label):
    assert search._section_label_allowed(label) is True


@pytest.mark.parametrize(
    "label",
    [
        "Next",
        "Previous",
        "Customised date range",
        "Choose what you’re giving feedback on",
        "Tools",
        "",
    ],
)
def test_section_chrome_is_rejected(label):
    assert search._section_label_allowed(label) is False


# ── extractor structure ────────────────────────────────────────────────────


def test_extractor_finds_udm_and_tbm_switches():
    js = _extractor_js()
    assert 'a[href*="udm="]' in js
    assert 'a[href*="tbm="]' in js
    # News is still exposed as tbm=nws while the others use udm=.
    assert "searchParams.get('tbm')" in js


def test_extractor_deduplicates_by_vertical():
    """'Images' and 'All images' share udm=2; only one may be reported."""
    js = _extractor_js()
    assert "seenVertical.has(key)" in js
    assert "seenVertical.add(key)" in js


def test_extractor_restricts_tabs_to_search_urls():
    js = _extractor_js()
    assert "u.pathname !== '/search'" in js


def test_extractor_resolves_relative_links_without_a_document_base():
    """The SERP's base is unusable in a detached document; google.com is not."""
    js = _extractor_js()
    assert "https://www.google.com" in js


def test_extractor_skips_result_titles_when_collecting_sections():
    """Result titles are headings too, but they sit in an outbound link."""
    js = _extractor_js()
    assert "el.closest('a')" in js


def test_extractor_detects_modules_without_a_heading():
    js = _extractor_js()
    assert "related-question-pair" in js
    assert "AIOverview" in js


def test_sections_are_only_computed_when_requested():
    js = _extractor_js()
    assert "if (!includeSections) return out;" in js


# ── behaviour through the tool ─────────────────────────────────────────────


class FakePage:
    def __init__(self, payload):
        self.payload = payload
        self.url = "https://www.google.com/search?q=electric+vehicles&hl=en"
        self.requested_sections: list[bool] = []

    async def evaluate(self, _script, include_sections):
        self.requested_sections.append(include_sections)
        return self.payload


@pytest.fixture()
def stub_tool(monkeypatch):
    """Replace the browser and the cache so the tool runs offline and fresh."""
    payload = {
        "tabs": [
            {"label": "AI Mode", "url": "https://www.google.com/search?q=x&udm=50", "vertical": "50"},
            {"label": "Images", "url": "https://www.google.com/search?q=x&udm=2", "vertical": "2"},
            {"label": "View all", "url": "https://www.google.com/search?q=x&udm=vids", "vertical": "vids"},
        ],
        "sections": ["Web results", "People also ask", "Next"],
    }
    page = FakePage(payload)

    @asynccontextmanager
    async def fake_browse(_url, **_kwargs):
        yield page

    async def noop(*_a, **_k):
        return None

    # Bypass the read-through cache: it is keyed by arguments, so a second run
    # of the same test would otherwise reuse the first run's output.
    async def cache_get(_key):
        return None

    async def cache_set(_key, _value, _ttl):
        return None

    async def singleflight(_key, factory):
        return await factory()

    monkeypatch.setattr(search, "browse_google", fake_browse)
    monkeypatch.setattr(search, "wait_for_google_results_ready", noop)
    monkeypatch.setattr(cache_mod.cache, "get", cache_get)
    monkeypatch.setattr(cache_mod.cache, "set", cache_set)
    monkeypatch.setattr(cache_mod.cache, "singleflight", singleflight)
    return page


async def test_default_mode_returns_only_tabs(stub_tool):
    out = await search.do_google_search_tabs_urls("electric vehicles")

    assert "Tabs:" in out
    assert "AI Mode" in out and "Images" in out
    assert "Sections:" not in out
    # Chrome label dropped, and the extractor was told not to compute sections.
    assert "View all" not in out
    assert stub_tool.requested_sections == [False]


async def test_include_sections_adds_result_type_headers(stub_tool):
    out = await search.do_google_search_tabs_urls("electric vehicles", True)

    assert "Sections:" in out
    assert "Web results" in out
    assert "People also ask" in out
    assert "Next" not in out, "page chrome must be filtered out"
    assert stub_tool.requested_sections == [True]


async def test_tabs_are_paired_with_their_urls(stub_tool):
    out = await search.do_google_search_tabs_urls("electric vehicles")

    assert "1. AI Mode" in out
    assert "URL: https://www.google.com/search?q=x&udm=50" in out


async def test_the_serp_url_is_reported(stub_tool):
    out = await search.do_google_search_tabs_urls("electric vehicles")
    assert "SERP: https://www.google.com/search?q=electric+vehicles&hl=en" in out


async def test_blocked_serp_returns_a_message(monkeypatch):
    """browse_google yields None when every attempt is blocked."""

    @asynccontextmanager
    async def blocked(_url, **_kwargs):
        yield None

    monkeypatch.setattr(search, "browse_google", blocked)

    out = await search.do_google_search_tabs_urls("anything")

    assert "blocked by bot detection" in out
