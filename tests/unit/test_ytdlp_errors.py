"""yt-dlp rejections must produce a message that names the remedy.

Context
-------
``list_subtitles`` returned *"Subtitle lookup failed. Please try again."* when
YouTube answered:

    ERROR: [youtube] aqz-KE-bpKQ: Sign in to confirm you're not a bot.
    Use --cookies-from-browser or --cookies for the authentication.

Two things were wrong with that outcome:

* the instruction "please try again" cannot work — YouTube will reject the same
  request again; the cookie jar has to be refreshed; and
* the only useful fact, that it was an authentication rejection, was thrown away
  by ``format_error`` (which is correct for unexpected exceptions, but this one is
  a known condition with a known fix).

``ytdlp.describe_error`` recognises the condition and writes the remedy; anything
it does not recognise returns ``None`` so the caller keeps the generic message and
no internal detail leaks.
"""

from __future__ import annotations

import pytest

from google_search_mcp import config
from google_search_mcp.utils import ytdlp


class _YtdlpError(Exception):
    """Stands in for yt_dlp.utils.DownloadError (same text, no import cost)."""


BOT_REJECTION = (
    "ERROR: [youtube] aqz-KE-bpKQ: Sign in to confirm you\u2019re not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication. "
    "See https://github.com/yt-dlp/yt-dlp/wiki/FAQ"
)


def test_bot_rejection_names_the_cookie_file_and_the_fix(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_COOKIE_PATH", "./cookies/youtube_cookies.txt")
    msg = ytdlp.describe_error(_YtdlpError(BOT_REJECTION))

    assert msg is not None
    assert "cookies/youtube_cookies.txt" in msg, "the message must name the file to refresh"
    assert "--manual-browser" in msg, "the message must say how to log in"
    assert "Netscape" in msg, "yt-dlp needs Netscape format; say so"
    assert "try again" not in msg.casefold(), "retrying cannot fix an auth rejection"


def test_bot_rejection_mentions_a_missing_cookie_file(monkeypatch, tmp_path):
    missing = str(tmp_path / "absent_cookies.txt")
    monkeypatch.setattr(config, "YOUTUBE_COOKIE_PATH", missing)
    msg = ytdlp.describe_error(_YtdlpError(BOT_REJECTION))

    assert msg is not None
    assert "No YouTube cookie file" in msg
    assert missing in msg


@pytest.mark.parametrize(
    "text, expected",
    [
        ("ERROR: [youtube] x: Private video. Sign in if you have been granted access",
         "unavailable or private"),
        ("ERROR: [youtube] x: Video unavailable", "unavailable or private"),
        ("ERROR: [youtube] x: This video is members-only", "members-only"),
    ],
)
def test_unavailable_video_is_explained(text, expected):
    msg = ytdlp.describe_error(_YtdlpError(text))
    assert msg is not None and expected in msg


@pytest.mark.parametrize(
    "text",
    [
        "ERROR: unable to download webpage: <urlopen error timed out>",
        "Postprocessing: ffprobe and ffmpeg not found",
        "something completely different",
    ],
)
def test_unrecognised_errors_fall_through(text):
    """Unknown failures must not be given a guessed explanation."""
    assert ytdlp.describe_error(_YtdlpError(text)) is None


def test_marker_match_is_case_insensitive():
    msg = ytdlp.describe_error(_YtdlpError("SIGN IN TO CONFIRM you're not a bot"))
    assert msg is not None
