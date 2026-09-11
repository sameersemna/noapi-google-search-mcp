"""Feed subscription tools — subscribe, check, search, and browse feeds.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Annotated

from pydantic import AliasChoices, Field

from mcp.server.fastmcp import Context
from playwright.async_api import async_playwright

from ..config import (
    ARXIV_CATEGORIES,
    FEEDS_DB_PATH,
    PRESET_NEWS_FEEDS,
    TRANSCRIBE_CACHE_DIR,
    TRANSCRIPT_CACHE_DIR,
)
from ..server import (
    mcp,
    _download_audio,
    _fetch_url_bytes,
    _transcribe_audio,
    _transcript_cache_path,
    format_timestamp,
    launch_browser,
    strip_html,
)


# Database
# ---------------------------------------------------------------------------

def _get_feeds_db() -> sqlite3.Connection:
    """Open (and initialise if needed) the feeds SQLite database."""
    db_path = os.environ.get("FEEDS_DB_PATH", FEEDS_DB_PATH)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_feeds_db(conn)
    return conn


def _init_feeds_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            identifier TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            feed_url TEXT NOT NULL DEFAULT '',
            last_checked TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source_type, identifier)
        );
        CREATE TABLE IF NOT EXISTS feed_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            source_type TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            UNIQUE(subscription_id, url),
            FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_items_sub ON feed_items(subscription_id);
        CREATE INDEX IF NOT EXISTS idx_items_type ON feed_items(source_type);
        CREATE INDEX IF NOT EXISTS idx_items_pub ON feed_items(published_at DESC);
    """)
    # FTS5 full-text search index
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS feed_items_fts USING fts5(
                title, content, author, content='feed_items', content_rowid='id'
            )
        """)
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS fts_ai AFTER INSERT ON feed_items BEGIN
                INSERT INTO feed_items_fts(rowid, title, content, author)
                VALUES (new.id, new.title, new.content, new.author);
            END;
            CREATE TRIGGER IF NOT EXISTS fts_ad AFTER DELETE ON feed_items BEGIN
                INSERT INTO feed_items_fts(feed_items_fts, rowid, title, content, author)
                VALUES ('delete', old.id, old.title, old.content, old.author);
            END;
        """)
    except Exception:
        pass  # FTS5 not available on this build — LIKE fallback used
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_rss_atom(xml_bytes: bytes) -> list[dict]:
    """Parse RSS 2.0 or Atom feed XML into a flat list of items."""
    root = ET.fromstring(xml_bytes)
    items: list[dict] = []

    # --- RSS 2.0 (<channel><item>) ---
    for item in root.iter("item"):
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "url": (item.findtext("link") or "").strip(),
            "content": strip_html(item.findtext("description") or ""),
            "published": (item.findtext("pubDate") or "").strip(),
            "author": (
                item.findtext("{http://purl.org/dc/elements/1.1/}creator")
                or item.findtext("author") or ""
            ).strip(),
        })
    if items:
        return items

    # --- Atom (<entry> with namespace) ---
    # NOTE: ElementTree leaf elements are falsy — never chain find() with `or`.
    atom = "http://www.w3.org/2005/Atom"
    media = "http://search.yahoo.com/mrss/"
    for entry in root.iter(f"{{{atom}}}entry"):
        link_el = entry.find(f"{{{atom}}}link[@rel='alternate']")
        if link_el is None:
            link_el = entry.find(f"{{{atom}}}link")

        content_el = entry.find(f"{{{atom}}}content")
        if content_el is None:
            content_el = entry.find(f"{{{atom}}}summary")
        if content_el is None:
            content_el = entry.find(f"{{{media}}}group/{{{media}}}description")

        pub_el = entry.find(f"{{{atom}}}published")
        if pub_el is None:
            pub_el = entry.find(f"{{{atom}}}updated")

        author_el = entry.find(f"{{{atom}}}author/{{{atom}}}name")
        items.append({
            "title": (entry.findtext(f"{{{atom}}}title") or "").strip(),
            "url": link_el.get("href", "") if link_el is not None else "",
            "content": strip_html(
                content_el.text if content_el is not None and content_el.text else ""
            ),
            "published": (
                pub_el.text if pub_el is not None and pub_el.text else ""
            ).strip(),
            "author": (
                author_el.text if author_el is not None and author_el.text else ""
            ).strip(),
        })
    if items:
        return items

    # --- Atom without namespace (fallback) ---
    for entry in root.iter("entry"):
        link_el = entry.find("link[@rel='alternate']")
        if link_el is None:
            link_el = entry.find("link")

        content_el = entry.find("content")
        if content_el is None:
            content_el = entry.find("summary")

        pub_el = entry.find("published")
        if pub_el is None:
            pub_el = entry.find("updated")

        author_el = entry.find("author/name")
        items.append({
            "title": (entry.findtext("title") or "").strip(),
            "url": link_el.get("href", "") if link_el is not None else "",
            "content": strip_html(
                content_el.text if content_el is not None and content_el.text else ""
            ),
            "published": (
                pub_el.text if pub_el is not None and pub_el.text else ""
            ).strip(),
            "author": (
                author_el.text if author_el is not None and author_el.text else ""
            ).strip(),
        })
    return items


