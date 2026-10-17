"""Phase 11 guard: every documented ``NOAPI_MCP_*`` variable must really exist.

The hardening plan's documentation gate says: "grep every env var name in docs
and confirm it exists in code". This test automates that so README/CHANGELOG
(and other docs) cannot advertise configuration that the server does not read.

A variable is considered real if the literal string appears anywhere under
``src/google_search_mcp`` (most are read through the ``config.env*`` helpers
with a literal name, but a few are read directly at their use site).
"""

from __future__ import annotations

import pathlib
import re

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "google_search_mcp"
_DOC_FILES = [
    _REPO / "README.md",
    _REPO / "CHANGELOG.md",
    _REPO / "SECURITY.md",
]
_DOC_GLOBS = ["docs/**/*.md"]

_ENV_RE = re.compile(r"NOAPI_MCP_[A-Z0-9_]+")


def _documented_env_vars() -> dict[str, set[str]]:
    """Map each documented NOAPI_MCP_* name to the doc files mentioning it."""
    found: dict[str, set[str]] = {}
    files = [p for p in _DOC_FILES if p.is_file()]
    for pattern in _DOC_GLOBS:
        files.extend(_REPO.glob(pattern))
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in _ENV_RE.findall(text):
            found.setdefault(name, set()).add(path.name)
    return found


def _source_text() -> str:
    parts = []
    for path in _SRC.rglob("*.py"):
        parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def _is_wired(name: str, source: str) -> bool:
    """Whether *name* is something the server actually reads.

    Two forms exist:

    * direct reads: ``os.environ.get("NOAPI_MCP_FOO", ...)`` — the literal
      ``NOAPI_MCP_FOO`` appears in the source; or
    * helper reads: ``config.env("FOO")`` / ``env_int("FOO")`` … — the
      ``NOAPI_MCP_`` prefix is added by the helper at runtime, so only the bare
      name appears in the source.
    """
    if name in source:
        return True
    prefix = "NOAPI_MCP_"
    if not name.startswith(prefix):
        return False
    bare = name[len(prefix):]
    return re.search(
        r'env(?:_flag|_int|_float)?\(\s*"' + re.escape(bare) + r'"', source
    ) is not None


def test_docs_mention_at_least_some_config():
    documented = _documented_env_vars()
    assert documented, "expected at least one NOAPI_MCP_* variable in the docs"


def test_every_documented_env_var_exists_in_code():
    source = _source_text()
    documented = _documented_env_vars()
    missing = {
        name: sorted(where)
        for name, where in documented.items()
        if not _is_wired(name, source)
    }
    assert not missing, (
        "documented but never read by the server:\n  "
        + "\n  ".join(f"{name} (in {', '.join(where)})" for name, where in sorted(missing.items()))
    )


@pytest.mark.parametrize(
    "name",
    [
        "NOAPI_MCP_MAX_BROWSERS",
        "NOAPI_MCP_BROWSER_POOL",
        "NOAPI_MCP_ENABLE_CACHE",
        "NOAPI_MCP_CACHE_TTL_SECONDS",
        "NOAPI_MCP_ALLOW_PRIVATE_URLS",
        "NOAPI_MCP_ALLOWED_ROOTS",
        "NOAPI_MCP_GOOGLE_RPS",
        "NOAPI_MCP_GOOGLE_BURST",
        "NOAPI_MCP_CIRCUIT_COOLDOWN_SECONDS",
        "NOAPI_MCP_IDEMPOTENCY_WINDOW_SECONDS",
        "NOAPI_MCP_FEEDS_CONCURRENCY",
        "NOAPI_MCP_MAX_DOCUMENT_CHARS",
        "NOAPI_MCP_MAX_EMAIL_BODY_CHARS",
        "NOAPI_MCP_TRANSPORT",
    ],
)
def test_key_settings_are_wired(name):
    """A spot-check that the headline settings are actually read somewhere."""
    assert _is_wired(name, _source_text()), f"{name} is not read by the server"
