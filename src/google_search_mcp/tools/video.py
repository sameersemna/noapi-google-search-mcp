"""Google video tools — transcribe_video, search_transcript, extract_video_clip.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from mcp.server.fastmcp import Context, Image
from playwright.async_api import async_playwright

from ..config import (
    CLIPS_DIR,
    TRANSCRIBE_CACHE_DIR,
    TRANSCRIPT_CACHE_DIR,
    VIDEO_CACHE_DIR,
    YTDLP_ALLOW_AUTO_SUBTITLES,
    YTDLP_PREFER_SUBTITLES,
)
from ..server import (
    mcp,
    browse_google,
    dismiss_consent,
    format_error,
    human_delay,
    is_blocked,
    launch_browser,
    load_cookies,
    save_cookies,
    try_solve_captcha,
)
from ..utils import ytdlp
from ..utils.text import format_timestamp

log = logging.getLogger("google_search_mcp.video")


# transcribe_video (YouTube/video transcription with timestamps)
# ---------------------------------------------------------------------------


def _cache_key(material: str) -> str:
    """Cache key for a piece of cache material (sha256).

    sha256 is used instead of md5 so security scanners do not flag the cache
    as a weak-hash use. This changes the on-disk file NAMES; the ``_legacy_*``
    helpers below let us still read caches written by older versions.
    """
    return hashlib.sha256(material.encode()).hexdigest()


def _legacy_cache_key(material: str) -> str:
    """The pre-hardening md5 key, kept only to read existing caches."""
    return hashlib.md5(material.encode(), usedforsecurity=False).hexdigest()


def _transcript_cache_path(url: str, model_size: str) -> str:
    """Get disk cache path for a transcript (sha256-keyed)."""
    return os.path.join(TRANSCRIPT_CACHE_DIR, f"{_cache_key(f'{url}|{model_size}')}.json")


def _transcript_cache_path_legacy(url: str, model_size: str) -> str:
    """Legacy md5-keyed transcript cache path (read-only fallback)."""
    return os.path.join(
        TRANSCRIPT_CACHE_DIR, f"{_legacy_cache_key(f'{url}|{model_size}')}.json"
    )


def _existing_cache_path(primary: str, legacy: str) -> str:
    """Return *primary* if it exists, else *legacy* if it exists, else *primary*."""
    if os.path.isfile(primary):
        return primary
    if os.path.isfile(legacy):
        return legacy
    return primary


async def _load_segments(
    url: str, model_size: str
) -> tuple[list[dict] | None, str, str]:
    """Return ``(segments, title, source_label)`` for *url*, cheapest source first.

    Order matters — this is what makes triage cheap:

    1. An existing transcript (captions or Whisper, both live in the same cache).
    2. Platform captions fetched from YouTube (~9 s, no GPU, no audio download).
    3. ``None`` — the caller reports that transcription is still required.

    Step 2 exists because ``search_transcript`` used to hard-fail with "call
    transcribe_video first" even when YouTube already had the captions, which is
    the wrong order of work: checking whether a video is relevant should not
    require transcribing it.
    """
    cache_path = _existing_cache_path(
        _transcript_cache_path(url, model_size),
        _transcript_cache_path_legacy(url, model_size),
    )
    if os.path.isfile(cache_path):
        try:
            with open(cache_path) as f:
                data = json.load(f)
        except Exception:
            data = {}
        segments = data.get("segments") or []
        if segments:
            return segments, data.get("title", "Unknown"), "cached transcript"

    # No transcript yet: try the platform captions before giving up.
    if not ytdlp.is_available():
        return None, "Unknown", ""
    try:
        captions = await asyncio.to_thread(
            ytdlp.download_subtitles, url, None, include_automatic=True
        )
    except Exception as exc:  # auth wall / network — caller shows the reason
        log.debug("caption fallback failed for %s: %s", url, exc)
        return None, "Unknown", ""

    tracks = captions.get("tracks") or {}
    if not tracks:
        return None, captions.get("title", "Unknown"), ""

    lang, track = next(iter(tracks.items()))
    segments = track.get("segments") or []
    if not segments:
        return None, captions.get("title", "Unknown"), ""

    # Seed the transcript cache so the next call (and extract_video_clip) reuses
    # these captions instead of paying for them again.
    try:
        _format_and_cache_transcript(
            url=url,
            cache_path=_transcript_cache_path(url, model_size),
            title=captions.get("title", "Unknown"),
            uploader=captions.get("uploader", "Unknown"),
            duration=captions.get("duration", 0) or 0,
            language=lang,
            language_note=f"{track.get('source', 'caption')} captions",
            segments=segments,
        )
    except Exception as exc:  # pragma: no cover - cache write is best effort
        log.debug("could not seed transcript cache for %s: %s", url, exc)

    source = f"captions ({track.get('source', 'unknown')}, {lang})"
    return segments, captions.get("title", "Unknown"), source


def _download_audio(url: str, cache_dir: str) -> dict:
    """Download audio from URL (runs in thread). Returns info dict.

    Delegates to :mod:`google_search_mcp.utils.ytdlp` so cookies, retries and
    timeouts are configured consistently across the project.
    """
    return ytdlp.download_audio(url, cache_dir)


def _format_and_cache_transcript(
    *,
    url: str,
    cache_path: str,
    title: str,
    uploader: str,
    duration: float,
    language: str,
    language_note: str,
    segments: list[dict],
) -> str:
    """Format a transcript, cache it to disk, and return the LLM-facing text.

    Shared by the caption path and the Whisper path so both produce an
    identical transcript shape — ``search_transcript`` reads the cached
    ``segments`` regardless of which source produced them.

    Long videos (>10 min, >50 segments) return a condensed preview instead of
    the full text to avoid flooding the model's context; the complete
    transcript is always written to the cache.

    Args:
        url: Source video URL (stored in the cache for reference).
        cache_path: Where to write the JSON cache entry.
        title: Video title.
        uploader: Channel/uploader name.
        duration: Duration in seconds.
        language: Detected or requested language code.
        language_note: Extra detail appended to the language line, e.g.
            ``"confidence: 98%"`` or ``"manual captions"``.
        segments: ``[{start, end, text}, ...]``.

    Returns:
        The formatted transcript (full or condensed).
    """
    header = [
        "Video Transcript",
        f"Title: {title}",
        f"Channel: {uploader}",
        f"Duration: {format_timestamp(duration)}",
        f"Language: {language} ({language_note})",
        # The source decides how much the text can be trusted: human captions are
        # exact, auto-captions are decent, Whisper is a reconstruction. Saying it
        # explicitly stops a model from quoting an auto-caption as if it were a
        # verbatim transcript.
        f"Source: {language_note}",
        f"URL: {url}",
        "",
    ]

    full_lines = header + ["--- Transcript ---"]
    for seg in segments:
        start = format_timestamp(seg["start"])
        end = format_timestamp(seg["end"])
        full_lines.append(f"[{start} - {end}] {seg['text']}")
    full_lines.append("")
    full_lines.append("--- End of Transcript ---")
    full_lines.append(f"Total segments: {len(segments)}")
    full_transcript = "\n".join(full_lines)

    # Cache to disk (formatted text + raw segments for search_transcript).
    try:
        with open(cache_path, "w") as f:
            json.dump(
                {
                    "url": url,
                    "title": title,
                    "source": language_note,
                    "transcript": full_transcript,
                    "segments": segments,
                },
                f,
            )
    except Exception:
        pass

    if duration > 600 and len(segments) > 50:
        preview_count = 15
        preview_lines = [
            f"Video Transcript (condensed - {len(segments)} segments total)",
        ] + header[1:] + [
            f"--- First {preview_count} segments ---",
        ]
        for seg in segments[:preview_count]:
            preview_lines.append(
                f"[{format_timestamp(seg['start'])} - "
                f"{format_timestamp(seg['end'])}] {seg['text']}"
            )
        preview_lines.append("")
        preview_lines.append(
            f"... ({len(segments) - preview_count * 2} more segments) ..."
        )
        preview_lines.append("")
        preview_lines.append(f"--- Last {preview_count} segments ---")
        for seg in segments[-preview_count:]:
            preview_lines.append(
                f"[{format_timestamp(seg['start'])} - "
                f"{format_timestamp(seg['end'])}] {seg['text']}"
            )
        preview_lines.append("")
        preview_lines.append(
            "IMPORTANT: This is a long video. To find specific topics, "
            "call search_transcript with url and a keyword query. "
            "To extract a clip, call extract_video_clip with the timestamps."
        )
        return "\n".join(preview_lines)

    return full_transcript


def _transcribe_audio(audio_path: str, model_size: str, language: str) -> dict:
    """Transcribe an audio file on the remote Whisper host (runs in a thread).

    Transcription is **always** delegated to ``WHISPER_REMOTE_URL`` — there is
    no local model. That host is ``promaxgb10-6116`` (Dell Pro Max GB10) on the
    LAN, which runs the model on a GPU: the same file that takes ~20 s per
    minute of audio on this machine's CPU comes back in well under a second.

    The local ``faster-whisper`` path was removed deliberately, not lost:

    * it was the largest memory consumer here (ctranslate2 arenas pushed the
      process to 6.9 GB and the kernel OOM-killer took down the desktop), and
    * a silent CPU fallback is worse than an error — a transcription that used
      to take 0.3 s would take minutes and the caller would not know why.

    Because there is no fallback, an unreachable remote raises
    :class:`UpstreamError` naming the host and the setting to fix, rather than
    returning a misleading "no speech detected".

    Returns the canonical segment shape::

        {
          "segments": [{"start": float, "end": float, "text": str}, ...],
          "language": str,                 # ISO code, e.g. "en"
          "language_probability": float,
        }

    Args:
        audio_path: Local path to the audio file to transcribe.
        model_size: Accepted for backward compatibility and used only as a
            transcript-cache key. The remote host decides which model actually
            runs (``WHISPER_REMOTE_MODEL``, or the model it loaded via
            ``/load`` for whisper.cpp), so this no longer selects a model.
        language: ISO 639-1 language code, or empty string for auto-detect.

    Raises:
        UpstreamError: the remote is disabled, unreachable, or returned an
            unusable response.
    """
    from ..utils import whisper_remote

    result = whisper_remote.transcribe_remote(audio_path, model_size, language)
    if result is not None:
        return result

    raise whisper_remote.remote_unavailable_error(audio_path)


@mcp.tool()
async def transcribe_video(
    url: str,
    model_size: Literal["tiny", "base", "small", "medium", "large"] = "tiny",
    language: str = "",
    prefer_subtitles: bool = True,
    ctx: Context = None,
) -> str:
    """Download and transcribe a YouTube video (or any video URL) with timestamps.

    Downloads the audio, transcribes it locally using Whisper, and returns a
    full timestamped transcript. The LLM can then answer questions about the
    video content and point to specific timestamps.

    When the platform publishes captions (YouTube does for most videos), those
    are used instead of running Whisper — they are exact for human-authored
    tracks and take seconds instead of minutes. Whisper is used automatically
    when no captions exist. Set ``prefer_subtitles=False`` to force Whisper.

    Results are cached to disk so repeat requests for the same video are instant.

    Supported model sizes: tiny, base, small, medium, large
    - tiny: fastest, good for most videos (~75MB, default)
    - base: better accuracy, slower (~150MB)
    - small: high accuracy, much slower (~500MB)
    - medium/large: best accuracy, very slow (~1.5GB/~3GB)

    Models are downloaded automatically on first use.

    Sample prompts that trigger this tool:
        - "Transcribe this video: https://youtube.com/watch?v=..."
        - "What is discussed in this video? https://youtube.com/watch?v=..."
        - "Summarize this YouTube video: https://..."
        - "At what timestamp do they talk about X in https://..."
        - "Explain the concept from 5:30 in this video: https://..."

    Args:
        url: YouTube URL or any video URL supported by yt-dlp.
        model_size: Whisper model size (tiny/base/small/medium/large). Default: tiny.
        language: Language code (e.g. "en", "de", "fr"). Auto-detected if empty.
        prefer_subtitles: Use platform captions when available (default: True).
    """
    # Validate model size
    valid_sizes = ("tiny", "base", "small", "medium", "large")
    if model_size not in valid_sizes:
        model_size = "tiny"

    # Check disk cache first (also honour a legacy md5-keyed entry)
    os.makedirs(TRANSCRIPT_CACHE_DIR, exist_ok=True)
    cache_path = _existing_cache_path(
        _transcript_cache_path(url, model_size),
        _transcript_cache_path_legacy(url, model_size),
    )
    if os.path.isfile(cache_path):
        try:
            with open(cache_path) as f:
                return json.load(f)["transcript"] + "\n\n(cached result)"
        except Exception:
            pass

    if not ytdlp.is_available():
        return "yt-dlp is required. Install with: pip install yt-dlp"

    # ── Fast path: platform-provided captions ──────────────────────────
    # Captions avoid downloading audio and running Whisper entirely. This is
    # the single biggest speed-up for the common case (YouTube videos with
    # captions), and human-authored tracks are more accurate than Whisper.
    if prefer_subtitles and YTDLP_PREFER_SUBTITLES:
        if ctx:
            await ctx.report_progress(
                progress=10, total=100, message="Checking for captions..."
            )
        try:
            subs = await asyncio.to_thread(
                ytdlp.download_subtitles,
                url,
                [language] if language else None,
                include_automatic=YTDLP_ALLOW_AUTO_SUBTITLES,
            )
        except Exception as e:
            log.debug("Subtitle fetch failed for %s: %s", url, e)
            subs = {"tracks": {}}

        tracks = subs.get("tracks") or {}
        if tracks:
            lang, track = next(iter(tracks.items()))
            segments = track["segments"]
            if ctx:
                await ctx.report_progress(
                    progress=100, total=100, message="Done (from captions)!"
                )
            return _format_and_cache_transcript(
                url=url,
                cache_path=cache_path,
                title=subs.get("title", "Unknown"),
                uploader=subs.get("uploader", "Unknown"),
                duration=subs.get("duration", 0),
                language=lang,
                language_note=f"{track['source']} captions",
                segments=segments,
            )

    from ..utils import whisper_remote as _whisper_remote

    if not _whisper_remote.is_configured():
        return str(_whisper_remote.remote_unavailable_error())

    os.makedirs(TRANSCRIBE_CACHE_DIR, exist_ok=True)

    # Download audio in a thread so the event loop stays alive
    if ctx:
        await ctx.report_progress(progress=0, total=100, message="Downloading audio...")

    try:
        dl_info = await asyncio.to_thread(
            _download_audio, url, TRANSCRIBE_CACHE_DIR
        )
    except Exception as e:
        return f"Failed to download video: {e}"

    title = dl_info["title"]
    duration = dl_info["duration"]
    uploader = dl_info["uploader"]
    actual_audio_path = dl_info["audio_path"]

    try:
        # Transcribe in a thread so progress notifications can be sent
        if ctx:
            await ctx.report_progress(progress=25, total=100, message="Transcribing on the remote Whisper host...")

        whisper_result = await asyncio.to_thread(
            _transcribe_audio, actual_audio_path, model_size, language
        )

        segments = whisper_result["segments"]

        if not segments:
            return f"No speech detected in: {title}"

        if ctx:
            await ctx.report_progress(progress=100, total=100, message="Done!")

        return _format_and_cache_transcript(
            url=url,
            cache_path=cache_path,
            title=title,
            uploader=uploader,
            duration=duration,
            language=whisper_result["language"],
            language_note=(
                f"confidence: {whisper_result['language_probability']:.0%}"
            ),
            segments=segments,
        )

    except Exception as e:
        return ytdlp.describe_error(e) or format_error("Video transcription", e)

    finally:
        try:
            os.remove(actual_audio_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# search_transcript (find segments by keyword in a cached transcript)
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_transcript(
    url: str,
    query: str,
    model_size: Literal["tiny", "base", "small", "medium", "large"] = "tiny",
    context_segments: int = 2,
) -> str:
    """Search inside an already-transcribed video for segments matching a keyword.

    IMPORTANT: This tool searches an EXISTING transcript — it does NOT download
    or transcribe a video. The video must have been transcribed first with
    transcribe_video. If the user says "search the transcript for X" or
    "find where they talk about X", use THIS tool, not transcribe_video.

    Returns matching segments with surrounding context so the LLM can determine
    the exact start and end timestamps for a topic, then call extract_video_clip.

    Sample prompts that trigger this tool:
        - "Search the transcript for memory bandwidth"
        - "Find where they talk about memory bandwidth in the video"
        - "What timestamp do they discuss pricing?"
        - "When do they mention the DGX Spark specs?"

    Args:
        url: The same video URL used with transcribe_video.
        query: Keyword or phrase to search for (case-insensitive).
        model_size: Must match the model_size used for transcription (default: tiny).
        context_segments: Number of surrounding segments to include (default: 2).
    """
    segments, title, source = await _load_segments(url, model_size)
    if segments is None:
        return (
            f"No transcript and no captions available for this URL. "
            f"Call transcribe_video first with url=\"{url}\" to transcribe the "
            f"audio."
        )
    if not segments:
        return "Transcript has no segments."

    query_lower = query.lower()

    # Find matching segment indices
    matches = []
    for i, seg in enumerate(segments):
        if query_lower in seg["text"].lower():
            matches.append(i)

    if not matches:
        return f"No segments found matching \"{query}\" in: {title}"

    # Group nearby matches into ranges with context
    ranges = []
    for idx in matches:
        start_idx = max(0, idx - context_segments)
        end_idx = min(len(segments) - 1, idx + context_segments)
        if ranges and start_idx <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], end_idx)
        else:
            ranges.append((start_idx, end_idx))

    # Format results
    lines = [
        f"Search results for \"{query}\" in: {title}",
        f"Matches found: {len(matches)} segments in {len(ranges)} section(s)",
        f"URL: {url}",
        f"Source: {source}",
        "",
    ]

    for r_idx, (start_idx, end_idx) in enumerate(ranges):
        section_start = segments[start_idx]["start"]
        section_end = segments[end_idx]["end"]
        lines.append(
            f"--- Section {r_idx + 1}: "
            f"{format_timestamp(section_start)} - {format_timestamp(section_end)} "
            f"(start_seconds={section_start:.1f}, end_seconds={section_end:.1f}) ---"
        )
        for i in range(start_idx, end_idx + 1):
            seg = segments[i]
            marker = " >>>" if i in matches else "    "
            lines.append(
                f"{marker} [{format_timestamp(seg['start'])} - "
                f"{format_timestamp(seg['end'])}] {seg['text']}"
            )
        lines.append("")

    lines.append(
        "To extract a clip, call extract_video_clip with the "
        "start_seconds and end_seconds shown above."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# list_subtitles (discover available caption languages for a video)
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_subtitles(url: str) -> str:
    """List the subtitle/caption languages available for a video.

    Use this before transcribe_video when you need a specific language, or to
    check whether captions exist at all. Captions are much faster than Whisper
    transcription and are exact for human-authored tracks.

    Sample prompts that trigger this tool:
        - "What subtitle languages does this video have?"
        - "Does this video have Arabic captions?"
        - "List the captions available for https://youtube.com/watch?v=..."

    Args:
        url: YouTube URL or any video URL supported by yt-dlp.
    """
    if not ytdlp.is_available():
        return "yt-dlp is required. Install with: pip install yt-dlp"

    try:
        info = await asyncio.to_thread(ytdlp.list_subtitles, url)
    except Exception as e:
        return ytdlp.describe_error(e) or format_error("Subtitle lookup", e)

    manual = info.get("manual") or {}
    automatic = info.get("automatic") or {}

    if not manual and not automatic:
        return (
            f"No subtitles available for: {info.get('title', url)}\n"
            f"Use transcribe_video to transcribe the audio with Whisper instead."
        )

    lines = [
        f"Subtitles for: {info.get('title', 'Unknown')}",
        f"Duration: {format_timestamp(info.get('duration', 0))}",
        "",
    ]

    if manual:
        lines.append(f"Human-authored captions ({len(manual)}):")
        for lang, fmt in sorted(manual.items()):
            lines.append(f"  {lang}  ({fmt})")
        lines.append("")

    if automatic:
        lines.append(f"Auto-generated captions ({len(automatic)}):")
        for lang, fmt in sorted(automatic.items()):
            lines.append(f"  {lang}  ({fmt})")
        lines.append("")

    lines.append(
        "Call transcribe_video with language=\"<code>\" to use a specific "
        "track, or omit it to use the video's original language."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# extract_video_clip (cut a segment from a video by topic)
# ---------------------------------------------------------------------------


def _video_cache_path(
    url: str, section: tuple[float, float] | None = None
) -> str:
    """Get cached video path for a URL (and optional time section).

    The section is part of the cache key: a file downloaded for one range
    contains only that range and its timeline starts at 0, so reusing it for
    a different range would cut the wrong footage.
    """
    key_material = url if not section else f"{url}|{section[0]:.1f}-{section[1]:.1f}"
    return os.path.join(VIDEO_CACHE_DIR, f"{_cache_key(key_material)}.mp4")


def _video_cache_path_legacy(
    url: str, section: tuple[float, float] | None = None
) -> str:
    """Legacy md5-keyed video cache path (read-only fallback)."""
    key_material = url if not section else f"{url}|{section[0]:.1f}-{section[1]:.1f}"
    return os.path.join(VIDEO_CACHE_DIR, f"{_legacy_cache_key(key_material)}.mp4")


def _download_video(
    url: str,
    section: tuple[float, float] | None = None,
) -> dict:
    """Download video from URL (runs in thread). Returns info dict. Caches to disk.

    When *section* is given, only that time range is downloaded (yt-dlp
    ``download_ranges``) instead of the whole video — a large speed-up for
    long videos. Falls back to a full download if the range download fails.

    Args:
        url: Video URL.
        section: Optional ``(start_seconds, end_seconds)`` to fetch.

    Returns:
        ``{"title", "video_path", "partial"}``.
    """
    return ytdlp.download_video(
        url,
        VIDEO_CACHE_DIR,
        cache_path=_existing_cache_path(
            _video_cache_path(url, section),
            _video_cache_path_legacy(url, section),
        ),
        section=section,
    )


def _extract_clip_pyav(
    video_path: str, out_path: str,
    clip_start: float, clip_end: float,
) -> dict:
    """Extract clip using PyAV (runs in thread). Returns clip info."""
    import av

    inp = av.open(video_path)

    video_stream = inp.streams.video[0] if inp.streams.video else None
    audio_stream = inp.streams.audio[0] if inp.streams.audio else None

    if not video_stream and not audio_stream:
        inp.close()
        raise ValueError("No video or audio streams found.")

    total_duration = float(inp.duration / av.time_base) if inp.duration else 0
    if total_duration and clip_end > total_duration:
        clip_end = total_duration

    out = av.open(out_path, 'w')

    o_vs = None
    if video_stream:
        o_vs = out.add_stream('libx264', rate=video_stream.average_rate)
        o_vs.width = video_stream.codec_context.width
        o_vs.height = video_stream.codec_context.height
        o_vs.pix_fmt = video_stream.codec_context.pix_fmt or 'yuv420p'

    o_as = None
    if audio_stream:
        o_as = out.add_stream('aac', rate=audio_stream.codec_context.sample_rate)
        o_as.layout = audio_stream.codec_context.layout

    if o_vs and video_stream:
        inp.seek(int(clip_start * av.time_base), any_frame=False)
        for frame in inp.decode(video=0):
            ts = float(frame.time) if frame.time is not None else 0
            if ts < clip_start:
                continue
            if ts > clip_end:
                break
            for packet in o_vs.encode(frame):
                out.mux(packet)
        for packet in o_vs.encode():
            out.mux(packet)

    if o_as and audio_stream:
        inp.seek(int(clip_start * av.time_base), any_frame=False)
        for frame in inp.decode(audio=0):
            ts = float(frame.time) if frame.time is not None else 0
            if ts < clip_start:
                continue
            if ts > clip_end:
                break
            frame.pts = None
            for packet in o_as.encode(frame):
                out.mux(packet)
        for packet in o_as.encode():
            out.mux(packet)

    out.close()
    inp.close()

    return {
        "size": os.path.getsize(out_path),
        "clip_end": clip_end,
    }


@mcp.tool()
async def extract_video_clip(
    url: str,
    start_seconds: float,
    end_seconds: float,
    buffer_seconds: float = 3.0,
    output_filename: str = "",
    ctx: Context = None,
) -> str:
    """Extract a video clip by topic from a YouTube video or local file.

    Used after transcribe_video. The LLM reads the transcript, finds the
    timestamps for the requested topic, and calls this tool to cut the clip.
    The user just asks "extract the part about X" - no manual timestamps needed.

    A buffer is added before and after to avoid cutting off content.
    The clip is saved to ~/clips/.

    Sample prompts that trigger this tool:
        - "Extract the part where they talk about memory bandwidth"
        - "Save the segment where they discuss pricing"
        - "Cut out the section about the hardware specs"
        - "Get me the intro of this video"

    Args:
        url: YouTube URL, video URL, or local file path.
        start_seconds: Start time in seconds (e.g. 150 for 2:30).
        end_seconds: End time in seconds (e.g. 315 for 5:15).
        buffer_seconds: Extra seconds before/after the segment (default: 3).
        output_filename: Optional filename for the clip (without extension).
    """
    try:
        import av  # noqa: F401
    except ImportError:
        return "PyAV is required. Install with: pip install av"

    os.makedirs(CLIPS_DIR, exist_ok=True)
    os.makedirs(TRANSCRIBE_CACHE_DIR, exist_ok=True)

    clip_start = max(0, start_seconds - buffer_seconds)
    clip_end = end_seconds + buffer_seconds

    video_path = None
    title = "clip"
    # When only a section was downloaded, the file's timeline starts at 0
    # rather than at the original video's timestamp. Track the offset so the
    # clip is cut at the right place inside the partial file.
    timeline_offset = 0.0

    if os.path.isfile(url):
        video_path = url
        title = Path(url).stem
    else:
        if not ytdlp.is_available():
            return "yt-dlp is required. Install with: pip install yt-dlp"

        if ctx:
            await ctx.report_progress(progress=0, total=100, message="Downloading video...")

        try:
            # Only fetch the requested range when possible — for a 2-hour
            # video this turns a multi-hundred-MB download into a few MB.
            dl_info = await asyncio.to_thread(
                _download_video, url, (clip_start, clip_end)
            )
            title = dl_info["title"]
            video_path = dl_info["video_path"]
            if dl_info.get("partial"):
                timeline_offset = clip_start
        except Exception as e:
            return f"Failed to download video: {e}"

    safe_title = re.sub(r'[^\w\s-]', '', title)[:50].strip().replace(' ', '_')
    if output_filename:
        safe_title = re.sub(r'[^\w\s-]', '', output_filename)[:50].strip().replace(' ', '_')

    start_str = format_timestamp(clip_start).replace(':', '-')
    end_str = format_timestamp(clip_end).replace(':', '-')
    out_name = f"{safe_title}_{start_str}_to_{end_str}.mp4"
    out_path = os.path.join(CLIPS_DIR, out_name)

    if ctx:
        await ctx.report_progress(progress=40, total=100, message="Extracting clip...")

    try:
        # Cut relative to the file's own timeline. For a full download the
        # offset is 0; for a section download the file starts at clip_start,
        # so the clip is cut from 0 to (clip_end - clip_start).
        clip_info = await asyncio.to_thread(
            _extract_clip_pyav,
            video_path,
            out_path,
            clip_start - timeline_offset,
            clip_end - timeline_offset,
        )

        clip_end = clip_info["clip_end"] + timeline_offset
        clip_size = clip_info["size"]
        clip_dur = clip_end - clip_start

        result = [
            f"Video clip extracted successfully!",
            f"",
            f"Source: {title}",
            f"Segment: {format_timestamp(clip_start)} - {format_timestamp(clip_end)} "
            f"(requested {format_timestamp(start_seconds)} - {format_timestamp(end_seconds)} + {buffer_seconds}s buffer)",
            f"Duration: {format_timestamp(clip_dur)}",
            f"Size: {clip_size / (1024*1024):.1f} MB",
            f"Saved to: {out_path}",
        ]
        return "\n".join(result)

    except Exception as e:
        return f"Failed to extract clip: {e}"