def _store_items(
    conn: sqlite3.Connection, sub_id: int, source_type: str, items: list[dict]
) -> int:
    """Store feed items in the database; skip duplicates. Returns new-item count."""
    new_count = 0
    now = datetime.now(timezone.utc).isoformat()
    for item in items:
        url = item.get("url", "")
        if not url:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO feed_items
               (subscription_id, source_type, title, content, url, author,
                published_at, fetched_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sub_id, source_type,
                item.get("title", ""), item.get("content", ""),
                url, item.get("author", ""),
                item.get("published", ""), now,
                item.get("metadata", "{}"),
            ),
        )
        if cur.rowcount > 0:
            new_count += 1
    conn.commit()
    return new_count


# ---------------------------------------------------------------------------
# Source-specific fetch functions
# ---------------------------------------------------------------------------

async def _check_source_rss(feed_url: str) -> list[dict]:
    """Fetch and parse any RSS/Atom feed."""
    data = await asyncio.to_thread(_fetch_url_bytes, feed_url)
    return _parse_rss_atom(data)


async def _check_source_reddit(subreddit: str) -> list[dict]:
    """Fetch recent posts from a subreddit via its native RSS feed."""
    url = f"https://www.reddit.com/r/{subreddit}/.rss"
    data = await asyncio.to_thread(_fetch_url_bytes, url)
    return _parse_rss_atom(data)


async def _check_source_hackernews(
    feed_type: str = "top", limit: int = 30
) -> list[dict]:
    """Fetch top/new/best stories from the Hacker News public API."""
    type_map = {"top": "topstories", "new": "newstories", "best": "beststories"}
    endpoint = type_map.get(feed_type, "topstories")
    url = f"https://hacker-news.firebaseio.com/v0/{endpoint}.json"

    data = await asyncio.to_thread(_fetch_url_bytes, url)
    story_ids = json.loads(data)[:limit]

    async def _get(sid: int):
        try:
            raw = await asyncio.to_thread(
                _fetch_url_bytes,
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
            )
            return json.loads(raw)
        except Exception:
            return None

    stories = await asyncio.gather(*[_get(sid) for sid in story_ids])

    items = []
    for s in stories:
        if not s or s.get("type") != "story":
            continue
        items.append({
            "title": s.get("title", ""),
            "url": s.get(
                "url", f"https://news.ycombinator.com/item?id={s['id']}"
            ),
            "content": strip_html(s.get("text", "")),
            "published": (
                datetime.fromtimestamp(s["time"], tz=timezone.utc).isoformat()
                if s.get("time") else ""
            ),
            "author": s.get("by", ""),
            "metadata": json.dumps({
                "score": s.get("score", 0),
                "comments": s.get("descendants", 0),
                "hn_id": s.get("id"),
            }),
        })
    return items


async def _check_source_github(repo: str) -> list[dict]:
    """Fetch releases (or commits) for a public GitHub repository."""
    url = f"https://github.com/{repo}/releases.atom"
    try:
        data = await asyncio.to_thread(_fetch_url_bytes, url)
        items = _parse_rss_atom(data)
    except Exception:
        # No releases — fall back to commits feed
        url = f"https://github.com/{repo}/commits.atom"
        data = await asyncio.to_thread(_fetch_url_bytes, url)
        items = _parse_rss_atom(data)
    for item in items:
        item.setdefault("author", repo)
    return items


