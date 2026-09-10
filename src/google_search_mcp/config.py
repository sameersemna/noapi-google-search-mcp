"""Centralized configuration for the Google Search MCP server.

All paths, constants, presets, and maps are defined here as the single
source of truth. Import from this module rather than defining literals
in individual tool modules.
"""

import os
from pathlib import Path

# Import comprehensive stealth JS from its dedicated module
from .stealth_js import STEALTH_JS  # noqa: F401

# ---------------------------------------------------------------------------
# User agent
# ---------------------------------------------------------------------------

USER_AGENT: str = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# File-system paths
# ---------------------------------------------------------------------------

HOME: str = os.path.expanduser("~")
CACHE_DIR: str = os.path.join(HOME, ".cache", "noapi-google-search-mcp")
CONFIG_DIR: str = os.path.join(HOME, ".config")

# Browser persistent profile
BROWSER_DATA_DIR: str = os.path.join(CONFIG_DIR, "google-mcp-browser")

# Cookies
COOKIE_JSON_PATH: str = os.path.join(HOME, ".google_mcp_cookies.json")
COOKIE_DIR: str = os.path.join(os.path.curdir, "cookies")

# CAPTCHA solver model
CAPTCHA_MODEL_DIR: str = os.path.join(HOME, ".google_mcp_models")
MOBILENET_ONNX_PATH: str = os.path.join(CAPTCHA_MODEL_DIR, "mobilenetv2-12.onnx")
IMAGENET_LABELS_PATH: str = os.path.join(CAPTCHA_MODEL_DIR, "imagenet_labels.json")

# Transcription / video
TRANSCRIBE_CACHE_DIR: str = os.path.join(CACHE_DIR)
TRANSCRIPT_CACHE_DIR: str = os.path.join(CACHE_DIR, "transcripts")
VIDEO_CACHE_DIR: str = os.path.join(CACHE_DIR, "videos")
CLIPS_DIR: str = os.path.join(HOME, "clips")

# Feeds database
FEEDS_DB_PATH: str = os.path.join(CACHE_DIR, "feeds.db")

# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

# Set to "1" to skip cookie validation at server startup (for development/testing)
SKIP_COOKIE_VALIDATION: bool = os.environ.get("SKIP_COOKIE_VALIDATION", "").strip().lower() in ("1", "true", "yes")

# Image discovery
IMAGE_EXTENSIONS: set[str] = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".tiff", ".tif",
}
DEFAULT_IMAGE_DIR: str = os.path.expanduser("~/lens")

# ---------------------------------------------------------------------------
# Google search time-range parameter map
# ---------------------------------------------------------------------------

TIME_RANGE_MAP: dict[str, str] = {
    "past_hour": "qdr:h",
    "past_day": "qdr:d",
    "past_week": "qdr:w",
    "past_month": "qdr:m",
    "past_year": "qdr:y",
}

# ---------------------------------------------------------------------------
# Language codes for translation
# ---------------------------------------------------------------------------

LANGUAGE_CODES: dict[str, str] = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de",
    "italian": "it", "portuguese": "pt", "japanese": "ja", "korean": "ko",
    "chinese": "zh-CN", "arabic": "ar", "russian": "ru", "hindi": "hi",
    "marathi": "mr",
    "dutch": "nl", "swedish": "sv", "turkish": "tr", "polish": "pl",
    "thai": "th", "vietnamese": "vi", "indonesian": "id", "greek": "el",
    "hebrew": "he", "czech": "cs", "danish": "da", "finnish": "fi",
    "norwegian": "no", "romanian": "ro", "hungarian": "hu", "ukrainian": "uk",
}

LANG_DETECTION_CONFIDENCE_THRESHOLD: float = 0.80

# ---------------------------------------------------------------------------
# Feed subscription presets
# ---------------------------------------------------------------------------

