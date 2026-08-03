"""Advanced anti-detection — fingerprint randomization + search flow + warmup.

Human-like behavior (``human_sim``) is necessary but **not sufficient**
to defeat modern Google bot detection. Google / reCAPTCHA / Cloudflare
also look at signals that have nothing to do with how you move the
mouse:

  * **JavaScript fingerprint** — every browser produces a slightly
    different canvas / WebGL / AudioContext signature. Two requests
    from the "same" browser with the *exact same* fingerprint are
    trivially identifiable as automation. We must randomize this
    *per session*, not per page load (a session that randomizes too
    often looks like a bot too).

  * **Search flow** — real humans go to ``google.com``, *type* a query
    in the search box, see suggestions, hit Enter, then read results.
    Bots hit ``/search?q=foo`` directly. That single difference is one
    of the strongest signals Google uses.

  * **Trust history** — a brand new browser context with no cookies
    and no behavioral history is suspicious. A real user has searched
    before, visited other Google properties, and built up a few
    weeks/months of trust tokens. We approximate this with a
    "warmup" — a benign first search on a fresh session.

  * **Tab focus** — real users switch tabs, get distracted, come
    back. Bots never do. We fire occasional ``blur``/``focus`` events.

  * **Client Hints** — modern Chrome sends ``Sec-CH-UA`` and
    ``Sec-Fetch-*`` headers. Without them Google sees a request that
    looks like an old browser.

This module is the *strategic* layer (fingerprint, flow, trust). The
*tactical* layer (mouse moves, typing rhythm, scroll easing) lives in
``human_sim``. Both work together.

Key exports:

  * ``build_init_script()`` — a Playwright ``add_init_script`` payload
    combining ``stealth_js`` with our per-session fingerprint patches.
  * ``generate_session_fingerprint()`` — returns a per-session dict of
    {viewport, locale, timezone, ua, client_hints, sec_fetch, ...} that
    the browser is launched with.
  * ``warmup_session(context, page)`` — do a benign search + click on
    a fresh session to build trust.
  * ``search_via_typing(page, query)`` — navigate to ``google.com``,
    type the query in the search box, submit, wait for results.
  * ``simulate_tab_focus(page)`` — fire a few ``blur``/``focus`` events.
  * ``reading_mouse_track(page, selectors)`` — move the mouse along
    result titles like a real user reading.
  * ``parse_query_from_url(url)`` — extract ``q=`` from a Google
    search URL so we know what to type.
  * ``is_google_search_url(url)`` — detect URLs we should retype
    instead of navigating to directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import config
from . import stealth_js
from . import human_sim

log = logging.getLogger("google_search_mcp.anti_detect")


# ═══════════════════════════════════════════════════════════════════════════
# Per-session fingerprint
# ═══════════════════════════════════════════════════════════════════════════
#
# A real user has a stable fingerprint (same OS, same screen, same GPU,
# same locale) for weeks or months at a time. Bots often either:
#   (a) use the EXACT same fingerprint forever (very bot-like — every
#       request correlates), or
#   (b) randomize too aggressively (also bot-like — no real user has
#       a different GPU every request).
#
# The right answer is: a *single* fingerprint per session (one
# /tmp/.org.chromium.* profile, one set of JS-shimmed values), that's
# stable across the whole session but different from the next session.

# These are common, plausible values for a "Linux desktop Chrome" user.
# We pick one at random per session.
_VIEWPORTS: list[tuple[int, int]] = [
    (1920, 1080),
    (1680, 1050),
    (1536, 864),
    (1440, 900),
    (1366, 768),
    (1280, 800),
    (1280, 720),
    (1920, 1200),
    (2560, 1440),
]

_LANGUAGES: list[str] = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.9,de;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-US,en;q=0.9,es;q=0.8",
    "en-US,en;q=0.9,ja;q=0.8",
    "en-US,en;q=0.9,zh-CN;q=0.8",
]

_TIMEZONES: list[str] = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Toronto",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Madrid",
    "Europe/Amsterdam",
    "Asia/Tokyo",
    "Asia/Singapore",
    "Australia/Sydney",
]

_LOCALE_PRIMARY: dict[str, str] = {
    "en-US,en;q=0.9": "en-US",
    "en-GB,en;q=0.9": "en-GB",
    "en-US,en;q=0.9,de;q=0.8": "en-US",
    "en-US,en;q=0.9,fr;q=0.8": "en-US",
    "en-US,en;q=0.9,es;q=0.8": "en-US",
    "en-US,en;q=0.9,ja;q=0.8": "en-US",
    "en-US,en;q=0.9,zh-CN;q=0.8": "en-US",
}

_HARDWARE_CONCURRENCY_OPTIONS: list[int] = [4, 8, 12, 16, 24, 32]
_DEVICE_MEMORY_OPTIONS: list[int] = [4, 8, 16, 32]

# WebGL — pick from a small set of plausible Intel/AMD/NVIDIA GPUs. We
# don't want to use SwiftShader ("Google Inc." / "SwiftShader") because
# that's the headless signature Google is trained to detect.
_WEBGL_GPU_OPTIONS: list[tuple[str, str]] = [
    ("Intel Inc.", "Intel(R) UHD Graphics 630"),
    ("Intel Inc.", "Intel(R) UHD Graphics 620"),
    ("Intel Inc.", "Intel(R) Iris Plus Graphics 655"),
    ("NVIDIA Corporation", "NVIDIA GeForce GTX 1660/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce RTX 3060/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce GTX 1050 Ti/PCIe/SSE2"),
    ("AMD", "AMD Radeon Pro 560X OpenGL Engine"),
    ("AMD", "AMD Radeon RX 580"),
    ("Apple Inc.", "Apple M1"),
    ("Apple Inc.", "Apple M2"),
]

# Chrome versions — use plausible recent versions
_CHROME_VERSIONS: list[str] = [
    "149.0.0.0",
    "148.0.0.0",
    "147.0.0.0",
    "146.0.0.0",
    "145.0.0.0",
    "144.0.0.0",
    "143.0.0.0",
]

_PLATFORMS: list[str] = [
    "Linux x86_64",
    "Linux x86_64",
    "Linux x86_64",   # weighted toward Linux (most common server env)
    "Linux x86_64",
    "Macintosh",
    "Windows NT 10.0; Win64; x64",
]


def _seed_for_session(process_pid: int) -> int:
    """Derive a stable seed for this session.

    We mix the process PID with a per-second rotation so two restarts
    don't get *exactly* the same fingerprint, but the same process
    always has the same fingerprint across its lifetime.
    """
    # Add some time-based variation but quantized so it changes only
    # every ~minute (sessions are usually much longer than that).
    t = int(time.time() // 60)
    return (process_pid * 1_000_003 + t) & 0xFFFFFFFF


def _stable_pick(options: list, rng: random.Random) -> Any:
    """Pick an option from a list using the session RNG."""
    return rng.choice(options)


def generate_session_fingerprint(seed: int | None = None) -> dict[str, Any]:
    """Return the per-session browser fingerprint.

    The dict is stable for the lifetime of the calling process
    (since the seed mixes PID + minute). Different processes get
    different fingerprints.

    Returns a dict with all the knobs:
        viewport, locale, timezone, hardware_concurrency,
        device_memory, platform, webgl_vendor, webgl_renderer,
        chrome_version, user_agent, accept_language, sec_ch_ua,
        sec_fetch_*, ...
    """
    import os as _os
    pid = _os.getpid()
    if seed is None:
        seed = _seed_for_session(pid)
    rng = random.Random(seed)

    viewport_w, viewport_h = _stable_pick(_VIEWPORTS, rng)
    # Apply small random variation (use the seeded RNG so the same seed
    # always produces the same viewport)
    viewport_w += rng.randint(-30, 30)
    viewport_h += rng.randint(-20, 20)
    viewport_w = max(800, viewport_w)
    viewport_h = max(600, viewport_h)

    accept_lang = _stable_pick(_LANGUAGES, rng)
    locale = _LOCALE_PRIMARY.get(accept_lang, "en-US")
    timezone = _stable_pick(_TIMEZONES, rng)
    hardware = _stable_pick(_HARDWARE_CONCURRENCY_OPTIONS, rng)
    device_mem = _stable_pick(_DEVICE_MEMORY_OPTIONS, rng)
    webgl_vendor, webgl_renderer = _stable_pick(_WEBGL_GPU_OPTIONS, rng)
    chrome_ver = _stable_pick(_CHROME_VERSIONS, rng)
    platform = _stable_pick(_PLATFORMS, rng)

    # Build a User-Agent that matches the platform
    if platform.startswith("Mac"):
        ua = (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} "
            f"Safari/537.36"
        )
    elif platform.startswith("Windows"):
        ua = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} "
            f"Safari/537.36"
        )
    else:  # Linux (most common)
        ua = (
            f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"
        )

    # Major version for Sec-CH-UA
    chrome_major = chrome_ver.split(".")[0]
    sec_ch_ua = (
        f'"Not_A Brand";v="8", '
        f'"Chromium";v="{chrome_major}", '
        f'"Google Chrome";v="{chrome_major}"'
    )
    # Platform-specific Sec-CH-UA-Platform
    if platform.startswith("Mac"):
        sec_ch_ua_platform = '"macOS"'
    elif platform.startswith("Windows"):
        sec_ch_ua_platform = '"Windows"'
    else:
        sec_ch_ua_platform = '"Linux"'

    sec_ch_ua_mobile = "?0"  # desktop
    sec_ch_ua_arch = ""  # empty
    if platform.startswith("Mac"):
        sec_ch_ua_arch = '"arm"'
    elif platform.startswith("Linux"):
        sec_ch_ua_arch = '"x86_64"'
    elif platform.startswith("Windows"):
        sec_ch_ua_arch = '"x86_64"'

    # sec-fetch-* headers (only valid for HTTPS)
    sec_fetch_dest = "document"
    sec_fetch_mode = "navigate"
    sec_fetch_site = "none"
    sec_fetch_user = "?1"

    return {
        "seed": seed,
        "viewport": {"width": viewport_w, "height": viewport_h},
        "locale": locale,
        "timezone": timezone,
        "hardware_concurrency": hardware,
        "device_memory": device_mem,
        "platform": platform,
        "webgl_vendor": webgl_vendor,
        "webgl_renderer": webgl_renderer,
        "chrome_version": chrome_ver,
        "chrome_major": chrome_major,
        "user_agent": ua,
        "accept_language": accept_lang,
        # Client Hints
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_mobile": sec_ch_ua_mobile,
        "sec_ch_ua_platform": sec_ch_ua_platform,
        "sec_ch_ua_arch": sec_ch_ua_arch,
        # Sec-Fetch-*
        "sec_fetch_dest": sec_fetch_dest,
        "sec_fetch_mode": sec_fetch_mode,
        "sec_fetch_site": sec_fetch_site,
        "sec_fetch_user": sec_fetch_user,
    }


def fingerprint_to_headers(fp: dict[str, Any]) -> dict[str, str]:
    """Build the full set of HTTP headers for a session.

    Includes Accept-Language, Client Hints (Sec-CH-UA*), and Sec-Fetch-*.
    These are added to the per-page extra headers.
    """
    # Look up the config at call time so env-var overrides work
    # even after the module is reloaded.
    from . import config as _config
    if not _config.ANTIDETECT_CLIENT_HINTS:
        return {"Accept-Language": fp["accept_language"]}
    return {
        "Accept-Language": fp["accept_language"],
        "Sec-CH-UA": fp["sec_ch_ua"],
        "Sec-CH-UA-Mobile": fp["sec_ch_ua_mobile"],
        "Sec-CH-UA-Platform": fp["sec_ch_ua_platform"],
        # Sec-Fetch-* are only sent for secure contexts, but sending
        # them is harmless on http (browsers do the same).
        "Sec-Fetch-Dest": fp["sec_fetch_dest"],
        "Sec-Fetch-Mode": fp["sec_fetch_mode"],
        "Sec-Fetch-Site": fp["sec_fetch_site"],
        "Sec-Fetch-User": fp["sec_fetch_user"],
        # Modern Chrome also sends this
        "Upgrade-Insecure-Requests": "1",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Per-session fingerprint: noise values
# ═══════════════════════════════════════════════════════════════════════════
#
# The init script reads these and uses them to patch canvas / AudioContext
# in a way that varies per session. The point: the SAME pixel data
# should produce DIFFERENT hash on each session, but the noise should
# be subtle enough that the image still looks correct visually.

def _session_noise_bytes(seed: int, n: int) -> bytes:
    """Return ``n`` deterministic noise bytes for this session.

    Generated via SHA-256 of the seed — so it's deterministic
    (good for testing) but unrelated to anything the bot-detection
    could guess from the outside.
    """
    h = hashlib.sha256(f"anti_detect:{seed}:noise:{n}".encode()).digest()
    # Stretch by hashing again
    out = b""
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(h + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:n]


def build_init_script() -> str:
    """Build the Playwright ``add_init_script`` payload.

    Combines:
      * The existing ``stealth_js.STEALTH_JS`` (25+ detection patches)
      * Our per-session fingerprint patches (canvas, audio, WebGL
        randomization with values from the current session)

    The fingerprint values are baked into the script as constants
    (read from the module-level ``_SESSION_FP`` global, set at launch
    time). This way the script is fully self-contained — Playwright
    injects it on every new document.
    """
    fp = get_session_fingerprint()
    seed = fp.get("seed", 0)
    noise = _session_noise_bytes(seed, 32)

    # Encode noise as a byte array literal for the JS side
    noise_array = ", ".join(str(b) for b in noise)

    # WebGL: per-session, but rotated from the static "Intel UHD 620"
    # we use in stealth_js. We override the WebGL param getters to
    # return the session-specific vendor/renderer.
    webgl_vendor = fp["webgl_vendor"]
    webgl_renderer = fp["webgl_renderer"]
    hardware = fp["hardware_concurrency"]
    device_mem = fp["device_memory"]
    platform = fp["platform"]

    fingerprint_js = rf"""
