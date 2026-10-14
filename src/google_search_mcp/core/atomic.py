"""Atomic file writes.

A crash mid-write used to leave a truncated file (cookie jar, transcript cache,
generated output) that poisoned every later call. ``atomic_write_*`` writes to a
temporary file in the same directory and ``os.replace``-s it into place, which
is atomic on POSIX. Secrets get ``0600`` by default.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: str, data: bytes, *, mode: int = 0o600) -> None:
    """Write *data* to *path* atomically, then chmod to *mode*."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=target.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: str, text: str, *, mode: int = 0o600) -> None:
    """Write *text* (UTF-8) to *path* atomically."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: str, data: Any, *, mode: int = 0o600) -> None:
    """Serialise *data* as JSON and write it atomically."""
    atomic_write_text(path, json.dumps(data, indent=2, default=str), mode=mode)
