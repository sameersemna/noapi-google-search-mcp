"""``visit_page`` truncation limit: configurable, clamped, and per call.

Background: the limit used to be a hardcoded module constant, so the ``env``
block people reasonably reached for did nothing. ``NOAPI_MCP_MAX_TEXT_CHARS``
was even documented as "generic text cap" while no code path read it — the
``tests/unit/test_docs_env_vars.py`` gate could not catch that, because it only
checks that a name *appears* somewhere in the source.

These tests assert behaviour, not presence: the value a client sets is the
value the tool uses.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from google_search_mcp import config
from google_search_mcp.tools import web_utils

# Every name the resolver understands, cleared before each test so a value set
# in the developer's own shell (or a previous test) cannot leak in.
_ENV_NAMES = (
    "NOAPI_MCP_MAX_PAGE_CHARS",
    "MAX_PAGE_CHARS",
    "NOAPI_MCP_MAX_CHARACTERS",
    "max_characters",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield


def _resolved(monkeypatch, **env: str) -> int:
    """Reload ``config`` with *env* set and return the resolved default."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    reloaded = importlib.reload(config)
    try:
        return reloaded.MAX_PAGE_CHARS
    finally:
        for name in env:
            monkeypatch.delenv(name, raising=False)
        importlib.reload(config)


# ── resolution order ───────────────────────────────────────────────────────


def test_default_is_8000_when_nothing_is_set():
    assert config.PAGE_CHARS_DEFAULT == 8000
    assert config.page_chars_default() == 8000


def test_namespaced_variable_is_honoured(monkeypatch):
    assert _resolved(monkeypatch, NOAPI_MCP_MAX_PAGE_CHARS="20000") == 20000


def test_legacy_name_still_works(monkeypatch):
    assert _resolved(monkeypatch, MAX_PAGE_CHARS="12000") == 12000


@pytest.mark.parametrize("name", ["NOAPI_MCP_MAX_CHARACTERS", "max_characters"])
def test_max_characters_alias_is_honoured(monkeypatch, name):
    """``max_characters`` is the spelling people try first."""
    assert _resolved(monkeypatch, **{name: "5000"}) == 5000


def test_namespaced_wins_over_legacy(monkeypatch):
    assert (
        _resolved(
            monkeypatch,
            NOAPI_MCP_MAX_PAGE_CHARS="20000",
            MAX_PAGE_CHARS="1000",
        )
        == 20000
    )


# ── clamping and bad input ─────────────────────────────────────────────────


def test_value_above_ceiling_is_clamped(monkeypatch):
    assert _resolved(monkeypatch, NOAPI_MCP_MAX_PAGE_CHARS="1000000") == config.PAGE_CHARS_CEILING


def test_ceiling_is_200k():
    assert config.PAGE_CHARS_CEILING == 200_000


@pytest.mark.parametrize("raw", ["not-a-number", "", "   ", "0", "-5", "1.5"])
def test_unusable_values_fall_back_to_default(monkeypatch, raw):
    """A typo in a client's env block must not stop the server from starting."""
    assert _resolved(monkeypatch, NOAPI_MCP_MAX_PAGE_CHARS=raw) == 8000


def test_unusable_first_name_falls_through_to_the_next(monkeypatch):
    monkeypatch.setenv("NOAPI_MCP_MAX_PAGE_CHARS", "oops")
    monkeypatch.setenv("MAX_PAGE_CHARS", "9000")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.MAX_PAGE_CHARS == 9000
    finally:
        monkeypatch.delenv("NOAPI_MCP_MAX_PAGE_CHARS", raising=False)
        monkeypatch.delenv("MAX_PAGE_CHARS", raising=False)
        importlib.reload(config)


def test_legacy_max_text_chars_defaults_to_the_page_cap(monkeypatch):
    monkeypatch.setenv("NOAPI_MCP_MAX_PAGE_CHARS", "15000")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.MAX_TEXT_CHARS == 15000
    finally:
        monkeypatch.delenv("NOAPI_MCP_MAX_PAGE_CHARS", raising=False)
        importlib.reload(config)


# ── per-call override ──────────────────────────────────────────────────────


def test_per_call_value_beats_the_configured_default(monkeypatch):
    monkeypatch.setattr(config, "MAX_PAGE_CHARS", 8000)
    assert web_utils._resolve_page_chars(20000) == 20000


def test_per_call_value_is_clamped(monkeypatch):
    monkeypatch.setattr(config, "MAX_PAGE_CHARS", 8000)
    assert web_utils._resolve_page_chars(10_000_000) == config.PAGE_CHARS_CEILING


@pytest.mark.parametrize("bad", [0, -1, "nope", "", None])
def test_unusable_per_call_value_uses_the_default(monkeypatch, bad):
    monkeypatch.setattr(config, "MAX_PAGE_CHARS", 8000)
    assert web_utils._resolve_page_chars(bad) == 8000


def test_per_call_value_accepts_a_numeric_string():
    assert web_utils._resolve_page_chars("1500") == 1500


# ── the tool itself ────────────────────────────────────────────────────────


def test_truncation_marker_reports_the_effective_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_PAGE_CHARS", 8000)
    capped = web_utils._cap_text("x" * 5000, web_utils._resolve_page_chars(100))
    assert capped.startswith("x" * 100)
    assert "100 characters" in capped


def test_visit_page_passes_the_per_call_limit_through(monkeypatch):
    """``visit_page`` must forward the argument, not ignore it."""
    seen: list[int | None] = []

    async def fake_fetch(url, max_characters=None):
        seen.append(max_characters)
        return "ok"

    monkeypatch.setattr(web_utils, "_fetch_page_text", fake_fetch)
    asyncio.run(web_utils.visit_page("https://example.com", max_characters=1234))
    assert seen == [1234]


def test_visit_page_treats_zero_as_no_override(monkeypatch):
    seen: list[int | None] = []

    async def fake_fetch(url, max_characters=None):
        seen.append(max_characters)
        return "ok"

    monkeypatch.setattr(web_utils, "_fetch_page_text", fake_fetch)
    asyncio.run(web_utils.visit_page("https://example.com"))
    assert seen == [None], "0 must mean 'use the configured default', not 'return nothing'"