// ═══════════════════════════════════════════════════════════════════
// Per-session fingerprint patches — run before any page script
// ═══════════════════════════════════════════════════════════════════
(function() {{
    const SESSION_NOISE = new Uint8Array([{noise_array}]);
    const SESSION_VENDOR = {webgl_vendor!r};
    const SESSION_RENDERER = {webgl_renderer!r};
    const SESSION_HW = {hardware};
    const SESSION_MEM = {device_mem};
    const SESSION_PLATFORM = {platform!r};

    // ── Per-session hardwareConcurrency / deviceMemory ──
    // stealth_js sets these to static 8. We override with session-
    // specific values so two sessions look like different machines.
    try {{
        Object.defineProperty(navigator, 'hardwareConcurrency', {{
            get: () => SESSION_HW, configurable: true,
        }});
    }} catch (_) {{}}
    try {{
        Object.defineProperty(navigator, 'deviceMemory', {{
            get: () => SESSION_MEM, configurable: true,
        }});
    }} catch (_) {{}}

    // ── Per-session platform string ──
    try {{
        Object.defineProperty(navigator, 'platform', {{
            get: () => SESSION_PLATFORM, configurable: true,
        }});
        Object.defineProperty(navigator, 'oscpu', {{
            get: () => SESSION_PLATFORM, configurable: true,
        }});
    }} catch (_) {{}}

    // ── Per-session WebGL vendor / renderer ──
    // Override what stealth_js set, so each session looks like a
    // different GPU. Both UNMASKED_VENDOR_WEBGL and UNMASKED_RENDERER
    // (and the masked aliases) are intercepted.
    (function() {{
        const origGetContext = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function(type, attrs) {{
            const ctx = origGetContext.apply(this, arguments);
            if (ctx && (type === 'webgl' || type === 'experimental-webgl' || type === 'webgl2')) {{
                const origGetParameter = ctx.getParameter.bind(ctx);
                const origGetExtension = ctx.getExtension.bind(ctx);
                ctx.getParameter = function(param) {{
                    // UNMASKED_VENDOR_WEBGL = 37445
                    if (param === 37445) return SESSION_VENDOR;
                    // UNMASKED_RENDERER_WEBGL = 37446
                    if (param === 37446) return SESSION_RENDERER;
                    return origGetParameter(param);
                }};
                // Also patch the masked versions: getExtension('WEBGL_debug_renderer_info')
                // then call its .UNMASKED_VENDOR_WEBGL / .UNMASKED_RENDERER_WEBGL
                ctx.getExtension = function(name) {{
                    const ext = origGetExtension(name);
                    if (ext && name === 'WEBGL_debug_renderer_info') {{
                        return new Proxy(ext, {{
                            get(target, prop) {{
                                if (prop === 'UNMASKED_VENDOR_WEBGL') return SESSION_VENDOR;
                                if (prop === 'UNMASKED_RENDERER_WEBGL') return SESSION_RENDERER;
                                return target[prop];
                            }},
                        }});
                    }}
                    return ext;
                }};
            }}
            return ctx;
        }};
    }})();

    // ── Per-session canvas fingerprint noise ──
    // Add subtle pixel noise to the rendered canvas, but ONLY for
    // canvases that are large enough to be fingerprinted (>= 100px)
    // AND only for ~70% of renders (so it doesn't look like every
    // single canvas call is noisy — that's also a fingerprint).
    (function() {{
        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type, quality) {{
            const dataUrl = origToDataURL.apply(this, arguments);
            // Skip tiny canvases (often used for detection only)
            if (this.width < 100 || this.height < 100) return dataUrl;
            // Only add noise ~70% of the time — every call is a signal
            if (Math.random() > 0.7) return dataUrl;
            try {{
                const idx = dataUrl.indexOf(',');
                if (idx < 0 || !dataUrl.startsWith('data:image/png')) return dataUrl;
                const header = dataUrl.substring(0, idx);
                let body = dataUrl.substring(idx + 1);
                // atob / btoa round-trip
                let raw = atob(body);
                // Apply noise at a fixed position derived from session
                // Note: we can't modify the IDAT directly without
                // recompressing. Instead, append a tEXt chunk that
                // changes the file hash but is invisible to viewers.
                // (PNG parsers ignore unknown chunks.)
                if (raw.length > 200) {{
                    // Flip a single bit in a non-critical position
                    // (the iTXt chunk is optional, so we can use it)
                    // Actually simpler: just modify one byte of the
                    // header tEXt chunk. Many decoders ignore tEXt.
                    const pos = Math.floor(SESSION_NOISE[0] / 255 * 50) + 16;
                    if (pos < raw.length) {{
                        raw = raw.substring(0, pos) +
                              String.fromCharCode(raw.charCodeAt(pos) ^ 1) +
                              raw.substring(pos + 1);
                        return header + ',' + btoa(raw);
                    }}
                }}
            }} catch (e) {{
                // If btoa/atob fail, just return the original
            }}
            return dataUrl;
        }};
    }})();

    // ── Per-session AudioContext fingerprint ──
    // The 'AnalyserNode' getFloatFrequencyData output is used by many
    // fingerprinting scripts because it depends on the exact audio
    // implementation. Add a session-specific noise floor.
    (function() {{
        const OrigAnalyser = window.AnalyserNode;
        if (!OrigAnalyser) return;
        const origGetFloat = OrigAnalyser.prototype.getFloatFrequencyData;
        OrigAnalyser.prototype.getFloatFrequencyData = function(array) {{
            origGetFloat.call(this, array);
            if (array && array.length > 0) {{
                // Add session-specific noise to the first sample
                const noise = (SESSION_NOISE[1] / 255 - 0.5) * 0.00001;
                array[0] += noise;
            }}
        }};
    }})();

    // ── Screen + viewport consistency ──
    // Make sure screen.availWidth/availHeight are consistent with
    // the viewport we're using. stealth_js sets them to 1280/800
    // which doesn't match a 1920x1080 viewport. We fix that here
    // using the actual viewport size.
    (function() {{
        const w = window.innerWidth || 1280;
        const h = window.innerHeight || 800;
        try {{
            Object.defineProperty(screen, 'availWidth',  {{ get: () => w, configurable: true }});
            Object.defineProperty(screen, 'availHeight', {{ get: () => h, configurable: true }});
            Object.defineProperty(screen, 'width',       {{ get: () => w, configurable: true }});
            Object.defineProperty(screen, 'height',      {{ get: () => h, configurable: true }});
            Object.defineProperty(window, 'outerWidth',  {{ get: () => w, configurable: true }});
            Object.defineProperty(window, 'outerHeight', {{ get: () => h, configurable: true }});
            Object.defineProperty(window, 'screenX',     {{ get: () => 0, configurable: true }});
            Object.defineProperty(window, 'screenY',     {{ get: () => 0, configurable: true }});
            Object.defineProperty(window, 'screenLeft',  {{ get: () => 0, configurable: true }});
            Object.defineProperty(window, 'screenTop',   {{ get: () => 0, configurable: true }});
        }} catch (_) {{}}
    }})();
}})();
"""

    # The init script runs stealth_js first, then our patches.
    # Order matters: stealth_js must set up the basic shims before
    # we override them.
    return stealth_js.STEALTH_JS + "\n" + fingerprint_js


# ═══════════════════════════════════════════════════════════════════════════
# Session fingerprint management
# ═══════════════════════════════════════════════════════════════════════════

_SESSION_FP: dict[str, Any] | None = None
_SESSION_FP_RAW_JS: str | None = None
_INIT_SCRIPT: str | None = None


def get_session_fingerprint() -> dict[str, Any]:
    """Return the current session's fingerprint, generating it lazily."""
    global _SESSION_FP, _SESSION_FP_RAW_JS, _INIT_SCRIPT
    if _SESSION_FP is None:
        _SESSION_FP = generate_session_fingerprint()
        _INIT_SCRIPT = build_init_script()
    return _SESSION_FP