PRESET_NEWS_FEEDS: dict[str, dict[str, str]] = {
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

ARXIV_CATEGORIES: dict[str, str] = {
    "ai": "cs.AI", "ml": "cs.LG", "cv": "cs.CV", "nlp": "cs.CL",
    "robotics": "cs.RO", "crypto": "cs.CR", "systems": "cs.DC",
    "hci": "cs.HC",
}

# ---------------------------------------------------------------------------
# IMAP server auto-detection
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
    "protonmail.com": "127.0.0.1",
    "proton.me": "127.0.0.1",
}

# ---------------------------------------------------------------------------
# CAPTCHA solver — ImageNet class mapping
# ---------------------------------------------------------------------------

CAPTCHA_CLASS_MAP: dict[str, list[int]] = {
    "traffic light": [920],
    "bus": [654, 779, 874],
    "bicycle": [444, 671],
    "motorcycle": [670, 665],
    "car": [436, 468, 511, 609, 656, 717, 751, 817],
    "taxi": [468],
    "cab": [468],
    "crosswalk": [],
    "bridge": [839],
    "boat": [472, 484, 554, 625, 814, 914],
    "airplane": [404, 405],
    "plane": [404, 405],
    "train": [466, 547, 820, 829],
    "truck": [555, 569, 656, 675, 717, 864, 867],
    "fire hydrant": [],
    "hydrant": [],
    "parking meter": [705],
    "stair": [],
    "mountain": [970, 972, 976, 979, 980],
    "palm": [],
    "chimney": [],
    "tractor": [866],
}

IMAGENET_MEAN: list[float] = [0.485, 0.456, 0.406]
IMAGENET_STD: list[float] = [0.229, 0.224, 0.225]

# ---------------------------------------------------------------------------
# Screenshot debugging — set SCREENSHOTS_DIR to enable, or None to disable
# ---------------------------------------------------------------------------

SCREENSHOTS_DIR: str | None = os.path.join(os.path.curdir, "screenshots")
# Set to None to disable screenshot capture entirely:
# SCREENSHOTS_DIR = None

# ---------------------------------------------------------------------------
# Manual intervention — open a headful browser when bot detection fails
# ---------------------------------------------------------------------------
# When all automatic anti-bot measures (stealth patches, CAPTCHA solve, retries)
# have been exhausted and Google is still blocking us, the server can open a
# visible (headful) browser window so the user can manually solve the
# CAPTCHA, log into their Google account, or complete a 2FA/verification
# challenge. The request is paused until the user resolves the issue (or
# the timeout expires), and then it retries automatically.
#
# Set ENABLE_MANUAL_INTERVENTION=0 to disable and always fall back to
# alternative search providers (DuckDuckGo, etc.) instead of opening a
# headful window. Default: enabled.
ENABLE_MANUAL_INTERVENTION: bool = os.environ.get(
    "ENABLE_MANUAL_INTERVENTION", "1"
).strip().lower() in ("1", "true", "yes", "on")

# How long (seconds) to wait for the user to resolve the block before
# giving up and falling back. Set to 0 to wait indefinitely.
MANUAL_INTERVENTION_TIMEOUT_SEC: int = int(
    os.environ.get("MANUAL_INTERVENTION_TIMEOUT_SEC", "300")
)

# How often (seconds) to poll the headful browser for resolution.
MANUAL_INTERVENTION_POLL_SEC: float = float(
    os.environ.get("MANUAL_INTERVENTION_POLL_SEC", "3.0")
)

# Reasons we may trigger manual intervention. Exposed for documentation and
# the MCP `open_manual_browser` tool which can be called explicitly.
MANUAL_INTERVENTION_REASONS: tuple[str, ...] = (
    "captcha",      # reCAPTCHA / image challenge / checkbox
    "login",        # Google sign-in page
    "rate_limit",   # "unusual traffic from your computer network"
    "consent",      # Persistent consent dialog
    "verification", # 2FA / phone verification
    "unknown",      # Fallback — page looks blocked but no specific indicator
)