async def _check_source_arxiv(
    category: str, max_results: int = 20
) -> list[dict]:
    """Fetch recent papers from arXiv by category."""
    url = (
        f"http://export.arxiv.org/api/query?search_query=cat:{category}"
        f"&start=0&max_results={max_results}"
        f"&sortBy=submittedDate&sortOrder=descending"
    )
    data = await asyncio.to_thread(_fetch_url_bytes, url)
    return _parse_rss_atom(data)


async def resolve_yt_channel(identifier: str) -> dict:
    """Resolve a YouTube handle/URL/ID to {channel_id, name, feed_url}."""
    if re.match(r"^UC[\w-]{22}$", identifier):
        return {
            "channel_id": identifier,
            "name": identifier,
            "feed_url": f"https://www.youtube.com/feeds/videos.xml?channel_id={identifier}",
        }

    if identifier.startswith("http"):
        url = identifier
    elif identifier.startswith("@"):
        url = f"https://www.youtube.com/{identifier}"
    else:
        url = f"https://www.youtube.com/@{identifier}"

    html = await asyncio.to_thread(_fetch_url_bytes, url)
    text = html.decode("utf-8", errors="ignore")

    m = (
        re.search(r'"channelId"\s*:\s*"(UC[\w-]{22})"', text)
        or re.search(r"channel_id=(UC[\w-]{22})", text)
        or re.search(r"/channel/(UC[\w-]{22})", text)
    )
    if not m:
        raise ValueError(f"Could not resolve YouTube channel: {identifier}")

    channel_id = m.group(1)
    name_m = re.search(r'"name"\s*:\s*"([^"]{1,100})"', text)
    name = name_m.group(1) if name_m else identifier

    return {
        "channel_id": channel_id,
        "name": name,
        "feed_url": f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
    }


async def _check_source_youtube(feed_url: str) -> list[dict]:
    """Fetch recent videos from a YouTube channel RSS feed."""
    data = await asyncio.to_thread(_fetch_url_bytes, feed_url)
    items = _parse_rss_atom(data)
    for item in items:
        if item.get("url") and "youtube.com" in item["url"]:
            meta = {"video_url": item["url"]}
            item["metadata"] = json.dumps(meta)
    return items


async def _auto_transcribe_youtube(
    conn: sqlite3.Connection,
    sub_id: int,
    items: list[dict],
    ctx: Context = None,
    max_videos: int = 5,
    model_size: str = "tiny",
) -> str:
    """Auto-transcribe new YouTube videos and store transcripts in the DB.

    Gracefully skips if yt-dlp or faster-whisper are not installed.
    Uses the existing transcript cache to avoid re-downloading.
    Caps at *max_videos* per invocation so check_feeds doesn't block forever.
    Transcripts are written into feed_items.content so FTS5 can search them.

    Returns a short status string to append to the check_feeds result line,
    or an empty string if nothing happened.
    """
    # ── dependency check — soft fail, never crash ──────────────────────
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return " (auto-transcription skipped: install yt-dlp)"
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        return " (auto-transcription skipped: install faster-whisper)"

    os.makedirs(TRANSCRIBE_CACHE_DIR, exist_ok=True)
    os.makedirs(TRANSCRIPT_CACHE_DIR, exist_ok=True)

    # ── collect video URLs that still need transcription ───────────────
    uncached: list[str] = []
    for item in items:
        url = item.get("url", "")
        if not url or "youtube.com" not in url:
            continue
        cache_path = _transcript_cache_path(url, model_size)
        if not os.path.isfile(cache_path):
            uncached.append(url)

    if not uncached:
        return ""

    total_pending = len(uncached)
    batch = uncached[:max_videos]

    transcribed = 0
    errors: list[str] = []

    for i, video_url in enumerate(batch):
        try:
            if ctx:
                await ctx.report_progress(
                    progress=i, total=len(batch),
                    message=f"Auto-transcribing video {i + 1}/{len(batch)}...",
                )

            # Download audio (in thread — keeps event loop alive)
            dl_info = await asyncio.to_thread(
                _download_audio, video_url, TRANSCRIBE_CACHE_DIR,
            )

            # Transcribe audio (in thread)
            whisper_result = await asyncio.to_thread(
                _transcribe_audio, dl_info["audio_path"], model_size, "",
            )

            segments = whisper_result["segments"]
            if not segments:
                errors.append(f"no speech: {video_url}")
                continue

            # Build transcript text (same format as transcribe_video tool)
            transcript_lines = [
                "Video Transcript",
                f"Title: {dl_info['title']}",
                f"Duration: {format_timestamp(dl_info['duration'])}",
                f"Language: {whisper_result['language']}\n",
            ]
            for seg in segments:
                ts = format_timestamp(seg["start"])
                transcript_lines.append(f"[{ts}] {seg['text']}")
            full_transcript = "\n".join(transcript_lines)

            # Save to disk cache (reusable by transcribe_video tool)
            cache_path = _transcript_cache_path(video_url, model_size)
            with open(cache_path, "w") as f:
                json.dump({"url": video_url, "transcript": full_transcript}, f)

            # Write transcript into feed_items.content → FTS5 searchable
            conn.execute(
                "UPDATE feed_items SET content = ? "
                "WHERE subscription_id = ? AND url = ?",
                (full_transcript, sub_id, video_url),
            )
            conn.commit()

            transcribed += 1

            # Clean up temp audio file
            try:
                os.remove(dl_info["audio_path"])
            except OSError:
                pass

        except Exception as e:
            errors.append(str(e))

    # ── build status string ────────────────────────────────────────────
    parts: list[str] = []
    if transcribed:
        parts.append(f"{transcribed} transcribed")
    if errors:
        parts.append(f"{len(errors)} failed")
    skipped = total_pending - len(batch)
    if skipped > 0:
        parts.append(f"{skipped} queued for next check")

    return (" — " + ", ".join(parts)) if parts else ""


