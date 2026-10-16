"""Feed subscription tools — subscribe, check, search, and browse feeds.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Annotated

from pydantic import AliasChoices, Field

from mcp.server.fastmcp import Context

from .. import config
from ..config import (
    ARXIV_CATEGORIES,
    FEEDS_DB_PATH,
    PRESET_NEWS_FEEDS,
    TRANSCRIBE_CACHE_DIR,
    TRANSCRIPT_CACHE_DIR,
    USER_AGENT,
)
from ..core.browser import pooled_browser
from ..server import (
    mcp,
    _download_audio,
    _fetch_url_bytes,
    _transcribe_audio,
    _transcript_cache_path,
    format_timestamp,
    strip_html,
)
from ..utils import ytdlp


# Database
# ---------------------------------------------------------------------------

def _get_feeds_db() -> sqlite3.Connection:
    """Open (and initialise if needed) the feeds SQLite database."""
    db_path = os.environ.get("FEEDS_DB_PATH", FEEDS_DB_PATH)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Wait (rather than fail) if another connection holds the write lock: the
    # concurrent check_feeds workers each open their own connection.
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_feeds_db(conn)
    return conn


def _init_feeds_db(conn: sqlite3.Connection) -> None:
    """Create/upgrade the feeds database to the current schema version."""
    _migrate(conn)


# Current schema version. Bump together with a new ``_migrate_to_*`` function.
_SCHEMA_VERSION = 3

# The feed_items DDL used from v2 onwards. ``dedupe_key`` is the dedupe unit
# (URL when present, else a content hash) so URL-less items are no longer
# dropped (B7). The FTS ``AFTER UPDATE`` trigger keeps the index in sync (B5).
_FEED_ITEMS_DDL = """
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
    dedupe_key TEXT NOT NULL DEFAULT '',
    UNIQUE(subscription_id, dedupe_key),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
);
"""

_SUBSCRIPTIONS_DDL = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    identifier TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    feed_url TEXT NOT NULL DEFAULT '',
    last_checked TEXT,
    created_at TEXT NOT NULL,
    etag TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    UNIQUE(source_type, identifier)
);
"""

_FEED_ITEM_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_items_sub ON feed_items(subscription_id)",
    "CREATE INDEX IF NOT EXISTS idx_items_type ON feed_items(source_type)",
    "CREATE INDEX IF NOT EXISTS idx_items_pub ON feed_items(published_at DESC)",
]

# External-content FTS5 table + the three triggers that keep it consistent.
# The UPDATE trigger is essential: without it, any future UPDATE desyncs the
# index (B5).
_FTS_STATEMENTS = [
    """CREATE VIRTUAL TABLE IF NOT EXISTS feed_items_fts USING fts5(
        title, content, author, content='feed_items', content_rowid='id'
    )""",
    """CREATE TRIGGER IF NOT EXISTS fts_ai AFTER INSERT ON feed_items BEGIN
        INSERT INTO feed_items_fts(rowid, title, content, author)
        VALUES (new.id, new.title, new.content, new.author);
    END""",
    """CREATE TRIGGER IF NOT EXISTS fts_ad AFTER DELETE ON feed_items BEGIN
        INSERT INTO feed_items_fts(feed_items_fts, rowid, title, content, author)
        VALUES ('delete', old.id, old.title, old.content, old.author);
    END""",
    """CREATE TRIGGER IF NOT EXISTS fts_au AFTER UPDATE ON feed_items BEGIN
        INSERT INTO feed_items_fts(feed_items_fts, rowid, title, content, author)
        VALUES ('delete', old.id, old.title, old.content, old.author);
        INSERT INTO feed_items_fts(rowid, title, content, author)
        VALUES (new.id, new.title, new.content, new.author);
    END""",
]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _install_fts(conn: sqlite3.Connection) -> None:
    """Create the FTS table + triggers; rebuild the index from content."""
    try:
        for stmt in _FTS_STATEMENTS:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO feed_items_fts(feed_items_fts) VALUES('rebuild')"
        )
    except sqlite3.OperationalError:
        pass  # FTS5 not available on this build — LIKE fallback used


