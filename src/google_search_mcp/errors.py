"""Typed error model for the Google Search MCP server.

Why this module exists
----------------------
Historically every failure was ``return``-ed as ordinary text (e.g.
``"Search failed: ..."``). A returned string has ``is_error=False``, so to the
model *and* to every client UI it looked like the tool had succeeded. That made
failures indistinguishable from content and defeated retries.

This module provides:

* :class:`GoogleMcpError` and its subclasses, raised *inside* tools.
* :func:`to_tool_error` / :func:`tool_guard`, which convert those (and
  unexpected crashes) into MCP ``ToolError``s. A ``ToolError`` comes back to the
  client as a normal result with ``is_error=True`` and the human-readable
  message preserved in ``content`` — so the wording the model sees barely
  changes, but the *signal* finally exists.

Verified API (mcp, FastMCP): ``from mcp.server.fastmcp.exceptions import
ToolError``.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import TypeVar

from mcp.server.fastmcp.exceptions import ToolError

log = logging.getLogger("google_search_mcp.errors")

F = TypeVar("F", bound=Callable)


class GoogleMcpError(Exception):
    """Base class for anticipated, user-actionable tool failures."""

    #: Short prefix used when the error has no message of its own.
    default_message = "The request could not be completed."

    def __init__(self, message: str = ""):
        super().__init__(message or self.default_message)


class UserInputError(GoogleMcpError):
    """The caller supplied invalid/unsupported arguments."""

    default_message = "Invalid request. Check the arguments and try again."


class UpstreamBlockedError(GoogleMcpError):
    """An upstream (usually Google) blocked or rate-limited the request.

    Carries the tool-facing message so the model can decide to serve from cache
    or use a different tool instead of retrying immediately.
    """

    default_message = (
        "Upstream blocked the request (bot detection or rate limit). "
        "Try again later, or use a non-Google tool such as a feed or Wikipedia."
    )


class UpstreamError(GoogleMcpError):
    """An upstream service returned an error or was unreachable."""

    default_message = "The upstream service is temporarily unavailable."


class DependencyMissingError(GoogleMcpError):
    """An optional dependency required by this tool is not installed."""

    default_message = "A required optional dependency is not installed."


def to_tool_error(
    exc: Exception, *, tool: str = "", operation: str = ""
) -> ToolError:
    """Convert *exc* into an MCP ``ToolError`` with a clean, model-readable text.

    * Known :class:`GoogleMcpError` subclasses keep their own message (the
      wording clients already relied on).
    * Anything else is treated as a crash: the real text is logged at ERROR,
      and the model receives a generic message (no tracebacks leak).
    """
    if isinstance(exc, GoogleMcpError):
        text = str(exc) or exc.default_message
        if operation:
            text = f"{operation} failed. {text}"
        return ToolError(text)

    log.error(
        "Unhandled error in tool %s%s",
        tool or "?",
        f" ({operation})" if operation else "",
        exc_info=exc,
    )
    where = operation or tool or "The operation"
    return ToolError(f"{where} failed. Please try again.")


def tool_guard(fn: F) -> F:
    """Decorator: raise MCP ``ToolError`` for anticipated failures.

    Apply *inside* the ``@mcp.tool(...)`` decorator to any tool that may raise a
    :class:`GoogleMcpError` (or crash). ``asyncio.CancelledError`` is allowed to
    propagate untouched so client cancellation still works.
    """

    import asyncio

    @functools.wraps(fn)
    async def _wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate: convert & report
            raise to_tool_error(exc, tool=fn.__name__) from exc

    return _wrapper  # type: ignore[return-value]