async def _check_source_podcast(feed_url: str) -> list[dict]:
    """Fetch episodes from a podcast RSS feed."""
    data = await asyncio.to_thread(_fetch_url_bytes, feed_url)
    root = ET.fromstring(data)
    items: list[dict] = []
    itunes = "http://www.itunes.com/dtds/podcast-1.0.dtd"
    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url", "") if enclosure is not None else ""
        meta: dict = {}
        if audio_url:
            meta["audio_url"] = audio_url
        dur_el = item.find(f"{{{itunes}}}duration")
        if dur_el is not None and dur_el.text:
            meta["duration"] = dur_el.text.strip()
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "url": (item.findtext("link") or audio_url).strip(),
            "content": strip_html(item.findtext("description") or ""),
            "published": (item.findtext("pubDate") or "").strip(),
            "author": (
                item.findtext(f"{{{itunes}}}author")
                or item.findtext("author") or ""
            ).strip(),
            "metadata": json.dumps(meta),
        })
    return items


async def _check_source_twitter(handle: str) -> list[dict]:
    """Scrape recent tweets from a public Twitter/X profile via Playwright."""
    handle = handle.lstrip("@")
    url = f"https://x.com/{handle}"

    async with async_playwright() as pw:
        context = await launch_browser(pw)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Dismiss login / signup walls
            for sel in (
                '[data-testid="xMigrationBottomBar"] button',
                'button:has-text("Not now")',
                '[role="button"]:has-text("Not now")',
                '[aria-label="Close"]',
            ):
                try:
                    btn = page.locator(sel)
                    if await btn.count() > 0:
                        await btn.first.click()
                        await page.wait_for_timeout(500)
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(3000)

            tweets = await page.evaluate("""
                () => {
                    const results = [];
                    const articles = document.querySelectorAll(
                        'article[data-testid="tweet"]'
                    );
                    for (const el of articles) {
                        const textEl = el.querySelector(
                            '[data-testid="tweetText"]'
                        );
                        const timeEl = el.querySelector('time');
                        const links = el.querySelectorAll(
                            'a[href*="/status/"]'
                        );
                        let tweetUrl = '';
                        for (const a of links) {
                            if (/\\/status\\/\\d+$/.test(
                                a.getAttribute('href') || ''
                            )) {
                                tweetUrl = a.href;
                                break;
                            }
                        }
                        if (textEl) {
                            results.push({
                                text: textEl.innerText.trim(),
                                time: timeEl
                                    ? timeEl.getAttribute('datetime') || ''
                                    : '',
                                url: tweetUrl,
                            });
                        }
                    }
                    return results;
                }
            """)

            items = []
            for t in tweets:
                if t.get("text"):
                    txt = t["text"]
                    items.append({
                        "title": (txt[:120] + "...") if len(txt) > 120 else txt,
                        "content": txt,
                        "url": t.get("url", f"https://x.com/{handle}"),
                        "published": t.get("time", ""),
                        "author": f"@{handle}",
                    })
            return items

        except Exception:
            return []  # Twitter scraping is best-effort
        finally:
            await context.close()


