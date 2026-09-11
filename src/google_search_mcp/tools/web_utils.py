"""Web utilities — page fetching, documents, email, pastebin, QR, archiving, Wikipedia, S3.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
import imaplib
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from email import policy as email_policy
from email.parser import BytesParser as EmailParser
from pathlib import Path

from mcp.server.fastmcp import Context, Image

from ..config import IMAP_SERVERS, MAX_PAGE_CHARS
from ..server import (
    mcp,
    _fetch_url_bytes,
    format_error,
    launch_browser,
    strip_html,
)
from ..utils.text import format_timestamp
from playwright.async_api import async_playwright


async def _fetch_page_text(url: str) -> str:
    """Fetch a URL with headless Chromium and extract readable text."""
    async with async_playwright() as pw:
        context = await launch_browser(pw)
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            text = await page.evaluate("""
                () => {
                    const remove = document.querySelectorAll(
                        'script, style, nav, footer, header, iframe, noscript, '
                        + 'svg, [role="navigation"], [role="banner"], '
                        + '[role="complementary"], .sidebar, .ad, .ads, .advertisement'
                    );
                    remove.forEach(el => el.remove());

                    const article = document.querySelector(
                        'article, main, [role="main"], .post-content, .article-body, '
                        + '.entry-content, .content, #content'
                    );
                    const source = article || document.body;
                    return source ? source.innerText : '';
                }
            """)

            text = re.sub(r'\n{3,}', '\n\n', text).strip()

            if not text:
                return f"Could not extract text content from: {url}"

            if len(text) > MAX_PAGE_CHARS:
                text = text[:MAX_PAGE_CHARS] + f"\n\n... [truncated, showing first {MAX_PAGE_CHARS} characters]"

            return f"Content from: {url}\n\n{text}"

        except Exception as e:
            return f"Failed to fetch {url}: {e}"

        finally:
            await context.close()


@mcp.tool()
async def visit_page(url: str) -> str:
    """Fetch a web page and return its text content. Use this after google_search to read the actual content of a result.

    Sample prompts that trigger this tool:
        - "Read this article for me: https://example.com/article"
        - "What does this page say? https://..."
        - "Summarize the content at this URL"
        - "Go to this link and tell me what it says"

    Args:
        url: The full URL to visit and extract text from.
    """
    return await _fetch_page_text(url)


# ---------------------------------------------------------------------------
# Local file transcription — audio & video files directly, no download
# ---------------------------------------------------------------------------


@mcp.tool()
async def transcribe_local(
    file_path: str,
    model_size: str = "tiny",
    language: str = "",
    ctx: Context = None,
) -> str:
    """Transcribe a local audio or video file with timestamps using Whisper.

    Supports any format FFmpeg can decode: mp3, wav, m4a, flac, ogg, aac,
    mp4, mkv, webm, avi, mov, wma, opus, and more.

    Results are cached — repeat requests for the same file are instant.

    Sample prompts that trigger this tool:
        - "Transcribe this recording: /path/to/meeting.mp3"
        - "What's said in this video? /path/to/lecture.mp4"
        - "Transcribe ~/Downloads/interview.wav"
        - "Transcribe the audio file on my desktop"

    Args:
        file_path: Absolute path to the audio or video file.
        model_size: Whisper model size (tiny/base/small/medium/large). Default: tiny.
        language: Language code (e.g. "en", "de", "fr"). Auto-detected if empty.
    """
    file_path = os.path.expanduser(file_path)
    if not os.path.isfile(file_path):
        return f"File not found: {file_path}"

    valid_sizes = ("tiny", "base", "small", "medium", "large")
    if model_size not in valid_sizes:
        model_size = "tiny"

    # Disk cache keyed on absolute path + model size
    os.makedirs(TRANSCRIPT_CACHE_DIR, exist_ok=True)
    abs_path = os.path.abspath(file_path)
    cache_path = _transcript_cache_path(abs_path, model_size)
    if os.path.isfile(cache_path):
        try:
            with open(cache_path) as f:
                return json.load(f)["transcript"] + "\n\n(cached result)"
        except Exception:
            pass

    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        return "faster-whisper is required. Install with: pip install faster-whisper"

    if ctx:
        await ctx.report_progress(
            progress=0, total=100, message="Transcribing (this may take a minute)...",
        )

    try:
        whisper_result = await asyncio.to_thread(
            _transcribe_audio, file_path, model_size, language,
        )
    except Exception as e:
        return format_error("Local transcription", e)

    segments = whisper_result["segments"]
    if not segments:
        return f"No speech detected in: {os.path.basename(file_path)}"

    if ctx:
        await ctx.report_progress(progress=100, total=100, message="Done!")

    filename = os.path.basename(file_path)
    full_lines = [
        "Transcript",
        f"File: {filename}",
        f"Language: {whisper_result['language']} "
        f"(confidence: {whisper_result['language_probability']:.0%})",
        "",
        "--- Transcript ---",
    ]
    for seg in segments:
        start = format_timestamp(seg["start"])
        end = format_timestamp(seg["end"])
        full_lines.append(f"[{start} - {end}] {seg['text']}")

    full_lines.append("")
    full_lines.append("--- End of Transcript ---")
    full_lines.append(f"Total segments: {len(segments)}")

    full_transcript = "\n".join(full_lines)

    try:
        with open(cache_path, "w") as f:
            json.dump({"url": abs_path, "transcript": full_transcript}, f)
    except Exception:
        pass

    return full_transcript


# ---------------------------------------------------------------------------
# Media format conversion — FFmpeg wrapper
# ---------------------------------------------------------------------------


@mcp.tool()
async def convert_media(
    input_path: str,
    output_format: str,
    output_path: str = "",
    quality: str = "medium",
    ctx: Context = None,
) -> str:
    """Convert audio or video files between formats using FFmpeg.

    Supports all FFmpeg formats: mp3, wav, m4a, flac, ogg, aac, opus,
    mp4, mkv, webm, avi, mov, gif, and more.

    Common conversions:
        - Video to audio: mp4 -> mp3
        - Audio formats: wav -> mp3, flac -> m4a
        - Video formats: mkv -> mp4, mp4 -> webm
        - Video to GIF: mp4 -> gif

    Sample prompts that trigger this tool:
        - "Convert this video to mp3: /path/to/video.mp4"
        - "Convert recording.wav to mp3"
        - "Make a gif from /path/to/clip.mp4"
        - "Convert this to m4a: /path/to/song.flac"
        - "Convert my video to webm"

    Args:
        input_path: Path to the input file.
        output_format: Target format (e.g. "mp3", "mp4", "wav", "gif").
        output_path: Optional output file path. Default: same name, new extension.
        quality: "low", "medium", or "high". Default: medium.
    """
    input_path = os.path.expanduser(input_path)
    if not os.path.isfile(input_path):
        return f"File not found: {input_path}"

    # Check ffmpeg availability
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-version"],
            capture_output=True, timeout=5,
        )
        if proc.returncode != 0:
            return (
                "FFmpeg not found. Install with:\n"
                "  Linux: sudo apt install ffmpeg\n"
                "  Mac: brew install ffmpeg\n"
                "  Windows: choco install ffmpeg"
            )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return (
            "FFmpeg not found. Install with:\n"
            "  Linux: sudo apt install ffmpeg\n"
            "  Mac: brew install ffmpeg\n"
            "  Windows: choco install ffmpeg"
        )

    output_format = output_format.lower().strip().lstrip(".")

    if not output_path:
        base = os.path.splitext(input_path)[0]
        output_path = f"{base}.{output_format}"
        if os.path.abspath(output_path) == os.path.abspath(input_path):
            output_path = f"{base}_converted.{output_format}"
    else:
        output_path = os.path.expanduser(output_path)

    quality_presets = {
        "low": {"ab": "96k", "crf": "28"},
        "medium": {"ab": "192k", "crf": "23"},
        "high": {"ab": "320k", "crf": "18"},
    }
    q = quality_presets.get(quality, quality_presets["medium"])

    audio_fmts = {"mp3", "wav", "m4a", "flac", "ogg", "aac", "wma", "opus"}

    cmd = ["ffmpeg", "-i", input_path, "-y"]

    if output_format in audio_fmts:
        cmd.extend(["-vn", "-b:a", q["ab"]])
    elif output_format == "gif":
        cmd.extend(["-vf", "fps=10,scale=480:-1:flags=lanczos", "-loop", "0"])
    else:
        cmd.extend(["-crf", q["crf"], "-preset", "fast"])

    cmd.append(output_path)

    if ctx:
        await ctx.report_progress(progress=0, total=100, message="Converting...")

    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd,
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return "Conversion timed out (10 minute limit)."

    if proc.returncode != 0:
        err = proc.stderr[-500:] if proc.stderr else "Unknown error"
        return f"FFmpeg error:\n{err}"

    if not os.path.isfile(output_path):
        return "Conversion failed — output file was not created."

    size_mb = os.path.getsize(output_path) / (1024 * 1024)

    if ctx:
        await ctx.report_progress(progress=100, total=100, message="Done!")

    return (
        f"Converted successfully.\n"
        f"Output: {output_path}\n"
        f"Size: {size_mb:.1f} MB"
    )


# ---------------------------------------------------------------------------
# Document reader — PDF, DOCX, plain text, HTML
# ---------------------------------------------------------------------------


def _read_pdf_text(file_path: str) -> str:
    """Extract text from a PDF. Tries pdftotext first, falls back to OCR."""
    # Attempt 1: pdftotext (poppler-utils) — fast, accurate for text PDFs
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", file_path, "-"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Attempt 2: OCR via our existing pipeline (for scanned PDFs)
    try:
        from rapidocr_onnxruntime import RapidOCR
        import cv2
        import tempfile

        # Convert PDF pages to images via pdftoppm
        with tempfile.TemporaryDirectory() as tmpdir:
            img_proc = subprocess.run(
                ["pdftoppm", "-png", "-r", "200", file_path, os.path.join(tmpdir, "page")],
                capture_output=True, timeout=120,
            )
            if img_proc.returncode != 0:
                return ""

            ocr = RapidOCR()
            all_text: list[str] = []
            for img_file in sorted(Path(tmpdir).glob("*.png")):
                result, _ = ocr(str(img_file))
                if result:
                    page_text = "\n".join(line[1] for line in result)
                    all_text.append(page_text)

            if all_text:
                return "\n\n--- Page Break ---\n\n".join(all_text)
    except Exception:
        pass

    return ""


def _read_docx_text(file_path: str) -> str:
    """Extract text from a .docx file using stdlib zipfile + XML parsing."""
    try:
        with zipfile.ZipFile(file_path) as z:
            xml_data = z.read("word/document.xml")
        root = ET.fromstring(xml_data)
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        paragraphs: list[str] = []
        for para in root.iter(f"{{{ns}}}p"):
            texts: list[str] = []
            for run in para.iter(f"{{{ns}}}t"):
                if run.text:
                    texts.append(run.text)
            if texts:
                paragraphs.append("".join(texts))
        return "\n".join(paragraphs)
    except Exception as e:
        return f"Failed to read DOCX: {e}"


@mcp.tool()
async def read_document(
    file_path: str,
    ctx: Context = None,
) -> str:
    """Read and extract text from documents — PDF, Word, and plain text files.

    Supported formats:
        - PDF (.pdf) — text extraction with pdftotext, OCR fallback for scans
        - Word (.docx) — paragraph and table text extraction (no extra deps)
        - Plain text (.txt, .md, .csv, .log, .json, .xml, .yaml, .yml, .ini, .cfg, .toml)
        - HTML (.html, .htm) — strips tags, returns clean text

    Sample prompts that trigger this tool:
        - "Read this PDF: /path/to/document.pdf"
        - "What does this document say? /path/to/report.docx"
        - "Extract text from /path/to/scanned.pdf"
        - "Read the CSV at /path/to/data.csv"
        - "Show me the contents of config.yaml"

    Args:
        file_path: Absolute path to the document file.
    """
    file_path = os.path.expanduser(file_path)
    if not os.path.isfile(file_path):
        return f"File not found: {file_path}"

    ext = Path(file_path).suffix.lower()
    filename = os.path.basename(file_path)
    size_kb = os.path.getsize(file_path) / 1024

    if ctx:
        await ctx.report_progress(progress=0, total=100, message=f"Reading {filename}...")

    # --- PDF ---
    if ext == ".pdf":
        text = await asyncio.to_thread(_read_pdf_text, file_path)
        if not text:
            return (
                f"Could not extract text from {filename}.\n"
                "For text PDFs, install poppler-utils: sudo apt install poppler-utils\n"
                "For scanned PDFs, ensure rapidocr-onnxruntime is installed."
            )
        return f"Document: {filename} ({size_kb:.0f} KB)\n\n{text}"

    # --- DOCX ---
    if ext == ".docx":
        text = await asyncio.to_thread(_read_docx_text, file_path)
        return f"Document: {filename} ({size_kb:.0f} KB)\n\n{text}"

    # --- HTML ---
    if ext in (".html", ".htm"):
        try:
            raw = await asyncio.to_thread(Path(file_path).read_text, "utf-8")
        except UnicodeDecodeError:
            raw = await asyncio.to_thread(
                Path(file_path).read_text, "latin-1",
            )
        text = strip_html(raw)
        return f"Document: {filename} ({size_kb:.0f} KB)\n\n{text}"

    # --- Plain text formats ---
    plain_exts = {
        ".txt", ".md", ".csv", ".log", ".json", ".xml",
        ".yaml", ".yml", ".ini", ".cfg", ".toml", ".conf",
        ".sh", ".bash", ".zsh", ".py", ".js", ".ts", ".go",
        ".rs", ".c", ".cpp", ".h", ".java", ".kt", ".rb",
        ".sql", ".r", ".m", ".swift", ".env",
    }
    if ext in plain_exts:
        try:
            raw = await asyncio.to_thread(Path(file_path).read_text, "utf-8")
        except UnicodeDecodeError:
            raw = await asyncio.to_thread(
                Path(file_path).read_text, "latin-1",
            )
        # Truncate very large files to avoid flooding the LLM context
        if len(raw) > 100_000:
            raw = raw[:100_000] + f"\n\n... (truncated at 100 KB, file is {size_kb:.0f} KB)"
        return f"Document: {filename} ({size_kb:.0f} KB)\n\n{raw}"

    return f"Unsupported file format: {ext}. Supported: .pdf, .docx, .html, .txt, .md, .csv, .json, .xml, .yaml, and more."


# ---------------------------------------------------------------------------
# Email — IMAP fetch (stdlib, works with Gmail/Outlook/Yahoo/any IMAP)
# ---------------------------------------------------------------------------

IMAP_SERVERS: dict[str, str] = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "imap-mail.outlook.com",
    "hotmail.com": "imap-mail.outlook.com",
    "live.com": "imap-mail.outlook.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "aol.com": "imap.aol.com",
    "zoho.com": "imap.zoho.com",
    "protonmail.com": "127.0.0.1",  # needs ProtonMail Bridge
    "proton.me": "127.0.0.1",
}


@mcp.tool()
async def fetch_emails(
    email_address: str,
    password: str,
    imap_server: str = "",
    folder: str = "INBOX",
    search: str = "UNSEEN",
    limit: int = 10,
    ctx: Context = None,
) -> str:
    """Fetch emails via IMAP. Works with Gmail, Outlook, Yahoo, iCloud, and any IMAP server.

    For Gmail: use an App Password (not your regular password).
    Generate at: https://myaccount.google.com/apppasswords

    For Outlook: enable IMAP in settings, use your regular password or app password.

    Sample prompts that trigger this tool:
        - "Check my email: user@gmail.com password: xxxx-xxxx-xxxx-xxxx"
        - "Fetch unread emails from my Gmail"
        - "Search my inbox for emails about invoice"
        - "Get my latest 5 emails"
        - "Show emails from sender@example.com"

    Args:
        email_address: Your email address.
        password: Password or app password (Gmail requires app password).
        imap_server: IMAP server hostname. Auto-detected for Gmail/Outlook/Yahoo if empty.
        folder: Mailbox folder. Default: INBOX. Common: INBOX, Sent, Drafts, Trash, Spam.
        search: IMAP search criteria. Default: UNSEEN (unread).
            Examples: ALL, SEEN, UNSEEN, FROM "sender@example.com",
            SUBJECT "keyword", SINCE "01-Jan-2024", BEFORE "01-Feb-2024".
        limit: Maximum number of emails to fetch. Default: 10.
    """
    # Auto-detect IMAP server from email domain
    if not imap_server:
        domain = email_address.split("@")[-1].lower()
        imap_server = IMAP_SERVERS.get(domain, "")
        if not imap_server:
            return (
                f"Cannot auto-detect IMAP server for '{domain}'.\n"
                f"Please provide the imap_server parameter "
                f"(e.g. 'imap.{domain}')."
            )

    def _fetch_sync() -> list[dict]:
        mail = imaplib.IMAP4_SSL(imap_server)
        try:
            mail.login(email_address, password)
            mail.select(folder, readonly=True)

            _, msg_nums = mail.search(None, search)
            ids = msg_nums[0].split()
            if not ids:
                return []

            # Most recent first, capped at limit
            ids = list(reversed(ids[-limit:]))
            parser = EmailParser(policy=email_policy.default)

            emails: list[dict] = []
            for mid in ids:
                _, data = mail.fetch(mid, "(RFC822)")
                if not data or not data[0] or not isinstance(data[0], tuple):
                    continue
                msg = parser.parsebytes(data[0][1])

                # Extract body — prefer plain text
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct == "text/plain":
                            payload = part.get_content()
                            if isinstance(payload, str):
                                body = payload
                                break
                    if not body:
                        for part in msg.walk():
                            ct = part.get_content_type()
                            if ct == "text/html":
                                payload = part.get_content()
                                if isinstance(payload, str):
                                    body = strip_html(payload)
                                    break
                else:
                    payload = msg.get_content()
                    if isinstance(payload, str):
                        ct = msg.get_content_type()
                        body = strip_html(payload) if ct == "text/html" else payload

                emails.append({
                    "from": str(msg.get("From", "")),
                    "to": str(msg.get("To", "")),
                    "subject": str(msg.get("Subject", "(no subject)")),
                    "date": str(msg.get("Date", "")),
                    "body": body.strip()[:2000],
                })

            return emails
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    if ctx:
        await ctx.report_progress(
            progress=0, total=100, message=f"Connecting to {imap_server}...",
        )

    try:
        emails = await asyncio.to_thread(_fetch_sync)
    except imaplib.IMAP4.error as e:
        err = str(e)
        if "AUTHENTICATIONFAILED" in err.upper() or "LOGIN" in err.upper():
            return (
                f"Authentication failed for {email_address}.\n"
                "For Gmail, make sure you're using an App Password:\n"
                "  https://myaccount.google.com/apppasswords"
            )
        return f"IMAP error: {e}"
    except Exception as e:
        return format_error("Email connection", e)

    if not emails:
        return f"No emails found matching '{search}' in {folder}."

    if ctx:
        await ctx.report_progress(progress=100, total=100, message="Done!")

    lines = [f"Emails ({len(emails)} results from {folder})\n"]
    for i, em in enumerate(emails, 1):
        lines.append(f"{i}. {em['subject']}")
        lines.append(f"   From: {em['from']}")
        lines.append(f"   Date: {em['date']}")
        if em["body"]:
            preview = em["body"][:300].replace("\n", " ")
            lines.append(f"   {preview}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pastebin — post text to dpaste.org (no auth, no API key)
# ---------------------------------------------------------------------------


@mcp.tool()
async def paste_text(
    content: str,
    title: str = "",
    syntax: str = "text",
    expiry_days: int = 7,
    ctx: Context = None,
) -> str:
    """Post text to dpaste.org and return a shareable URL.

    Great for sharing code, logs, configs, or any text output.
    No account or API key needed. Pastes expire automatically.

    Sample prompts that trigger this tool:
        - "Paste this code and give me a link"
        - "Upload this log to a pastebin"
        - "Share this config file online"
        - "Create a paste with this error output"

    Args:
        content: The text content to paste.
        title: Optional title for the paste.
        syntax: Syntax highlighting (e.g. "python", "json", "bash"). Default: text.
        expiry_days: Days until the paste expires (1-365). Default: 7.
    """
    if not content.strip():
        return "Nothing to paste — content is empty."

    expiry_days = max(1, min(365, expiry_days))

    def _post() -> str:
        import urllib.parse
        errors: list[str] = []

        # 1. paste.rs (simple, reliable)
        try:
            data = content.encode("utf-8")
            req = urllib.request.Request(
                "https://paste.rs/", data=data,
                headers={"User-Agent": "Mozilla/5.0", "Content-Type": "text/plain"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                url = resp.read().decode().strip()
                if url.startswith("http"):
                    return url
        except Exception as e:
            errors.append(f"paste.rs: {e}")

        # 2. dpaste.com
        try:
            data = urllib.parse.urlencode({
                "content": content, "syntax": syntax,
                "expiry_days": str(expiry_days),
            }).encode()
            req = urllib.request.Request(
                "https://dpaste.com/api/v2/", data=data,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                url = resp.read().decode().strip()
                if url.startswith("http"):
                    return url
        except Exception as e:
            errors.append(f"dpaste.com: {e}")

        # 3. dpaste.org (fallback)
        try:
            data = urllib.parse.urlencode({
                "content": content, "title": title,
                "syntax": syntax, "expiry_days": str(expiry_days),
            }).encode()
            req = urllib.request.Request(
                "https://dpaste.org/api/", data=data,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                url = resp.read().decode().strip().strip('"')
                if url.startswith("http"):
                    return url
        except Exception as e:
            errors.append(f"dpaste.org: {e}")

        raise RuntimeError("All paste services failed: " + "; ".join(errors))

    try:
        url = await asyncio.to_thread(_post)
    except Exception as e:
        return f"Failed to create paste: {e}"

    return f"Paste created: {url}\nExpires in {expiry_days} days."


# ---------------------------------------------------------------------------
# URL shortener — TinyURL (no auth, no API key)
# ---------------------------------------------------------------------------


@mcp.tool()
async def shorten_url(
    url: str,
    ctx: Context = None,
) -> str:
    """Shorten a long URL using TinyURL. No account or API key needed.

    Sample prompts that trigger this tool:
        - "Shorten this URL: https://very-long-url.com/path/..."
        - "Give me a short link for this"
        - "Create a tinyurl for https://..."

    Args:
        url: The URL to shorten.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    def _shorten() -> str:
        api_url = f"https://tinyurl.com/api-create.php?url={urllib.request.quote(url, safe='')}"
        req = urllib.request.Request(
            api_url, headers={"User-Agent": "NoAPI-MCP/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode().strip()

    try:
        short = await asyncio.to_thread(_shorten)
    except Exception as e:
        return f"Failed to shorten URL: {e}"

    return f"Short URL: {short}\nOriginal: {url}"


# ---------------------------------------------------------------------------
# QR code generator — OpenCV (already a dependency)
# ---------------------------------------------------------------------------


@mcp.tool()
async def generate_qr(
    data: str,
    output_path: str = "",
    size: int = 400,
    ctx: Context = None,
) -> str:
    """Generate a QR code image from text, URLs, Wi-Fi credentials, or any data.

    Use cases:
        - URLs: shareable links, payment pages
        - Wi-Fi: WIFI:T:WPA;S:NetworkName;P:password;;
        - Contact info: vCard format
        - Plain text: any message

    Sample prompts that trigger this tool:
        - "Generate a QR code for https://example.com"
        - "Create a QR code for my Wi-Fi: SSID=MyNet, password=secret123"
        - "Make a QR code with this text"
        - "QR code for my Bitcoin address"

    Args:
        data: The content to encode in the QR code.
        output_path: Optional output file path. Default: ~/qr_code.png.
        size: Image size in pixels (width=height). Default: 400.
    """
    if not data.strip():
        return "Nothing to encode — data is empty."

    try:
        import cv2
        import numpy as np
    except ImportError:
        return "OpenCV is required. Install with: pip install opencv-python-headless"

    if not output_path:
        output_path = os.path.join(os.path.expanduser("~"), "qr_code.png")
    else:
        output_path = os.path.expanduser(output_path)

    def _generate() -> str:
        encoder = cv2.QRCodeEncoder.create()
        qr_img = encoder.encode(data)
        if qr_img is None or qr_img.size == 0:
            raise ValueError("QR encoding failed — data may be too long.")
        # Resize to requested size
        h, w = qr_img.shape[:2]
        scale = max(size // w, 1)
        resized = cv2.resize(
            qr_img, (w * scale, h * scale),
            interpolation=cv2.INTER_NEAREST,
        )
        cv2.imwrite(output_path, resized)
        return output_path

    try:
        path = await asyncio.to_thread(_generate)
    except Exception as e:
        return format_error("QR code generation", e)

    return f"QR code saved to: {path}\nData: {data[:100]}{'...' if len(data) > 100 else ''}"


# ---------------------------------------------------------------------------
# Archive.is — save a webpage snapshot
# ---------------------------------------------------------------------------


@mcp.tool()
async def archive_webpage(
    url: str,
    ctx: Context = None,
) -> str:
    """Archive a webpage on archive.today (archive.is) for permanent preservation.

    Creates a timestamped snapshot of any webpage. Useful for preserving:
        - News articles before they're edited or deleted
        - Social media posts
        - Product pages with specific prices
        - Any web content you want to reference later

    Sample prompts that trigger this tool:
        - "Archive this page: https://example.com/article"
        - "Save this webpage to archive.is"
        - "Preserve this article before it gets taken down"
        - "Create an archive snapshot of this URL"

    Args:
        url: The URL of the webpage to archive.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    def _archive() -> str:
        # First check if already archived
        check_url = f"https://archive.org/wayback/available?url={urllib.request.quote(url, safe='')}"
        req = urllib.request.Request(
            check_url, headers={"User-Agent": "NoAPI-MCP/1.0"},
        )
        existing = ""
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                snap = data.get("archived_snapshots", {}).get("closest", {})
                if snap.get("available"):
                    existing = snap["url"]
        except Exception:
            pass

        # Submit to Wayback Machine Save Page Now
        save_url = f"https://web.archive.org/save/{url}"
        req = urllib.request.Request(
            save_url,
            headers={"User-Agent": "NoAPI-MCP/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                final_url = resp.url
                if "web.archive.org" in final_url:
                    return final_url
        except Exception:
            pass

        # Fallback: return existing archive if save failed
        if existing:
            return existing

        # Final fallback: return the Wayback Machine URL pattern
        return f"https://web.archive.org/web/*/{url}"

    if ctx:
        await ctx.report_progress(
            progress=0, total=100, message="Submitting to archive...",
        )

    try:
        archive_url = await asyncio.to_thread(_archive)
    except Exception as e:
        return format_error("Webpage archiving", e)

    return (
        f"Archived: {archive_url}\n"
        f"Original: {url}"
    )


# ---------------------------------------------------------------------------
# Wikipedia — article lookup (no API key)
# ---------------------------------------------------------------------------


@mcp.tool()
async def wikipedia(
    query: str,
    language: str = "en",
    sentences: int = 0,
    ctx: Context = None,
) -> str:
    """Look up a Wikipedia article and return its content.

    Returns the article summary or full text. Supports all Wikipedia languages.

    Sample prompts that trigger this tool:
        - "Wikipedia: quantum computing"
        - "Look up Albert Einstein on Wikipedia"
        - "What does Wikipedia say about the French Revolution?"
        - "Get the Wikipedia article for Python programming language"
        - "Wikipedia en español: inteligencia artificial"

    Args:
        query: The topic to search for.
        language: Wikipedia language code (e.g. "en", "de", "fr", "es", "ja"). Default: en.
        sentences: Number of sentences for summary (0 = full article extract). Default: 0.
    """
    if not query.strip():
        return "No query provided."

    def _fetch() -> dict:
        # Search for the best matching article
        search_url = (
            f"https://{language}.wikipedia.org/api/rest_v1/page/summary/"
            f"{urllib.request.quote(query.replace(' ', '_'))}"
        )
        req = urllib.request.Request(
            search_url,
            headers={"User-Agent": "NoAPI-MCP/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.request.HTTPError:
            # Try search API as fallback
            search_api = (
                f"https://{language}.wikipedia.org/w/api.php?"
                f"action=opensearch&search={urllib.request.quote(query)}"
                f"&limit=1&format=json"
            )
            req2 = urllib.request.Request(
                search_api,
                headers={"User-Agent": "NoAPI-MCP/1.0"},
            )
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                results = json.loads(resp2.read())
                if results[1]:
                    title = results[1][0]
                    # Retry with the correct title
                    retry_url = (
                        f"https://{language}.wikipedia.org/api/rest_v1/page/summary/"
                        f"{urllib.request.quote(title.replace(' ', '_'))}"
                    )
                    req3 = urllib.request.Request(
                        retry_url,
                        headers={"User-Agent": "NoAPI-MCP/1.0"},
                    )
                    with urllib.request.urlopen(req3, timeout=10) as resp3:
                        return json.loads(resp3.read())
                return {}

    try:
        data = await asyncio.to_thread(_fetch)
    except Exception as e:
        return format_error("Wikipedia lookup", e)

    if not data or data.get("type") == "not_found":
        return f"No Wikipedia article found for: {query}"

    title = data.get("title", query)
    extract = data.get("extract", "")
    page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
    description = data.get("description", "")

    if not extract:
        return f"No content found for: {query}"

    if sentences > 0:
        parts = extract.split(". ")
        extract = ". ".join(parts[:sentences])
        if not extract.endswith("."):
            extract += "."

    lines = [f"Wikipedia: {title}"]
    if description:
        lines.append(f"({description})")
    lines.append("")
    lines.append(extract)
    if page_url:
        lines.append(f"\nSource: {page_url}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MinIO / S3-compatible object storage upload
# ---------------------------------------------------------------------------


@mcp.tool()
async def upload_to_s3(
    file_path: str,
    bucket: str,
    key: str = "",
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    ctx: Context = None,
) -> str:
    """Upload a file to MinIO, AWS S3, or any S3-compatible storage.

    Works with MinIO (self-hosted), AWS S3, DigitalOcean Spaces,
    Backblaze B2, Cloudflare R2, and any S3-compatible service.

    Credentials can be passed directly or read from environment variables:
        AWS_ENDPOINT_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY

    Sample prompts that trigger this tool:
        - "Upload report.pdf to my MinIO bucket"
        - "Upload this file to S3 bucket my-bucket"
        - "Store backup.tar.gz in MinIO at backup-bucket/daily/"
        - "Upload to my DigitalOcean Space"

    Args:
        file_path: Local file to upload.
        bucket: Bucket name.
        key: Object key (path in bucket). Default: filename.
        endpoint: S3 endpoint URL (e.g. "http://localhost:9000" for MinIO).
            Falls back to AWS_ENDPOINT_URL env var, then AWS S3 default.
        access_key: Access key. Falls back to AWS_ACCESS_KEY_ID env var.
        secret_key: Secret key. Falls back to AWS_SECRET_ACCESS_KEY env var.
    """
    file_path = os.path.expanduser(file_path)
    if not os.path.isfile(file_path):
        return f"File not found: {file_path}"

    if not key:
        key = os.path.basename(file_path)

    endpoint = endpoint or os.environ.get("AWS_ENDPOINT_URL", "")
    access_key = access_key or os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret_key = secret_key or os.environ.get("AWS_SECRET_ACCESS_KEY", "")

    if not access_key or not secret_key:
        return (
            "Missing credentials. Provide access_key/secret_key or set env vars:\n"
            "  export AWS_ACCESS_KEY_ID=your-key\n"
            "  export AWS_SECRET_ACCESS_KEY=your-secret\n"
            "  export AWS_ENDPOINT_URL=http://localhost:9000  (for MinIO)"
        )

    # Use AWS CLI or mc (MinIO Client) — check what's available
    def _upload() -> str:
        # Try MinIO client (mc) first
        mc_path = None
        for name in ("mc", "mcli"):
            try:
                r = subprocess.run(
                    [name, "--version"], capture_output=True, timeout=5,
                )
                if r.returncode == 0:
                    mc_path = name
                    break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

        if mc_path:
            # Configure alias and upload
            alias = "noapi_tmp"
            ep = endpoint or "https://s3.amazonaws.com"
            subprocess.run(
                [mc_path, "alias", "set", alias, ep, access_key, secret_key],
                capture_output=True, timeout=10,
            )
            r = subprocess.run(
                [mc_path, "cp", file_path, f"{alias}/{bucket}/{key}"],
                capture_output=True, text=True, timeout=300,
            )
            # Clean up alias
            subprocess.run(
                [mc_path, "alias", "remove", alias],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                return f"s3://{bucket}/{key}"
            raise RuntimeError(r.stderr or "mc upload failed")

        # Fallback: AWS CLI
        try:
            cmd = ["aws", "s3", "cp", file_path, f"s3://{bucket}/{key}"]
            env = os.environ.copy()
            env["AWS_ACCESS_KEY_ID"] = access_key
            env["AWS_SECRET_ACCESS_KEY"] = secret_key
            if endpoint:
                cmd.extend(["--endpoint-url", endpoint])
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300, env=env,
            )
            if r.returncode == 0:
                return f"s3://{bucket}/{key}"
            raise RuntimeError(r.stderr or "aws cli upload failed")
        except FileNotFoundError:
            pass

        return ""

    if ctx:
        await ctx.report_progress(progress=0, total=100, message="Uploading...")

    try:
        result = await asyncio.to_thread(_upload)
    except Exception as e:
        return format_error("S3 upload", e)

    if not result:
        return (
            "No S3 client found. Install one of:\n"
            "  MinIO Client: https://min.io/docs/minio/linux/reference/minio-mc.html\n"
            "  AWS CLI: pip install awscli"
        )

    size_mb = os.path.getsize(file_path) / (1024 * 1024)

    if ctx:
        await ctx.report_progress(progress=100, total=100, message="Done!")

    return (
        f"Uploaded successfully.\n"
        f"Location: {result}\n"
        f"Size: {size_mb:.1f} MB"
    )
