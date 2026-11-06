"""The manual browser must refresh yt-dlp's cookie jars, not just the JSON store.

Why this exists
---------------
``--manual-browser`` saved Playwright cookies to ``~/.google_mcp_cookies.json``.
The browser-based tools read that file, so logging in fixed Google scraping — but
yt-dlp reads **Netscape-format** jars, so ``cookies/youtube_cookies.txt`` stayed
stale and YouTube kept answering:

    Sign in to confirm you're not a bot. Use --cookies-from-browser ...

The advice in that message ("log in with --manual-browser") therefore could not
work for YouTube. ``export_ytdlp_cookie_files`` closes that gap.

Every test here redirects the configured paths into ``tmp_path``: these functions
write files, and a test must never touch the operator's real credentials.
"""

from __future__ import annotations

import pytest

from google_search_mcp import browser, config


def _cookie(domain: str, name: str = "SID", value: str = "abc") -> dict:
    return {
        "domain": domain,
        "path": "/",
        "secure": True,
        "expires": 1900000000,
        "name": name,
        "value": value,
    }


@pytest.fixture
def jars(monkeypatch, tmp_path):
    """Point both configured jar paths at tmp_path and return them."""
    yt = tmp_path / "youtube_cookies.txt"
    gg = tmp_path / "google_cookies.txt"
    monkeypatch.setattr(config, "YOUTUBE_COOKIE_PATH", str(yt))
    monkeypatch.setattr(config, "GOOGLE_COOKIE_PATH", str(gg))
    return yt, gg


def test_youtube_cookies_are_written_in_netscape_format(jars):
    yt, gg = jars
    written = browser.export_ytdlp_cookie_files([_cookie(".youtube.com")])

    assert written == {str(yt): 1}
    assert not gg.exists(), "no Google cookies seen, so no Google jar"
    text = yt.read_text()
    assert text.startswith("# Netscape HTTP Cookie File")
    assert ".youtube.com" in text and "\tSID\tabc" in text


def test_google_cookies_go_to_the_google_jar(jars):
    yt, gg = jars
    written = browser.export_ytdlp_cookie_files([_cookie(".google.com", "NID", "xyz")])

    assert written == {str(gg): 1}
    assert not yt.exists()
    assert "\tNID\txyz" in gg.read_text()


def test_both_jars_are_written_from_one_selection(jars):
    yt, gg = jars
    written = browser.export_ytdlp_cookie_files([
        _cookie(".youtube.com", "A", "1"),
        _cookie(".google.com", "B", "2"),
        _cookie(".example.com", "C", "3"),
    ])
    assert written == {str(yt): 1, str(gg): 1}
    assert ".example.com" not in yt.read_text() + gg.read_text()


def test_empty_selection_never_truncates_a_good_jar(jars):
    """A failed login must not destroy working credentials."""
    yt, gg = jars
    yt.write_text("# Netscape HTTP Cookie File\n.youtu.be\tTRUE\t/\tTRUE\t1\tK\tV\n")
    before = yt.read_text()

    assert browser.export_ytdlp_cookie_files([]) == {}
    assert yt.read_text() == before


def test_unrelated_cookies_do_not_truncate_either(jars):
    yt, _gg = jars
    yt.write_text("# Netscape HTTP Cookie File\n.youtu.be\tTRUE\t/\tTRUE\t1\tK\tV\n")
    before = yt.read_text()

    assert browser.export_ytdlp_cookie_files([_cookie(".example.com")]) == {}
    assert yt.read_text() == before


def test_relative_paths_resolve_against_the_repo_root():
    """The server runs from the repo root; a CLI run from elsewhere must match it."""
    resolved = browser._resolve_cookie_path("./cookies/youtube_cookies.txt")
    assert resolved.endswith("noapi-google-search-mcp/cookies/youtube_cookies.txt")
    assert resolved.startswith("/")


def test_absolute_paths_are_left_alone(tmp_path):
    p = str(tmp_path / "x.txt")
    assert browser._resolve_cookie_path(p) == p


def test_manual_browser_uses_the_exporting_saver():
    """Wiring check: the save loop must call _save_all, not save_cookies only."""
    import inspect

    from google_search_mcp import manual_browser

    src = inspect.getsource(manual_browser)
    assert "_save_all(context)" in src
    assert "export_ytdlp_cookie_files" in src

    saver = inspect.getsource(manual_browser._save_all)
    assert "save_cookies" in saver, "the JSON store must still be written"
    assert "export_ytdlp_cookie_files" in saver, "yt-dlp's jars must be written too"
