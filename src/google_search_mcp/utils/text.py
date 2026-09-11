"""Text processing utilities — HTML stripping, timestamp formatting, chunk splitting."""

import logging
import re

logger = logging.getLogger(__name__)


def format_error(operation: str, exc: BaseException | None = None) -> str:
    """Return a clean, user-facing error message and log the real exception.

    Tools should return this instead of leaking raw exception strings (e.g.
    ``f"Search failed: {e}"``) into the LLM context. The full exception is
    logged server-side for debugging; the returned string is a concise,
    actionable message with no internal traceback noise.

    Args:
        operation: A short human-readable label for what failed, e.g.
                   ``"Google Maps search"``.
        exc: The caught exception (optional). If provided, its type and
             message are logged at ERROR level.

    Returns:
        A clean message like ``"Google Maps search failed. Please try again."``
    """
    if exc is not None:
        logger.error("%s failed: %s: %s", operation, type(exc).__name__, exc)
    return f"{operation} failed. Please try again."


def strip_html(text: str) -> str:
    """Remove HTML tags from text, collapsing whitespace."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def format_timestamp(seconds: float) -> str:
    """Format seconds into H:MM:SS or M:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def split_translation_chunks(text: str, max_chars: int = 1500) -> list[str]:
    """Split long text into chunks at sentence/word boundaries for APIs with size limits."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            parts.append(remaining.strip())
            break

        cut = max(
            remaining.rfind("\n", 0, max_chars),
            remaining.rfind(". ", 0, max_chars),
            remaining.rfind("! ", 0, max_chars),
            remaining.rfind("? ", 0, max_chars),
            remaining.rfind("; ", 0, max_chars),
            remaining.rfind(", ", 0, max_chars),
            remaining.rfind(" ", 0, max_chars),
        )
        if cut < max_chars // 3:
            cut = max_chars

        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()

    return [p for p in parts if p]


def collapse_newlines(text: str) -> str:
    """Collapse 3+ consecutive newlines into 2."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()
