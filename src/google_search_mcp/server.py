"""Google Search MCP Server — 38 tools for local LLMs.

Provides real Google search, live feeds, vision, OCR, video transcription,
email, document reading, and web utilities — all running locally through
headless Chromium and open-source ML models. No API keys required.
"""

import asyncio
import hashlib
import imaplib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email import policy as email_policy
from email.parser import BytesParser as EmailParser
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus, unquote, urlparse, parse_qs

from pydantic import AliasChoices, Field

from mcp.server.fastmcp import Context, FastMCP, Image
from playwright.async_api import async_playwright

from .config import (
    CAPTCHA_CLASS_MAP,
    CLIPS_DIR,
    COOKIE_DIR,
    COOKIE_JSON_PATH,
    ENABLE_MANUAL_INTERVENTION,
    FEEDS_DB_PATH,
    IMAGENET_MEAN,
    IMAGENET_STD,
    IMAGE_EXTENSIONS,
    DEFAULT_IMAGE_DIR,
    LANGUAGE_CODES,
    LANG_DETECTION_CONFIDENCE_THRESHOLD,
    MANUAL_INTERVENTION_POLL_SEC,
    MANUAL_INTERVENTION_REASONS,
    MANUAL_INTERVENTION_TIMEOUT_SEC,
    MAX_PAGE_CHARS,
    MAX_OBJECTS,
    MOBILENET_ONNX_PATH,
    PRESET_NEWS_FEEDS,
    ARXIV_CATEGORIES,
    SKIP_COOKIE_VALIDATION,
    TIME_RANGE_MAP,
    TRANSCRIBE_CACHE_DIR,
    TRANSCRIPT_CACHE_DIR,
    VIDEO_CACHE_DIR,
    USER_AGENT,
    IMAP_SERVERS,
)
from .browser import (
    _check_display_available,
    _parse_netscape_cookie_file,
    detect_block_reason,
    dismiss_consent,
    get_last_intervention_result,
    human_delay,
    is_login_required,
    is_manual_intervention_active,
    launch_browser,
    load_cookies,
    login_and_save_cookies,
    manual_intervention_for_block,
    save_cookies,
    setup_page_stealth,
    simulate_human_behavior,
    take_debug_screenshot,
    validate_google_cookies,
    wait_for_google_results_ready,
    warmup_retry,
)
from .anti_bot import browse_google
from .captcha import is_blocked, try_solve_captcha
from .utils.text import (
    strip_html,
    format_timestamp,
    split_translation_chunks,
    collapse_newlines,
    format_error,
)
from .utils.network import (
    fetch_url_bytes,
    fallback_web_search,
    fallback_duckduckgo_news,
    fallback_duckduckgo_images,
    format_fallback_results,
    resolve_urls,
)

# Internal alias for legacy function references within this module
_fetch_url_bytes = fetch_url_bytes
from .utils.image import (
    is_base64_image,
    save_base64_image,
    is_local_file,
)
from .utils.language import (
    detect_source_language,
    resolve_language_code,
)
from . import health_server


# ═══════════════════════════════════════════════════════════════════════════
# Startup lifecycle — validate cookies before accepting any requests
# ═══════════════════════════════════════════════════════════════════════════


