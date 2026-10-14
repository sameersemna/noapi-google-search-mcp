"""Phase 1 regression tests for missing-name bugs in ``tools/web_utils.py``.

* **B12** — ``transcribe_local`` used ``TRANSCRIPT_CACHE_DIR``,
  ``_transcript_cache_path`` and ``_transcribe_audio`` without importing them
  → ``NameError`` on every call.
* **B13** — ``_read_docx_text`` used ``xml.etree.ElementTree`` (``ET``) without
  importing it → ``NameError`` for every ``.docx`` read.
"""

from __future__ import annotations

import asyncio
import zipfile

import pytest

from google_search_mcp.tools import web_utils


def test_transcribe_local_names_are_imported():
    for name in ("TRANSCRIPT_CACHE_DIR", "_transcript_cache_path", "_transcribe_audio"):
        assert hasattr(web_utils, name), f"web_utils is missing {name} (B12)"


def test_et_is_imported():
    assert hasattr(web_utils, "ET"), "web_utils is missing the ET import (B13)"


def test_read_docx_extracts_text(tmp_path):
    docx = tmp_path / "sample.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    out = asyncio.run(web_utils.read_document(str(docx)))
    assert "Hello DOCX" in out, f"docx text not extracted: {out!r}"
    assert "not defined" not in out


def test_transcribe_local_does_not_nameerror_on_missing_deps(tmp_path, monkeypatch):
    # With no real audio, the call must fail *gracefully* (missing dependency or
    # missing whisper), never with a NameError about our own symbols.
    f = tmp_path / "audio.mp3"
    f.write_bytes(b"not real audio")
    out = asyncio.run(web_utils.transcribe_local(str(f)))
    assert "is not defined" not in out, out
    assert "TRANSCRIPT_CACHE_DIR" not in out
