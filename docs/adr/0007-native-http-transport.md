# ADR 0007 — Serve the MCP HTTP transports natively (retire `mcp-proxy`)

**Status:** accepted (2026-11-04)
**Supersedes:** the `npx mcp-proxy` deployment documented in `NOTES.prs`

## Context

Port `11403` was owned by a Node process, not by this server:

```text
client ──HTTP/SSE──► mcp-proxy (Node, :11403) ──stdio JSON-RPC──► python -m google_search_mcp
```

`mcp-google.service` launched it as:

```ini
ExecStart=…/node …/npx -y mcp-proxy --port 11403 --transport streamable-http \
          -- …/python -m google_search_mcp
```

Three things went wrong with that arrangement.

### 1. The `/sse` endpoint returned `400 No active transport` on every POST

`mcp-proxy` 6.x implements the deprecated HTTP+SSE transport with an in-memory
session registry (`activeTransports`) inside the one Node process. Reading
`src/startHTTPServer.ts` → `handleSSERequest`, the order is:

1. `new SSEServerTransport("/messages", res)` — mints the session id;
2. `await createServer(req)` — cheap; wraps the shared upstream client;
3. `activeTransports[transport.sessionId] = transport` — **registered**;
4. `res.on("close", handleResponseClose)` — deletes that entry;
5. `await server.connect(transport)` — **this** writes `event: endpoint`.

So receiving `event: endpoint` proves the session *was* registered. A subsequent
`400 No active transport` means the entry was **deleted between the two
requests**, and every deletion site hangs off the SSE GET stream closing
(`handleResponseClose`, `res.closed`, and the connect-failure path).

That is inherent to the transport, not a bug: the legacy SSE transport binds a
session to a single open GET stream, and responses are delivered *on that
stream*. A client that reads the endpoint event and then closes the stream (or
whose connection is dropped by an intermediary, or that speaks to a restarted
process) loses the session. The `400` is the only symptom.

The control test that "proved" the server never registers sessions did not
discriminate: an unknown id and a just-deleted id both miss the map, and
`mcp-proxy` logs **nothing** on the legacy path, so the service log could not
show either way.

### 2. `--transport` was never a valid `mcp-proxy` flag

`mcp-proxy` selects transports with `--server {sse,stream}`; yargs is
non-strict, so `--transport streamable-http` was parsed and **silently ignored**.
Both `/sse` and `/mcp` had always been enabled — the unit file's stated intent
was never in effect.

### 3. The Python server could serve HTTP all along

`FastMCP.run(transport="streamable-http")` already exists in the SDK this
project pins. The proxy added a process, an unpinned `npx -y` download on every
restart, and a deprecated transport on the wire, in exchange for nothing the
SDK does not provide.

## Decision

Serve the transports from the Python process itself, in a new module
`src/google_search_mcp/http_app.py`:

* `POST/GET /mcp` — streamable HTTP, **stateful** by default (the client
  receives `Mcp-Session-Id` on `initialize` and echoes it back), with a
  **stateless** mode behind `NOAPI_MCP_HTTP_STATELESS=1`.
* `GET /sse` + `POST /messages/` — the deprecated HTTP+SSE transport, kept for
  one release behind `NOAPI_MCP_HTTP_ENABLE_LEGACY_SSE` (default on).
* `mcp-google.service` binds `0.0.0.0:11403` directly. The port does not move,
  so client configuration does not change.

### Two SDK behaviours that forced more than a one-line change

**DNS-rebinding protection is frozen at construction.** `FastMCP.__init__`
auto-enables `TransportSecuritySettings` with a loopback-only allow-list
whenever `host` is `127.0.0.1` — which it is at construction
(`server.py:341`). Assigning `mcp.settings.host = "0.0.0.0"` later does **not**
update it, so requests addressed to `latitude:11403` would be answered with
`421 Invalid Host header`. `http_app.apply_http_settings` therefore overwrites
`settings.transport_security` explicitly.

It is **off by default**: the previous deployment performed no Host validation,
and turning it on with the auto-generated loopback list would break every
existing LAN client. `NOAPI_MCP_HTTP_DNS_REBINDING_PROTECTION=1` plus
`NOAPI_MCP_HTTP_ALLOWED_HOSTS` opt in. Note the SDK only understands `host:*`
suffix wildcards — CIDR and `192.168.178.*` patterns do **not** work, so hosts
must be listed by name.

**The lifespan is not process-scoped.** Both
`StreamableHTTPSessionManager._handle_stateful_request` and
`_handle_stateless_request` call `self.app.run(...)`, which enters FastMCP's
lifespan; the SSE app enters it once per connection. `app_lifespan` starts the
health server (binding `:11499`) and closes the shared browser pool on exit, so
running it per session would rebind the health port on every new client and
tear the browser pool down whenever any one client disconnected.

`http_app.build_http_app` therefore takes ownership: its composed lifespan runs
`_startup_services` once, runs `session_manager.run()`, and calls
`_shutdown_services` once. `server.set_external_lifecycle(True)` flips
`app_lifespan` into a pass-through so per-session entry is inert. The stdio path
is unchanged.

## Consequences

* One process instead of two; no `npx` download at start; no Node dependency on
  the MCP path (Node stays in `PATH` for yt-dlp's JavaScript runtime).
* Restarting the service no longer requires a proxy to re-resolve packages, and
  `Restart=on-failure` now supervises the actual server.
* The failure mode behind the original report is gone from `/mcp` (a stateful
  session survives disconnects; stateless keeps no registry at all) and is
  *documented* for `/sse` rather than mysterious.
* `restart_server.sh` no longer contains a second half that killed the
  systemd-owned proxy and `nohup`ed a competing one — which by itself could
  invalidate every session.
* `restart_server.sh` / `start.sh` no longer pass `-i`; the process must exit on
  crash so systemd can restart it.
* The PyInstaller spec now targets `scripts/frozen_entry.py`. It previously
  pointed at `server.py`, which has no `__main__` guard — that binary imported
  the server and exited, so it could never have served anything.

## Rejected alternatives

***Patch the `npx` cache.*** It is a version-specific download directory that
`npx -y` re-creates unpinned. Any patch disappears on the next restart.

*Keep `mcp-proxy` and fix the bridge's stream handling.* The bridge would still
be relying on a deprecated transport whose session dies with a TCP connection,
through an extra process that cannot be supervised independently. Fixing the
client and keeping the proxy leaves the failure mode reachable.

*Run `mcp.run(transport="streamable-http")` directly.* Simplest possible change,
but it hits both SDK behaviours above: `421` on LAN hostnames and
per-session health/pool churn.