# ---------------------------------------------------------------------------
# Human-like behavior — controls how much "real human" simulation we add
# ---------------------------------------------------------------------------
# Bot-detection systems (Google reCAPTCHA, "unusual traffic" checks)
# fingerprint automation by looking at *behavior* — not just by inspecting
# the JS environment. Real humans have:
#   - Bezier-curve mouse trajectories with variable speed (not straight lines)
#   - Hover + aim + pause + click (not instant clicks)
#   - Smooth scroll with momentum decay (not jump scrolls)
#   - Variable typing speed with bursts and pauses
#   - Idle micro-movements when "reading"
#   - Dwell time on links before clicking
#
# We simulate all of this. The level is tunable:
#   off    — no extra delays or movements (fastest, most detectable)
#   low    — short delays, single mouse move per page
#   medium — full Bezier, smooth scroll, natural typing (default)
#   high   — all of the above + idle micro-movements + reading pauses
#              + occasional back-scroll (slowest, most realistic)
HUMAN_BEHAVIOR_LEVEL: str = os.environ.get("HUMAN_BEHAVIOR_LEVEL", "medium").strip().lower()
if HUMAN_BEHAVIOR_LEVEL not in ("off", "low", "medium", "high"):
    HUMAN_BEHAVIOR_LEVEL = "medium"

# Optional: disable mouse-jitter completely (for very fast / scripted use)
HUMAN_BEHAVIOR_DISABLE_JITTER: bool = os.environ.get(
    "HUMAN_BEHAVIOR_DISABLE_JITTER", ""
).strip().lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Advanced anti-detect — fingerprint randomization + search flow + warmup
# ---------------------------------------------------------------------------
# These options go BEYOND "human-like behavior" and address the deeper
# signals that modern Google reCAPTCHA and "unusual traffic" filters
# look at: TLS / canvas / WebGL / AudioContext fingerprints, the search
# flow itself (humans go to google.com and TYPE — they don't hit
# /search?q=... directly), and the trust history of the profile.
#
# Disabling any of these makes the server look more bot-like. The defaults
# are tuned for the lowest CAPTCHA rate we can get without slowing the
# server down too much.

# Master switch: off|low|medium|high. Scales how aggressive the patches
# and flows are. "high" adds more wait time but looks more human.
ANTIDETECT_LEVEL: str = os.environ.get("ANTIDETECT_LEVEL", "medium").strip().lower()
if ANTIDETECT_LEVEL not in ("off", "low", "medium", "high"):
    ANTIDETECT_LEVEL = "medium"

# Whether to navigate to google.com and TYPE the query in the search box
# instead of going directly to /search?q=... . This is the single most
# impactful change — humans never hit /search directly.
ANTIDETECT_SEARCH_VIA_TYPING: bool = os.environ.get(
    "ANTIDETECT_SEARCH_VIA_TYPING", "1"
).strip().lower() in ("1", "true", "yes", "on")

# Whether to do a one-time "warmup" on a fresh session: a benign search
# (e.g. "weather today"), click on a result briefly, then return. This
# builds up cookie diversity and trust tokens so the first REAL search
# on a fresh session doesn't look suspicious.
ANTIDETECT_WARMUP_ON_FIRST_REQUEST: bool = os.environ.get(
    "ANTIDETECT_WARMUP_ON_FIRST_REQUEST", "1"
).strip().lower() in ("1", "true", "yes", "on")

# Whether to fire occasional blur/focus events on the page (simulating
# the user switching tabs). This is a small but consistent signal of
# "real user is interacting with the browser".
ANTIDETECT_TAB_FOCUS_EVENTS: bool = os.environ.get(
    "ANTIDETECT_TAB_FOCUS_EVENTS", "1"
).strip().lower() in ("1", "true", "yes", "on")

# Whether to add Sec-CH-UA / Sec-Fetch-* / Accept-Language client-hint
# headers. Modern Chrome sends these automatically; without them, Google
# sees a request that looks like an old browser.
ANTIDETECT_CLIENT_HINTS: bool = os.environ.get(
    "ANTIDETECT_CLIENT_HINTS", "1"
).strip().lower() in ("1", "true", "yes", "on")

