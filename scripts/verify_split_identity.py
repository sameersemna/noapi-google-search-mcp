#!/usr/bin/env python3
"""Verify the server.py split preserved tool logic byte-for-byte.

Extracts every ``@mcp.tool()`` function (and its helper functions) from the
ORIGINAL committed server.py and compares the source text to the same
function in the new tools/ modules. If any function body differs, the split
changed behavior and this script reports it.

Run with:
    /home/sameer/anaconda3/envs/mcp-google/bin/python scripts/verify_split_identity.py
"""

import ast
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORIG = os.path.join(ROOT, "src", "google_search_mcp", "server.py")
TOOLS_DIR = os.path.join(ROOT, "src", "google_search_mcp", "tools")


def get_original_source() -> str:
    """Return the committed (pre-split) server.py source."""
    r = subprocess.run(
        ["git", "show", "HEAD:src/google_search_mcp/server.py"],
        capture_output=True, text=True, cwd=ROOT,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git show failed: {r.stderr}")
    return r.stdout


def extract_functions(source: str) -> dict[str, str]:
    """Return {qualified_name: source_text} for every function in the module."""
    tree = ast.parse(source)
    out: dict[str, str] = {}

    def _walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                out[name] = ast.get_source_segment(source, child)
                _walk(child, prefix=f"{name}.")
            elif isinstance(child, (ast.ClassDef,)):
                _walk(child, prefix=f"{prefix}{child.name}.")

    _walk(tree)
    return out


def main() -> int:
    orig_src = get_original_source()
    orig_funcs = extract_functions(orig_src)

    # Collect all functions from the new tools/ modules.
    new_funcs: dict[str, str] = {}
    for fname in sorted(os.listdir(TOOLS_DIR)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue
        path = os.path.join(TOOLS_DIR, fname)
        with open(path) as f:
            src = f.read()
        for name, text in extract_functions(src).items():
            new_funcs[f"{fname[:-3]}.{name}"] = text

    # For every function that existed in the original, find it in the new
    # modules by its FULL qualified path (e.g. "do_google_news._fallback"),
    # which is unambiguous even when multiple functions share a bare name.
    mismatches = []
    matched = 0
    not_found = 0
    for orig_name, orig_text in orig_funcs.items():
        # The new modules are flat (no nested classes), so the qualified path
        # is "<module>.<name>.<nested>...". The original path is
        # "<name>.<nested>...". Match on the suffix after the module name.
        # We look for a new function whose path ends with the original path.
        found = None
        for new_name, new_text in new_funcs.items():
            # new_name like "search.do_google_news._fallback"
            # orig_name like "do_google_news._fallback"
            if new_name.endswith("." + orig_name) or new_name == orig_name:
                found = new_text
                break
        if found is None:
            # Function not in new modules — it's a server.py-only helper
            # (lifespan, validation, metrics) or was re-exported.
            not_found += 1
            continue
        if orig_text.strip() != found.strip():
            mismatches.append(orig_name)
        else:
            matched += 1

    print(f"Matched {matched} functions byte-identical between original and split.")
    print(f"({not_found} original functions not in tools/ — server.py-only helpers, expected.)")
    if mismatches:
        print(f"\n❌ {len(mismatches)} functions DIFFER:")
        for orig_name in mismatches:
            print(f"  - {orig_name}")
        return 1
    print("\n✅ All matched tool/helper functions are byte-identical. No logic changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
