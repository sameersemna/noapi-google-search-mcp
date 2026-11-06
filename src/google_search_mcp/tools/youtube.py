"""yt-dlp-backed YouTube triage, caption and discovery tools.

Why these exist
---------------
Every YouTube question used to funnel through ``transcribe_video``: a full
Whisper pass (or caption fetch) before anything could be answered. But most
questions are cheap ones — *is this video relevant?*, *which part covers my
topic?*, *which video should I watch at all?* — and yt-dlp answers those from
metadata and captions alone.

Measured on this deployment:

    metadata (title, channel, chapters, caption languages)   ~7-12 s   no GPU
    captions (manual preferred)                             ~9-14 s   no GPU
    Whisper transcription                                   GPU host
    ytsearch / channel listing (extract_flat)               ~0.8 s

So the tools below are ordered from cheapest to most expensive, and none of them
touches the GPU host. ``transcribe_video`` remains the last resort, for videos
with no captions at all.

Source labelling is deliberate: human captions are exact, auto-captions are
decent, Whisper is a reconstruction. Every result says which it is, so a model
does not quote an auto-caption as if it were verbatim.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from ..config import MAX_PAGE_CHARS
from ..server import format_error, mcp
from ..utils import ytdlp
from ..utils.text import format_timestamp

log = logging.getLogger("google_search_mcp.youtube")

# A transcript/caption body is capped like any other page text: it lands in a
# context window, and a one-hour video is ~50k characters.
_CAPTION_CAP_DEFAULT = 8000


def _youtube_error(exc: BaseException, what: str) -> str:
    """Uniform, actionable error for every yt-dlp-backed tool.

    ``describe_error`` recognises the YouTube auth wall (and private / paid
    videos) and names the remedy; anything else keeps the generic message so no
    internals leak. Without this, an expired cookie session read as
    "please try again" — advice that cannot possibly work.
    """
    return ytdlp.describe_error(exc) or format_error(what, exc)


def _describe_availability(info: dict[str, Any]) -> str:
    """One-line summary of what can be done with this video, cheapest first."""
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    if manual:
        langs = ", ".join(sorted(manual)[:6])
        return f"human captions ({langs}) — get_captions is enough, no transcription needed"
    if automatic:
        langs = ", ".join(sorted(automatic)[:6])
        return (
            f"auto-generated captions only ({langs}) — good for relevance checks; "
            f"use transcribe_video when exact wording matters"
        )
    return "no captions — transcribe_video is required to read the content"


@mcp.tool()
async def youtube_video_info(url: str) -> str:
    """Describe a YouTube video from metadata alone: is it worth watching at all?

    This is the cheapest possible look at a video — one metadata request, no
    audio download, no transcription, no GPU. Use it first, before
    `get_captions` or `transcribe_video`, to decide whether the video is worth
    spending real time on.

    It reports the things a decision actually depends on: title, channel,
    duration, view count, upload date, whether captions exist (human or
    auto-generated), the chapter list with timestamps, and the opening of the
    description.

    Chapters are the practical highlight: when a video is chaptered you can send
    `extract_video_clip` straight at the right timestamps and skip
    transcribing the other 90% of it.

    Sample prompts that trigger this tool:
        - "What is this video about: https://youtube.com/watch?v=..."
        - "Is this video worth watching? How long is it?"
        - "What chapters does this talk have?"
        - "Does this video have captions? In which languages?"

    Args:
        url: YouTube video URL.
    """
    if not ytdlp.is_available():
        return "yt-dlp is required. Install with: pip install yt-dlp"

    try:
        info = await asyncio.to_thread(ytdlp.extract_info, url)
    except Exception as exc:
        return _youtube_error(exc, "Video info lookup")

    if not info:
        return f"No metadata returned for {url}"

    duration = info.get("duration") or 0
    lines = [
        f"Title: {info.get('title', 'Unknown')}",
        f"Channel: {info.get('uploader') or info.get('channel') or 'Unknown'}",
        f"Duration: {format_timestamp(duration)}"
        + (f" ({int(duration)}s)" if duration else ""),
    ]

    views = info.get("view_count")
    if views:
        lines.append(f"Views: {views:,}")
    upload_date = info.get("upload_date")
    if upload_date and len(str(upload_date)) == 8:
        d = str(upload_date)
        lines.append(f"Uploaded: {d[:4]}-{d[4:6]}-{d[6:8]}")
    if info.get("was_live"):
        lines.append("Live: yes (recorded)")
    if info.get("age_limit"):
        lines.append(f"Age limit: {info['age_limit']}+")

    lines.append("")
    lines.append(f"Captions: {_describe_availability(info)}")
    automatic = info.get("automatic_captions") or {}
    if automatic:
        lines.append(f"  auto-generated languages: {len(automatic)}")

    chapters = info.get("chapters") or []
    if chapters:
        lines.append("")
        lines.append(f"Chapters ({len(chapters)}):")
        for chapter in chapters:
            start = chapter.get("start_time") or 0
            lines.append(
                f"  {format_timestamp(start)} - {chapter.get('title', '(untitled)')}"
                f"  (start_seconds={start:.0f})"
            )
    else:
        lines.append("")
        lines.append(
            "Chapters: none published. To find a topic, use get_captions with a "
            "query, or search_transcript after transcribing."
        )

    description = (info.get("description") or "").strip()
    if description:
        excerpt = " ".join(description.split())[:400]
        lines.append("")
        lines.append(f"Description: {excerpt}{'...' if len(description) > 400 else ''}")

    lines.append("")
    lines.append(f"URL: {info.get('webpage_url') or url}")
    return "\n".join(lines)


def _match_segments(
    segments: list[dict[str, Any]], query: str, context: int = 1
) -> tuple[list[int], list[tuple[int, int]]]:
    """Return matching indices and grouped ranges (with context) for *query*."""
    lowered = query.casefold()
    matches = [
        i for i, seg in enumerate(segments) if lowered in str(seg.get("text", "")).casefold()
    ]
    ranges: list[tuple[int, int]] = []
    for idx in matches:
        start = max(0, idx - context)
        end = min(len(segments) - 1, idx + context)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    return matches, ranges


@mcp.tool()
async def get_captions(
    url: str,
    languages: str = "",
    include_automatic: bool = True,
    query: str = "",
    max_characters: int = 0,
) -> str:
    """Fetch a YouTube video's captions directly — fast relevance check, no transcription.

    This is the tool to use when the question is "does this video talk about X?"
    It reads the platform captions (human-authored preferred, auto-generated as a
    fallback) instead of transcribing audio, so it costs seconds and no GPU time.

    With `query` set it returns only the matching passages, with timestamps you
    can hand straight to `extract_video_clip`. Without `query` it returns the
    caption text, capped so it cannot flood the context.

    The result is also cached as a transcript, so a following `search_transcript`
    or `extract_video_clip` call reuses it and `transcribe_video` becomes
    unnecessary for captioned videos.

    Sample prompts that trigger this tool:
        - "Does this video mention Kubernetes? https://youtube.com/watch?v=..."
        - "Find where they discuss pricing in this video"
        - "Get the captions of this video in German"
        - "Skim this talk for mentions of memory bandwidth"

    Args:
        url: YouTube video URL.
        languages: Comma-separated language codes (e.g. "en,de"). Empty means the
            video's original language, then English, then whatever exists.
        include_automatic: Accept auto-generated captions when no human track
            exists (default True). Set False to demand human captions.
        query: Optional keyword or phrase. When given, only matching passages are
            returned, with timestamps.
        max_characters: Cap on the returned caption text (0 = server default).
            Clamped to 200000.
    """
    if not ytdlp.is_available():
        return "yt-dlp is required. Install with: pip install yt-dlp"

    requested = [part.strip() for part in languages.split(",") if part.strip()] or None

    try:
        captions = await asyncio.to_thread(
            ytdlp.download_subtitles, url, requested, include_automatic=include_automatic
        )
    except Exception as exc:
        return _youtube_error(exc, "Caption fetch")

    tracks = captions.get("tracks") or {}
    if not tracks:
        manual = await _caption_languages(url)
        if manual.get("manual"):
            return (
                f"No captions fetched, but this video has human captions in: "
                f"{manual['manual']}. Try a specific language, or set "
                f"include_automatic=True."
            )
        if manual.get("automatic") and not include_automatic:
            return (
                f"This video only has auto-generated captions "
                f"({len(manual['automatic'])} languages), and include_automatic "
                f"is False. Set include_automatic=True to use them."
            )
        return (
            f"No captions available for: {captions.get('title', url)}\n"
            f"Use transcribe_video to transcribe the audio with Whisper instead."
        )

    lang, track = next(iter(tracks.items()))
    segments = track.get("segments") or []
    source = track.get("source", "unknown")
    title = captions.get("title", "Unknown")
    source_line = f"Source: captions ({source}, {lang})"

    # Cache the captions as a transcript so later search/clip calls reuse them.
    await _seed_transcript_cache(url, captions, lang, track)

    if query:
        matches, ranges = _match_segments(segments, query)
        if not matches:
            return (
                f'No mention of "{query}" in the captions of: {title}\n'
                f"{source_line}\n"
                f"Searched {len(segments)} caption segments. The video may still "
                f"discuss it in different words — try a broader keyword."
            )
        lines = [
            f'Caption matches for "{query}" in: {title}',
            f"{source_line}",
            f"Matches: {len(matches)} segments in {len(ranges)} passage(s)",
            f"URL: {url}",
            "",
        ]
        for index, (start_idx, end_idx) in enumerate(ranges, 1):
            start = segments[start_idx].get("start", 0)
            end = segments[end_idx].get("end", 0)
            lines.append(
                f"--- Passage {index}: {format_timestamp(start)} - "
                f"{format_timestamp(end)} "
                f"(start_seconds={start:.1f}, end_seconds={end:.1f}) ---"
            )
            for i in range(start_idx, end_idx + 1):
                seg = segments[i]
                marker = " >>>" if i in matches else "    "
                lines.append(
                    f"{marker} [{format_timestamp(seg.get('start', 0))} - "
                    f"{format_timestamp(seg.get('end', 0))}] {seg.get('text', '')}"
                )
            lines.append("")
        lines.append(
            "To extract a clip, call extract_video_clip with the start_seconds "
            "and end_seconds above."
        )
        return "\n".join(lines)

    text = track.get("text") or "\n".join(
        f"[{format_timestamp(s.get('start', 0))}] {s.get('text', '')}" for s in segments
    )
    limit = max_characters if max_characters > 0 else MAX_PAGE_CHARS
    limit = min(limit, 200_000)
    truncated = len(text) > limit
    body = text[:limit]

    header = [
        f"Captions for: {title}",
        f"Channel: {captions.get('uploader', 'Unknown')}",
        f"Duration: {format_timestamp(captions.get('duration') or 0)}",
        source_line,
        f"Segments: {len(segments)}" + (f" (text truncated to {limit} chars)" if truncated else ""),
        f"URL: {url}",
        "",
    ]
    hint = (
        "\n\nTo search these captions by topic, call get_captions again with a "
        "query, or search_transcript (already cached)."
    )
    return "\n".join(header) + body + hint


async def _caption_languages(url: str) -> dict[str, Any]:
    """Best-effort caption-language lookup used only for error messages."""
    try:
        info = await asyncio.to_thread(ytdlp.list_subtitles, url)
    except Exception:
        return {}
    return {
        "manual": ", ".join(sorted(info.get("manual") or {})[:8]),
        "automatic": list(info.get("automatic") or {}),
    }


async def _seed_transcript_cache(
    url: str, captions: dict[str, Any], lang: str, track: dict[str, Any]
) -> None:
    """Store fetched captions in the transcript cache (best effort)."""
    from .video import _format_and_cache_transcript, _transcript_cache_path

    try:
        segments = track.get("segments") or []
        if not segments:
            return
        await asyncio.to_thread(
            _format_and_cache_transcript,
            url=url,
            cache_path=_transcript_cache_path(url, "tiny"),
            title=captions.get("title", "Unknown"),
            uploader=captions.get("uploader", "Unknown"),
            duration=captions.get("duration") or 0,
            language=lang,
            language_note=f"{track.get('source', 'caption')} captions",
            segments=segments,
            source_label=f"captions ({track.get('source', 'caption')}, {lang})",
        )
    except Exception as exc:  # pragma: no cover - cache write is best effort
        log.debug("could not seed transcript cache for %s: %s", url, exc)


@mcp.tool()
async def youtube_search(query: str, limit: int = 10) -> str:
    """Search YouTube and return matching videos — no API key, no cookies needed.

    Use this when you want *videos about a topic* rather than web pages. Google
    search returns videos incidentally; this searches YouTube's own catalogue and
    reports duration and channel, which is what decides whether a video is worth
    opening.

    Cheap: one request for the whole page of results (no per-video lookups). When
    you pick one, call `youtube_video_info` to see its chapters and captions.

    Sample prompts that trigger this tool:
        - "Find YouTube videos about Kubernetes operators"
        - "Search YouTube for talks on memory bandwidth"
        - "Which videos does Blender have about geometry nodes?"

    Args:
        query: Search query.
        limit: Maximum number of videos to return (1-25, default 10).
    """
    if not ytdlp.is_available():
        return "yt-dlp is required. Install with: pip install yt-dlp"

    query = (query or "").strip()
    if not query:
        return (
            "A search query is required, e.g. youtube_search(query=\"memory "
            "bandwidth explained\")."
        )

    limit = max(1, min(int(limit), 25))
    try:
        entries = await asyncio.to_thread(ytdlp.search_videos, query, limit)
    except Exception as exc:
        return _youtube_error(exc, "YouTube search")

    if not entries:
        return f'No YouTube videos found for: "{query}"'

    lines = [f'YouTube search results for "{query}" ({len(entries)} videos)', ""]
    for index, entry in enumerate(entries, 1):
        title = entry.get("title") or "(untitled)"
        duration = entry.get("duration")
        dur = f" [{format_timestamp(duration)}]" if duration else ""
        channel = entry.get("channel") or entry.get("uploader") or "unknown channel"
        lines.append(f"{index}. {title}{dur}")
        lines.append(f"   Channel: {channel}")
        if entry.get("url"):
            lines.append(f"   URL: {entry['url']}")
        views = entry.get("view_count")
        if views:
            lines.append(f"   Views: {views:,}")
        lines.append("")

    lines.append(
        "Next: call youtube_video_info on a URL to see its chapters and captions, "
        "or get_captions to check whether it covers a topic."
    )
    return "\n".join(lines)


@mcp.tool()
async def list_channel_videos(target: str, limit: int = 20) -> str:
    """List recent videos from a YouTube channel or playlist — no API key needed.

    Cheap and flat: one request returns the whole page of entries with titles,
    durations and links (about a second for 20 videos). Use it to survey a
    channel before opening anything.

    Accepts a channel handle (`@3Blue1Brown`), a channel URL
    (`https://www.youtube.com/@Blender/videos`), or a playlist URL.

    Sample prompts that trigger this tool:
        - "What has 3Blue1Brown published recently?"
        - "List the videos in this playlist: https://youtube.com/playlist?list=..."
        - "Show me the newest videos from @Blender"

    Args:
        target: Channel handle, channel URL, or playlist URL.
        limit: Maximum videos to list (1-50, default 20).
    """
    if not ytdlp.is_available():
        return "yt-dlp is required. Install with: pip install yt-dlp"

    target = (target or "").strip()
    if not target:
        return (
            "A channel target is required: an @handle, a UC... channel id, a "
            "channel name, or a channel/playlist URL."
        )

    limit = max(1, min(int(limit), 50))
    try:
        listing = await asyncio.to_thread(ytdlp.list_entries, _channel_target(target), limit)
    except Exception as exc:
        return _youtube_error(exc, "Channel listing")

    entries = listing.get("entries") or []
    if not entries:
        return (
            f"No videos found for {target}.\n"
            f"Check the handle or playlist URL — some channels only expose a "
            f"'featured' tab, which has no video list."
        )

    heading = listing.get("title") or target
    lines = [f"Videos from {heading} ({len(entries)} shown)", ""]
    for index, entry in enumerate(entries, 1):
        title = entry.get("title") or "(untitled)"
        duration = entry.get("duration")
        dur = f" [{format_timestamp(duration)}]" if duration else ""
        lines.append(f"{index}. {title}{dur}")
        if entry.get("url"):
            lines.append(f"   {entry['url']}")
    lines.append("")
    lines.append("Next: youtube_video_info for chapters and captions of any entry.")
    return "\n".join(lines)


def _channel_target(target: str) -> str:
    """Normalise a channel handle or URL into something yt-dlp's tab extractor accepts."""
    target = target.strip()
    if not target:
        raise ValueError("channel target is empty")
    if target.startswith("http://") or target.startswith("https://"):
        return target
    if target.startswith("@"):
        return f"https://www.youtube.com/{target}/videos"
    if re.fullmatch(r"UC[\w-]{22}", target):
        return f"https://www.youtube.com/channel/{target}/videos"
    # Bare name: treat it as a handle.
    return f"https://www.youtube.com/@{target}/videos"
