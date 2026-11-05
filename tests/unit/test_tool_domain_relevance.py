"""Regression tests for domain relevance across the Google tools.

A tool must return data from its own vertical, not generic web results. The
failures these guard against were all silent: nothing raised, the output simply
was not what the tool promised.

* ``google_lens`` dumped Google's navigation chrome ("Skip to main content /
  Footer links"), which read as results.
* ``google_trends`` matched every ``[class*="related"]`` widget, so *interest by
  region* was reported as *related queries*.
* ``google_maps_directions`` lost its trip container and fell back to "first
  distance anywhere on the page", reporting 25 mi for a 191 km route.
* ``google_finance`` read the panel before it rendered and then replaced the whole
  result with the /finance/quote fallback, losing the company name and stat rows.

Also asserted: the escaping trap. ``page.evaluate`` scripts live in Python
strings, and the raw/non-raw distinction changes what JavaScript receives — a
single ``\\b`` in a non-raw string becomes a backspace and silently kills the
regex. Both cases appear below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "src" / "google_search_mcp" / "tools"


def _fn_body(module: str, marker: str) -> str:
    src = (TOOLS / module).read_text(encoding="utf-8")
    start = src.index(marker)
    ends = [
        idx
        for idx in (
            src.find("\n@mcp.tool()", start + 1),
            src.find("\n@cached", start + 1),
            src.find("\n# ---", start + 1),
        )
        if idx != -1
    ]
    return src[start : min(ends)] if ends else src[start:]


def _evaluate_block(body: str) -> str:
    """All inline scripts in the function, concatenated.

    A function may hold several (e.g. a readiness poll followed by the real
    extractor), so tests assert against the whole set rather than the first one.
    """
    matches = re.findall(r'page\.evaluate\(\s*\n\s*(r?)"""(.*?)"""', body, re.S)
    assert matches, "no inline evaluate script found"
    return "\n".join(script for _raw, script in matches)


def _is_raw(body: str) -> bool:
    match = re.search(r'page\.evaluate\(\s*\n\s*(r?)"""', body, re.S)
    return bool(match and match.group(1) == "r")



def _code_only(js: str) -> str:
    """Strip JavaScript comments so assertions inspect code, not documentation."""
    without_block = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(
        line.split("//")[0] if "//" in line else line
        for line in without_block.splitlines()
    )

# ── google_lens: never present page chrome as results ──────────────────────


def _lens_body() -> str:
    return _fn_body("vision.py", "async def do_google_lens(")


def test_lens_anchors_on_the_results_container():
    js = _evaluate_block(_lens_body())
    assert "resultsRoot" in js
    assert "'#rso" in js or '"#rso' in js


def test_lens_does_not_fall_back_to_a_whole_body_dump():
    """The chrome dump was the bug: it looked like output but contained nothing."""
    js = _evaluate_block(_lens_body())
    assert "raw_text = main.innerText" not in js
    assert "document.body.innerText.substring(0, 5000)" not in js


def test_lens_reports_an_empty_result_honestly():
    body = _lens_body()
    assert "returned no results" in body
    assert "Open in a browser" in body


def test_lens_counts_real_result_links():
    """Distinguishes 'no results' from 'results present but not parsed'."""
    js = _evaluate_block(_lens_body())
    assert "result_links" in js


# ── google_trends: title-anchored widgets, index-anchored rows ─────────────


def _trends_body() -> str:
    return _fn_body("search.py", "async def do_google_trends(")


def test_trends_anchors_widgets_by_title():
    js = _evaluate_block(_trends_body())
    assert "trends-widget" in js
    assert "related queries" in js.lower()
    assert "interest by region" in js.lower()


def test_trends_no_longer_uses_the_greedy_class_selector():
    """`[class*="related"] .item` matched region widgets and chrome."""
    js = _evaluate_block(_trends_body())
    assert '[class*="related"]' not in js


def test_trends_row_parser_consumes_each_row():
    """A numeric value (region share "53") must not be read as the next index."""
    js = _evaluate_block(_trends_body())
    assert "i += consumed" in js
    assert "consumed = 3" in js


def test_trends_strips_bidi_marks_before_matching_dates():
    js = _evaluate_block(_trends_body())
    assert "u202a" in js, "Google wraps trend dates in bidi isolation marks"


