"""Centralized logging configuration and request-level tool-call logging.

Two responsibilities:

1. :func:`configure_logging` — install a single, consistent log format and
   level for the whole process (Rich handler when available, plain stream
   handler otherwise). Idempotent, so it never duplicates output.

2. :func:`install_tool_call_logging` — wrap the MCP ``CallToolRequest``
   handler so every tool invocation logs *which* tool was called (plus a
   compact, redacted summary of its arguments) instead of the bare
   ``Processing request of type CallToolRequest`` line the SDK emits.

Why wrap the handler instead of using a ``logging.Filter``?
-----------------------------------------------------------
The SDK logs::

    logger.info("Processing request of type %s", type(req).__name__)

Only the *type name* is passed as the log argument, so a filter never sees
the request object and cannot recover ``params.name``. Wrapping the
registered handler gives us the real ``CallToolRequest`` while leaving the
SDK itself untouched.

The wrapper is installed defensively: if the SDK's internal layout ever
changes (``_mcp_server`` / ``request_handlers`` missing), we silently skip
installation rather than breaking the server. Logging is a nice-to-have;
serving tools is not.
"""

import logging
import os
import time
from typing import Any

log = logging.getLogger("google_search_mcp")

# ---------------------------------------------------------------------------
# Argument redaction / truncation
# ---------------------------------------------------------------------------

# Argument names whose values must never be written to the log. Matched
# case-insensitively against the *whole* key name.
_REDACT_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_key",
        "secret_key",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "private_key",
        "session_key",
    }
)

# Per-value and total truncation limits. Base64 images can be hundreds of
# kilobytes, so truncation is not cosmetic — it keeps the log readable.
_MAX_VALUE_CHARS = 60
_MAX_ARGS_CHARS = 300

_REDACTED = "'***'"


def _format_value(name: str, value: Any) -> str:
    """Render a single argument value, redacting secrets and truncating."""
    if name.lower() in _REDACT_KEYS:
        return _REDACTED
    text = repr(value)
    if len(text) > _MAX_VALUE_CHARS:
        text = f"{text[:_MAX_VALUE_CHARS]}...<{len(text)} chars>"
    return text


def format_tool_arguments(arguments: dict[str, Any] | None) -> str:
    """Render tool arguments as a compact, redacted, single-line string.

    Args:
        arguments: The ``params.arguments`` dict from a ``CallToolRequest``.

    Returns:
        Something like ``query='python', num_results=5`` — or ``""`` when
        there are no arguments.
    """
    if not arguments or not isinstance(arguments, dict):
        return ""
    parts = [f"{key}={_format_value(key, value)}" for key, value in arguments.items()]
    joined = ", ".join(parts)
    if len(joined) > _MAX_ARGS_CHARS:
        joined = f"{joined[:_MAX_ARGS_CHARS]}..."
    return joined


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------


def configure_logging(level: str | None = None) -> None:
    """Install a consistent log format/level for the whole process.

    The MCP SDK already calls ``configure_logging`` when ``FastMCP`` is
    constructed, so by the time this runs the root logger usually has a
    Rich handler attached. In that case we only adjust the level — adding a
    second handler would duplicate every line.

    Args:
        level: Log level name (``DEBUG``/``INFO``/...). Defaults to the
            ``LOG_LEVEL`` environment variable, then ``INFO``.
    """
    resolved = (level or os.environ.get("LOG_LEVEL", "") or "INFO").strip().upper()
    numeric = logging.getLevelName(resolved)
    if not isinstance(numeric, int):
        numeric = logging.INFO

    root = logging.getLogger()
    if not root.handlers:
        handler: logging.Handler
        try:  # pragma: no cover - depends on optional rich install
            from rich.console import Console
            from rich.logging import RichHandler

            handler = RichHandler(console=Console(stderr=True), rich_tracebacks=True)
        except ImportError:  # pragma: no cover
            handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)

    root.setLevel(numeric)


# ---------------------------------------------------------------------------
# Tool-call logging
# ---------------------------------------------------------------------------


def install_tool_call_logging(mcp: Any) -> bool:
    """Wrap the MCP ``CallToolRequest`` handler to log the tool name.

    After installation, a tool call produces::

        CallToolRequest: google_search(query='python', num_results=5)

    instead of the SDK's bare ``Processing request of type CallToolRequest``.

    Args:
        mcp: The ``FastMCP`` instance whose tools should be logged.

    Returns:
        ``True`` if the wrapper was installed, ``False`` if the SDK layout
        was not recognised or the wrapper was already present.
    """
    try:
        from mcp import types
    except ImportError:  # pragma: no cover - mcp is a hard dependency
        return False

    server = getattr(mcp, "_mcp_server", None)
    handlers = getattr(server, "request_handlers", None)
    if not isinstance(handlers, dict):
        return False

    original = handlers.get(types.CallToolRequest)
    if original is None or getattr(original, "_gsmcp_logged", False):
        return False

    async def _logged_handler(req: Any) -> Any:
        params = getattr(req, "params", None)
        name = getattr(params, "name", "?")
        summary = format_tool_arguments(getattr(params, "arguments", None))
        log.info("CallToolRequest: %s(%s)", name, summary)

        start = time.monotonic()
        try:
            return await original(req)
        except Exception:
            log.exception("CallToolRequest: %s raised", name)
            raise
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            log.debug("CallToolRequest: %s finished in %.0f ms", name, elapsed_ms)

    # Mark so a second call (e.g. a re-import) does not double-wrap.
    _logged_handler._gsmcp_logged = True  # type: ignore[attr-defined]
    handlers[types.CallToolRequest] = _logged_handler
    return True