def get_init_script() -> str:
    """Return the current session's init script, building it lazily."""
    get_session_fingerprint()  # ensures _INIT_SCRIPT is set
    assert _INIT_SCRIPT is not None
    return _INIT_SCRIPT


def reset_session() -> None:
    """Reset the session fingerprint. Call this to force a new one."""
    global _SESSION_FP, _INIT_SCRIPT
    _SESSION_FP = None
    _INIT_SCRIPT = None


# ═══════════════════════════════════════════════════════════════════════════
# Search-flow helpers
# ═══════════════════════════════════════════════════════════════════════════

_GOOGLE_SEARCH_URL_RE = re.compile(
    r"^https?://(?:www\.)?google\.(?:com(?:\.[a-z]{2,3})?|[a-z]{2,3}(?:\.[a-z]{2})?)/search",
    re.IGNORECASE,
)


def is_google_search_url(url: str) -> bool:
    """Return True if ``url`` is a Google ``/search`` URL."""
    return bool(_GOOGLE_SEARCH_URL_RE.match(url))


def parse_query_from_url(url: str) -> str:
    """Extract the ``q=`` query from a Google search URL.

    Returns the empty string if no query is present.
    """
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=False)
        if "q" in qs and qs["q"]:
            return qs["q"][0]
    except Exception:
        pass
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# Warmup
# ═══════════════════════════════════════════════════════════════════════════
#
# On a fresh session (no cookies, no behavioral history), the very
# first request to Google is the most suspicious. The first few
# requests to a brand-new profile are very likely to be challenged
# regardless of how good your fingerprint is.
#
# To reduce that risk, we do a "warmup" workflow on the first request
# of each session:
#   1. Navigate to https://www.google.com/ncr
#   2. Scroll / mouse around a bit (look "engaged")
#   3. (Optional) Type a benign query and submit
#   4. Wait on the results page
#   5. Click on one of the results briefly
#   6. Go back
#
# This is opt-in via ANTIDETECT_WARMUP_ON_FIRST_REQUEST. It costs ~10
# seconds of extra time on the first request but reduces CAPTCHA rate
# significantly.

