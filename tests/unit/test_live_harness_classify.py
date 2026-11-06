"""The live-test harness must not mistake content for a failure.

Why this exists
---------------
``scripts/live_test_all_tools.py`` is the harness used to claim "all tools are
working", so a false positive there is worse than a missing check: it sends
someone hunting a bug that does not exist.

It matched a bare-word marker list against the entire tool reply, and
``get_feed_items`` was scored FAIL because a BBC headline read *"'Several
victims' after bus crashes in France"*. Nothing was broken — a news payload
simply contains the word.

A real server crash never reaches the marker check: ``run_group`` wraps the call
and reports ``FAIL "raised"`` on an exception and ``FAIL "timed out"`` on a
timeout. So ambiguous English words earn nothing and cost accuracy.

The rule now: unambiguous phrases match anywhere; short tool-side failures
("Google Maps search failed.") match only the opening line.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live_test_all_tools.py"


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location("live_test_harness", _SCRIPT)
    assert spec and spec.loader, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_test_harness"] = module
    spec.loader.exec_module(module)
    return module


def _case(harness, expect=()):
    return harness.Case(tool="t", args={}, group="local", expect=expect)


def _ok():
    return SimpleNamespace(isError=False)


def _err():
    return SimpleNamespace(isError=True)


# ── the regression ─────────────────────────────────────────────────────────


def test_news_headline_containing_crash_is_not_a_failure(harness):
    """The exact payload that produced the false FAIL."""
    text = (
        "Recent Feed Items (3 items)\n\n"
        "1. [news/BBC] 'Several victims' after bus crashes in France, officials say\n"
        "   Published: 2026-11-06T09:57:11+00:00\n"
        "   URL: https://www.bbc.co.uk/news/articles/cmly00l4p3lwo\n"
        "   The local mayor says the bus \"left the road and hit a tree\".\n\n"
        "2. [news/NPR] Morning news brief\n"
        "   Canceled talks resume after the failed motion.\n"
    )
    verdict, why = harness.classify(_case(harness), _ok(), text)
    assert verdict == "PASS", f"content mistaken for a failure: {why}"


@pytest.mark.parametrize(
    "word",
    ["crashes", "cancelled", "failed", "crashing", "error"],
)
def test_ambiguous_words_in_long_content_do_not_fail(harness, word):
    text = "Recent Feed Items (2 items)\n\n1. [news/BBC] Something about " + word + "\n" + "x" * 400
    verdict, why = harness.classify(_case(harness), _ok(), text)
    assert verdict == "PASS", f"{word!r} in content scored {verdict}: {why}"


# ── real failures must still fail ──────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Google Maps search failed. Please try again.",
        "Google Trends lookup failed: timeout",
        "Failed to fetch https://x: connection refused",
        "Could not extract text content from: https://x",
        "Traceback (most recent call last): ...",
        "An unhandled exception occurred",
        "raw page content: skip to main content ...",
    ],
)
def test_genuine_tool_failures_still_fail(harness, text):
    verdict, _why = harness.classify(_case(harness), _ok(), text)
    assert verdict == "FAIL", f"real failure scored {verdict}: {text[:50]}"


def test_is_error_with_clean_config_message_is_needs_config(harness):
    verdict, _why = harness.classify(
        _case(harness), _err(), "Missing credentials. Set AWS_ACCESS_KEY_ID."
    )
    assert verdict == "NEEDS_CONFIG"


def test_is_error_without_explanation_fails(harness):
    verdict, _why = harness.classify(_case(harness), _err(), "boom")
    assert verdict == "FAIL"


def test_opening_line_failure_is_caught_even_in_long_output(harness):
    """A failure message followed by a long dump is still a failure."""
    text = "Transcription failed. Please try again.\n" + "x" * 5000
    verdict, _why = harness.classify(_case(harness), _ok(), text)
    assert verdict == "FAIL"


def test_marker_lists_have_no_bare_ambiguous_words(harness):
    """Guard the class of bug, not just the instance."""
    for marker in harness.FAILURE_MARKERS + harness.OPENING_FAILURE_MARKERS:
        assert marker not in {"crash", "crashed", "cancel", "cancelled", "error", "fail"}, (
            f"{marker!r} is ordinary English and will match real content"
        )


def test_youtube_auth_wall_is_needs_config_not_pass_or_fail(harness):
    """A cookie rejection is a credential problem, not a defect and not a pass.

    The message contains no failure marker, so before this rule it fell through
    to the expectation check and was scored PASS — the worst outcome, because the
    tool had plainly refused to work.
    """
    text = (
        "YouTube rejected the request as automated even though a cookie file is "
        "present at ./cookies/youtube_cookies.txt — the signed-in session behind "
        "it is no longer valid. Refresh the cookies by opening YouTube logged in: "
        "noapi-google-search-mcp --manual-browser https://www.youtube.com"
    )
    verdict, why = harness.classify(_case(harness), _ok(), text)
    assert verdict == "NEEDS_CONFIG", f"auth wall scored {verdict}: {why}"
