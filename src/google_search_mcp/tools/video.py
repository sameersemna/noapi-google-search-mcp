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


def _transcript_cache_path(url: str, model_size: str) -> str:
    """Get disk cache path for a transcript."""
    key = hashlib.md5(f"{url}|{model_size}".encode()).hexdigest()
    return os.path.join(TRANSCRIPT_CACHE_DIR, f"{key}.json")


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


def _transcribe_audio_local(audio_path: str, model_size: str, language: str) -> dict:
    """Transcribe audio file locally with faster-whisper on CPU/int8.

    Always invoked as ``await asyncio.to_thread(...)`` by the calling tool —
    the function itself is synchronous.

    Returns the canonical segment shape::

        {
          "segments": [{"start": float, "end": float, "text": str}, ...],
          "language": str,
          "language_probability": float,
        }
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    transcribe_opts = {"beam_size": 5}
    if language:
        transcribe_opts["language"] = language

    segments_gen, whisper_info = model.transcribe(audio_path, **transcribe_opts)

    segments = []
    for seg in segments_gen:
        segments.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
        })

    return {
        "segments": segments,
        "language": whisper_info.language,
        "language_probability": whisper_info.language_probability,
    }


def _transcribe_audio(audio_path: str, model_size: str, language: str) -> dict:
    """Transcribe audio file (runs in thread). Returns segments + info.

    Strategy:
        1. If a remote Whisper server is configured (``WHISPER_REMOTE_ENABLED=1``
           and ``WHISPER_REMOTE_URL`` set), try it first via
           ``utils.whisper_remote.transcribe_remote``. This offloads heavy
           transcription to a GPU host (e.g. a Dell Pro Max GB10 running
           ``speaches``) and dramatically reduces wall time on long audio.
        2. On any failure (remote disabled, unreachable, timeout, ineligible
           file size, malformed response), fall back transparently to the
           local ``faster-whisper`` CPU path.

    The returned shape is identical regardless of which path produced it, so
    every downstream consumer (``_format_and_cache_transcript``,
    ``search_transcript``, ``transcribe_local``, ``_auto_transcribe_youtube``)
    works unchanged.

    Args:
        audio_path: Local path to the audio file to transcribe.
        model_size: Whisper model size (tiny|base|small|medium|large). Used
            for the LOCAL path; the remote path always uses
            ``WHISPER_REMOTE_MODEL``.
        language: ISO 639-1 language code or empty string for auto-detect.
    """
    # ── Remote first (best-effort) ─────────────────────────────────────
    # transcribe_remote never raises; it returns None on any failure so
    # the caller can fall back. The try/except is purely defensive — if
    # anything in the import path itself blows up, we still want to
    # produce a transcript locally rather than fail the whole call.
    try:
        from ..utils import whisper_remote

        remote_result = whisper_remote.transcribe_remote(
            audio_path, model_size, language
        )
        if remote_result is not None:
            return remote_result
    except Exception as exc:  # pragma: no cover - defensive
        # Swallow — we are about to try the local path anyway.
        log.debug("Remote Whisper path raised unexpectedly: %s", exc)

    # ── Local fallback ─────────────────────────────────────────────────
    return _transcribe_audio_local(audio_path, model_size, language)


@mcp.tool()
async def transcribe_video(
    url: str,
    model_size: str = "tiny",
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

    # Check disk cache first
    os.makedirs(TRANSCRIPT_CACHE_DIR, exist_ok=True)
    cache_path = _transcript_cache_path(url, model_size)
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

    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        return "faster-whisper is required. Install with: pip install faster-whisper"

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
            await ctx.report_progress(progress=25, total=100, message="Transcribing audio (this may take a minute)...")

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
        return format_error("Video transcription", e)

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
    model_size: str = "tiny",
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
    cache_path = _transcript_cache_path(url, model_size)
    if not os.path.isfile(cache_path):
        return (
            f"No cached transcript found for this URL. "
            f"Call transcribe_video first with url=\"{url}\"."
        )

    try:
        with open(cache_path) as f:
            data = json.load(f)
    except Exception:
        return "Failed to read cached transcript."

    segments = data.get("segments", [])
    title = data.get("title", "Unknown")
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
        return format_error("Subtitle lookup", e)

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
    key = hashlib.md5(key_material.encode()).hexdigest()
    return os.path.join(VIDEO_CACHE_DIR, f"{key}.mp4")


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
        cache_path=_video_cache_path(url, section),
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
