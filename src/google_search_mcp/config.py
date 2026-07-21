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
