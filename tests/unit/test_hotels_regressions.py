"""Regression tests for ``google_hotels``.

The tool used to search ``google.com/search?q=hotels+<query>`` — a generic web
SERP — and parse ``div[data-ved] a[href*="hotel"]``. Two consequences:

* when Google did not render a hotel pack there were no listings at all;
* the SERP's tab links (Images / News / Forums) *matched*, because their query
  string is the hotel query, so they were returned as "hotels" with nothing but a
  URL. That is the reported symptom.

It now targets ``google.com/travel/search`` and parses the travel result cards.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from google_search_mcp.tools import travel

REPO = Path(__file__).resolve().parents[2]
TRAVEL = REPO / "src" / "google_search_mcp" / "tools" / "travel.py"


def _hotels_source() -> str:
    """The body of ``do_google_hotels`` (URL construction + extractor)."""
    src = TRAVEL.read_text(encoding="utf-8")
    start = src.index("async def do_google_hotels")
    end = src.index("\n@mcp.tool()", start + 1)
    return src[start:end]


def _extractor_js() -> str:
    match = re.search(
        r'page\.evaluate\(\s*\n\s*r"""(.*?)"""', _hotels_source(), re.S
    )
    assert match, "do_google_hotels no longer evaluates an inline script"
    return match.group(1)


# ── navigation-label filter ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label",
    ["Sign in", "AI Mode", "View all hotels", "Images", "News", "Forums", "Maps"],
)
def test_hotel_result_filter_rejects_google_navigation_labels(label):
    assert travel._is_hotel_listing_name(label) is False


@pytest.mark.parametrize(
    "name",
    ["Lord Elgin Hotel", "MEININGER Hotel Berlin East Side Gallery", "Hotel Klassik"],
)
def test_hotel_result_filter_keeps_real_hotel_names(name):
    assert travel._is_hotel_listing_name(name) is True


def test_hotel_result_filter_rejects_empty_names():
    assert travel._is_hotel_listing_name("") is False
    assert travel._is_hotel_listing_name("   ") is False


# ── the tool targets Google Travel, not the web SERP ───────────────────────


def test_hotels_uses_the_google_travel_search_endpoint():
    """Regression: it used www.google.com/search, i.e. the web SERP."""
    source = _hotels_source()
    assert "https://www.google.com/travel/search?q=" in source
    assert "www.google.com/search?q=" not in source


def test_hotels_does_not_duplicate_the_word_hotels_in_the_query():
    """It used to prefix the query with 'hotels ', producing
    'hotels Hotels in Berlin under $200 per night' on the wire."""
    source = _hotels_source()
    assert 'quote_plus(f"hotels {query}")' not in source
    assert "quote_plus(query)" in source


def test_hotels_does_not_claim_a_currency_parameter():
    """Google accepts ``curr=`` but ignores it (verified against the live page),
    so the tool must not pretend prices are converted."""
    source = _hotels_source()
    assert "&curr=" not in source


# ── extractor structure ────────────────────────────────────────────────────


def test_extractor_targets_travel_hotel_cards():
    js = _extractor_js()
    assert ".uaTTDe" in js, "hotel card container"
    assert ".PVOOXe" in js, "hotel name / detail link"
    assert "h2" in js


def test_extractor_reads_price_rating_and_reviews_from_the_right_nodes():
    js = _extractor_js()
    # Price must not come from a bare .kixHKb lookup: the rating row reuses that
    # class, which would put "4.0 (1.0K)" in the price field.
    assert ".kixHKb.flySGb" in js
    assert ".KFi5wf" in js, "rating"
    assert ".jdzyld" in js, "review count"
    assert ".DPNIGe" in js, "stay dates"


def test_extractor_normalises_the_price_token():
    """The price node can hold '€49 total1 night with taxes + fees'."""
    js = _extractor_js()
    assert "m[0]" in js, "the matched currency token is what gets used"


def test_extractor_deduplicates_hotels():
    """The name node and the link node both match the card selector."""
    js = _extractor_js()
    assert "seen.has(" in js
    assert "seen.add(" in js


def test_extractor_does_not_pick_the_account_menu_as_the_list_link():
    """Regression: the account link is accounts.google.com/...?continue=<travel
    search>, which matched a plain ``a[href*="/travel/search"]`` lookup."""
    js = _extractor_js()
    assert "accounts\\.google\\.com" in js or "accounts.google.com" in js


def test_hotels_keeps_the_bot_detection_guard():
    source = _hotels_source()
    assert "is_blocked(page)" in source
    assert "Google Hotels blocked by bot detection" in source
