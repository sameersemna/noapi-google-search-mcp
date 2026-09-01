#!/usr/bin/env python3
"""Regression tests for MCP tool JSON Schema validity.

This guards against a class of bug where a tool parameter's `description`
(or any other schema keyword that MUST be a plain string per JSON Schema /
the OpenAI / MCP tool-calling spec) is accidentally built as a list or
tuple and never joined into a single string.

Concrete example that broke downstream providers (Novita via OpenRouter):

    Field(
        validation_alias=AliasChoices("identifier", "channel"),
        description=(
            "Source identifier. Depends on source_type: news preset "
            "(bbc, cnn, ...), subreddit name, owner/repo, arXiv "
            "category, @handle / URL / channel ID, or RSS URL. "
            "Some clients send this as `channel` instead of "
            "`identifier`; both are accepted.",   # <-- trailing comma!
        ),
    ),

The trailing comma after the closing parenthesis turns the parenthesized
string into a *tuple*, which Pydantic serializes as a JSON array:

    "identifier": {
      "description": ["Source identifier. ..."],   # array, NOT a string
      "title": "Identifier",
      "type": "string"
    }

Providers that strictly validate tool schemas reject the *entire* request
with a generic 400 the moment this malformed schema is present.

This test fetches the server's real `tools/list` response (the exact
schemas handed to an MCP client) and asserts, recursively over every
tool's JSON Schema, that every schema keyword expected to be a string
(`description`, `title`, `type`, `format`, `pattern`, `const`) is always
a `str`, never a `list`/`dict`/`tuple`. It fails if any tool violates
this, so a future tool addition can't silently reintroduce the bug.

Run with:
    /home/sameer/anaconda3/envs/mcp-google/bin/python test_tool_schema.py
"""

import asyncio
import os
import sys

sys.path.insert(0, "src")
# Skip cookie validation so the server module can be imported without
# touching the network / cookie files.
os.environ.setdefault("SKIP_COOKIE_VALIDATION", "1")

from google_search_mcp.server import mcp  # noqa: E402


# ── Schema validation helpers ──────────────────────────────────────────

# JSON Schema keywords that MUST be a plain string when present.
STRING_KEYS = {"description", "title", "type", "format", "pattern", "const"}

# Container keywords whose values are themselves schemas (or lists of
# schemas) that we must recurse into.
SCHEMA_CONTAINERS = {
    "properties",
    "items",
    "anyOf",
    "allOf",
    "oneOf",
    "not",
    "$defs",
    "definitions",
    "additionalProperties",
    "prefixItems",
}


def _walk_schema(node, path, problems):
    """Recursively walk a JSON Schema node, collecting non-string values
    for any keyword that is required to be a string."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in STRING_KEYS and value is not None and not isinstance(value, str):
                problems.append((path + "/" + key, type(value).__name__, value))
            elif key in SCHEMA_CONTAINERS:
                if isinstance(value, dict):
                    for sub_name, sub_schema in value.items():
                        _walk_schema(sub_schema, path + "/" + key + "/" + sub_name, problems)
                elif isinstance(value, list):
                    for i, item in enumerate(value):
                        _walk_schema(item, path + "/" + key + f"[{i}]", problems)
            elif key not in STRING_KEYS and isinstance(value, dict):
                # Unknown keyword holding a nested schema object — recurse
                # so we don't miss anything (e.g. `default` is allowed to be
                # any type, but a nested object may still contain schema
                # keywords that must be strings).
                _walk_schema(value, path + "/" + key, problems)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk_schema(item, path + f"[{i}]", problems)


def _validate_tool_schemas(tools):
    """Return a list of (path, actual_type, value) problems across all tools."""
    problems = []
    for tool in tools:
        # Tool's own top-level description must be a string too.
        desc = getattr(tool, "description", None)
        if desc is not None and not isinstance(desc, str):
            problems.append((tool.name + "/description", type(desc).__name__, desc))
        # Recurse over the tool's input JSON Schema.
        _walk_schema(tool.inputSchema, tool.name, problems)
    return problems


# ── Test harness ───────────────────────────────────────────────────────

passed = 0
failed = 0
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
        return True
    failed += 1
    failures.append(f"{label}: {detail}")
    print(f"  FAIL  {label}: {detail}")
    return False


async def test_tools_list_schema_strings():
    """Fetch the real tools/list and assert every string-keyword is a str."""
    print("\n[test_tools_list_schema_strings]")
    tools = await mcp.list_tools()
    check("tools/list returns tools", len(tools) > 0, f"got {len(tools)} tools")

    problems = _validate_tool_schemas(tools)
    if problems:
        detail = "; ".join(
            f"{path} is {typ} (value={value!r})" for path, typ, value in problems
        )
        check("all schema string keywords are str", False, detail)
    else:
        check(
            f"all {len(tools)} tools have valid string schema keywords",
            True,
        )


async def test_subscribe_identifier_description_is_string():
    """Specifically guard the subscribe tool's identifier.description."""
    print("\n[test_subscribe_identifier_description_is_string]")
    tools = await mcp.list_tools()
    subscribe = next((t for t in tools if t.name == "subscribe"), None)
    check("subscribe tool exists", subscribe is not None)
    if subscribe is None:
        return

    props = subscribe.inputSchema.get("properties", {})
    identifier = props.get("identifier", {})
    desc = identifier.get("description")
    check(
        "subscribe.identifier.description is a str",
        isinstance(desc, str),
        f"got {type(desc).__name__}: {desc!r}",
    )


async def main():
    print("=" * 70)
    print("  MCP tool JSON Schema validity tests")
    print("=" * 70)
    print()

    await test_tools_list_schema_strings()
    await test_subscribe_identifier_description_is_string()

    # Summary
    print()
    print("=" * 70)
    total = passed + failed
    print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
    print("=" * 70)
    if failures:
        print()
        print("Failures:")
        for f in failures:
            print(f"  - {f}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