def _migrate_to_1(conn: sqlite3.Connection) -> None:
    """Migration 1: base schema at v2 shape (fresh databases)."""
    conn.execute(_SUBSCRIPTIONS_DDL)
    conn.execute(_FEED_ITEMS_DDL)
    for stmt in _FEED_ITEM_INDEXES:
        conn.execute(stmt)
    _install_fts(conn)


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    """Migration 2: upgrade a pre-hardening (v1) database in place.

    ``CREATE TABLE IF NOT EXISTS`` in migration 1 is a no-op for an existing
    database, so v1 databases arrive here still missing ``dedupe_key``. Rebuild
    ``feed_items`` to add it (SQLite cannot add a UNIQUE column in place),
    backfill ``dedupe_key`` (URL, else a synthetic per-row key), normalize
    ``published_at`` to ISO-8601 UTC (B4), then resync FTS (B5).
    """
    if "dedupe_key" in _table_columns(conn, "feed_items"):
        return  # already v2-shaped (e.g. a fresh DB created by migration 1)

    conn.execute("ALTER TABLE feed_items RENAME TO feed_items_old")
    conn.execute(_FEED_ITEMS_DDL)
    conn.execute(
        """INSERT INTO feed_items
           (id, subscription_id, source_type, title, content, url, author,
            published_at, fetched_at, metadata, dedupe_key)
           SELECT id, subscription_id, source_type, title, content, url, author,
                  published_at, fetched_at, metadata,
                  CASE WHEN url <> '' THEN url ELSE 'legacy-' || id END
           FROM feed_items_old"""
    )

    # Normalize published_at in Python (RFC 822 / ISO / offset variants).
    rows = conn.execute("SELECT id, published_at FROM feed_items").fetchall()
    for row_id, raw in rows:
        normalized = _normalize_published(raw or "")
        if normalized != (raw or ""):
            conn.execute(
                "UPDATE feed_items SET published_at = ? WHERE id = ?",
                (normalized, row_id),
            )

    conn.execute("DROP TABLE feed_items_old")
    for stmt in _FEED_ITEM_INDEXES:
        conn.execute(stmt)
    _install_fts(conn)


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """Migration 3: per-subscription HTTP validators for conditional GET.

    Storing the last ``ETag`` / ``Last-Modified`` lets ``check_feeds`` send
    ``If-None-Match`` / ``If-Modified-Since`` and treat a ``304 Not Modified``
    as "no new items", which stops re-downloading unchanged feeds on every
    check (both faster and friendlier to the upstream).
    """
    cols = _table_columns(conn, "subscriptions")
    if "etag" not in cols:
        conn.execute(
            "ALTER TABLE subscriptions ADD COLUMN etag TEXT NOT NULL DEFAULT ''"
        )
    if "last_modified" not in cols:
        conn.execute(
            "ALTER TABLE subscriptions ADD COLUMN last_modified TEXT NOT NULL DEFAULT ''"
        )


_MIGRATIONS = {1: _migrate_to_1, 2: _migrate_to_2, 3: _migrate_to_3}