# ---------------------------------------------------------------------------
# MCP tools — Feed subscriptions
# ---------------------------------------------------------------------------


@mcp.tool()
async def subscribe(
    source_type: str,
    identifier: Annotated[
        str,
        # Accept either `identifier` (legacy LM Studio / Claude Desktop
        # clients) or `channel` (newer MCP clients that rename the
        # identifier field to `channel` in their tool-call schema).
        Field(
            validation_alias=AliasChoices("identifier", "channel"),
            description=(
                "Source identifier. Depends on source_type: news preset "
                "(bbc, cnn, ...), subreddit name, owner/repo, arXiv "
                "category, @handle / URL / channel ID, or RSS URL. "
                "Some clients send this as `channel` instead of "
                "`identifier`; both are accepted."
            ),
        ),
    ],
    name: str = "",
) -> str:
    """Subscribe to a content source for automatic monitoring and search.

    Supported source types: news, reddit, hackernews, github, arxiv, youtube, podcast, twitter.

    After subscribing, run check_feeds to fetch content, then search_feeds to query it.

    Sample prompts that trigger this tool:
        - "Subscribe to BBC News"
        - "Follow r/LocalLLaMA on Reddit"
        - "Monitor Hacker News top stories"
        - "Watch anthropics/claude-code on GitHub for new releases"
        - "Subscribe to the YouTube channel @3Blue1Brown"
        - "Follow @elonmusk on Twitter"
        - "Subscribe to the machine learning arXiv category"
        - "Add this podcast: https://feeds.example.com/podcast.xml"
        - "Subscribe to CNN, NPR, and The Guardian"

    Args:
        source_type: One of: news, reddit, hackernews, github, arxiv, youtube, podcast, twitter.
        identifier: Source identifier — depends on type:
            - news: preset name (bbc, cnn, nyt, guardian, npr, aljazeera, techcrunch, ars, verge, wired, reuters) or a custom RSS URL
            - reddit: subreddit name (e.g. "LocalLLaMA", "programming")
            - hackernews: "top", "new", or "best"
            - github: "owner/repo" (e.g. "anthropics/claude-code")
            - arxiv: shortcut (ai, ml, cv, nlp, robotics, crypto) or arXiv category like "cs.AI"
            - youtube: channel handle (@name), URL, or channel ID (UCxxxx)
            - podcast: RSS feed URL
            - twitter: username with or without @ (e.g. "elonmusk")
        name: Optional display name for this subscription.
    """
    valid_types = (
        "news", "reddit", "hackernews", "github",
        "arxiv", "youtube", "podcast", "twitter",
    )
    source_type = source_type.lower().strip()
    if source_type not in valid_types:
        return (
            f"Invalid source type '{source_type}'. "
            f"Must be one of: {', '.join(valid_types)}"
        )

    identifier = identifier.strip()
    feed_url = ""
    display_name = name

    if source_type == "news":
        key = identifier.lower().replace(" ", "")
        if key in PRESET_NEWS_FEEDS:
            preset = PRESET_NEWS_FEEDS[key]
            feed_url = preset["url"]
            display_name = display_name or preset["name"]
            identifier = key
        elif identifier.startswith("http"):
            feed_url = identifier
            display_name = display_name or identifier
        else:
            presets = ", ".join(sorted(PRESET_NEWS_FEEDS.keys()))
            return (
                f"Unknown news preset '{identifier}'.\n"
                f"Available presets: {presets}\n"
                f"Or provide a custom RSS feed URL."
            )

    elif source_type == "reddit":
        identifier = identifier.lstrip("r/").strip("/")
        feed_url = f"https://www.reddit.com/r/{identifier}/.rss"
        display_name = display_name or f"r/{identifier}"

    elif source_type == "hackernews":
        identifier = identifier.lower()
        if identifier not in ("top", "new", "best"):
            identifier = "top"
        feed_url = f"hackernews:{identifier}"
        display_name = display_name or f"Hacker News ({identifier})"

    elif source_type == "github":
        if "/" not in identifier:
            return "GitHub identifier must be 'owner/repo' (e.g. 'anthropics/claude-code')."
        feed_url = f"https://github.com/{identifier}/releases.atom"
        display_name = display_name or identifier

    elif source_type == "arxiv":
        cat = ARXIV_CATEGORIES.get(identifier.lower(), identifier)
        identifier = cat
        feed_url = (
            f"http://export.arxiv.org/api/query?search_query=cat:{cat}"
            f"&max_results=20&sortBy=submittedDate&sortOrder=descending"
        )
        display_name = display_name or f"arXiv {cat}"

    elif source_type == "youtube":
        try:
            info = await resolve_yt_channel(identifier)
            identifier = info["channel_id"]
            feed_url = info["feed_url"]
            display_name = display_name or info["name"]
        except ValueError as e:
            return str(e)

    elif source_type == "podcast":
        if not identifier.startswith("http"):
            return "Podcast identifier must be an RSS feed URL."
        feed_url = identifier
        if not display_name:
            try:
                data = await asyncio.to_thread(_fetch_url_bytes, feed_url)
                root = ET.fromstring(data)
                t = root.findtext(".//channel/title")
                display_name = t.strip() if t else identifier
            except Exception:
                display_name = identifier

    elif source_type == "twitter":
        identifier = identifier.lstrip("@")
        feed_url = f"twitter:{identifier}"
        display_name = display_name or f"@{identifier}"

    conn = _get_feeds_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO subscriptions
               (source_type, identifier, name, feed_url, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (source_type, identifier, display_name, feed_url, now),
        )
        conn.commit()
        return (
            f"Subscribed to {display_name} ({source_type}).\n"
            f"Run check_feeds to fetch content."
        )
    except sqlite3.IntegrityError:
        return f"Already subscribed to {display_name} ({source_type})."
    finally:
        conn.close()