# Module-level state: which sessions have already been warmed up.
# Key is process PID (since we're per-process). Cleared on process
# restart.
import os as _os
_WARMED_UP_SESSIONS: set[int] = set()
_FIRST_REQUEST_LOCK: "Any | None" = None  # lazy init


def is_first_request_in_session() -> bool:
    """Return True if this is the first browser request in this process."""
    return _os.getpid() not in _WARMED_UP_SESSIONS


def mark_session_warmed_up() -> None:
    """Mark the current process as having done its warmup."""
    _WARMED_UP_SESSIONS.add(_os.getpid())


def reset_warmup_state() -> None:
    """Reset warmup state (useful for tests)."""
    _WARMED_UP_SESSIONS.clear()


async def warmup_session(
    context: Any,
    page: Any,
    *,
    force: bool = False,
) -> bool:
    """Run a one-time warmup workflow on a fresh session.

    Returns True if warmup was performed, False if it was skipped
    (already done, or disabled).

    Workflow:
      1. Visit https://www.google.com/ncr
      2. Move the mouse around to look "engaged"
      3. Optional: type a benign query and submit
      4. Wait for results
      5. Optional: click on one result briefly
      6. Go back
    """
    if not config.ANTIDETECT_WARMUP_ON_FIRST_REQUEST and not force:
        return False
    if is_first_request_in_session() and not force:
        pass  # proceed
    elif not force:
        return False

    log.info("Running anti-detect warmup (first request in session)")
    try:
        # Step 1: Visit the homepage
        await page.goto("https://www.google.com/ncr", wait_until="domcontentloaded", timeout=30000)
        await human_sim.human_read(page, duration_sec=random.uniform(1.5, 3.0))
        await simulate_light_browser_activity(page)

        # Step 2: Optional benign search
        if config.ANTIDETECT_WARMUP_QUERIES:
            query = random.choice(config.ANTIDETECT_WARMUP_QUERIES)
            try:
                await search_via_typing(
                    page,
                    query,
                    submit=True,
                    read_after=True,
                )
            except Exception as e:
                log.debug("Warmup typing failed: %s", e)

        # Step 3: Click on one of the results briefly (real users
        # almost always click at least one result)
        try:
            await click_first_result_and_return(page, dwell_sec=random.uniform(1.0, 2.5))
        except Exception as e:
            log.debug("Warmup click failed: %s", e)

        mark_session_warmed_up()
        return True
    except Exception as e:
        log.warning("Warmup failed (continuing): %s", e)
        # Still mark as warmed up so we don't retry
        mark_session_warmed_up()
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Search-via-typing
# ═══════════════════════════════════════════════════════════════════════════
#
# This is the single most important anti-detect change. Instead of:
#
#     await page.goto("https://www.google.com/search?q=python+tutorial")
#
# We do:
#
#     await page.goto("https://www.google.com")           # homepage
#     await human_idle(...)
#     await page.click("input[name=q]")                    # focus search box
#     await human_type(page, "input[name=q]", "python tutorial")  # type it
#     await page.keyboard.press("Enter")                    # submit
#     await page.wait_for_selector("#search")              # wait for results
#
# This is exactly what a real human does. A bot that goes directly to
# /search?q=... is a dead giveaway.

