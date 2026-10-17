"""Backward-compatibility tripwire for the MCP tool registry.

This is the single most important guard in the hardening project: it asserts
that the server still exposes **exactly** the same tool names and parameter
names as the pre-hardening baseline (v0.3.6 / commit ``cc37fd3``).

Rules for maintaining this file (see PROGRESS.md / task prime directives):

* Tool **names** must never be removed or renamed.
* Parameter **names** may only be *added* (new optional params). Removing or
  renaming a parameter is a breaking change and must show up as a diff here.
* New tools may be added — but then this snapshot must be updated on purpose
  and the addition recorded in CHANGELOG.md.

If this test fails, do NOT weaken it. Either fix the regression or, for an
intentional additive change, update ``EXPECTED`` deliberately.
"""

from __future__ import annotations

import pytest

from google_search_mcp.server import mcp

# name -> (sorted param names, sorted required param names)
EXPECTED: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "archive_webpage": (("idempotency_key", "url"), ("url",)),
    "check_cookies": ((), ()),
    "check_feeds": (("source_type",), ()),
    "convert_media": (
        ("input_path", "output_format", "output_path", "quality"),
        ("input_path", "output_format"),
    ),
    "extract_video_clip": (
        ("buffer_seconds", "end_seconds", "output_filename", "start_seconds", "url"),
        ("end_seconds", "start_seconds", "url"),
    ),
    "fetch_emails": (
        ("email_address", "folder", "imap_server", "limit", "password", "search"),
        (),
    ),
    "generate_qr": (("data", "output_path", "size"), ("data",)),
    "get_feed_items": (("limit", "source", "source_type"), ()),
    "google_books": (("num_results", "query"), ("query",)),
    "google_finance": (("query",), ("query",)),
    "google_flights": (
        ("date", "destination", "origin", "return_date"),
        ("destination", "origin"),
    ),
    "google_hotels": (("num_results", "query"), ("query",)),
    "google_images": (("num_results", "query"), ("query",)),
    "google_lens": (("image_source",), ("image_source",)),
    "google_lens_detect": (("image_source",), ("image_source",)),
    "google_maps": (("num_results", "query"), ("query",)),
    "google_maps_directions": (
        ("destination", "mode", "origin"),
        ("destination", "origin"),
    ),
    "google_news": (("num_results", "query"), ("query",)),
    "google_scholar": (("num_results", "query"), ("query",)),
    "google_search": (
        (
            "language",
            "num_results",
            "page",
            "query",
            "region",
            "site",
            "time_range",
        ),
        ("query",),
    ),
    "google_shopping": (("num_results", "query"), ("query",)),
    "google_translate": (
        ("from_language", "text", "to_language"),
        ("text", "to_language"),
    ),
    "google_trends": (("query",), ("query",)),
    "google_weather": (("location",), ("location",)),
    "list_images": (("directory",), ()),
    "list_subscriptions": ((), ()),
    "list_subtitles": (("url",), ("url",)),
    "manual_intervention_status": ((), ()),
    "ocr_image": (("image_source",), ("image_source",)),
    "open_manual_browser": (("reason", "timeout_sec", "url"), ()),
    "paste_text": (
        ("content", "expiry_days", "idempotency_key", "syntax", "title"),
        ("content",),
    ),
    "read_document": (("file_path",), ("file_path",)),
    "search_feeds": (
        ("limit", "query", "since", "source_type", "until"),
        ("query",),
    ),
    "search_transcript": (
        ("context_segments", "model_size", "query", "url"),
        ("query", "url"),
    ),
    "shorten_url": (("idempotency_key", "url"), ("url",)),
    "subscribe": (("identifier", "name", "source_type"), ("identifier", "source_type")),
    "transcribe_local": (("file_path", "language", "model_size"), ("file_path",)),
    "transcribe_video": (
        ("language", "model_size", "prefer_subtitles", "url"),
        ("url",),
    ),
    "unsubscribe": (("identifier", "source_type"), ("identifier", "source_type")),
    "upload_to_s3": (
        (
            "access_key",
            "bucket",
            "endpoint",
            "file_path",
            "idempotency_key",
            "key",
            "overwrite",
            "secret_key",
        ),
        ("bucket", "file_path"),
    ),
    "visit_page": (("url",), ("url",)),
    # `fresh` is an additive cache-bypass param (Phase 5).
    "wikipedia": (("fresh", "language", "query", "sentences"), ("query",)),
}

EXPECTED_TOOL_COUNT = 42


@pytest.fixture(scope="module")
def registry() -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    out: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for tool in tools:
        schema = tool.inputSchema or {}
        params = tuple(sorted((schema.get("properties") or {}).keys()))
        required = tuple(sorted(schema.get("required") or []))
        out[tool.name] = (params, required)
    return out


def test_exactly_42_tools_registered(registry):
    assert len(registry) == EXPECTED_TOOL_COUNT, (
        f"tool count changed: expected {EXPECTED_TOOL_COUNT}, got {len(registry)}. "
        f"New: {sorted(set(registry) - set(EXPECTED))}  "
        f"Missing: {sorted(set(EXPECTED) - set(registry))}"
    )


def test_no_tools_removed_or_renamed(registry):
    missing = sorted(set(EXPECTED) - set(registry))
    assert not missing, f"tools were removed/renamed (breaking): {missing}"


def test_parameter_names_match_snapshot(registry):
    """Every existing tool keeps its exact parameter-name set (no removals)."""
    problems = []
    for name, (exp_params, _exp_required) in EXPECTED.items():
        if name not in registry:
            continue  # reported by test_no_tools_removed_or_renamed
        actual_params, actual_required = registry[name]
        removed = sorted(set(exp_params) - set(actual_params))
        added = sorted(set(actual_params) - set(exp_params))
        if removed:
            problems.append(f"{name}: removed params {removed}")
        # `added` params are allowed (additive, backward compatible) but we
        # still surface them so an intentional change gets reviewed/updated.
        if added:
            problems.append(
                f"{name}: NEW params {added} (additive — update EXPECTED + CHANGELOG)"
            )
        removed_required = sorted(set(_exp_required) - set(actual_required))
        added_required = sorted(set(actual_required) - set(_exp_required))
        if removed_required:
            problems.append(f"{name}: required params relaxed {removed_required}")
        if added_required:
            problems.append(f"{name}: NEW required params {added_required} (BREAKING)")
    assert not problems, "registry drift:\n  " + "\n  ".join(problems)


def test_all_tools_have_string_description(registry):
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    bad = [t.name for t in tools if not isinstance(getattr(t, "description", ""), str)]
    assert not bad, f"tools with non-string description: {bad}"
