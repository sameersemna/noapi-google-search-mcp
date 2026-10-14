"""Phase 1 regression tests for version single-sourcing (offline).

* **B3** — the package version used to be a hardcoded literal in
  ``__init__.py`` that could drift from ``pyproject.toml``. It is now read
  from installed package metadata; this test guarantees the two stay in sync
  (and still works for an uninstalled source checkout via the fallback).
"""

from __future__ import annotations

import pathlib
import re

from google_search_mcp import __version__

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_version_is_nonempty_string():
    assert isinstance(__version__, str)
    assert __version__.strip()


def test_version_matches_pyproject():
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "no [project] version found in pyproject.toml"
    assert __version__ == match.group(1), (
        f"__version__ ({__version__!r}) drifted from pyproject.toml "
        f"({match.group(1)!r})"
    )