_GOOGLE_SEARCH_INPUT_SELECTORS: list[str] = [
    'input[name="q"]',
    'textarea[name="q"]',
    'input[aria-label="Search"]',
    'input[title="Search"]',
    'input[role="combobox"]',
]


async def _find_google_search_input(page: Any) -> Any:
    """Find the Google search input element."""
    for selector in _GOOGLE_SEARCH_INPUT_SELECTORS:
        try:
            loc = page.locator(selector).first
            count = await loc.count()
            if count > 0:
                # Check it's visible
                box = await loc.bounding_box(timeout=1000)
                if box and box["width"] > 50:
                    return loc
        except Exception:
            continue
    return None


async def search_via_typing(
    page: Any,
    query: str,
    *,
    submit: bool = True,
    read_after: bool = True,
) -> bool:
    """Type a search query into the Google search box and submit.

    Assumes the page is already at the Google homepage (or any Google
    page that has a search box). If not, navigates to the homepage
    first.

    Workflow:
      1. If not on a Google page with a search box, navigate to one
      2. Move mouse to search box, click to focus
      3. Type the query with human-like rhythm
      4. Pause briefly (so the autocomplete suggestions can appear)
      5. Press Enter (or click the search button)
      6. Wait for results to load
      7. (Optional) human_read

    Returns True if the search was submitted successfully.
    """
    if not config.ANTIDETECT_SEARCH_VIA_TYPING:
        return False

    log.info("Search via typing: %r", query)
    try:
        # If we don't see a search box, navigate to the homepage
        current_url = page.url
        if "google.com" not in current_url.lower():
            await page.goto("https://www.google.com/ncr", wait_until="domcontentloaded", timeout=30000)

        # Make sure we have a search box visible
        search_input = await _find_google_search_input(page)
        if search_input is None:
            # Try the consent page — Google sometimes shows a consent
            # dialog that hides the search box. Try dismissing.
            try:
                await page.evaluate(
                    "() => { const btn = document.querySelector('button[aria-label*=\"Accept\" i], button[aria-label*=\"Reject\" i], button#L2AGLb'); if (btn) btn.click(); }"
                )
                await page.wait_for_timeout(random.randint(800, 1500))
            except Exception:
                pass
            search_input = await _find_google_search_input(page)
            if search_input is None:
                log.warning("No search input found on Google homepage")
                return False

        # Click the search input to focus it (real users do this)
        await human_sim.human_click(page, _GOOGLE_SEARCH_INPUT_SELECTORS[0])
        # Small wait after focusing
        await page.wait_for_timeout(random.randint(300, 600))

        # Type the query with human-like rhythm
        ok = await human_sim.human_type(
            page,
            _GOOGLE_SEARCH_INPUT_SELECTORS[0],
            query,
            clear_first=True,
        )
        if not ok:
            log.warning("Failed to type into search box")
            return False

        # Let the autocomplete suggestions appear briefly
        # (a real user pauses here to look at suggestions)
        await page.wait_for_timeout(random.randint(400, 1200))

        # Optional: simulate clicking a suggestion ~30% of the time
        if random.random() < 0.3:
            try:
                # Look for autocomplete suggestions
                suggestion = page.locator('li[role="presentation"]').first
                if await suggestion.count() > 0 and await suggestion.is_visible():
                    await human_sim.human_click(page, 'li[role="presentation"]')
                    # If we clicked a suggestion, we don't need to press Enter
                    submit = False
            except Exception:
                pass

        if submit:
            # Press Enter to submit
            await page.keyboard.press("Enter")

        # Wait for the page to navigate and results to appear
        try:
            await page.wait_for_url(re.compile(r"/search\?"), timeout=15000)
        except Exception:
            # Maybe it didn't navigate; try a direct goto as fallback
            log.debug("Search did not navigate via /search, trying direct")

        # Wait for results to render
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass

        # Short read dwell
        if read_after:
            await human_sim.human_read(page, duration_sec=random.uniform(0.8, 2.0))

        return True
    except Exception as e:
        log.warning("search_via_typing failed: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Tab focus / blur events
# ═══════════════════════════════════════════════════════════════════════════
#
# Real users switch tabs and come back. Bots never do. We fire a
# blur/focus event cycle (with a small delay) to look like the user
# tabbed away briefly. We don't actually need to leave the page — just
# firing the events is enough to register as "user interaction" in
# most bot detection systems.

async def simulate_tab_focus(
    page: Any,
    *,
    fire_visibility: bool = True,
    fire_blur: bool = True,
    min_blur_ms: int = 600,
    max_blur_ms: int = 4500,
) -> None:
    """Simulate the user tabbing away from the page briefly.

    Fires a blur + visibilitychange (hidden) event, waits a random
    interval, then fires focus + visibilitychange (visible) again.
    Only on higher intensity levels — at low/off this is a no-op.
    """
    if not config.ANTIDETECT_TAB_FOCUS_EVENTS:
        return
    if config.ANTIDETECT_LEVEL == "off":
        return
    if config.ANTIDETECT_LEVEL == "low" and random.random() > 0.25:
        return

    # 40-60% chance per call to actually do this (we don't want to
    # do it every single time — that would also be a signal)
    if random.random() > 0.5:
        return

    blur_ms = random.randint(min_blur_ms, max_blur_ms)
    try:
        if fire_blur:
            await page.evaluate("() => window.dispatchEvent(new Event('blur'))")
        if fire_visibility:
            await page.evaluate(
                "() => { Object.defineProperty(document, 'visibilityState', {get: () => 'hidden', configurable: true}); document.dispatchEvent(new Event('visibilitychange')); }"
            )
        await page.wait_for_timeout(blur_ms)
        if fire_visibility:
            await page.evaluate(
                "() => { Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true}); document.dispatchEvent(new Event('visibilitychange')); }"
            )
        if fire_blur:
            await page.evaluate("() => window.dispatchEvent(new Event('focus'))")
    except Exception as e:
        log.debug("Tab focus simulation failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# Reading mouse track
# ═══════════════════════════════════════════════════════════════════════════
#
# On a search results page, real users hover their mouse over the
# result titles they're considering clicking. We simulate this by
# moving the mouse along the result list, hovering briefly on each
# visible title, before finally "deciding" to read further down or
# scroll back.

async def reading_mouse_track(
    page: Any,
    *,
    min_hovers: int = 1,
    max_hovers: int = 3,
    selector: str = "h3",
) -> None:
    """Move the mouse along result titles like a real user reading.

    Identifies the first few ``selector`` elements (default: ``h3``,
    which is what Google uses for result titles) and hovers the mouse
    over each one briefly. This adds realistic mouse-track telemetry
    that bot detection looks for.

    Only runs at medium/high intensity.
    """
    if config.ANTIDETECT_LEVEL in ("off", "low"):
        return
    if random.random() > 0.6:  # not every call
        return
    n = random.randint(min_hovers, max_hovers)
    try:
        locs = page.locator(selector)
        count = await locs.count()
        if count == 0:
            return
        n = min(n, count)
        for i in range(n):
            loc = locs.nth(i)
            try:
                box = await loc.bounding_box(timeout=1000)
                if not box or box["width"] < 10:
                    continue
                # Slightly off-center (humans don't hit exact centers)
                tx = box["x"] + box["width"] * random.uniform(0.25, 0.75)
                ty = box["y"] + box["height"] * random.uniform(0.4, 0.6)
                await human_sim.human_mouse_move(page, tx, ty)
                # Hover for a bit
                await page.wait_for_timeout(int(random.gauss(450, 120)))
            except Exception:
                continue
    except Exception as e:
        log.debug("reading_mouse_track failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# Light browser activity (used in warmup)
# ═══════════════════════════════════════════════════════════════════════════

async def simulate_light_browser_activity(page: Any) -> None:
    """Small mouse moves + micro-scrolls to look like a real user.

    Used in the warmup workflow and after a fresh page load.
    """
    if config.ANTIDETECT_LEVEL == "off":
        return
    try:
        # 1-3 small mouse moves to different viewport positions
        n = random.randint(1, 3)
        for _ in range(n):
            vw, vh = await human_sim._safe_page_size(page)
            tx = random.randint(int(vw * 0.2), int(vw * 0.8))
            ty = random.randint(int(vh * 0.2), int(vh * 0.8))
            await human_sim.human_mouse_move(page, tx, ty)
            await page.wait_for_timeout(random.randint(80, 250))
        # A small scroll
        if random.random() < 0.5:
            await human_sim.human_scroll(
                page,
                random.choice([-100, -50, 50, 100, 150]),
                overshoot_chance=0.1,
            )
    except Exception as e:
        log.debug("simulate_light_browser_activity failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# Click first result and return (for warmup)
# ═══════════════════════════════════════════════════════════════════════════

async def click_first_result_and_return(
    page: Any,
    dwell_sec: float = 2.0,
) -> bool:
    """Click the first search result, dwell briefly, then go back.

    Used in the warmup workflow to make the session look "engaged".
    """
    try:
        # Find first result link
        first_result = page.locator('div#search a[href^="http"]:not([href*="google.com"])').first
        count = await first_result.count()
        if count == 0:
            # Fallback: any visible result h3
            first_result = page.locator('h3').first
            count = await first_result.count()
            if count == 0:
                return False
        # Click it
        ok = await human_sim.human_click(page, 'div#search a[href^="http"]:not([href*="google.com"])')
        if not ok:
            return False
        # Wait for the result page to load
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        # Dwell briefly (humans actually read the result)
        await human_sim.human_read(page, duration_sec=dwell_sec)
        # Go back
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=10000)
        except Exception:
            pass
        return True
    except Exception as e:
        log.debug("click_first_result_and_return failed: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Anti-detect status (for /health)
# ═══════════════════════════════════════════════════════════════════════════

def get_antidetect_status() -> dict[str, Any]:
    """Return a snapshot of the current anti-detect configuration + state."""
    fp = get_session_fingerprint()
    return {
        "level": config.ANTIDETECT_LEVEL,
        "search_via_typing": config.ANTIDETECT_SEARCH_VIA_TYPING,
        "warmup_on_first_request": config.ANTIDETECT_WARMUP_ON_FIRST_REQUEST,
        "tab_focus_events": config.ANTIDETECT_TAB_FOCUS_EVENTS,
        "client_hints": config.ANTIDETECT_CLIENT_HINTS,
        "randomize_fingerprint": config.ANTIDETECT_RANDOMIZE_FINGERPRINT,
        "visit_homepage_first": config.ANTIDETECT_VISIT_HOMEPAGE_FIRST,
        "warmup_queries": list(config.ANTIDETECT_WARMUP_QUERIES),
        "session_warmed_up": not is_first_request_in_session(),
        "session_fingerprint": {
            "viewport": fp["viewport"],
            "platform": fp["platform"],
            "chrome_version": fp["chrome_version"],
            "webgl_vendor": fp["webgl_vendor"],
            "webgl_renderer": fp["webgl_renderer"],
            "hardware_concurrency": fp["hardware_concurrency"],
            "device_memory": fp["device_memory"],
            "locale": fp["locale"],
            "timezone": fp["timezone"],
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# CLI for ad-hoc testing
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:  # pragma: no cover
    """Print the current session fingerprint and config to stdout."""
    print("=== anti_detect status ===")
    print(json.dumps(get_antidetect_status(), indent=2))
    print()
    print("=== init script (first 500 chars) ===")
    print(get_init_script()[:500])
    print("...")
    print()
    print("=== fingerprint test ===")
    fp = generate_session_fingerprint(seed=12345)
    print(f"UA: {fp['user_agent']}")
    print(f"Viewport: {fp['viewport']}")
    print(f"WebGL: {fp['webgl_vendor']} / {fp['webgl_renderer']}")
    print(f"Locale: {fp['locale']}, Timezone: {fp['timezone']}")
    print(f"Client Hints: {fp['sec_ch_ua']}")
    print()
    print("=== URL parsing ===")
    test_urls = [
        "https://www.google.com/search?q=python+tutorial&num=10",
        "https://google.com/search?q=hello%20world",
        "https://www.google.co.uk/search?q=foo&hl=en",
        "https://example.com/not-google",
        "https://www.google.com/",
    ]
    for u in test_urls:
        print(f"  {u}")
        print(f"    is_google_search: {is_google_search_url(u)}")
        print(f"    query:             {parse_query_from_url(u)!r}")


if __name__ == "__main__":  # pragma: no cover
    main()