def _format_validation_error(result: dict) -> str:
    """Format a cookie validation error into a human-readable message."""
    lines = [
        "=" * 60,
        "  GOOGLE MCP — COOKIE VALIDATION FAILED",
        "=" * 60,
        "",
        "The server cannot start because Google cookies are invalid or missing.",
        "Without valid cookies, all Google tools will fail with bot detection.",
        "",
    ]

    if result["errors"]:
        lines.append("Errors:")
        for err in result["errors"]:
            lines.append(f"  ! {err}")
        lines.append("")

    if result["warnings"]:
        lines.append("Warnings:")
        for w in result["warnings"]:
            lines.append(f"  ? {w}")
        lines.append("")

    g = result.get("google", {})
    lines.append("Google cookie status:")
    lines.append(f"  File found:      {g.get('file_found', False)}")
    lines.append(f"  Cookies loaded:   {g.get('cookie_count', 0)}")
    lines.append(f"  Auth cookies OK: {len(g.get('auth_cookies_present', []))}/{len(_REQUIRED_AUTH_COOKIES)}")
    if g.get("auth_cookies_missing"):
        lines.append(f"  Missing auth:    {', '.join(g['auth_cookies_missing'])}")
    if g.get("expired_cookies"):
        lines.append(f"  Expired:         {len(g['expired_cookies'])} cookie(s)")
    lines.append(f"  Logged in:       {g.get('logged_in', False)}")
    lines.append("")

    yt = result.get("youtube", {})
    lines.append("YouTube cookie status:")
    lines.append(f"  File found:      {yt.get('file_found', False)}")
    lines.append(f"  Cookies loaded:   {yt.get('cookie_count', 0)}")
    lines.append(f"  Logged in:       {yt.get('logged_in', False)}")
    lines.append("")

    lines.append("How to fix:")
    lines.append("  1. Open Chrome and sign into your Google account")
    lines.append("  2. Run: node export_cookies.js")
    lines.append("     (or use a cookie export extension to get Netscape-format .txt files)")
    lines.append("  3. Place the exported files in: " + COOKIE_DIR)
    lines.append("  4. Restart the MCP server")
    lines.append("")
    lines.append("To bypass this check (development only), set:")
    lines.append("  export SKIP_COOKIE_VALIDATION=1")
    lines.append("=" * 60)

    return "\n".join(lines)