@mcp.tool()
async def unsubscribe(
    source_type: str,
    identifier: Annotated[
        str,
        # Same client-compat: some MCP clients send this as `channel`
        # instead of `identifier`. Accept both names.
        Field(
            validation_alias=AliasChoices("identifier", "channel"),
            description=(
                "The same identifier used when subscribing. "
                "Some clients send this as `channel` instead of "
                "`identifier`; both are accepted."
            ),
        ),
    ],
) -> str:
    """Remove a subscription and all its stored content.

    Sample prompts that trigger this tool:
        - "Unsubscribe from BBC News"
        - "Stop following r/LocalLLaMA"
        - "Remove the YouTube channel @3Blue1Brown"

    Args:
        source_type: The source type (news, reddit, hackernews, github, arxiv, youtube, podcast, twitter).
        identifier: The same identifier used when subscribing.
    """
    conn = _get_feeds_db()
    try:
        row = conn.execute(
            "SELECT id, name FROM subscriptions WHERE source_type = ? AND identifier = ?",
            (source_type.lower().strip(), identifier.strip()),
        ).fetchone()

        if not row:
            row = conn.execute(
                "SELECT id, name FROM subscriptions WHERE name LIKE ? OR identifier LIKE ?",
                (f"%{identifier.strip()}%", f"%{identifier.strip()}%"),
            ).fetchone()

        if not row:
            return f"No subscription found for '{identifier}'."

        conn.execute("DELETE FROM feed_items WHERE subscription_id = ?", (row["id"],))
        conn.execute("DELETE FROM subscriptions WHERE id = ?", (row["id"],))
        conn.commit()
        return f"Unsubscribed from {row['name']}. Stored content removed."
    finally:
        conn.close()


@mcp.tool()
async def list_subscriptions() -> str:
    """List all active feed subscriptions with item counts.

    Sample prompts that trigger this tool:
        - "Show my subscriptions"
        - "What feeds am I following?"
        - "List all my monitored sources"
    """
    conn = _get_feeds_db()
    try:
        rows = conn.execute(
            """SELECT s.*, COUNT(i.id) as item_count
               FROM subscriptions s
               LEFT JOIN feed_items i ON i.subscription_id = s.id
               GROUP BY s.id
               ORDER BY s.source_type, s.name""",
        ).fetchall()

        if not rows:
            presets = ", ".join(sorted(PRESET_NEWS_FEEDS.keys()))
            return (
                "No active subscriptions.\n\n"
                "Use subscribe() to add sources. "
                f"Available news presets: {presets}"
            )

        lines = [f"Active Subscriptions ({len(rows)} total)\n"]
        current_type = ""
        for r in rows:
            if r["source_type"] != current_type:
                current_type = r["source_type"]
                lines.append(f"\n  {current_type.upper()}")
            checked = r["last_checked"][:16] if r["last_checked"] else "never"
            lines.append(
                f"    {r['name']} — "
                f"{r['item_count']} items, last checked: {checked}"
            )

        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
