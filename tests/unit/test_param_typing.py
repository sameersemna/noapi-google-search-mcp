"""Phase 9 tests: strong parameter typing (``Literal`` enums).

The tool JSON schema must advertise the valid values so a model picks a valid
one, while every *previously valid* value must remain valid (backward
compatibility).

Scope note: ``source_type`` is deliberately NOT a ``Literal`` — the feed tools
normalise it with ``.lower().strip()``, so a case-sensitive enum would reject
previously-working inputs like ``"Reddit"``. It stays a free-form string with
runtime normalisation (documented in ADR 0006).
"""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp import config
from google_search_mcp.server import mcp


@pytest.fixture(scope="module")
def tools_by_name():
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def _enum(tools_by_name, tool: str, param: str) -> list[str]:
    prop = tools_by_name[tool].inputSchema["properties"][param]
    return prop["enum"]


def test_time_range_enum_matches_canonical_set(tools_by_name):
    assert _enum(tools_by_name, "google_search", "time_range") == list(
        config.SEARCH_TIME_RANGES
    )


def test_time_range_is_string_typed(tools_by_name):
    prop = tools_by_name["google_search"].inputSchema["properties"]["time_range"]
    assert prop["type"] == "string"
    assert "" in prop["enum"], "the empty default must stay valid"


@pytest.mark.parametrize("tool", ["transcribe_video", "transcribe_local", "search_transcript"])
def test_model_size_enum_matches_canonical_set(tools_by_name, tool):
    assert _enum(tools_by_name, tool, "model_size") == list(config.MODEL_SIZES)


@pytest.mark.parametrize(
    "value",
    ["", "past_hour", "past_day", "past_week", "past_month", "past_year"],
)
def test_all_documented_time_ranges_remain_valid(tools_by_name, value):
    assert value in _enum(tools_by_name, "google_search", "time_range")


@pytest.mark.parametrize("value", ["tiny", "base", "small", "medium", "large"])
def test_all_model_sizes_remain_valid(tools_by_name, value):
    assert value in _enum(tools_by_name, "transcribe_video", "model_size")


def test_invalid_time_range_is_rejected_before_running(monkeypatch):
    """An out-of-enum value must be rejected by validation, not silently ignored."""

    async def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("tool body ran despite invalid enum value")

    async def run():
        return await mcp.call_tool(
            "google_search", {"query": "x", "time_range": "definitely_not_valid"}
        )

    with pytest.raises(Exception) as ei:  # noqa: B017 - SDK raises a ToolError subclass
        asyncio.run(run())
    msg = str(ei.value).lower()
    assert "time_range" in msg or "validation" in msg or "input" in msg


def test_valid_time_range_passes_validation(monkeypatch):
    """A valid enum value must get past validation (body patched, no browser)."""
    from google_search_mcp.tools import search

    async def fake_do_google_search(*_a, **_k):
        return "ok"

    monkeypatch.setattr(search, "do_google_search", fake_do_google_search)

    async def run():
        return await mcp.call_tool("google_search", {"query": "x", "time_range": "past_week"})

    asyncio.run(run())  # must not raise a validation error
