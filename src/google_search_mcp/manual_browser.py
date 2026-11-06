"""Open a visible Chromium window that shares the server's login state.

Why a separate profile instead of the server's own
---------------------------------------------------
The server keeps a persistent Chromium context on ``BROWSER_DATA_DIR``
(``~/.config/google-mcp-browser``, ~1.3 GB) for the life of the process. Chromium
allows one writer per profile, and the profile's ``Cookies`` file is SQLite — two
concurrent writers risk lock errors and corruption. The profile is also far too
large to copy per session.

So this opens its own profile (``google-mcp-browser-manual``) and shares the
*session* rather than the directory:

1. ``load_cookies()`` injects the same cookies the server uses — the files in
   ``cookies/`` plus the auto-saved ``~/.google_mcp_cookies.json`` — so you start
   already logged in.
2. Cookies are re-saved to ``~/.google_mcp_cookies.json`` while the window is open
   (and once more on close). The server reads that file on every browser tool
   call, so anything you log into here is picked up by the next tool call.

``--profile-dir ~/.config/google-mcp-browser`` is available for the case where the
service is stopped and you want the exact profile (localStorage, fingerprint).

Typical uses: signing into a site the tools need, clearing a consent dialog,
finding a site's feed URL, or just browsing before calling a tool.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys

from . import browser as browser_mod
from . import config

__all__ = ["run_manual_browser", "DEFAULT_URL"]

DEFAULT_URL = "https://www.google.com"

#: How often to flush cookies while the window is open. Closing the window
#: destroys the context, after which its cookies can no longer be read, so the
#: periodic save is what makes "log in, then close the window" reliable.
SAVE_INTERVAL_SEC = 5.0


def _profile_dir(override: str | None) -> str:
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(config.CONFIG_DIR, "google-mcp-browser-manual")


async def _session(url: str, profile: str, timeout_sec: int) -> int:
    from playwright.async_api import async_playwright

    from .browser import launch_browser, load_cookies, save_cookies

    # launch_browser() reads the module-level constant, so point it at this
    # session's profile. Same approach as scripts/bench_browser.py.
    browser_mod.BROWSER_DATA_DIR = profile

    async with async_playwright() as pw:
        print(f"[manual-browser] profile: {profile}", file=sys.stderr, flush=True)
        context = await launch_browser(pw, headless=False)

        try:
            loaded = await load_cookies(context)
            print(
                f"[manual-browser] loaded {loaded} cookie(s) from the server's cookie store",
                file=sys.stderr,
                flush=True,
            )

            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:  # a bad URL should not kill the session
                print(f"[manual-browser] could not open {url}: {exc}", file=sys.stderr, flush=True)

            print(
                "\n[manual-browser] Window is open — do what you need, then close it.\n"
                "                 Cookies are saved every "
                f"{SAVE_INTERVAL_SEC:.0f}s, so the MCP tools pick up any new login.\n"
                "                 Ctrl+C also finishes the session.\n",
                file=sys.stderr,
                flush=True,
            )

            closed = asyncio.Event()
            with contextlib.suppress(Exception):
                context.on("close", lambda *_: closed.set())
            browser = getattr(context, "browser", None)
            if browser is not None:
                with contextlib.suppress(Exception):
                    browser.on("disconnected", lambda *_: closed.set())

            deadline = asyncio.get_running_loop().time() + timeout_sec if timeout_sec > 0 else None
            while not closed.is_set():
                # Wait for the window to close, flushing cookies meanwhile.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(closed.wait(), timeout=SAVE_INTERVAL_SEC)
                # Persist while the context is still alive.
                with contextlib.suppress(Exception):
                    await _save_all(context)
                if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                    print(
                        f"[manual-browser] timeout of {timeout_sec}s reached",
                        file=sys.stderr,
                        flush=True,
                    )
                    break
        finally:
            with contextlib.suppress(Exception):
                await _save_all(context)
            with contextlib.suppress(Exception):
                await context.close()

    print(
        f"[manual-browser] cookies saved to {config.COOKIE_JSON_PATH}\n"
        "                 The server re-reads them on its next browser tool call.",
        file=sys.stderr,
        flush=True,
    )
    return 0


async def _save_all(context) -> None:
    """Persist cookies to the browser store **and** to yt-dlp's Netscape jars.

    Two stores, two formats: the browser tools read the JSON file, yt-dlp reads
    the Netscape files. Saving only the JSON is why logging in here fixed Google
    scraping but left YouTube rejecting requests with "Sign in to confirm you're
    not a bot".
    """
    with contextlib.suppress(Exception):
        from .browser import export_ytdlp_cookie_files, save_cookies

        await save_cookies(context)
    with contextlib.suppress(Exception):
        from .browser import export_ytdlp_cookie_files

        written = export_ytdlp_cookie_files(await context.cookies())
        for path, count in written.items():
            print(
                f"[manual-browser] wrote {count} cookie(s) to {path} (yt-dlp)",
                file=sys.stderr,
                flush=True,
            )


def run_manual_browser(
    url: str = DEFAULT_URL,
    *,
    profile_dir: str | None = None,
    timeout_sec: int = 0,
) -> int:
    """Open a headful Chromium window sharing the server's cookies. Blocking."""
    from .browser import _check_display_available

    if not _check_display_available():
        print(
            "Cannot open a visible browser: no graphical display detected "
            "(DISPLAY / WAYLAND_DISPLAY unset).\n"
            "Run this from a desktop session, or start the MCP service instead and "
            "call the `open_manual_browser` tool.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    profile = _profile_dir(profile_dir)
    os.makedirs(profile, exist_ok=True)

    try:
        return asyncio.run(_session(url or DEFAULT_URL, profile, max(0, timeout_sec)))
    except KeyboardInterrupt:
        print("\n[manual-browser] interrupted — cookies were saved", file=sys.stderr, flush=True)
        return 0
    except Exception as exc:  # pragma: no cover - surfaced to the operator
        print(f"[manual-browser] failed: {exc}", file=sys.stderr, flush=True)
        if "ProcessSingleton" in str(exc) or "in use" in str(exc).lower():
            print(
                f"\nThe profile {profile} is already open in another Chromium.\n"
                "Close that window, or pick a different profile with --profile-dir.",
                file=sys.stderr,
                flush=True,
            )
        return 1