async def check_feeds(source_type: str = "", ctx: Context = None) -> str:
    """Check all (or specific) subscriptions for new content. Fetches and stores latest items.

    Sample prompts that trigger this tool:
        - "Check my feeds"
        - "What's new in my subscriptions?"
        - "Fetch latest news"
        - "Check Reddit feeds"
        - "Update all my feed subscriptions"

    Args:
        source_type: Optionally limit to one type (news, reddit, hackernews, github, arxiv, youtube, podcast, twitter). Leave empty to check all.
    """
    conn = _get_feeds_db()
    try:
        if source_type:
            subs = conn.execute(
                "SELECT * FROM subscriptions WHERE source_type = ?",
                (source_type.lower().strip(),),
            ).fetchall()
        else:
            subs = conn.execute("SELECT * FROM subscriptions").fetchall()

        if not subs:
            return "No subscriptions to check. Use subscribe() first."

        results = []
        total_new = 0

        for idx, sub in enumerate(subs):
            try:
                if ctx:
                    await ctx.report_progress(
                        progress=idx, total=len(subs),
                        message=f"Checking {sub['name']}...",
                    )

                st = sub["source_type"]
                items: list[dict] = []

                if st == "news":
                    items = await _check_source_rss(sub["feed_url"])
                elif st == "reddit":
                    items = await _check_source_reddit(sub["identifier"])
                elif st == "hackernews":
                    items = await _check_source_hackernews(sub["identifier"])
                elif st == "github":
                    items = await _check_source_github(sub["identifier"])
                elif st == "arxiv":
                    items = await _check_source_arxiv(sub["identifier"])
                elif st == "youtube":
                    items = await _check_source_youtube(sub["feed_url"])
                elif st == "podcast":
                    items = await _check_source_podcast(sub["feed_url"])
                elif st == "twitter":
                    items = await _check_source_twitter(sub["identifier"])

                new_count = _store_items(conn, sub["id"], st, items)

                conn.execute(
                    "UPDATE subscriptions SET last_checked = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), sub["id"]),
                )
                conn.commit()

                total_new += new_count
                note = ""
                if st == "youtube":
                    note = await _auto_transcribe_youtube(
                        conn, sub["id"], items, ctx=ctx,
                    )
                elif st == "podcast" and new_count > 0:
                    note = " — audio URLs stored, use transcribe_video to transcribe"

                results.append(f"  {sub['name']}: {new_count} new items{note}")

            except Exception as e:
                results.append(f"  {sub['name']}: ERROR — {e}")

        lines = ["Feed Check Complete\n"]
        lines.extend(results)
        lines.append(f"\nTotal: {total_new} new items across {len(subs)} sources")

        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
