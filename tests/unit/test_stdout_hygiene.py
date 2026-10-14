"""Phase 3 test: no ``print()`` to stdout outside CLI entry points.

On the stdio transport, **stdout is the MCP protocol channel** — a stray
``print()`` corrupts the protocol stream. This test enforces that ``print()``
only appears inside a function named ``main`` (a module's CLI entry point) or
inside an ``if __name__ == "__main__"`` guard.
"""

from __future__ import annotations

import ast
import pathlib

_PKG_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "google_search_mcp"


def _main_guard_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            ):
                for n in ast.walk(node):
                    if hasattr(n, "lineno"):
                        lines.add(n.lineno)
    return lines


def _enclosing_func_names(tree: ast.AST) -> dict[int, str]:
    """Map each line number to the name of its innermost enclosing function."""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(node):
                if hasattr(n, "lineno"):
                    owner[n.lineno] = node.name
    return owner


def test_no_stdout_print_outside_cli_main():
    violations: list[str] = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        guard_lines = _main_guard_lines(tree)
        owner = _enclosing_func_names(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                continue
            if node.lineno in guard_lines:
                continue
            if owner.get(node.lineno) == "main":
                continue
            # print(..., file=sys.stderr) is fine: stderr is the log channel.
            if any(
                kw.arg == "file" and "stderr" in ast.dump(kw.value)
                for kw in node.keywords
            ):
                continue
            violations.append(f"{path.relative_to(_PKG_ROOT.parent.parent)}:{node.lineno}")
    assert not violations, (
        "print() to stdout outside a CLI 'main' entry point corrupts the stdio "
        "transport:\n  " + "\n  ".join(violations)
    )
