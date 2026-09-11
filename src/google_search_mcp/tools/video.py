"""Google video tools — transcribe_video, search_transcript, extract_video_clip.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
import hashlib
import json
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
from ..utils.text import format_timestamp


# transcribe_video (YouTube/video transcription with timestamps)
# ---------------------------------------------------------------------------


def _transcript_cache_path(url: str, model_size: str) -> str:
    """Get disk cache path for a transcript."""
    key = hashlib.md5(f"{url}|{model_size}".encode()).hexdigest()
    return os.path.join(TRANSCRIPT_CACHE_DIR, f"{key}.json")


def _download_audio(url: str, cache_dir: str) -> dict:
    """Download audio from URL (runs in thread). Returns info dict."""
    import yt_dlp

    audio_path = os.path.join(cache_dir, "audio_temp")
    # Clean up leftover files
    for f in os.listdir(cache_dir):
        if f.startswith("audio_temp"):
            try:
                os.remove(os.path.join(cache_dir, f))
            except OSError:
                pass

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": audio_path + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    title = info.get("title", "Unknown")
    duration = info.get("duration", 0)
    uploader = info.get("uploader", "Unknown")
    ext = info.get("ext", "m4a")
    actual_path = audio_path + "." + ext

    if not os.path.isfile(actual_path):
        for f in os.listdir(cache_dir):
            if f.startswith("audio_temp"):
                actual_path = os.path.join(cache_dir, f)
                break
        else:
            raise FileNotFoundError("Failed to download audio.")

    return {
        "title": title,
        "duration": duration,
        "uploader": uploader,
        "audio_path": actual_path,
    }


def _transcribe_audio(audio_path: str, model_size: str, language: str) -> dict:
    """Transcribe audio file (runs in thread). Returns segments + info."""
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


@mcp.tool()
async def transcribe_video(
    url: str,
    model_size: str = "tiny",
    language: str = "",
    ctx: Context = None,
) -> str:
    """Download and transcribe a YouTube video (or any video URL) with timestamps.

    Downloads the audio, transcribes it locally using Whisper, and returns a
    full timestamped transcript. The LLM can then answer questions about the
    video content and point to specific timestamps.

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

    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return "yt-dlp is required. Install with: pip install yt-dlp"

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

        # Format full transcript (always stored in cache)
        full_lines = [
            f"Video Transcript",
            f"Title: {title}",
            f"Channel: {uploader}",
            f"Duration: {format_timestamp(duration)}",
            f"Language: {whisper_result['language']} (confidence: {whisper_result['language_probability']:.0%})",
            f"URL: {url}",
            f"",
            f"--- Transcript ---",
        ]

        for seg in segments:
            start = format_timestamp(seg["start"])
            end = format_timestamp(seg["end"])
            full_lines.append(f"[{start} - {end}] {seg['text']}")

        full_lines.append("")
        full_lines.append("--- End of Transcript ---")
        full_lines.append(f"Total segments: {len(segments)}")

        full_transcript = "\n".join(full_lines)

        # Cache to disk (save both formatted text and raw segments for search)
        try:
            with open(cache_path, "w") as f:
                json.dump({
                    "url": url,
                    "title": title,
                    "transcript": full_transcript,
                    "segments": segments,
                }, f)
        except Exception:
            pass

        # For long videos (>10 min), return condensed version to avoid
        # overwhelming the model. Full transcript is always in the cache.
        if duration > 600 and len(segments) > 50:
            preview_count = 15
            preview_lines = [
                f"Video Transcript (condensed - {len(segments)} segments total)",
                f"Title: {title}",
                f"Channel: {uploader}",
                f"Duration: {format_timestamp(duration)}",
                f"Language: {whisper_result['language']} (confidence: {whisper_result['language_probability']:.0%})",
                f"URL: {url}",
                f"",
                f"--- First {preview_count} segments ---",
            ]
            for seg in segments[:preview_count]:
                preview_lines.append(
                    f"[{format_timestamp(seg['start'])} - "
                    f"{format_timestamp(seg['end'])}] {seg['text']}"
                )
            preview_lines.append(f"")
            preview_lines.append(f"... ({len(segments) - preview_count * 2} more segments) ...")
            preview_lines.append(f"")
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
# extract_video_clip (cut a segment from a video by topic)
# ---------------------------------------------------------------------------


def _video_cache_path(url: str) -> str:
    """Get cached video path for a URL."""
    key = hashlib.md5(url.encode()).hexdigest()
    return os.path.join(VIDEO_CACHE_DIR, f"{key}.mp4")


def _download_video(url: str) -> dict:
    """Download video from URL (runs in thread). Returns info dict. Caches to disk."""
    import yt_dlp

    os.makedirs(VIDEO_CACHE_DIR, exist_ok=True)
    cached = _video_cache_path(url)

    # Check if already cached
    if os.path.isfile(cached) and os.path.getsize(cached) > 0:
        # Get title from yt-dlp without downloading
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        return {"title": info.get("title", "clip"), "video_path": cached}

    ydl_opts = {
        "format": "best[ext=mp4][height<=480]/best[ext=mp4]/best",
        "outtmpl": cached,
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if not os.path.isfile(cached):
        raise FileNotFoundError("Failed to download video.")

    return {"title": info.get("title", "clip"), "video_path": cached}


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

    if os.path.isfile(url):
        video_path = url
        title = Path(url).stem
    else:
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            return "yt-dlp is required. Install with: pip install yt-dlp"

        if ctx:
            await ctx.report_progress(progress=0, total=100, message="Downloading video...")

        try:
            dl_info = await asyncio.to_thread(_download_video, url)
            title = dl_info["title"]
            video_path = dl_info["video_path"]
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
        clip_info = await asyncio.to_thread(
            _extract_clip_pyav, video_path, out_path, clip_start, clip_end
        )

        clip_end = clip_info["clip_end"]
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
