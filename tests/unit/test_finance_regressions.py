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
