"""OCR is an opt-in extra: every entry point degrades with a hint, never a crash.

Why this exists: ``rapidocr-onnxruntime`` declares ``Requires-Python <3.13``
upstream (no 3.13 wheel), so keeping it in the core dependencies made
``pip install noapi-google-search-mcp`` unsatisfiable on Python 3.13. It now
lives in the ``[ocr]`` extra. The install must work without it, and the two
tools that need it (``ocr_image``, and the scanned-PDF fallback in
``read_document``) must say so instead of raising or silently returning nothing.

The scanned-PDF case is the subtle one: the import used to sit inside a bare
``except Exception: pass``, so with no OCR installed a scanned PDF looked
exactly like a blank document.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from google_search_mcp import config
from google_search_mcp.tools import vision, web_utils


def _pyproject_text() -> str:
    import pathlib

    return (pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
        encoding="utf-8"
    )


def _core_dependency_block(pyproject: str) -> str:
    """Return only the core ``dependencies = [...]`` list, comments removed.

    Comments are stripped because they legitimately *mention* the excluded
    packages ("OCR is NOT here"); matching raw text would flag the explanation
    rather than a real dependency.
    """
    block = pyproject.split("[project.optional-dependencies]", 1)[0]
    block = block.split("dependencies = [", 1)[1]
    kept = []
    for line in block.splitlines():
        code = line.split("#", 1)[0]
        if code.strip():
            kept.append(code)
        if code.strip() == "]":
            break
    return "\n".join(kept)


def test_hint_names_the_extra():
    assert "[ocr]" in config.OCR_INSTALL_HINT
    assert "optional" in config.OCR_INSTALL_HINT.lower()


def test_ocr_image_returns_the_hint_when_rapidocr_is_missing(monkeypatch, tmp_path):
    img = tmp_path / "image.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(web_utils, "_ocr_available", lambda: False, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "rapidocr_onnxruntime", None)

    out = asyncio.run(vision.ocr_image(str(img)))
    assert out == config.OCR_INSTALL_HINT


def test_ocr_available_uses_find_spec_not_an_import():
    """Checks the capability without paying the onnxruntime import cost."""
    import importlib.util
    import inspect

    src = inspect.getsource(web_utils._ocr_available)
    assert "find_spec" in src
    assert importlib.util.find_spec  # documented dependency of the check


def test_read_pdf_text_skips_ocr_when_the_extra_is_absent(monkeypatch):
    """No OCR installed -> no pdftoppm/RapidOCR work, and no exception."""
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        if cmd[0] == "pdftotext":
            # Simulate a scanned PDF: pdftotext succeeds but yields nothing.
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(web_utils.subprocess, "run", fake_run)
    monkeypatch.setattr(web_utils, "_ocr_available", lambda: False)

    assert web_utils._read_pdf_text("scanned.pdf") == ""
    assert [c[0] for c in calls] == ["pdftotext"], (
        "pdftoppm must not run when the OCR extra is missing"
    )


def test_read_document_pdf_hint_names_the_extra(monkeypatch, tmp_path):
    pdf = tmp_path / "scanned.pdf"
    pdf.write_bytes(b"%PDF-1.4 not really")

    monkeypatch.setattr(web_utils, "_ocr_available", lambda: False)
    monkeypatch.setattr(web_utils, "_read_pdf_text", lambda path: "")

    out = asyncio.run(web_utils.read_document(str(pdf)))
    assert "[ocr]" in out
    assert "poppler-utils" in out, "the text-PDF hint must still be offered"


def test_read_document_pdf_hint_changes_when_ocr_is_present(monkeypatch, tmp_path):
    pdf = tmp_path / "scanned.pdf"
    pdf.write_bytes(b"%PDF-1.4 not really")

    monkeypatch.setattr(web_utils, "_ocr_available", lambda: True)
    monkeypatch.setattr(web_utils, "_read_pdf_text", lambda path: "")

    out = asyncio.run(web_utils.read_document(str(pdf)))
    assert "[ocr]" not in out, "must not tell an OCR-enabled user to install it"
    assert "OCR ran" in out


def test_dependency_report_labels_ocr_as_an_extra():
    from google_search_mcp import _dependency_report

    report, _critical_ok = _dependency_report()
    assert "OCR extra" in report
    # Declared core deps the code imports directly must be visible too.
    assert "av (clip extraction)" in report
    assert "onnxruntime (captcha solver)" in report


def test_dependency_report_offers_the_extra_when_ocr_is_missing(monkeypatch):
    """The install hint must appear only on the missing path."""
    import google_search_mcp as gsc

    real = gsc._has_module
    monkeypatch.setattr(
        gsc,
        "_has_module",
        lambda name: False if name == "rapidocr_onnxruntime" else real(name),
    )
    report, _critical_ok = gsc._dependency_report()
    assert "[MISSING] rapidocr" in report
    assert "[ocr]" in report


def test_missing_ocr_does_not_make_the_service_unhealthy(monkeypatch):
    """rapidocr/onnxruntime are non-critical: /health must stay 200/ok."""
    from google_search_mcp import health_server

    deps = health_server._check_dependencies()
    payload = {
        "dependencies": deps,
        "files": health_server._check_files(),
        "manual_intervention": health_server._check_manual_intervention(),
    }
    deps["rapidocr"] = {"ok": False, "version": "not installed", "error": "not installed"}
    deps["onnxruntime"] = {"ok": False, "version": "not installed", "error": "not installed"}

    status = health_server._compute_status(payload)
    assert status in ("ok", "degraded"), (
        "a missing optional OCR dep must never put the service in 'down' (503)"
    )


@pytest.mark.parametrize(
    "core_dep",
    ["av", "onnxruntime"],
)
def test_directly_imported_deps_are_declared_in_pyproject(core_dep):
    """`av` and `onnxruntime` are imported by shipped code, so declare them."""
    assert f'"{core_dep}>=' in _core_dependency_block(
        _pyproject_text()
    ), f"{core_dep} must be a core dependency"


def test_rapidocr_is_not_a_core_dependency():
    assert '"rapidocr-onnxruntime' not in _core_dependency_block(
        _pyproject_text()
    ), "rapidocr must stay in the [ocr] extra or Python 3.13 installs break"


def test_playwright_floor_supports_python_313():
    assert 'playwright>=1.48.0' in _pyproject_text(), (
        "1.48 is the first release that officially supports Python 3.13"
    )