def _migrate(conn: sqlite3.Connection) -> None:
    """Run all pending migrations, recording progress in ``PRAGMA user_version``."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= _SCHEMA_VERSION:
        return
    for version in range(current + 1, _SCHEMA_VERSION + 1):
        _MIGRATIONS[version](conn)
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_subreddit(identifier: str) -> str:
    """Strip an optional ``r/`` or ``/r/`` prefix (and surrounding slashes).

    NOTE: ``str.lstrip("r/")`` strips the *character set* ``{'r', '/'}``, so it
    mangles identifiers that merely *start* with those characters — e.g.
    ``"rust"`` -> ``"ust"`` and ``"r/rprogramming"`` -> ``"programming"``.
    This performs a real prefix removal instead.
    """
    ident = identifier.strip().strip("/")
    if ident.lower().startswith("r/"):
        ident = ident[2:]
    return ident.strip("/")


def _like_escape(value: str) -> str:
    """Escape LIKE wildcards so user input is matched literally (B6).

    Without this, a query containing ``%`` or ``_`` acts as a wildcard and can
    match unintended rows. Must be used together with ``ESCAPE '\\'`` in the
    SQL statement. Escape the escape character first.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_published(raw: str) -> str:
    """Normalize a feed timestamp to ISO-8601 UTC (B4).

    Feeds mix RFC 822 (RSS), ISO 8601 (Atom) and epoch-derived values, so a
    lexical ``ORDER BY published_at`` sorted incorrectly. Unparseable values
    are returned unchanged (never dropped).
    """
    if not raw:
        return ""
    value = raw.strip()
    if not value:
        return ""
    iso_candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    return value


