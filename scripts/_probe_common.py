"""Shared helpers for the throwaway SERP probe scripts.

The probes dump captured HTML so extractors can be iterated on offline. Writing
that file from inside an ``async def`` trips ruff's ASYNC230/S108 rules, so the
I/O lives here as a plain synchronous helper using *tempfile*'s directory.
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def capture_dir() -> Path:
    """Directory the probes write captured SERPs to."""
    return Path(tempfile.gettempdir())


def save_text(name: str, text: str) -> Path:
    """Write *text* to ``<tmp>/<name>`` and return the path."""
    path = capture_dir() / name
    path.write_text(text, encoding="utf-8")
    return path


def save_html(name: str, html: str) -> Path:
    """Write a captured page and report its size on stderr-style stdout."""
    path = save_text(name, html)
    print(f"SAVED : {path} ({len(html)} bytes)")
    return path
