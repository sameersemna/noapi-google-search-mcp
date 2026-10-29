"""Phase 1 regression tests for ``tools/finance.py`` (offline).

* **B11** — the module uses ``re.sub`` but never imports ``re`` → ``NameError``
  at runtime on those code paths. Guard against the missing import.
"""

from __future__ import annotations

import types

from google_search_mcp.tools import finance


def test_finance_module_has_re_imported():
    assert isinstance(getattr(finance, "re", None), types.ModuleType), (
        "tools/finance.py references `re.` but `import re` is missing (B11)"
    )


def test_finance_display_price_rejects_multiline_chart_text():
    assert finance._clean_display_price("345.61") == "345.61"
    assert finance._clean_display_price("9\n8\n7\n345.61") == ""


def test_finance_extracts_price_near_requested_ticker():
    page_text = """AAPL:NASDAQ
addAdd to list
Apple Inc
$344.19
arrow_downward
-0.84%
(-2.91) Today
NVDA
NVIDIA Corp
$214.69"""

    assert finance._extract_contextual_price("AAPL:NASDAQ", page_text) == "$344.19"


def test_finance_display_price_counts_as_financial_data():
    assert finance._has_financial_data({"display_price": "$531.01"})
    assert not finance._has_financial_data({})
