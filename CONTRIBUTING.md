# Contributing

Thanks for taking the time to help. This project is a forked, hardened MCP server
— see the "This fork vs upstream" section of the [README](README.md) for how the
two trees relate.

## Set up

```bash
git clone https://github.com/sameersemna/noapi-google-search-mcp.git
cd noapi-google-search-mcp
python -m venv .venv && . .venv/bin/activate    # Python 3.10–3.13
pip install -e ".[dev]"
playwright install chromium
```

Optional extras, when your change touches them:

```bash
pip install -e ".[dev,ocr]"      # ocr_image, scanned-PDF fallback
```

## The test policy

The default suite is **offline**: no network, no browser, no Google traffic.

```bash
pytest -m "not live" -q                                  # must be green
pytest -m "not live" -q --cov=google_search_mcp.core      # coverage gate: 80%
ruff check src/google_search_mcp/core src/google_search_mcp/errors.py \
           src/google_search_mcp/tools/annotations.py \
           src/google_search_mcp/__init__.py src/google_search_mcp/__main__.py
python -m google_search_mcp --check                       # dependency doctor
```

Tests that genuinely need Google or a browser are marked `live` and are opt-in
(`pytest -m live`). Do not add an unmarked network test — CI has no browser and
no cookies, and an unmarked test that hits Google is a flaky test that will
eventually fail someone else's pull request.

Two guard tests are worth knowing about because they fail loudly on refactors:

- `tests/unit/test_undefined_names.py` runs `ruff --select F821,F823` over the
  whole package. It exists because a module-layout change once left five tool
  modules using names they never imported — every affected tool raised
  `NameError` on its first line, and nothing caught it.
- `tests/unit/test_evaluate_scripts_syntax.py` extracts every inline
  `page.evaluate` script with `ast` and compiles it with `node --check`. A
  JavaScript syntax error in one of those strings is invisible to Python.

## Adding or changing a tool

A new tool touches five places. Missing one of them fails a test (deliberately):

1. `src/google_search_mcp/tools/<module>.py` — the function, decorated
   `@mcp.tool()`, with a docstring that includes sample prompts and an `Args:`
   block (the docstring is the tool description the model reads).
2. `src/google_search_mcp/tools/annotations.py` — add to `_SPECS` so the tool
   advertises `readOnlyHint` / `destructiveHint` / `idempotentHint` /
   `openWorldHint` / `title`.
3. `src/google_search_mcp/core/cache.py` — add a `PER_TOOL_TTL` entry if the tool
   is read-only and should be cached.
4. `tests/unit/test_tool_registry.py` — add to `EXPECTED` and bump
   `EXPECTED_TOOL_COUNT`.
5. The tool count in `pyproject.toml`, `__init__.py` and `README.md`.

For a parameter that changes behaviour, also record it in `CHANGELOG.md` and, if
it is an environment variable, in the README configuration table — a test
(`tests/unit/test_docs_env_vars.py`) fails if a documented variable is not read
anywhere in `src/`.

### Tool output conventions

- Return a `str` unless the tool genuinely returns images or mixed content
  (`list`). Returning a Pydantic model replaces the text channel.
- Never raise for an expected condition. Return a message that says what is
  missing and how to fix it — `Missing credentials …`, `File not found: …`,
  `OCR is an optional extra. Install it with: …`.
- Never `print()` to stdout. Stdout is the JSON-RPC channel on the stdio
  transport; logs go to stderr via `logging` (`tests/unit/test_stdout_hygiene.py`
  enforces this).
- Prefer double-quoted strings inside inline JavaScript. The escaping traps in
  these blocks have bitten three times: `\b` in a non-raw Python string becomes a
  backspace, `\\u` in a raw string doubles the escape, and `\'` in a non-raw
  string produces invalid JavaScript.

## Commits and pull requests

- Keep the change and its test in the same commit; a fix without a regression
  test will regress.
- Explain *why* in the commit body. Describe the observed symptom and the
  mechanism, not just the edit — the next reader did not watch it fail.
- Run the four commands in "The test policy" above before opening a pull
  request; the PR template repeats them.
- For a change to dependency metadata, say which Python versions you installed
  on and whether you tested without the optional extra.

## Filing a bug

Include the output of:

```bash
noapi-google-search-mcp --version
python -m google_search_mcp --check
```

and, if the tool ran over HTTP, the relevant part of `GET /health` (it reports
dependencies, the active transport, and per-tool call counters). Redact cookies
and credentials.
