"""Centralized yt-dlp integration.

Every yt-dlp invocation in this project goes through this module so that
cookies, headers, retries, and output paths are configured consistently.

Why this module exists
----------------------
Before this module, each call site built its own ``ydl_opts`` dict. None of
them passed a cookie file, which is why YouTube downloads intermittently
failed with ``HTTP Error 403: Forbidden`` — YouTube serves a signed URL that
is only valid for the session that requested it, and without cookies the
follow-up media request is rejected.

It also adds **subtitle support**. YouTube (and most other platforms) publish
human-authored or auto-generated captions for most videos. Fetching those is
dramatically cheaper than downloading audio and running Whisper:

* no audio download (megabytes instead of tens of megabytes)
* no Whisper model load or inference (seconds instead of minutes)
* exact punctuation and casing for human-authored captions
* available in many languages, so the same video can be searched in each

Whisper remains the fallback for videos with no captions at all.

All functions here are **synchronous** and are expected to be called from a
worker thread via ``asyncio.to_thread`` so the event loop stays responsive.
"""

import glob
import logging
import os
import re
import shutil
from typing import Any, Callable

from .. import config

log = logging.getLogger("google_search_mcp.ytdlp")

# Subtitle formats we can parse, in order of preference. ``srt`` is easiest to
# parse; ``vtt`` is what YouTube serves natively. ``json3`` is YouTube's raw
# timed-text format and is the most reliable when available.
_SUBTITLE_FORMAT_PREFERENCE = "json3/srv3/vtt/srt/best"

# Extensions yt-dlp may produce for the formats above.
_SUBTITLE_EXTENSIONS = ("json3", "srv3", "vtt", "srt", "ttml", "srv1", "srv2")


# ---------------------------------------------------------------------------
# Cookie resolution
# ---------------------------------------------------------------------------


def resolve_cookiefile(url: str) -> str | None:
    """Pick the best cookie file for *url*, or ``None`` if none applies.

    YouTube cookies are only sent to YouTube hosts, Google cookies only to
    Google hosts. Sending the wrong jar is worse than sending none — it can
    look like a hijacked session.

    Args:
        url: The media URL about to be fetched.

    Returns:
        Path to a Netscape-format cookie file, or ``None``.
    """
    if not url:
        return None

    host = ""
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
    except Exception:  # pragma: no cover - malformed URL
        return None

    candidates: list[str] = []
    if "youtube.com" in host or "youtu.be" in host or "youtube-nocookie" in host:
        candidates.append(config.YOUTUBE_COOKIE_PATH)
    if "google." in host or "youtube.com" in host:
        candidates.append(config.GOOGLE_COOKIE_PATH)

    for path in candidates:
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


# ---------------------------------------------------------------------------
# Option building
# ---------------------------------------------------------------------------


