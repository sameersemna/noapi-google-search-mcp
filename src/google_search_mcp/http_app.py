"""Native HTTP transports — serve the MCP server without ``mcp-proxy``.

Before this module the deployment chain was::

    client ──HTTP/SSE──► mcp-proxy (Node, :11403) ──stdio JSON-RPC──► python

which meant an extra process, an unpinned ``npx -y`` download at every start, and
the deprecated HTTP+SSE transport's single-stream session model on the wire.

This module serves the FastMCP server directly over the two transports the SDK
implements, from the same process that runs the tools:

* ``/mcp``  — streamable HTTP (recommended). A stateful session survives
  disconnects and is identified by the ``Mcp-Session-Id`` header; stateless mode
  (``NOAPI_MCP_HTTP_STATELESS=1``) builds a transport per request and issues no
  session id at all.
* ``/sse`` + ``/messages/`` — the deprecated HTTP+SSE transport, kept available
  behind ``NOAPI_MCP_HTTP_ENABLE_LEGACY_SSE`` for one release.

Two behaviours of ``FastMCP`` make a naive ``mcp.run(transport=...)`` unsafe here,
and both are handled below:

1. **DNS-rebinding protection is decided at construction time.** ``FastMCP``
   snapshots ``transport_security`` in ``__init__`` from the *initial* host
   (``127.0.0.1`` → loopback-only allow-list). Assigning
   ``mcp.settings.host = "0.0.0.0"`` afterwards does not update it, so requests
   addressed to a LAN name such as ``latitude:11403`` would be answered with
   ``421 Invalid Host header``. ``_apply_http_settings`` overwrites it explicitly.

2. **The lifespan is not process-scoped.** The streamable-HTTP session manager
   enters ``app.run(...)`` once per new session (stateful) or once per request
   (stateless), and the SSE app enters it once per connection. ``app_lifespan``
   starts the health server on :11499 and closes the shared browser pool on exit,
   so running it per session would rebind the health port on every new client and
   tear the browser pool down whenever one client disconnected. ``build_http_app``
   therefore takes ownership: it starts those services once in the composed
   lifespan and flips ``set_external_lifecycle(True)`` so the per-session lifespan
   is a pass-through.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette

from . import config as default_config
from . import server as server_module

__all__ = ["build_http_app", "serve", "apply_http_settings"]


def _build_transport_security(cfg: Any) -> Any:
    """Translate the ``HTTP_*`` security settings into the SDK's model.

    Imported lazily so this module stays importable without paying for the SDK's
    FastMCP stack at import time (the project keeps heavy imports lazy).
    """
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(cfg.HTTP_DNS_REBINDING_PROTECTION),
        allowed_hosts=list(cfg.HTTP_ALLOWED_HOSTS),
        allowed_origins=list(cfg.HTTP_ALLOWED_ORIGINS),
    )


def apply_http_settings(
    mcp: Any,
    config_obj: Any = None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Push the HTTP-transport settings onto a ``FastMCP`` instance.

    Must run *before* ``streamable_http_app()``/``sse_app()``: those calls build
    the session manager and the transports from ``mcp.settings`` once and do not
    re-read it afterwards.

    *host* / *port* are the already-resolved bind values (CLI flag first, then
    config), so an explicit ``--host`` is never clobbered by the config default.
    """
    cfg = config_obj or default_config

    mcp.settings.host = host or cfg.HTTP_HOST
    mcp.settings.port = cfg.HTTP_PORT if port is None else int(port)
    mcp.settings.stateless_http = bool(cfg.HTTP_STATELESS)
    mcp.settings.json_response = bool(cfg.HTTP_JSON_RESPONSE)
    # Overwrite whatever ``FastMCP.__init__`` inferred from the loopback default.
    mcp.settings.transport_security = _build_transport_security(cfg)


def _merge_routes(primary: list[Any], extra: list[Any]) -> list[Any]:
    """Append *extra* to *primary*, skipping routes whose path already exists.

    The SSE app and the streamable-HTTP app both append
    ``_custom_starlette_routes`` and (when auth is configured) the protected
    resource metadata routes, so merging them verbatim can register the same path
    twice. Starlette resolves routes first-match, so a duplicate is harmless but
    confusing in a route table dump — dedupe instead.
    """
    merged = list(primary)
    seen = {getattr(route, "path", None) for route in merged}
    for route in extra:
        path = getattr(route, "path", None)
        if path is not None and path in seen:
            continue
        seen.add(path)
        merged.append(route)
    return merged


def build_http_app(
    mcp: Any = None,
    config_obj: Any = None,
    host: str | None = None,
    port: int | None = None,
) -> Starlette:
    """Return a Starlette app serving the MCP endpoints over HTTP.

    The returned app's lifespan owns the process-wide services (health server,
    browser pool) and runs the streamable-HTTP session manager, so it must be
    driven by a real ASGI server rather than instantiated and discarded.
    """
    cfg = config_obj or default_config
    srv = mcp or server_module.mcp

    apply_http_settings(srv, cfg, host=host, port=port)

    # Build the streamable-HTTP app first: this is what lazily creates the
    # session manager, and it is the only transport that needs a lifespan.
    streamable = srv.streamable_http_app()
    routes = list(streamable.routes)

    if cfg.HTTP_ENABLE_LEGACY_SSE:
        routes = _merge_routes(routes, srv.sse_app().routes)

    # From here on the per-session FastMCP lifespan must not touch process-wide
    # services — this composed lifespan is the single owner.
    server_module.set_external_lifecycle(True)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        # ``_startup_services`` may terminate the process (invalid cookies and
        # the operator declined the login flow); it does so with os._exit so the
        # decision is not reported as a framework crash.
        health = await server_module._startup_services(srv)
        try:
            async with srv.session_manager.run():
                yield
        finally:
            await server_module._shutdown_services(health)

    return Starlette(routes=routes, lifespan=lifespan)


def serve(
    mcp: Any = None,
    host: str | None = None,
    port: int | None = None,
    config_obj: Any = None,
) -> None:
    """Run the MCP server over HTTP (blocking, in the foreground)."""
    cfg = config_obj or default_config
    srv = mcp or server_module.mcp

    bind_host = host or cfg.HTTP_HOST
    bind_port = cfg.HTTP_PORT if port is None else int(port)

    app = build_http_app(srv, cfg, host=bind_host, port=bind_port)

    endpoints = [srv.settings.streamable_http_path]
    if cfg.HTTP_ENABLE_LEGACY_SSE:
        endpoints.append(srv.settings.sse_path)
    mode = "stateless" if cfg.HTTP_STATELESS else "stateful"
    print(
        f"[http] serving MCP on http://{bind_host}:{bind_port} "
        f"({', '.join(endpoints)}; {mode} streamable HTTP"
        f"{'; legacy SSE enabled' if cfg.HTTP_ENABLE_LEGACY_SSE else ''})",
        file=sys.stderr,
        flush=True,
    )

    import uvicorn

    uvicorn.run(
        app,
        host=bind_host,
        port=bind_port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
