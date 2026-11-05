"""``format_error`` must not erase errors that were written for the caller.

Context: a transcription failed with a deliberately detailed message —

    Transcription is unavailable: the remote Whisper host
    http://promaxgb10-6116:8768 did not transcribe the audio (...). Check that
    the service on that host is running and reachable ... There is no local
    transcription fallback on this machine.

— and the tool returned "Transcription failed. Please try again." The one
sentence that named the host and the setting to fix was dropped, and the advice
to retry was wrong: retrying an unreachable host cannot work.

``format_error`` exists to keep raw tracebacks out of the model's context, which
is right for unexpected exceptions. Our own ``GoogleMcpError`` subclasses are the
exception: their message is the user-facing explanation.
"""

from __future__ import annotations

from google_search_mcp.errors import (
    DependencyMissingError,
    GoogleMcpError,
    UpstreamBlockedError,
    UpstreamError,
    UserInputError,
)
from google_search_mcp.utils.text import format_error


def test_upstream_error_message_is_surfaced():
    exc = UpstreamError("the remote host is unreachable; set WHISPER_REMOTE_URL")
    out = format_error("Transcription", exc)
    assert "WHISPER_REMOTE_URL" in out
    assert "Please try again" not in out


def test_dependency_missing_error_message_is_surfaced():
    out = format_error("OCR", DependencyMissingError("install the [ocr] extra"))
    assert "[ocr]" in out


def test_user_input_error_message_is_surfaced():
    out = format_error("Read document", UserInputError("path is outside ALLOWED_ROOTS"))
    assert "ALLOWED_ROOTS" in out


def test_blocked_error_message_is_surfaced():
    out = format_error("Google search", UpstreamBlockedError("blocked by bot detection"))
    assert "bot detection" in out


def test_unknown_exception_still_gets_the_generic_message():
    """Unexpected crashes must not leak internals into the model's context."""
    out = format_error("Transcription", RuntimeError("sk-abc123 secret-ish detail"))
    assert out == "Transcription failed. Please try again."
    assert "sk-abc123" not in out


def test_no_exception_is_still_generic():
    assert format_error("Wikipedia") == "Wikipedia failed. Please try again."


def test_empty_known_error_falls_back_to_generic():
    """A GoogleMcpError with no message must not produce an empty tool result."""
    exc = GoogleMcpError()
    out = format_error("Search", exc)
    # GoogleMcpError.__init__ supplies its default_message, so this is never
    # blank; assert we never return an empty string either way.
    assert out.strip()
