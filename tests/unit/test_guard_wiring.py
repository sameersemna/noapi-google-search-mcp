"""Phase 7 integration tests: the SSRF + filesystem guards are actually wired.

These call the real tool functions (not just the guard helpers) to prove the
guards are enforced on the request path.
"""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp.errors import UserInputError
from google_search_mcp.tools import web_utils


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    # Keep these tests independent of any cached result.
    from google_search_mcp.core import cache as cachemod

    monkeypatch.setattr(cachemod.config, "ENABLE_CACHE", False)


def test_visit_page_rejects_file_scheme():
    with pytest.raises(UserInputError) as ei:
        asyncio.run(web_utils._fetch_page_text("file:///etc/passwd"))
    assert "Refusing" in str(ei.value)


def test_visit_page_rejects_cloud_metadata():
    with pytest.raises(UserInputError):
        asyncio.run(web_utils._fetch_page_text("http://169.254.169.254/latest/meta-data/"))


def test_archive_webpage_rejects_metadata_ip():
    with pytest.raises(UserInputError):
        asyncio.run(web_utils.archive_webpage("http://169.254.169.254/"))


def test_read_document_rejects_path_outside_allowed_roots():
    with pytest.raises(UserInputError) as ei:
        asyncio.run(web_utils.read_document("/etc/hostname"))
    assert "allowed roots" in str(ei.value)


def test_transcribe_local_rejects_path_outside_allowed_roots():
    with pytest.raises(UserInputError):
        asyncio.run(web_utils.transcribe_local("/etc/passwd"))


def test_list_images_rejects_path_outside_allowed_roots():
    from google_search_mcp.tools import vision

    with pytest.raises(UserInputError):
        asyncio.run(vision.list_images("/etc"))
