"""Filesystem guard: confine path-taking tools to allowed roots.

``read_document``, ``list_images``, ``ocr_image``, ``google_lens``,
``upload_to_s3``, ``convert_media`` and ``transcribe_local`` all accept
caller-supplied paths. Without a guard, a prompt-injected model could read
arbitrary files (e.g. ``~/.ssh/id_rsa``).

The guard resolves symlinks (so a symlink cannot escape) and requires the
resolved path to live inside one of ``NOAPI_MCP_ALLOWED_ROOTS`` (default: the
user's home directory, per D5).

Verified APIs: stdlib only (``pathlib``, ``os``).
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import config
from ..errors import UserInputError


def allowed_roots() -> tuple[Path, ...]:
    """Resolved, de-duplicated allowed roots from config."""
    roots: list[Path] = []
    for raw in config.ALLOWED_ROOTS:
        try:
            roots.append(Path(os.path.expanduser(raw)).resolve())
        except (OSError, RuntimeError):
            continue
    return tuple(roots)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def check_path(path_str: str, *, must_exist: bool = False) -> str | None:
    """Return a rejection reason for *path_str*, or ``None`` if allowed."""
    if not path_str or not path_str.strip():
        return "empty path"
    try:
        raw = Path(os.path.expanduser(path_str.strip()))
        resolved = raw.resolve()
    except (OSError, RuntimeError) as exc:
        return f"unresolvable path ({exc})"

    roots = allowed_roots()
    if not roots:
        return None  # nothing configured -> no restriction
    if not any(_within(resolved, root) for root in roots):
        shown = ", ".join(str(r) for r in roots)
        return f"path is outside the allowed roots ({shown})"
    if must_exist and not resolved.exists():
        return "path does not exist"
    return None


def assert_path_allowed(path_str: str, *, must_exist: bool = False) -> Path:
    """Return the resolved path, raising :class:`UserInputError` if disallowed."""
    reason = check_path(path_str, must_exist=must_exist)
    if reason:
        raise UserInputError(f"Refusing to access {path_str!r}: {reason}.")
    return Path(os.path.expanduser(path_str.strip())).resolve()


def is_path_allowed(path_str: str, *, must_exist: bool = False) -> bool:
    """Boolean form of :func:`check_path`."""
    return check_path(path_str, must_exist=must_exist) is None