# Reference for _format_validation_error — must match browser.py
_REQUIRED_AUTH_COOKIES: set[str] = {
    "SID", "SAPISID", "APISID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
}


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    """Validate Google cookies at server startup.

    If cookies are invalid or show a logged-out session, the server offers
    to open a headful browser for manual login. If the user declines or
    login fails, the server exits with a non-zero code.
    YouTube warnings are printed but don't block startup.

    Also starts a small HTTP health server on a separate port (default
    11499) so monitoring tools / k8s probes can check liveness, readiness,
    and pull a full status snapshot without going through the MCP proxy.
    """
    # Register the FastMCP server so /health can introspect its tools
    health_server.set_mcp_server(server)

    # ── Health server (started first so probes work even during cookie
    # validation) ──
    health = health_server.HealthServer()
    await health.start()
    # Mark start time so uptime_sec is accurate
    health_server.set_loaded_at()

    if SKIP_COOKIE_VALIDATION:
        print("SKIP_COOKIE_VALIDATION is set — skipping cookie validation.", file=sys.stderr)
        health_server.set_startup_result(
            skipped=True, passed=None,
            errors=[], warnings=[],
        )
        try:
            yield {}
        finally:
            await health.stop()
        return

    print("Validating Google cookies at startup...", file=sys.stderr)
    result = await validate_google_cookies()

    if result.get("warnings"):
        for w in result["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)

    if not result["valid"]:
        msg = _format_validation_error(result)
        print(msg, file=sys.stderr)
        health_server.set_startup_result(
            skipped=False, passed=False,
            errors=result.get("errors", []),
            warnings=result.get("warnings", []),
        )

        # ── Offer headful login flow ──
        print("", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print("  COOKIE VALIDATION FAILED", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print("", file=sys.stderr)
        print("Options:", file=sys.stderr)
        print("  1. Auto-login — Open a browser window to log into Google", file=sys.stderr)
        print("     (cookies will be saved automatically)", file=sys.stderr)
        print("  2. Exit — Stop the server (you can fix cookies manually)", file=sys.stderr)
        print("", file=sys.stderr)
        print("Choose option 1 or 2 (default: 2): ", file=sys.stderr, end="", flush=True)

        try:
            # Read from /dev/tty directly — stdin is the MCP protocol pipe
            with open("/dev/tty", "r") as tty:
                choice = tty.readline().strip()
        except (OSError, EOFError, KeyboardInterrupt):
            choice = "2"

        if choice == "1":
            print("\nStarting headful login flow...", file=sys.stderr)
            success = await login_and_save_cookies()
            if success:
                # Re-validate after login
                print("\nRe-validating cookies after login...", file=sys.stderr)
                result = await validate_google_cookies()
                if result["valid"]:
                    print("Cookie validation PASSED after login.", file=sys.stderr)
                    health_server.set_startup_result(
                        skipped=False, passed=True,
                        errors=[], warnings=result.get("warnings", []),
                    )
                    try:
                        yield {}
                    finally:
                        await health.stop()
                    return
                else:
                    print("Cookie validation still FAILED after login.", file=sys.stderr)
                    for err in result.get("errors", []):
                        print(f"  ! {err}", file=sys.stderr)
                    health_server.set_startup_result(
                        skipped=False, passed=False,
                        errors=result.get("errors", []),
                        warnings=result.get("warnings", []),
                    )
            else:
                print("Login flow did not complete successfully.", file=sys.stderr)
        else:
            print("Exiting. Please fix cookies manually and restart.", file=sys.stderr)

        print("Cookie validation FAILED — exiting.", file=sys.stderr)
        # Stop the health server before exiting so the process is clean
        await health.stop()
        os._exit(1)

    print("Cookie validation PASSED — Google session is active.", file=sys.stderr)
    if result.get("youtube", {}).get("logged_in"):
        print("YouTube session is also active.", file=sys.stderr)
    health_server.set_startup_result(
        skipped=False, passed=True,
        errors=[], warnings=result.get("warnings", []),
    )
    try:
        yield {}
    finally:
        await health.stop()


mcp = FastMCP("google-search", lifespan=app_lifespan)


# ---------------------------------------------------------------------------
# check_cookies — diagnostic tool
# ---------------------------------------------------------------------------


@mcp.tool()
async def check_cookies() -> str:
    """Check the status of Google cookies loaded from your browser export.

    Verifies cookie files exist, shows domain coverage, and reports
    auto-saved session cookies. Use this to debug CAPTCHA/block issues.
    """
    lines = ["=== Google MCP Cookie Status ===\n"]

    user_files = []
    if os.path.isdir(COOKIE_DIR):
        for fname in sorted(os.listdir(COOKIE_DIR)):
            if fname.endswith(".txt"):
                fpath = os.path.join(COOKIE_DIR, fname)
                size = os.path.getsize(fpath)
                user_files.append((fname, size))
    if user_files:
        lines.append(f"User cookie files ({COOKIE_DIR}):")
        for name, size in user_files:
            lines.append(f"  {name} ({size:,} bytes)")
        all_domains = set()
        for name, _ in user_files:
            fpath = os.path.join(COOKIE_DIR, name)
            cookies = _parse_netscape_cookie_file(fpath)
            for c in cookies:
                d = c.get("domain", "")
                if "google" in d:
                    all_domains.add(d)
        if all_domains:
            lines.append(f"\n  Google domains covered: {len(all_domains)}")
            for d in sorted(all_domains):
                lines.append(f"    - {d}")
        else:
            lines.append("\n  No Google domains found in cookie files!")
    else:
        lines.append(f"User cookie files ({COOKIE_DIR}):")
        lines.append("  No .txt cookie files found.")

    if os.path.isfile(COOKIE_JSON_PATH):
        try:
            with open(COOKIE_JSON_PATH) as f:
                session_cookies = json.load(f)
            lines.append(f"\nAuto-saved session cookies ({COOKIE_JSON_PATH}):")
            lines.append(f"  {len(session_cookies)} cookies from previous sessions")
        except Exception:
            lines.append(f"\nAuto-saved session cookies: file exists but could not be read")
    else:
        lines.append(f"\nAuto-saved session cookies: none yet")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manual intervention — open a headful browser when bot detection fires
# ---------------------------------------------------------------------------


@mcp.tool()
async def open_manual_browser(
    url: str = "https://www.google.com",
    reason: str = "unknown",
    timeout_sec: int = 0,
) -> str:
    """Open a visible (headful) browser window so you can manually resolve
    a Google bot-detection block (CAPTCHA, login, 2FA, consent dialog, ...).

    When this tool is called, the server closes its current headless browser,
    opens a real visible Chromium window, and waits for you to interact with
    the page. Once the page is no longer blocked (search results visible,
    avatar showing, etc.) the system saves the fresh cookies and you can
    retry your previous request — it will use the updated session.

    When to call this:
      - You (or a previous tool result) noticed a CAPTCHA or login page
      - Automatic anti-bot measures failed and you want to take over
      - Cookies look stale and you want to refresh them via the GUI
      - You're using this MCP server for the first time and want to log in

    What you can do in the window:
      - Solve any reCAPTCHA / image challenge
      - Log into your Google account
      - Complete 2-step verification / phone check
      - Accept consent dialogs

    Args:
        url: The URL to open in the visible browser. Default is Google home
             which is the right page for logging in.
        reason: One of the supported reasons for documentation purposes:
                "captcha", "login", "rate_limit", "consent", "verification",
                "unknown". Just helps the system log what you're doing.
        timeout_sec: How long to wait (seconds) before giving up. 0 = use
                     the server default (MANUAL_INTERVENTION_TIMEOUT_SEC env var,
                     default 300 = 5 minutes). Set to a higher value for
                     long 2FA flows, or 0 to wait forever.

    Returns a status message describing the outcome.
    """
    if not ENABLE_MANUAL_INTERVENTION:
        return (
            "Manual intervention is disabled. Set ENABLE_MANUAL_INTERVENTION=1 "
            "in the server environment to enable headful browser fallback."
        )
    if not _check_display_available():
        return (
            "Cannot open headful browser: no graphical display detected "
            "(DISPLAY / WAYLAND_DISPLAY unset). The server is likely running "
            "headless on a remote machine. Run the server on a machine with "
            "a desktop session or use X-forwarding."
        )
    if is_manual_intervention_active():
        return (
            "Another manual-intervention window is already open. "
            "Please close the existing window (or wait for it to finish) "
            "before opening a new one."
        )

    actual_timeout = timeout_sec if timeout_sec and timeout_sec > 0 else MANUAL_INTERVENTION_TIMEOUT_SEC
    print(
        f"\n[open_manual_browser] Opening headful browser for manual "
        f"intervention (reason={reason}, timeout={actual_timeout}s)...",
        file=sys.stderr, flush=True,
    )

    # Run the headful flow. The function returns the new cookies if the
    # user resolved the block, or None otherwise.
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            # Capture any existing auto-saved cookies so the user doesn't
            # lose their session
            initial_cookies: list[dict] = []
            try:
                if os.path.isfile(COOKIE_JSON_PATH):
                    with open(COOKIE_JSON_PATH) as f:
                        initial_cookies = json.load(f)
            except Exception:
                initial_cookies = []

            new_cookies = await manual_intervention_for_block(
                pw,
                blocked_url=url,
                reason=reason,
                initial_cookies=initial_cookies,
                timeout_sec=actual_timeout,
            )
    except Exception as e:
        return f"❌ Failed to open headful browser: {e}"

    last = get_last_intervention_result()
    if new_cookies is not None:
        return (
            f"✅ Manual intervention successful. "
            f"{len(new_cookies)} cookies saved to disk. "
            f"Please retry your previous request — it should now pass the "
            f"block. (elapsed {last.get('elapsed_sec', 0):.1f}s)"
        )
    outcome = last.get("outcome", "unknown")
    return (
        f"⚠️  Manual intervention ended without resolution "
        f"(outcome: {outcome}, elapsed {last.get('elapsed_sec', 0):.1f}s). "
        f"You can call `open_manual_browser` again to retry, or run "
        f"`check_cookies` to see the current cookie state."
    )


@mcp.tool()
async def manual_intervention_status() -> str:
    """Report whether a headful intervention window is open, plus info
    about the most recent one (if any).

    Useful for the LLM to check the state without triggering anything.
    """
    lines = ["=== Manual intervention status ===\n"]
    lines.append(f"ENABLE_MANUAL_INTERVENTION: {ENABLE_MANUAL_INTERVENTION}")
    lines.append(f"Currently active:           {is_manual_intervention_active()}")
    lines.append(f"Timeout (sec):               {MANUAL_INTERVENTION_TIMEOUT_SEC}")
    lines.append(f"Poll interval (sec):         {MANUAL_INTERVENTION_POLL_SEC}")
    lines.append(f"Display available:           {_check_display_available()}")
    lines.append("")

    last = get_last_intervention_result()
    if last.get("timestamp", 0) > 0:
        lines.append("Most recent intervention:")
        ts = last.get("timestamp", 0)
        if ts:
            from datetime import datetime as _dt
            lines.append(f"  Time:     {_dt.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"  URL:      {last.get('url', '')}")
        lines.append(f"  Reason:   {last.get('reason', '')}")
        lines.append(f"  Resolved: {last.get('resolved', False)}")
        lines.append(f"  Outcome:  {last.get('outcome', 'unknown')}")
        lines.append(f"  Elapsed:  {last.get('elapsed_sec', 0):.1f}s")
    else:
        lines.append("No manual intervention has run yet.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Google search tools — moved to tools/search.py
# ---------------------------------------------------------------------------
from .tools import search as _search  # noqa: F401  (registers search tools)



# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Google Maps tools — moved to tools/maps.py
# ---------------------------------------------------------------------------
from .tools import maps as _maps  # noqa: F401  (registers maps tools)



# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Google finance/weather/shopping/books — moved to tools/finance.py
# ---------------------------------------------------------------------------
from .tools import finance as _finance  # noqa: F401  (registers finance tools)



# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Google translate/flights/hotels — moved to tools/travel.py
# ---------------------------------------------------------------------------
from .tools import travel as _travel  # noqa: F401  (registers travel tools)



# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Google vision tools — moved to tools/vision.py
# ---------------------------------------------------------------------------
from .tools import vision as _vision  # noqa: F401  (registers vision tools)


# ---------------------------------------------------------------------------
# Google video tools — moved to tools/video.py
# ---------------------------------------------------------------------------
from .tools import video as _video  # noqa: F401  (registers video tools)

# Re-export video helpers for backward compatibility (feeds.py and legacy
# tests import these directly from server.py).
_download_audio = _video._download_audio
_download_video = _video._download_video
_extract_clip_pyav = _video._extract_clip_pyav
_transcribe_audio = _video._transcribe_audio
_transcript_cache_path = _video._transcript_cache_path
_video_cache_path = _video._video_cache_path


# ---------------------------------------------------------------------------
# visit_page
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Web utilities — moved to tools/web_utils.py
# ---------------------------------------------------------------------------
from .tools import web_utils as _web_utils  # noqa: F401  (registers web tools)

# Re-export web-utility tools for backward compatibility (legacy tests import
# these directly from server.py).
archive_webpage = _web_utils.archive_webpage
convert_media = _web_utils.convert_media
fetch_emails = _web_utils.fetch_emails
generate_qr = _web_utils.generate_qr
paste_text = _web_utils.paste_text
read_document = _web_utils.read_document
shorten_url = _web_utils.shorten_url
transcribe_local = _web_utils.transcribe_local
upload_to_s3 = _web_utils.upload_to_s3
visit_page = _web_utils.visit_page
wikipedia = _web_utils.wikipedia



# ---------------------------------------------------------------------------
# Feed Subscription System — Subscribe, monitor, and search across sources
# ---------------------------------------------------------------------------

FEEDS_DB_PATH = os.path.join(
    os.path.expanduser("~"), ".cache", "noapi-google-search-mcp", "feeds.db"
)

PRESET_NEWS_FEEDS = {
    "bbc": {"name": "BBC News", "url": "http://feeds.bbci.co.uk/news/rss.xml"},
    "cnn": {"name": "CNN", "url": "http://rss.cnn.com/rss/cnn_topstories.rss"},
    "nyt": {"name": "New York Times", "url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"},
    "guardian": {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss"},
    "npr": {"name": "NPR News", "url": "https://feeds.npr.org/1001/rss.xml"},
    "aljazeera": {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    "techcrunch": {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
    "ars": {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    "verge": {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
    "wired": {"name": "Wired", "url": "https://www.wired.com/feed/rss"},
    "reuters": {"name": "Reuters", "url": "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best"},
}

ARXIV_CATEGORIES = {
    "ai": "cs.AI", "ml": "cs.LG", "cv": "cs.CV", "nlp": "cs.CL",
    "robotics": "cs.RO", "crypto": "cs.CR", "systems": "cs.DC",
    "hci": "cs.HC",
}


# ---------------------------------------------------------------------------
# Feed subscription tools — moved to tools/feeds.py
# ---------------------------------------------------------------------------
from .tools import feeds as _feeds  # noqa: F401  (registers feed tools)

# Re-export feed helpers for backward compatibility (legacy tests import
# these directly from server.py).
_check_source_arxiv = _feeds._check_source_arxiv
_check_source_github = _feeds._check_source_github
_check_source_hackernews = _feeds._check_source_hackernews
_check_source_podcast = _feeds._check_source_podcast
_check_source_reddit = _feeds._check_source_reddit
_check_source_rss = _feeds._check_source_rss
_check_source_youtube = _feeds._check_source_youtube
_get_feeds_db = _feeds._get_feeds_db
_parse_rss_atom = _feeds._parse_rss_atom
_store_items = _feeds._store_items
_auto_transcribe_youtube = _feeds._auto_transcribe_youtube
check_feeds = _feeds.check_feeds
get_feed_items = _feeds.get_feed_items
list_subscriptions = _feeds.list_subscriptions
search_feeds = _feeds.search_feeds
subscribe = _feeds.subscribe
unsubscribe = _feeds.unsubscribe


# ---------------------------------------------------------------------------
# Metrics wiring — wrap every registered tool so /health can report usage
# ---------------------------------------------------------------------------

def _install_metrics_wrappers() -> None:
    """Wrap each registered MCP tool's ``run`` to record call metrics.

    This is called once after all tools are defined. It monkey-patches the
    ``run`` method of every tool in the FastMCP tool manager so that each
    invocation records call count, success/failure, and latency into the
    global metrics registry (exposed via ``/health`` -> ``metrics``).
    """
    import time as _time

    from . import metrics as _metrics

    tm = getattr(mcp, "_tool_manager", None)
    if tm is None:
        return
    tools = getattr(tm, "_tools", {}) or {}
    for name, tool in tools.items():
        original_run = getattr(tool, "run", None)
        if original_run is None:
            continue

        async def _wrapped_run(
            arguments,
            context=None,
            convert_result=False,
            _orig=original_run,
            _name=name,
        ):
            start = _time.monotonic()
            ok = True
            try:
                return await _orig(arguments, context, convert_result)
            except Exception:
                ok = False
                raise
            finally:
                latency_ms = (_time.monotonic() - start) * 1000.0
                _metrics.record_tool_call(_name, ok=ok, latency_ms=latency_ms)

        # Preserve the original signature metadata where possible.
        try:
            _wrapped_run.__name__ = getattr(original_run, "__name__", name)
            _wrapped_run.__doc__ = getattr(original_run, "__doc__", None)
        except Exception:
            pass
        # Tool is a Pydantic model — bypass its __setattr__ validation.
        object.__setattr__(tool, "run", _wrapped_run)


_install_metrics_wrappers()
