---
name: Bug report
about: A tool returned the wrong thing, crashed, or the server would not start
title: ""
labels: bug
assignees: ""
---

## What happened

<!-- The exact output or error, and what you expected instead. -->

## How to reproduce

<!--
The smallest sequence that shows it. If it involves a specific page, include the
URL; if it involves a model, name the client (LM Studio, Claude Desktop, …).
-->

1.
2.
3.

## Environment

```text
noapi-google-search-mcp --version
python -m google_search_mcp --check
```

## Which parts are affected

- [ ] Tool call (say which tool)
- [ ] Server startup / install
- [ ] Transport (`stdio` / `/mcp` / `/sse`)
- [ ] `/health` reporting
- [ ] Docs

## Anything else

<!--
For an HTTP deployment, the relevant part of `GET /health` helps: it reports
dependency status, the active transport, and per-tool call counters.
Redact cookies and credentials before pasting.
-->