# Whether to randomize the canvas / WebGL / AudioContext fingerprint
# per-session (vs. using a fixed fingerprint that could be correlated
# across requests). This is the second most impactful change after
# search-via-typing.
ANTIDETECT_RANDOMIZE_FINGERPRINT: bool = os.environ.get(
    "ANTIDETECT_RANDOMIZE_FINGERPRINT", "1"
).strip().lower() in ("1", "true", "yes", "on")

# Comma-separated list of benign queries to use during session warmup.
# Pick one at random per session. Make them look like a real person's
# casual browsing.
ANTIDETECT_WARMUP_QUERIES: tuple[str, ...] = tuple(
    q.strip() for q in os.environ.get(
        "ANTIDETECT_WARMUP_QUERIES",
        "weather today,news today,time now,calculator,translate hello",
    ).split(",") if q.strip()
)

# Whether to also run a "trust building" workflow: visit the Google
# homepage, scroll a bit, then go to the search. Done once per session.
ANTIDETECT_VISIT_HOMEPAGE_FIRST: bool = os.environ.get(
    "ANTIDETECT_VISIT_HOMEPAGE_FIRST", "1"
).strip().lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Search-result redirect resolution
# ---------------------------------------------------------------------------
# Google wraps every organic SERP result in a redirect URL (/url?q=... or
# /goto?url=...). We resolve these to the final destination before returning
# results. These settings tune that resolution.
#
# REDIRECT_RESOLVE_TIMEOUT: per-URL timeout (seconds) for following a /goto
#   redirect. A slow redirect shouldn't stall the whole search.
# REDIRECT_RESOLVE_CACHE_SIZE: max number of resolved redirects to memoize
#   in-process. Google reuses the same /goto token across searches, so caching
#   avoids re-following the same redirect (faster + fewer requests to Google).
#   Set to 0 to disable caching.
REDIRECT_RESOLVE_TIMEOUT: int = int(os.environ.get("REDIRECT_RESOLVE_TIMEOUT", "8"))
REDIRECT_RESOLVE_CACHE_SIZE: int = int(
    os.environ.get("REDIRECT_RESOLVE_CACHE_SIZE", "512")
)

# ---------------------------------------------------------------------------
# Health server — separate HTTP endpoint for /health, /version, etc.
# ---------------------------------------------------------------------------
# Runs in the same Python process as the MCP server, on its own port
# (default 11499). Independent of the MCP transport — works whether the
# server is launched with stdio (mcp-proxy) or streamable_http.
#
# Set ENABLE_HEALTH_SERVER=0 to disable.
ENABLE_HEALTH_SERVER: bool = os.environ.get(
    "ENABLE_HEALTH_SERVER", "1"
).strip().lower() in ("1", "true", "yes", "on")

HEALTH_HOST: str = os.environ.get("HEALTH_HOST", "0.0.0.0")
HEALTH_PORT: int = int(os.environ.get("HEALTH_PORT", "11499"))

# Optional bearer token for /health auth (e.g. for k8s probes that share
# the endpoint publicly). Empty / unset = no auth required.
HEALTH_AUTH_TOKEN: str = os.environ.get("HEALTH_AUTH_TOKEN", "").strip()

# ---------------------------------------------------------------------------
# Rate limiting — minimum gap (seconds) between requests to Google
# ---------------------------------------------------------------------------

GOOGLE_REQUEST_MIN_GAP: float = 3.0  # seconds between Google requests

# ---------------------------------------------------------------------------
# Retry / backoff settings
# ---------------------------------------------------------------------------

MAX_GOOGLE_RETRIES: int = 2
RETRY_BACKOFF_SECONDS: list[float] = [1.0, 3.0, 5.0]

# ---------------------------------------------------------------------------
# General limits
# ---------------------------------------------------------------------------

MAX_PAGE_CHARS: int = 8000
MAX_OBJECTS: int = 4
