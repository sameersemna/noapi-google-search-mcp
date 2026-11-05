"""Validate every inline ``page.evaluate`` script in the tool modules.

The scraping code embeds JavaScript in Python triple-quoted strings. Nothing type-
checks or compiles it, and a broken script does not fail loudly: it raises inside
``page.evaluate`` and is swallowed by the surrounding ``except Exception``, which
turns into a friendly "Google <tool> lookup failed. Please try again." — i.e. a
tool that looks temporarily blocked while it is actually permanently broken.

Three real regressions of exactly this shape:

* a stray ``}`` (an unbalanced script is a syntax error, so *every* call fails);
* ``\\b`` in a non-raw Python string, which Python turns into a backspace and
  which silently kills the regex it was part of;
* ``\\\\u`` in a *raw* string, where the doubling leaked into the JS pattern.

This test extracts every script and compiles it with Node when available
(``node --check`` is a real JavaScript parse, not a heuristic). Without Node it
falls back to a string-literal-aware brace/paren balance check, which still
catches the structural class of bug.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "google_search_mcp"


def _scripts() -> list[tuple[str, int, str]]:
    """Every inline script as ``(relative file, line, source)``.

    The script is taken from the *parsed* module rather than from the file text.
    That distinction matters: a non-raw Python string is escaped before the
    browser sees it, so reading the raw file would report a ``\\\\`` that never
    reaches JavaScript (and miss the ``\\b`` that does). ``ast`` hands back the
    exact runtime string, including the raw/non-raw difference.
    """
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "server_selenium.py":  # dead code, excluded from lint too
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "evaluate"):
                continue
            # page.evaluate(script, *args) — a non-literal script is skipped.
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            script = node.args[0].value
            if not isinstance(script, str):
                continue
            found.append((str(path.relative_to(SRC.parent.parent)), node.lineno, script))
    return found


def _node_available() -> bool:
    return shutil.which("node") is not None


def _check_with_node(script: str) -> tuple[bool, str]:
    """Parse *script* as a JS expression with Node."""
    node = shutil.which("node")
    assert node, "node disappeared between the availability check and use"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        # Wrapping in parentheses makes a bare arrow function (or object literal)
        # a valid expression, which is how Playwright consumes it.
        fh.write(f"const __evaluate = (\n{script}\n);\n")
        path = fh.name
    try:
        # Arguments are fixed (absolute node, --check, a temp file we wrote), so
        # there is no untrusted input here.
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [node, "--check", path],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0:
            return True, ""
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, detail.splitlines()[0] if detail else "node --check failed"
    finally:
        Path(path).unlink(missing_ok=True)


def _strip_literals(script: str) -> str:
    """Remove strings, template literals and comments.

    Approximate by design: telling a ``//`` comment apart from the ``//`` inside a
    regex literal (``/^https?:\\/\\//``) needs a real lexer. This is only the
    fallback for environments without Node — where Node exists, its parser is the
    authority.
    """
    out: list[str] = []
    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            i = script.find("\n", i)
            i = n if i == -1 else i
            continue
        if ch == "/" and nxt == "*":
            end = script.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n:
                if script[i] == "\\":
                    i += 2
                    continue
                if script[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _balanced(script: str) -> tuple[bool, str]:
    stripped = _strip_literals(script)
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for ch in stripped:
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs and (not stack or stack.pop() != pairs[ch]):
            return False, f"unbalanced {ch!r}"
    if stack:
        return False, f"unclosed {stack[-1]!r} (depth {len(stack)})"
    return True, ""


@pytest.mark.parametrize(
    ("rel_path", "line", "script"),
    _scripts(),
    ids=lambda value: value if isinstance(value, str) and "/" in value else "",
)
def test_inline_evaluate_script_is_valid(rel_path, line, script):
    assert script.strip(), f"{rel_path}:{line} has an empty evaluate script"

    if _node_available():
        # Authoritative: a real JavaScript parse.
        ok, why = _check_with_node(script)
        assert ok, f"{rel_path}:{line}: JavaScript syntax error: {why}"
    else:
        # Fallback only. Structural problems (a stray brace) are by far the most
        # common defect and this catches them without any extra dependency.
        ok, why = _balanced(script)
        assert ok, f"{rel_path}:{line}: {why} (approximate check — node not installed)"

def test_no_script_contains_a_raw_backspace():
    """``\\b`` written in a non-raw Python string arrives as U+0008."""
    offenders = [
        f"{path}:{line}"
        for path, line, script in _scripts()
        if "\x08" in script
    ]
    assert not offenders, (
        "a \\b escape leaked a backspace character into JavaScript source "
        "(use a raw string or double the backslash): " + ", ".join(offenders)
    )


def test_scripts_are_discoverable():
    """Guard: the parametrisation above must actually find scripts.

    A refactor that changed how scripts are declared would silently turn these
    tests into no-ops.
    """
    scripts = _scripts()
    assert len(scripts) >= 20, f"only found {len(scripts)} inline scripts"