def build_ydl_opts(
    url: str = "",
    *,
    outtmpl: str | None = None,
    fmt: str | None = None,
    quiet: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``ydl_opts`` dict with project-wide defaults applied.

    Args:
        url: The target URL — used to select the right cookie jar.
        outtmpl: Output template/path. Omit for metadata-only calls.
        fmt: yt-dlp format selector. Defaults to best audio.
        quiet: Suppress yt-dlp's own console output.
        extra: Additional options merged in last (caller wins).

    Returns:
        A dict ready to pass to ``yt_dlp.YoutubeDL``.
    """
    opts: dict[str, Any] = {
        "quiet": quiet,
        "no_warnings": quiet,
        "noprogress": quiet,
        "retries": config.YTDLP_RETRIES,
        "fragment_retries": config.YTDLP_RETRIES,
        "socket_timeout": config.YTDLP_SOCKET_TIMEOUT,
        "nocheckcertificate": False,
        # Keep yt-dlp from writing .part/.ytdl files we then have to clean up.
        "overwrites": True,
    }

    if fmt:
        opts["format"] = fmt
    if outtmpl:
        opts["outtmpl"] = outtmpl

    cookiefile = resolve_cookiefile(url)
    if cookiefile:
        opts["cookiefile"] = cookiefile
        log.debug("Using cookie file for %s: %s", url, cookiefile)

    if config.YTDLP_PROXY:
        opts["proxy"] = config.YTDLP_PROXY

    # YouTube requires a JavaScript runtime to solve its signature ("nsig")
    # challenges. Without one, yt-dlp logs "nsig extraction failed" and every
    # media request returns HTTP 403. The runtime must be enabled explicitly
    # for node (deno is the only one enabled by default).
    if _is_youtube(url):
        runtime = detect_js_runtime()
        if runtime:
            opts["js_runtimes"] = {runtime: {}}
        else:
            log.warning(
                "No JavaScript runtime found for YouTube downloads. "
                "Install node >= 22 or deno >= 2.3, otherwise downloads "
                "will fail with HTTP 403."
            )
        # The default client selection resolves to "android vr", which 403s
        # in this environment. See config.YTDLP_YOUTUBE_CLIENTS for why
        # "web_embedded" is preferred.
        opts["extractor_args"] = {
            "youtube": {"player_client": list(config.YTDLP_YOUTUBE_CLIENTS)}
        }

    if extra:
        opts.update(extra)

    return opts


def _is_youtube(url: str) -> bool:
    """Return ``True`` if *url* points at a YouTube host."""
    if not url:
        return False
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
    except Exception:  # pragma: no cover - malformed URL
        return False
    return "youtube.com" in host or "youtu.be" in host


def detect_js_runtime() -> str | None:
    """Return the name of a usable JavaScript runtime, or ``None``.

    yt-dlp needs a JS runtime to solve YouTube's signature challenges. Node
    must be enabled explicitly (``js_runtimes``); deno is enabled by default
    but only from version 2.3.0, so an older deno is not usable.

    Returns:
        ``"node"``, ``"deno"``, ``"bun"``, ``"quickjs"`` or ``None``.
    """
    # Ordered by preference. node/deno/bun have a minimum version that yt-dlp
    # enforces; quickjs has no version gate here because its binary does not
    # report a parseable version, but note that QuickJS builds older than
    # 2025-04-26 are missing optimisations and can take minutes per solve.
    for name, binary, minimum in (
        ("node", "node", (22, 0, 0)),
        ("deno", "deno", (2, 3, 0)),
        ("bun", "bun", (1, 2, 11)),
        ("quickjs", "qjs", None),
    ):
        path = shutil.which(binary)
        if not path:
            continue
        if minimum is None:
            return name
        version = _binary_version(path)
        if version and version >= minimum:
            return name
        log.debug(
            "%s %s is too old for yt-dlp (needs >= %s)",
            name,
            ".".join(map(str, version)) if version else "?",
            ".".join(map(str, minimum)),
        )
    return None


def _binary_version(path: str) -> tuple[int, ...] | None:
    """Parse the first ``X.Y.Z`` version number printed by *path*."""
    import subprocess

    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout or result.stderr or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _require_ytdlp():
    """Import and return the ``yt_dlp`` module, or raise a clear error."""
    try:
        import yt_dlp  # noqa: F401

        return yt_dlp
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError(
            "yt-dlp is required. Install with: pip install yt-dlp"
        ) from exc


def is_available() -> bool:
    """Return ``True`` if yt-dlp can be imported."""
    try:
        _require_ytdlp()
        return True
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def extract_info(url: str, *, download: bool = False) -> dict[str, Any]:
    """Fetch metadata for *url* without downloading media.

    Args:
        url: Media URL.
        download: Pass ``True`` only when combined with a download option.

    Returns:
        The yt-dlp info dict (``title``, ``duration``, ``uploader``, ...).
    """
    yt_dlp = _require_ytdlp()
    opts = build_ydl_opts(url, extra={"skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=download) or {}


# ---------------------------------------------------------------------------
# Subtitle discovery and download
# ---------------------------------------------------------------------------


def list_subtitles(url: str) -> dict[str, Any]:
    """List available subtitle tracks for *url*.

    Returns:
        ``{"title": str, "manual": {lang: name}, "automatic": {lang: name}}``.
        ``manual`` are human-authored captions; ``automatic`` are
        machine-generated. Manual tracks are higher quality.
    """
    info = extract_info(url)
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    return {
        "title": info.get("title", "Unknown"),
        "duration": info.get("duration", 0),
        "uploader": info.get("uploader", "Unknown"),
        "manual": {lang: _track_label(tracks) for lang, tracks in manual.items()},
        "automatic": {
            lang: _track_label(tracks) for lang, tracks in automatic.items()
        },
    }


def _track_label(tracks: list[dict[str, Any]] | None) -> str:
    """Human-readable label for a subtitle track list (e.g. ``"json3"``)."""
    if not tracks:
        return "unknown"
    exts = [t.get("ext", "?") for t in tracks if isinstance(t, dict)]
    return "/".join(dict.fromkeys(exts)) or "unknown"


def download_subtitles(
    url: str,
    languages: list[str] | None = None,
    *,
    include_automatic: bool = True,
    workdir: str | None = None,
) -> dict[str, Any]:
    """Download subtitle tracks for *url* and parse them into segments.

    Args:
        url: Media URL.
        languages: Language codes to fetch (e.g. ``["en", "ar"]``). ``None``
            or empty means "whatever is available", preferring the video's
            original language.
        include_automatic: Also accept machine-generated captions when no
            human-authored track exists for a language.
        workdir: Directory to write subtitle files into. Defaults to a
            temporary directory that is removed afterwards.

    Returns:
        ``{"title", "duration", "uploader", "tracks": {lang: {...}}}`` where
        each track has ``segments``, ``text``, ``source`` (``"manual"`` or
        ``"automatic"``) and ``ext``. ``tracks`` is empty when the video has
        no captions at all.
    """
    yt_dlp = _require_ytdlp()

    info = extract_info(url)
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}

    wanted = _select_languages(languages, manual, automatic, info)
    if not wanted:
        return {
            "title": info.get("title", "Unknown"),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", "Unknown"),
            "tracks": {},
        }

    # Split the request: languages with human captions vs. auto-only ones.
    manual_langs = [lang for lang in wanted if lang in manual]
    auto_langs = [
        lang for lang in wanted if lang not in manual and lang in automatic
    ]

    tmpdir = workdir or _make_temp_dir()
    created_tmp = workdir is None
    tracks: dict[str, Any] = {}

    try:
        if manual_langs:
            _run_subtitle_download(
                yt_dlp, url, tmpdir, manual_langs, automatic=False
            )
        if auto_langs and include_automatic:
            _run_subtitle_download(
                yt_dlp, url, tmpdir, auto_langs, automatic=True
            )

        for lang in wanted:
            parsed = _read_subtitle_for_lang(tmpdir, lang)
            if not parsed:
                continue
            source = "manual" if lang in manual else "automatic"
            tracks[lang] = {
                "source": source,
                "ext": parsed["ext"],
                "segments": parsed["segments"],
                "text": parsed["text"],
            }
    finally:
        if created_tmp:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return {
        "title": info.get("title", "Unknown"),
        "duration": info.get("duration", 0),
        "uploader": info.get("uploader", "Unknown"),
        "tracks": tracks,
    }


def _select_languages(
    requested: list[str] | None,
    manual: dict[str, Any],
    automatic: dict[str, Any],
    info: dict[str, Any],
) -> list[str]:
    """Decide which subtitle languages to fetch.

    With no explicit request we prefer the video's original language, then
    English, then whatever exists — so a single call still returns something
    useful for non-English videos.
    """
    available = list(manual) + [lang for lang in automatic if lang not in manual]
    if not available:
        return []

    if requested:
        # Normalise "en-US" -> "en" style requests against what exists.
        resolved: list[str] = []
        for lang in requested:
            if lang in available:
                resolved.append(lang)
                continue
            base = lang.split("-")[0]
            match = next(
                (a for a in available if a == base or a.startswith(f"{base}-")),
                None,
            )
            if match:
                resolved.append(match)
        return list(dict.fromkeys(resolved))

    preferred: list[str] = []
    original = info.get("language")
    if original:
        preferred.append(original)
    preferred.extend(["en", "en-US", "en-GB"])
    for lang in preferred:
        if lang in available:
            return [lang]
    return [available[0]]


def _run_subtitle_download(
    yt_dlp: Any,
    url: str,
    tmpdir: str,
    languages: list[str],
    *,
    automatic: bool,
) -> None:
    """Invoke yt-dlp once to write subtitle files for *languages*."""
    opts = build_ydl_opts(
        url,
        outtmpl=os.path.join(tmpdir, "sub"),
        extra={
            "skip_download": True,
            "writesubtitles": not automatic,
            "writeautomaticsub": automatic,
            "subtitleslangs": languages,
            "subtitlesformat": _SUBTITLE_FORMAT_PREFERENCE,
            # Ask yt-dlp to convert to srt when ffmpeg is available; harmless
            # if it is not (the original format is kept).
            "convertsubtitles": "srt" if shutil.which("ffmpeg") else None,
        },
    )
    # ``convertsubtitles: None`` is not a valid yt-dlp value — drop it.
    if opts.get("convertsubtitles") is None:
        opts.pop("convertsubtitles", None)

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def _read_subtitle_for_lang(tmpdir: str, lang: str) -> dict[str, Any] | None:
    """Find and parse the subtitle file yt-dlp wrote for *lang*."""
    for ext in _SUBTITLE_EXTENSIONS:
        # yt-dlp writes "<outtmpl>.<lang>.<ext>"; the lang may carry a region
        # suffix, so match loosely.
        pattern = os.path.join(tmpdir, f"sub*.{lang}*.{ext}")
        matches = sorted(glob.glob(pattern))
        if not matches:
            continue
        path = matches[0]
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                raw = handle.read()
        except OSError:
            continue
        segments = parse_subtitle_text(raw, ext)
        if not segments:
            continue
        return {
            "ext": ext,
            "segments": segments,
            "text": _segments_to_text(segments),
        }
    return None


# ---------------------------------------------------------------------------
# Subtitle parsing
# ---------------------------------------------------------------------------

_VTT_TIMESTAMP_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})"
)


def _parse_timestamp(value: str) -> float:
    """Convert ``HH:MM:SS.mmm`` (or ``,`` decimal) to seconds."""
    value = value.replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
        return float(value)
    except ValueError:
        return 0.0


def parse_subtitle_text(raw: str, ext: str = "") -> list[dict[str, Any]]:
    """Parse subtitle content into ``[{start, end, text}, ...]`` segments.

    Handles YouTube's ``json3`` timed-text format as well as the standard
    ``vtt``/``srt`` cue formats. Unknown formats return an empty list rather
    than raising, so a single unparseable track never fails a whole request.

    Args:
        raw: Raw subtitle file contents.
        ext: File extension, used to pick the parser.

    Returns:
        A list of segments with float ``start``/``end`` and stripped ``text``.
    """
    if not raw or not raw.strip():
        return []

    if ext == "json3" or raw.lstrip().startswith("{"):
        segments = _parse_json3(raw)
        if segments:
            return segments

    return _parse_cue_format(raw)


def _parse_json3(raw: str) -> list[dict[str, Any]]:
    """Parse YouTube's ``json3`` timed-text payload."""
    import json

    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []

    segments: list[dict[str, Any]] = []
    for event in data.get("events") or []:
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(seg.get("utf8", "") for seg in segs).strip()
        if not text:
            continue
        start_ms = event.get("tStartMs", 0)
        duration_ms = event.get("dDurationMs", 0)
        segments.append(
            {
                "start": start_ms / 1000.0,
                "end": (start_ms + duration_ms) / 1000.0,
                "text": text,
            }
        )
    return segments


def _parse_cue_format(raw: str) -> list[dict[str, Any]]:
    """Parse ``vtt``/``srt`` style cue blocks."""
    segments: list[dict[str, Any]] = []
    # Normalise line endings, then split into blocks on blank lines.
    blocks = re.split(r"\r?\n\r?\n", raw.replace("\r\n", "\n"))

    for block in blocks:
        match = _VTT_TIMESTAMP_RE.search(block)
        if not match:
            continue
        start = _parse_timestamp(match.group("start"))
        end = _parse_timestamp(match.group("end"))

        # Everything after the timestamp line is the cue text.
        remainder = block[match.end():]
        lines = [
            line.strip()
            for line in remainder.split("\n")
            if line.strip() and not line.strip().isdigit()
        ]
        text = " ".join(lines)
        text = re.sub(r"<[^>]+>", "", text)  # strip <c>/<i> styling tags
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            segments.append({"start": start, "end": end, "text": text})

    return segments


def _segments_to_text(segments: list[dict[str, Any]]) -> str:
    """Render segments as ``[H:MM:SS] text`` lines."""
    from ..utils.text import format_timestamp

    return "\n".join(
        f"[{format_timestamp(seg['start'])}] {seg['text']}" for seg in segments
    )


# ---------------------------------------------------------------------------
# Media download
# ---------------------------------------------------------------------------


def download_audio(url: str, cache_dir: str) -> dict[str, Any]:
    """Download the best available audio track for *url*.

    Args:
        url: Media URL.
        cache_dir: Directory to write the audio file into.

    Returns:
        ``{"title", "duration", "uploader", "audio_path"}``.
    """
    yt_dlp = _require_ytdlp()
    os.makedirs(cache_dir, exist_ok=True)

    audio_path = os.path.join(cache_dir, "audio_temp")
    _cleanup_prefix(cache_dir, "audio_temp")

    opts = build_ydl_opts(
        url,
        outtmpl=f"{audio_path}.%(ext)s",
        fmt="bestaudio[ext=m4a]/bestaudio",
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}

    actual_path = _find_downloaded_file(cache_dir, "audio_temp")
    if not actual_path:
        raise FileNotFoundError("Failed to download audio.")

    return {
        "title": info.get("title", "Unknown"),
        "duration": info.get("duration", 0),
        "uploader": info.get("uploader", "Unknown"),
        "audio_path": actual_path,
    }


def download_video(
    url: str,
    cache_dir: str,
    *,
    cache_path: str | None = None,
    max_height: int = 480,
    section: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Download a video, optionally only a time section of it.

    Args:
        url: Media URL.
        cache_dir: Directory for the download.
        cache_path: Exact output path. When it already exists and is
            non-empty the download is skipped and only metadata is fetched.
            Callers should encode the requested section in this path so a
            cached section is never reused for a different range.
        max_height: Cap the vertical resolution to keep clips small.
        section: ``(start_seconds, end_seconds)`` to download only that
            range. Requires yt-dlp's ``download_ranges`` support; when the
            range download fails we transparently retry the full video.

    Returns:
        ``{"title", "video_path", "partial"}``. ``partial`` is ``True`` when
        the returned file contains only the requested section, meaning its
        timeline starts at 0 rather than at ``section[0]``.
    """
    yt_dlp = _require_ytdlp()
    os.makedirs(cache_dir, exist_ok=True)

    target = cache_path or os.path.join(cache_dir, "video_temp.mp4")
    want_section = bool(section and config.YTDLP_SECTION_DOWNLOAD)

    if os.path.isfile(target) and os.path.getsize(target) > 0:
        info = extract_info(url)
        return {
            "title": info.get("title", "clip"),
            "video_path": target,
            # The cached file is a section file iff a section was requested
            # for this cache path — the caller keys the path by section.
            "partial": want_section,
        }

    fmt = f"best[ext=mp4][height<={max_height}]/best[ext=mp4]/best"
    extra: dict[str, Any] = {}

    if want_section:
        start, end = section  # type: ignore[misc]
        extra["download_ranges"] = _make_range_callback(start, end)
        # Cut on keyframes so the clip starts cleanly.
        extra["force_keyframes_at_cuts"] = True

    opts = build_ydl_opts(url, outtmpl=target, fmt=fmt, extra=extra)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
        partial = want_section
    except Exception as exc:
        if not extra:
            raise
        # Range download is an optimisation — fall back to the full video.
        log.warning("Section download failed (%s); retrying full video", exc)
        opts = build_ydl_opts(url, outtmpl=target, fmt=fmt)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
        partial = False

    if not os.path.isfile(target):
        found = _find_downloaded_file(cache_dir, os.path.basename(target))
        if not found:
            raise FileNotFoundError("Failed to download video.")
        target = found

    return {
        "title": info.get("title", "clip"),
        "video_path": target,
        "partial": partial,
    }


def _make_range_callback(start: float, end: float) -> Callable:
    """Build the ``download_ranges`` callback yt-dlp expects.

    yt-dlp calls this with ``(info_dict, ydl)`` and expects an iterable of
    ``{"start_time": float, "end_time": float}`` dicts.
    """

    def _ranges(_info: dict[str, Any], _ydl: Any):
        return [{"start_time": start, "end_time": end}]

    return _ranges


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_temp_dir() -> str:
    """Create a temporary directory for subtitle downloads."""
    import tempfile

    return tempfile.mkdtemp(prefix="gsmcp-subs-")


def _cleanup_prefix(directory: str, prefix: str) -> None:
    """Remove leftover files starting with *prefix*."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return
    for name in entries:
        if name.startswith(prefix):
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass


def _find_downloaded_file(directory: str, prefix: str) -> str | None:
    """Return the first file in *directory* whose name starts with *prefix*."""
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return None
    for name in entries:
        if name.startswith(prefix):
            path = os.path.join(directory, name)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
    return None


def ytdlp_version() -> str | None:
    """Return the installed yt-dlp version, or ``None`` if unavailable."""
    try:
        yt_dlp = _require_ytdlp()
        return getattr(yt_dlp.version, "__version__", None)
    except Exception:
        return None


def ffmpeg_available() -> bool:
    """Return ``True`` if an ``ffmpeg`` binary is on ``PATH``."""
    return shutil.which("ffmpeg") is not None