async def search_feeds(
    query: str,
    source_type: str = "",
    limit: int = 20,
) -> str:
    """Full-text search across all stored feed content (articles, posts, tweets, transcripts).

    Sample prompts that trigger this tool:
        - "Search my feeds for machine learning"
        - "Find mentions of GPT in my news feeds"
        - "What have my Reddit feeds said about Rust?"
        - "Search Twitter feeds for product launch"
        - "Look for arxiv papers about transformers in my feeds"

    Args:
        query: Search query (supports FTS5 syntax: AND, OR, NOT, "quoted phrases").
        source_type: Optionally limit to one type. Leave empty to search everything.
        limit: Max results to return (default 20).
    """
    conn = _get_feeds_db()
    try:
        rows = None
        # Try FTS5 first
        try:
            if source_type:
                rows = conn.execute(
                    """SELECT f.*, s.name as source_name
                       FROM feed_items_fts fts
                       JOIN feed_items f ON f.id = fts.rowid
                       JOIN subscriptions s ON s.id = f.subscription_id
                       WHERE feed_items_fts MATCH ? AND f.source_type = ?
                       ORDER BY rank LIMIT ?""",
                    (query, source_type.lower(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT f.*, s.name as source_name
                       FROM feed_items_fts fts
                       JOIN feed_items f ON f.id = fts.rowid
                       JOIN subscriptions s ON s.id = f.subscription_id
                       WHERE feed_items_fts MATCH ?
                       ORDER BY rank LIMIT ?""",
                    (query, limit),
                ).fetchall()
        except Exception:
            rows = None  # FTS5 unavailable or query syntax error

        # Fallback to LIKE
        if rows is None:
            like_q = f"%{query}%"
            if source_type:
                rows = conn.execute(
                    """SELECT f.*, s.name as source_name
                       FROM feed_items f
                       JOIN subscriptions s ON s.id = f.subscription_id
                       WHERE (f.title LIKE ? OR f.content LIKE ?) AND f.source_type = ?
                       ORDER BY f.published_at DESC LIMIT ?""",
                    (like_q, like_q, source_type.lower(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT f.*, s.name as source_name
                       FROM feed_items f
                       JOIN subscriptions s ON s.id = f.subscription_id
                       WHERE f.title LIKE ? OR f.content LIKE ?
                       ORDER BY f.published_at DESC LIMIT ?""",
                    (like_q, like_q, limit),
                ).fetchall()

        if not rows:
            return f'No results found for: "{query}"'

        lines = [f'Feed Search: "{query}" ({len(rows)} results)\n']
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i}. [{r['source_type']}/{r['source_name']}] {r['title']}"
            )
            if r["published_at"]:
                lines.append(f"   Published: {r['published_at']}")
            if r["url"]:
                lines.append(f"   URL: {r['url']}")
            if r["content"]:
                snippet = r["content"][:200]
                if len(r["content"]) > 200:
                    snippet += "..."
                lines.append(f"   {snippet}")
            lines.append("")

        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
async def get_feed_items(
    source: str = "",
    source_type: str = "",
    limit: int = 20,
) -> str:
    """Get recent items from feed subscriptions, optionally filtered by source or type.

    Sample prompts that trigger this tool:
        - "What's new in my feeds?"
        - "Show me the latest BBC News articles"
        - "Show recent Reddit posts"
        - "What are the latest Hacker News stories?"
        - "Show me recent tweets from my followed accounts"
        - "Get latest YouTube videos from my subscriptions"

    Args:
        source: Filter by source name (e.g. "BBC", "LocalLLaMA"). Leave empty for all.
        source_type: Filter by type (news, reddit, hackernews, github, arxiv, youtube, podcast, twitter). Leave empty for all.
        limit: Max items to return (default 20).
    """
    conn = _get_feeds_db()
    try:
        if source:
            rows = conn.execute(
                """SELECT f.*, s.name as source_name
                   FROM feed_items f
                   JOIN subscriptions s ON s.id = f.subscription_id
                   WHERE s.name LIKE ? OR s.identifier LIKE ?
                   ORDER BY f.published_at DESC LIMIT ?""",
                (f"%{source}%", f"%{source}%", limit),
            ).fetchall()
        elif source_type:
            rows = conn.execute(
                """SELECT f.*, s.name as source_name
                   FROM feed_items f
                   JOIN subscriptions s ON s.id = f.subscription_id
                   WHERE f.source_type = ?
                   ORDER BY f.published_at DESC LIMIT ?""",
                (source_type.lower(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT f.*, s.name as source_name
                   FROM feed_items f
                   JOIN subscriptions s ON s.id = f.subscription_id
                   ORDER BY f.published_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

        if not rows:
            return "No feed items found. Run check_feeds to fetch content."

        lines = [f"Recent Feed Items ({len(rows)} items)\n"]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i}. [{r['source_type']}/{r['source_name']}] {r['title']}"
            )
            if r["published_at"]:
                lines.append(f"   Published: {r['published_at']}")
            if r["url"]:
                lines.append(f"   URL: {r['url']}")
            if r["content"]:
                snippet = r["content"][:200]
                if len(r["content"]) > 200:
                    snippet += "..."
                lines.append(f"   {snippet}")
            lines.append("")

        return "\n".join(lines)
    finally:
        conn.close()

