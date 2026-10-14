"""Phase 1 tests for the Google SERP URL builder (offline, B9).

Documents and locks in the pagination behaviour:

* **Legacy (default)** — ``&start`` strides by ``num_results``; ``&num`` is
  ``num_results + 2``. This is consistent across pages *only if* ``num_results``
  is unchanged between calls.
* **Opt-in fixed stride** — ``NOAPI_MCP_SEARCH_PAGE_SIZE`` makes ``&start`` use
  a fixed stride so changing ``num_results`` between pages cannot skip/overlap.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from google_search_mcp.tools import search


def _q(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


@pytest.fixture(autouse=True)
def _no_fixed_stride(monkeypatch):
    monkeypatch.delenv("NOAPI_MCP_SEARCH_PAGE_SIZE", raising=False)


def test_page_one_url_shape_legacy():
    url, limit = search._build_google_search_url("hello world", 5)
    assert url.startswith("https://www.google.com/search?")
    q = _q(url)
    assert q["q"] == ["hello world"]
    assert q["num"] == ["7"]  # num_results + 2
    assert q["hl"] == ["en"]
    assert "start" not in q  # page 1 has no start offset
    assert limit == 5


def test_legacy_stride_scales_with_num_results():
    url, _ = search._build_google_search_url("x", 10, page=3)
    assert _q(url)["start"] == ["20"]  # (3-1)*10


def test_filters_are_appended():
    url, _ = search._build_google_search_url(
        "python", 5, time_range="past_week", site="example.com",
        language="de", region="de",
    )
    q = _q(url)
    assert q["q"] == ["site:example.com python"]
    assert q["lr"] == ["lang_de"] and q["hl"] == ["de"]
    assert q["gl"] == ["de"]
    assert q["tbs"] == [search.TIME_RANGE_MAP["past_week"]]


def test_invalid_time_range_is_ignored():
    url, _ = search._build_google_search_url("x", 5, time_range="not_a_range")
    assert "tbs" not in _q(url)


def test_fixed_page_stride_env_opt_in(monkeypatch):
    monkeypatch.setenv("NOAPI_MCP_SEARCH_PAGE_SIZE", "10")
    url, limit = search._build_google_search_url("x", 5, page=2)
    # stride is now fixed at 10 regardless of num_results
    assert _q(url)["start"] == ["10"]
    assert _q(url)["num"] == ["12"]  # 10 + 2
    assert limit == 10


def test_fixed_stride_consistent_across_changing_num_results(monkeypatch):
    monkeypatch.setenv("NOAPI_MCP_SEARCH_PAGE_SIZE", "10")
    _, _ = search._build_google_search_url("x", 5, page=1)
    url2, _ = search._build_google_search_url("x", 5, page=2)
    url2b, _ = search._build_google_search_url("x", 8, page=2)
    # page 2 offset must not depend on num_results when a fixed stride is set
    assert _q(url2)["start"] == _q(url2b)["start"] == ["10"]
