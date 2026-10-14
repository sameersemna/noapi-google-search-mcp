"""Phase 5 test: every registered tool advertises MCP annotations.

A client (and the model) must be able to tell which tools are read-only,
idempotent, destructive or touch the open world. This asserts the contract on
the real ``tools/list`` output.
"""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp.server import mcp
from google_search_mcp.tools import annotations as ann


@pytest.fixture(scope="module")
def tools_by_name():
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_every_tool_has_annotations(tools_by_name):
    missing = [
        name
        for name, tool in tools_by_name.items()
        if getattr(tool, "annotations", None) is None
    ]
    assert not missing, f"tools without annotations: {missing}"


def test_every_tool_has_a_title(tools_by_name):
    missing = [
        name
        for name, tool in tools_by_name.items()
        if not getattr(tool, "annotations", None) or not tool.annotations.title
    ]
    assert not missing, f"tools without a title: {missing}"


def test_annotation_map_covers_all_tools(tools_by_name):
    unmapped = sorted(set(tools_by_name) - set(ann._SPECS))
    assert not unmapped, f"tools missing from the annotation map: {unmapped}"


@pytest.mark.parametrize(
    "name",
    [
        "google_search", "google_news", "google_scholar", "wikipedia",
        "visit_page", "google_weather", "google_finance",
    ],
)
def test_google_lookups_are_readonly_idempotent_openworld(tools_by_name, name):
    a = tools_by_name[name].annotations
    assert a.readOnlyHint is True
    assert a.idempotentHint is True
    assert a.openWorldHint is True


@pytest.mark.parametrize(
    "name",
    ["list_images", "ocr_image", "read_document", "search_feeds", "get_feed_items"],
)
def test_local_reads_are_readonly_and_not_openworld(tools_by_name, name):
    a = tools_by_name[name].annotations
    assert a.readOnlyHint is True
    assert a.openWorldHint is False


def test_unsubscribe_is_destructive(tools_by_name):
    assert tools_by_name["unsubscribe"].annotations.destructiveHint is True


def test_upload_is_destructive(tools_by_name):
    # Default overwrite=True can clobber an existing object.
    assert tools_by_name["upload_to_s3"].annotations.destructiveHint is True


@pytest.mark.parametrize("name", ["paste_text", "shorten_url", "archive_webpage", "upload_to_s3"])
def test_remote_writes_are_not_readonly(tools_by_name, name):
    a = tools_by_name[name].annotations
    assert a.readOnlyHint is False
    assert a.openWorldHint is True
