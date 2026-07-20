"""Text processing utilities — HTML stripping, timestamp formatting, chunk splitting."""

import re


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
