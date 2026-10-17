# ADR 0006 — Phase 9: parameter typing and structured output

- **Status:** Accepted
- **Date:** 2026-10-17
- **Context:** Phase 9 of the hardening plan ("MCP protocol polish"): strong
  parameter typing, structured output, transports, CLI.

## Context and constraints

The overriding constraint is the task's prime directive #1: **existing tool
names, parameter names, defaults and the shape of their text output must stay
backward compatible**. New behaviour may only be added behind optional
parameters or env flags.

Two mechanisms were evaluated against that constraint.

## Decision 1 — `Literal` enums: applied to two parameters only

**Applied:**
| Tool | Parameter | Values |
|---|---|---|
| `google_search` | `time_range` | `""`, `past_hour`, `past_day`, `past_week`, `past_month`, `past_year` |
| `transcribe_video`, `transcribe_local`, `search_transcript` | `model_size` | `tiny`, `base`, `small`, `medium`, `large` |

Rationale: previously an out-of-range value was **silently** mishandled —
`time_range` was silently ignored (the user asked for a time filter and got
none), and `model_size` was silently **coerced to `tiny`** (a different model
than requested). Both are surprising wrong results, so turning them into a
clear validation error is an improvement, and **every previously-valid value
remains valid** (the defaults `""` / `"tiny"` are included in the enums).

**Deliberately NOT applied:** feed `source_type` (`subscribe`, `unsubscribe`,
`check_feeds`, `search_feeds`, `get_feed_items`).

Reason: those tools normalise the input with `source_type.lower().strip()`, so
`"Reddit"`, `" NEWS "` etc. currently work. A `Literal` is **case-sensitive**
and would reject those previously-working inputs — a backward-compatibility
regression. Per the plan's own rule ("previously-invalid values … now get a
clear error *only if that's safe* — otherwise keep the default and document"),
the free-form string with runtime normalisation is kept, and the valid values
remain documented in the tool docstrings.

The canonical value tuples (`SEARCH_TIME_RANGES`, `FEED_SOURCE_TYPES`,
`MODEL_SIZES`) live in `config.py` and a test asserts the `Literal` arguments
match them exactly, so the schema and the docs cannot drift.

## Decision 2 — rich structured output: DEFERRED (evidence-based)

`FastMCP` 1.x (as installed) emits `structuredContent` for **every** tool
automatically. The return type controls *what* is emitted. Measured behaviour
(`mcp.call_tool`, verified by introspection):

| Tool return type | `content` (text blocks) | `structuredContent` |
|---|---|---|
| `str` (today) | human text (unchanged) | `{"result": "<the same text>"}` |
| Pydantic `BaseModel` | **the model dumped as pretty-printed JSON** | the model dict |
| `tuple[str, dict]` | human text **plus an extra JSON text block** | `{"result": [text, dict]}` |

Consequences:

- Returning a model **replaces the human-readable text with raw JSON**, which
  breaks the text-output compatibility contract.
- The tuple form keeps the text but **injects a duplicate JSON text block** into
  the content the model reads, changing the content array shape and wasting
  context.

There is therefore no mechanism in the installed SDK that yields human text
*plus* a custom structured payload without one of those regressions.

**Decision:** keep returning `str` (text output byte-identical to today, and
`structuredContent` already present as `{"result": …}`), and defer per-tool
typed schemas until either (a) the project can adopt the MCP SDK 2.x
`MCPServer`, whose structured-output semantics differ, or (b) a client
requirement justifies changing the text channel. Recorded here so the decision
is not re-litigated from scratch.

## Consequences

- Schemas for `time_range` / `model_size` now advertise `enum` (models pick
  valid values; a bad value fails fast with a clear message instead of silently
  producing a wrong result).
- No change to any tool's text output; registry snapshot unaffected (param
  *names* unchanged).
- Rich structured output is explicitly out of scope for v0.4.