def _dedupe_key(item: dict) -> str:
    """Deterministic dedupe key for a feed item (B7).

    Prefer the URL. Items without a URL (some podcast / Twitter / arXiv feeds)
    fall back to a content hash so they are still stored and deduped instead
    of being silently dropped.
    """
    url = (item.get("url") or "").strip()
    if url:
        return url
    material = "|".join(
        [
            item.get("source_type", ""),
            (item.get("title") or "").strip(),
            (item.get("published") or "").strip(),
            (item.get("content") or "").strip(),
        ]
    )
    return "hash:" + hashlib.sha256(material.encode("utf-8", "ignore")).hexdigest()


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
    """Store feed items in the database; skip duplicates. Returns new-item count.

    Dedupe is by ``dedupe_key`` (URL, else content hash) so URL-less items are
    kept (B7); ``published_at`` is normalized to ISO-8601 UTC (B4). Inserts are
    batched via ``executemany`` in a single transaction, and the number of
    rows actually inserted is measured with ``total_changes`` (``cursor.rowcount``
    is unusable for a bulk ``INSERT OR IGNORE``).
    """
    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple] = []
    for item in items:
        item_type = item.get("source_type") or source_type
        dedupe_key = _dedupe_key({**item, "source_type": item_type})
        if not dedupe_key:
            continue
        rows.append(
            (
                sub_id, item_type,
                item.get("title", ""), item.get("content", ""),
                item.get("url", ""), item.get("author", ""),
                _normalize_published(item.get("published", "")), now,
                item.get("metadata", "{}"), dedupe_key,
            )
        )
    if not rows:
        return 0
    # Count via COUNT(*) rather than ``conn.total_changes``: the FTS triggers
    # fire extra changes, which would inflate the delta.
    before = conn.execute(
        "SELECT COUNT(*) FROM feed_items WHERE subscription_id = ?", (sub_id,)
    ).fetchone()[0]
    conn.executemany(
        """INSERT OR IGNORE INTO feed_items
           (subscription_id, source_type, title, content, url, author,
            published_at, fetched_at, metadata, dedupe_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    after = conn.execute(
        "SELECT COUNT(*) FROM feed_items WHERE subscription_id = ?", (sub_id,)
    ).fetchone()[0]
    conn.commit()
    return after - before


# ---------------------------------------------------------------------------
# Conditional GET + per-source refresh plumbing (Phase 8)
# ---------------------------------------------------------------------------


def _fetch_feed_bytes(
    url: str, etag: str = "", last_modified: str = ""
) -> tuple[bytes | None, str, str]:
    """Fetch a feed, optionally with HTTP validators.

    Returns ``(data, etag, last_modified)``. ``data`` is ``None`` when the
    server replied ``304 Not Modified`` — the caller should then skip parsing
    and storing entirely. With no validators this is byte-for-byte the old
    behaviour (delegates to ``_fetch_url_bytes``), which keeps existing
    monkeypatched tests working.
    """
    if not etag and not last_modified:
        return _fetch_url_bytes(url), "", ""
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            return (
                data,
                resp.headers.get("ETag", "") or "",
                resp.headers.get("Last-Modified", "") or "",
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, etag, last_modified
        raise


async def _fetch_source_items(sub: dict) -> list[dict]:
    """Dispatch to the right source fetcher for a subscription row."""
    st = sub.get("source_type", "")
    if st == "news":
        return await _check_source_rss(sub.get("feed_url", ""))
    if st == "reddit":
        return await _check_source_reddit(sub.get("identifier", ""))
    if st == "hackernews":
        return await _check_source_hackernews(sub.get("identifier", ""))
    if st == "github":
        return await _check_source_github(sub.get("identifier", ""))
    if st == "arxiv":
        return await _check_source_arxiv(sub.get("identifier", ""))
    if st == "youtube":
        return await _check_source_youtube(sub.get("feed_url", ""))
    if st == "podcast":
        return await _check_source_podcast(sub.get("feed_url", ""))
    if st == "twitter":
        return await _check_source_twitter(sub.get("identifier", ""))
    return []


async def _refresh_subscription(
    sub: dict,
) -> tuple[list[dict] | None, str, str]:
    """Fetch a subscription's items, using conditional GET where applicable.

    Returns ``(items, etag, last_modified)``; ``items is None`` means the feed
    was unchanged (HTTP 304) so nothing should be stored.
    """
    etag = sub.get("etag", "") or ""
    last_modified = sub.get("last_modified", "") or ""
    feed_url = sub.get("feed_url", "") or ""
    st = sub.get("source_type", "")

    # Only the pure-URL sources can be refreshed conditionally. Reddit / arXiv
    # / GitHub / Hacker News / Twitter derive their URL (or use JSON APIs) at
    # fetch time; adding validators there is future work.
    if st in ("news", "podcast") and feed_url.startswith(("http://", "https://")):
        data, new_etag, new_lm = await asyncio.to_thread(
            _fetch_feed_bytes, feed_url, etag, last_modified
        )
        if data is None:
            return None, etag, last_modified
        return _parse_rss_atom(data), new_etag, new_lm

    return await _fetch_source_items(sub), etag, last_modified


def _store_source(sub_id: int, source_type: str, items: list[dict]) -> int:
    """Store items for one source using its own short-lived connection.

    Each concurrent worker opens its own connection (WAL + busy_timeout) so a
    single ``sqlite3.Connection`` is never shared across tasks.
    """
    conn = _get_feeds_db()
    try:
        return _store_items(conn, sub_id, source_type, items)
    finally:
        conn.close()


def _update_subscription_meta(
    sub_id: int, etag: str, last_modified: str
) -> None:
    """Record the HTTP validators + last-checked timestamp for a subscription."""
    conn = _get_feeds_db()
    try:
        conn.execute(
            "UPDATE subscriptions SET last_checked = ?, etag = ?, last_modified = ?"
            " WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), etag, last_modified, sub_id),
        )
        conn.commit()
    finally:
        conn.close()


async def _auto_transcribe_youtube_isolated(
    sub_id: int, items: list[dict], ctx: Context = None
) -> str:
    """Run the YouTube auto-transcription with its own DB connection.

    ``check_feeds`` workers run concurrently and must not share a connection,
    so this opens (and closes) a dedicated one around
    :func:`_auto_transcribe_youtube`.
    """
    conn = await asyncio.to_thread(_get_feeds_db)
    try:
        return await _auto_transcribe_youtube(conn, sub_id, items, ctx=ctx)
    finally:
        await asyncio.to_thread(conn.close)


def _prune_items() -> int:
    """Apply the optional retention policy. Returns the number of rows removed.

    Disabled by default (both limits 0), which preserves current behaviour.
    """
    max_items = getattr(config, "FEEDS_MAX_ITEMS_PER_SUB", 0)
    max_age_days = getattr(config, "FEEDS_MAX_AGE_DAYS", 0)
    if not max_items and not max_age_days:
        return 0
    conn = _get_feeds_db()
    removed = 0
    try:
        if max_age_days:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=max_age_days)
            ).isoformat()
            cur = conn.execute(
                "DELETE FROM feed_items WHERE published_at <> '' AND published_at < ?",
                (cutoff,),
            )
            removed += cur.rowcount
        if max_items:
            cur = conn.execute(
                """DELETE FROM feed_items WHERE id IN (
                       SELECT id FROM (
                           SELECT id, ROW_NUMBER() OVER (
                               PARTITION BY subscription_id
                               ORDER BY published_at DESC, id DESC
                           ) AS rn
                           FROM feed_items
                       ) WHERE rn > ?
                   )""",
                (max_items,),
            )
            removed += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return removed


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
        f"https://export.arxiv.org/api/query?search_query=cat:{category}"
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
    if not ytdlp.is_available():
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

    async with pooled_browser() as (context, page):
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
        identifier = _normalize_subreddit(identifier)
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
            f"https://export.arxiv.org/api/query?search_query=cat:{cat}"
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
                "SELECT id, name FROM subscriptions "
                "WHERE name LIKE ? ESCAPE '\\' OR identifier LIKE ? ESCAPE '\\'",
                (
                    f"%{_like_escape(identifier.strip())}%",
                    f"%{_like_escape(identifier.strip())}%",
                ),
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

    Subscriptions are refreshed concurrently (bounded by
    NOAPI_MCP_FEEDS_CONCURRENCY, default 8) and each source is isolated: one
    failing feed does not abort the batch. Feeds that advertise ETag /
    Last-Modified are fetched conditionally, so an unchanged feed costs no
    re-download.

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
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE source_type = ?",
                (source_type.lower().strip(),),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM subscriptions").fetchall()
        # Detach from the connection before it closes: the workers below each
        # use their own connection.
        subs = [dict(r) for r in rows]
    finally:
        conn.close()

    if not subs:
        return "No subscriptions to check. Use subscribe() first."

    total = len(subs)
    semaphore = asyncio.Semaphore(max(1, config.FEEDS_CONCURRENCY))
    counter = {"done": 0}
    counter_lock = asyncio.Lock()
    results: list[str] = [""] * total
    new_counts: list[int] = [0] * total

    async def _worker(idx: int, sub: dict) -> None:
        async with semaphore:
            try:
                items, etag, last_modified = await _refresh_subscription(sub)
                st = sub.get("source_type", "")

                if items is None:
                    # HTTP 304 — nothing changed since the last check.
                    new_count = 0
                    note = " (not modified)"
                else:
                    new_count = await asyncio.to_thread(
                        _store_source, sub["id"], st, items
                    )
                    note = ""
                    if st == "youtube" and items:
                        note = await _auto_transcribe_youtube_isolated(
                            sub["id"], items, ctx=ctx,
                        )
                    elif st == "podcast" and new_count > 0:
                        note = " — audio URLs stored, use transcribe_video to transcribe"

                await asyncio.to_thread(
                    _update_subscription_meta, sub["id"], etag, last_modified
                )
                new_counts[idx] = new_count
                results[idx] = f"  {sub.get('name', '')}: {new_count} new items{note}"

            except Exception as e:
                results[idx] = f"  {sub.get('name', '')}: ERROR — {e}"

            finally:
                async with counter_lock:
                    counter["done"] += 1
                    done = counter["done"]
                if ctx:
                    with contextlib.suppress(Exception):
                        await ctx.report_progress(
                            progress=done, total=total,
                            message=f"Checked {done}/{total} sources...",
                        )

    await asyncio.gather(*(_worker(i, s) for i, s in enumerate(subs)))

    total_new = sum(new_counts)
    lines = ["Feed Check Complete\n"]
    lines.extend(results)

    pruned = await asyncio.to_thread(_prune_items)
    if pruned:
        lines.append(f"\n(pruned {pruned} old items by retention policy)")

    lines.append(f"\nTotal: {total_new} new items across {total} sources")
    return "\n".join(lines)


@mcp.tool()
async def search_feeds(
    query: str,
    source_type: str = "",
    limit: int = 20,
    since: str = "",
    until: str = "",
) -> str:
    """Full-text search across all stored feed content (articles, posts, tweets, transcripts).

    Results are ranked by FTS5 relevance (bm25) when available and include a
    highlighted snippet; otherwise a literal LIKE match is used.

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
        since: Only items published on/after this ISO date (e.g. "2026-01-01"). Optional.
        until: Only items published on/before this ISO date (e.g. "2026-03-31"). Optional.
    """
    conn = _get_feeds_db()

    date_clauses: list[str] = []
    date_params: list[str] = []
    if since:
        date_clauses.append("f.published_at >= ?")
        date_params.append(since)
    if until:
        # Date-only upper bound means "end of that day".
        bound = f"{until}T23:59:59+00:00" if len(until) == 10 else until
        date_clauses.append("f.published_at <= ?")
        date_params.append(bound)
    date_sql = (" AND " + " AND ".join(date_clauses)) if date_clauses else ""

    try:
        rows = None
        # Try FTS5 first: bm25() gives a proper relevance score (lower is a
        # better match) and snippet() returns highlighted context.
        try:
            sql = """SELECT f.*, s.name as source_name,
                             bm25(feed_items_fts) AS score,
                             snippet(feed_items_fts, 1, '', '', '…', 20) AS snippet
                      FROM feed_items_fts fts
                      JOIN feed_items f ON f.id = fts.rowid
                      JOIN subscriptions s ON s.id = f.subscription_id
                      WHERE feed_items_fts MATCH ?"""
            params: list = [query]
            if source_type:
                sql += " AND f.source_type = ?"
                params.append(source_type.lower())
            sql += date_sql
            params.extend(date_params)
            sql += " ORDER BY score LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        except Exception:
            rows = None  # FTS5 unavailable or query syntax error

        # Fallback to LIKE
        if rows is None:
            like_q = f"%{_like_escape(query)}%"
            sql = """SELECT f.*, s.name as source_name, NULL as score, NULL as snippet
                     FROM feed_items f
                     JOIN subscriptions s ON s.id = f.subscription_id
                     WHERE (f.title LIKE ? ESCAPE '\\' OR f.content LIKE ? ESCAPE '\\')"""
            params = [like_q, like_q]
            if source_type:
                sql += " AND f.source_type = ?"
                params.append(source_type.lower())
            sql += date_sql
            params.extend(date_params)
            sql += " ORDER BY f.published_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()

        if not rows:
            return f'No results found for: "{query}"'

        def _row_value(row, key):
            try:
                return row[key]
            except (IndexError, KeyError):
                return None

        lines = [f'Feed Search: "{query}" ({len(rows)} results)\n']
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i}. [{r['source_type']}/{r['source_name']}] {r['title']}"
            )
            if r["published_at"]:
                lines.append(f"   Published: {r['published_at']}")
            if r["url"]:
                lines.append(f"   URL: {r['url']}")
            highlighted = _row_value(r, "snippet")
            if highlighted:
                lines.append(f"   {highlighted}")
            elif r["content"]:
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
                   WHERE s.name LIKE ? ESCAPE '\\' OR s.identifier LIKE ? ESCAPE '\\'
                   ORDER BY f.published_at DESC LIMIT ?""",
                (
                    f"%{_like_escape(source)}%",
                    f"%{_like_escape(source)}%",
                    limit,
                ),
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

