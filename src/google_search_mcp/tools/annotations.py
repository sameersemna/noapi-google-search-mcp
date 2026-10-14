"""MCP tool annotations (hints) for every registered tool.

Why a central map?
------------------
Every tool must advertise whether it is read-only, idempotent, destructive and
whether it touches the open world, so MCP clients (and the model) can reason
about retries and side effects. Rather than scatter 42 ``@mcp.tool(...)``
arguments across eight modules, the mapping lives here as the single source of
truth and is applied once, at startup, by :func:`apply_tool_annotations`.

The application mutates the FastMCP ``Tool`` objects in the tool manager. This
is the same defensive, degrade-silently pattern the metrics wrapper already uses
for private SDK layout; if the SDK layout changes we log and continue (the
server still serves every tool — annotations are metadata, not behaviour).

Verified API (mcp): ``from mcp.types import ToolAnnotations`` with fields
``readOnlyHint``, ``destructiveHint``, ``idempotentHint``, ``openWorldHint``,
``title``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mcp.types import ToolAnnotations

log = logging.getLogger("google_search_mcp.annotations")


@dataclass(frozen=True)
class ToolSpec:
    title: str
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool

    def to_annotations(self) -> ToolAnnotations:
        return ToolAnnotations(
            title=self.title,
            readOnlyHint=self.read_only,
            destructiveHint=self.destructive,
            idempotentHint=self.idempotent,
            openWorldHint=self.open_world,
        )


def _spec(title: str, *, ro: bool, de: bool, idem: bool, ow: bool) -> ToolSpec:
    return ToolSpec(title, ro, de, idem, ow)


# ── Reusable category specs ────────────────────────────────────────────────
# Read-only, repeatable, hits the open network (Google lookups, web reads).
_LOOKUP = dict(ro=True, de=False, idem=True, ow=True)
# Read-only, repeatable, operates only on local state (DB / files / compute).
_LOCAL_READ = dict(ro=True, de=False, idem=True, ow=False)
# Writes only to the local cache/disk; safe to repeat; no remote side effect.
_LOCAL_WRITE = dict(ro=False, de=False, idem=True, ow=False)
# Remotely visible write, made idempotent via idempotency keys / dedupe.
_REMOTE_WRITE = dict(ro=False, de=False, idem=True, ow=True)


_SPECS: dict[str, ToolSpec] = {
    # ── Google lookups (16) ──
    "google_search": _spec("Google Web Search", **_LOOKUP),
    "google_news": _spec("Google News", **_LOOKUP),
    "google_scholar": _spec("Google Scholar", **_LOOKUP),
    "google_images": _spec("Google Images", **_LOOKUP),
    "google_trends": _spec("Google Trends", **_LOOKUP),
    "google_maps": _spec("Google Maps", **_LOOKUP),
    "google_maps_directions": _spec("Google Maps Directions", **_LOOKUP),
    "google_finance": _spec("Google Finance", **_LOOKUP),
    "google_weather": _spec("Google Weather", **_LOOKUP),
    "google_shopping": _spec("Google Shopping", **_LOOKUP),
    "google_books": _spec("Google Books", **_LOOKUP),
    "google_translate": _spec("Google Translate", **_LOOKUP),
    "google_flights": _spec("Google Flights", **_LOOKUP),
    "google_hotels": _spec("Google Hotels", **_LOOKUP),
    "google_lens": _spec("Google Lens (reverse image search)", **_LOOKUP),
    "google_lens_detect": _spec("Google Lens Object Detection", **_LOOKUP),
    # ── Web reads (3) ──
    "visit_page": _spec("Visit Web Page", **_LOOKUP),
    "wikipedia": _spec("Wikipedia", **_LOOKUP),
    # Read-only IMAP (uses BODY.PEEK so mail is not marked seen).
    "fetch_emails": _spec("Fetch Emails", **_LOOKUP),
    # ── Local reads (7) ──
    "list_images": _spec("List Images", **_LOCAL_READ),
    "ocr_image": _spec("OCR Image", **_LOCAL_READ),
    "read_document": _spec("Read Document", **_LOCAL_READ),
    "search_transcript": _spec("Search Transcript", **_LOCAL_READ),
    "list_subscriptions": _spec("List Feed Subscriptions", **_LOCAL_READ),
    "search_feeds": _spec("Search Feeds", **_LOCAL_READ),
    "get_feed_items": _spec("Get Feed Items", **_LOCAL_READ),
    # ── Local compute writing to cache (2) ──
    "transcribe_video": _spec("Transcribe Video", ro=False, de=False, idem=True, ow=True),
    "transcribe_local": _spec("Transcribe Local Media", **_LOCAL_WRITE),
    # ── Local file outputs (3) ──
    "extract_video_clip": _spec("Extract Video Clip", ro=False, de=False, idem=True, ow=True),
    "convert_media": _spec("Convert Media", **_LOCAL_WRITE),
    "generate_qr": _spec("Generate QR Code", **_LOCAL_WRITE),
    # ── Remote writes (4) ──
    "paste_text": _spec("Paste Text to Pastebin", **_REMOTE_WRITE),
    "shorten_url": _spec("Shorten URL", **_REMOTE_WRITE),
    "archive_webpage": _spec("Archive Web Page", **_REMOTE_WRITE),
    # upload_to_s3 can overwrite an existing key by default -> destructive.
    "upload_to_s3": _spec("Upload to S3/MinIO", ro=False, de=True, idem=True, ow=True),
    # ── Feed subscriptions (3) ──
    "subscribe": _spec("Subscribe to Feed Source", ro=False, de=False, idem=True, ow=False),
    "unsubscribe": _spec("Unsubscribe from Feed Source", ro=False, de=True, idem=True, ow=False),
    "check_feeds": _spec("Check Feeds", ro=False, de=False, idem=True, ow=True),
    # ── Misc (4) ──
    "check_cookies": _spec("Check Cookie Status", **_LOCAL_READ),
    "manual_intervention_status": _spec("Manual Intervention Status", **_LOCAL_READ),
    "open_manual_browser": _spec("Open Manual Browser", ro=False, de=False, idem=False, ow=True),
    "list_subtitles": _spec("List Video Subtitles", **_LOOKUP),
}


def annotation_for(name: str) -> ToolAnnotations | None:
    """Return the annotations for a tool name, or ``None`` if unmapped."""
    spec = _SPECS.get(name)
    return spec.to_annotations() if spec else None


def apply_tool_annotations(mcp) -> int:
    """Attach annotations + titles to every registered tool. Returns count applied.

    Idempotent and defensive: silently no-ops if the SDK's private tool
    registry is unavailable.
    """
    tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", None)
    if not isinstance(tools, dict):
        log.warning("Tool registry layout changed; annotations not applied")
        return 0

    applied = 0
    for name, spec in _SPECS.items():
        tool = tools.get(name)
        if tool is None:
            continue
        try:
            tool.annotations = spec.to_annotations()
            if not getattr(tool, "title", None):
                tool.title = spec.title
            applied += 1
        except Exception as exc:  # noqa: BLE001 - metadata only, never fatal
            log.debug("Could not annotate %s: %s", name, exc)

    unmapped = sorted(set(tools) - set(_SPECS))
    if unmapped:
        log.warning("Tools without annotations: %s", ", ".join(unmapped))
    return applied