def test_trends_waits_for_populated_widgets():
    body = _trends_body()
    assert "trends-widget" in body, "must poll the widgets, not a fixed sleep"


# ── google_maps_directions: pair distance with duration ───────────────────


def _directions_body() -> str:
    return _fn_body("maps.py", "async def do_google_maps_directions(")


def test_directions_pairs_distance_with_duration():
    js = _evaluate_block(_directions_body())
    assert "pairFor" in js
    assert "DIST_RE" in js and "DUR_RE" in js


def test_directions_has_no_whole_page_distance_fallback():
    """That fallback reported the first number on the page (25 mi for 191 km)."""
    js = _evaluate_block(_directions_body())
    assert "const allText = document.body.innerText;" not in js


def test_directions_regexes_are_escaped_for_the_string_kind():
    """The maps evaluate block is a NON-raw Python string.

    ``\\d`` must therefore be written ``\\\\d``: a lone ``\\b`` in a non-raw
    string is a backspace character, which silently broke the duration regex.
    """
    body = _directions_body()
    assert _is_raw(body) is False
    assert "\\\\d" in body, "must double-escape backslashes in a non-raw string"
    assert re.search(r"(?<!\\)\\b", body) is None, "found a single \\b escape"


# ── google_finance: read the panel that actually exists ───────────────────


def _finance_body() -> str:
    return _fn_body("finance.py", "async def do_google_finance(")


def test_finance_anchors_on_data_attrid():
    js = _evaluate_block(_finance_body())
    assert '[data-attrid="' in js
    assert "attrid('title')" in js


def test_finance_waits_for_the_panel_before_reading_it():
    body = _finance_body()
    assert "wait_for_selector" in body
    assert '[data-attrid="title"], [data-last-price]' in body


def test_finance_merges_the_quote_fallback_instead_of_replacing():
    body = _finance_body()
    assert "panel = dict(data)" in body
    assert "for key, value in panel.items()" in body


@pytest.mark.parametrize(
    "tool_module,body_marker",
    [
        ("search.py", "async def do_google_trends("),
        ("vision.py", "async def do_google_lens("),
    ],
)
def test_raw_blocks_use_single_backslash_escapes(tool_module, body_marker):
    """In a RAW string ``\\\\d`` reaches JavaScript as a literal backslash."""
    body = _fn_body(tool_module, body_marker)
    if _is_raw(body):
        js = _evaluate_block(body)
        assert "\\\\d" not in js, "double-escaped regex inside a raw string"


# ── google_shopping: product cards, not a raw text dump ───────────────────


def _shopping_body() -> str:
    return _fn_body("finance.py", "async def do_google_shopping(")


def test_shopping_abandoned_the_dead_selectors():
    """Every class the old extractor used has disappeared from the SERP.

    Comments are stripped first: the script explains *why* those classes are gone
    and names them, which is documentation rather than a selector.
    """
    js = _code_only(_evaluate_block(_shopping_body()))
    for dead in (".sh-dgr__content", "[data-docid]", ".KZmu8e", ".xcR77"):
        assert dead not in js, f"{dead} no longer exists on the shopping SERP"


def test_shopping_anchors_on_the_price_element_and_its_box():
    """Product boxes are found from the price span, whose class churns."""
    js = _evaluate_block(_shopping_body())
    assert ".UC8ZCe" in js, "known product-box container"
    assert ".lmQWe" in js, "price span anchor"
    assert "boxes.add(" in js


def test_shopping_requires_a_title_before_the_price():
    """Guards against filter chips ('Under €60') becoming products."""
    js = _evaluate_block(_shopping_body())
    assert "priceIdx < 1" in js


def test_shopping_parses_price_list_price_store_and_rating():
    js = _evaluate_block(_shopping_body())
    assert "old_price" in js
    assert "RATING_RE" in js
    assert "store" in js


def test_shopping_deduplicates_products():
    js = _evaluate_block(_shopping_body())
    assert "seen.has(key)" in js


def test_shopping_raw_fallback_is_labelled():
    """An unlabelled dump reads like real output and hid the selector rot."""
    body = _shopping_body()
    assert "Could not extract structured results. Raw page content:" in body
