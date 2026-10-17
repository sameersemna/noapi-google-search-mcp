"""Regression tests for the S3/MinIO upload path (offline).

* **B10** — ``upload_to_s3`` used a *fixed* ``mc`` alias name (concurrent
  uploads raced on it) and passed the secret key on the command line (visible
  to any local user via ``ps``). It must now use a per-call random alias and
  pass credentials via the ``MC_HOST_<alias>`` environment variable only.
* **Phase 5** — idempotency (a retried identical upload must not re-upload),
  an ``overwrite`` flag, and the filesystem path guard.
"""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp.core import idempotency
from google_search_mcp.errors import UserInputError
from google_search_mcp.tools import web_utils

_SECRET = "S3CRET-please-do-not-leak"
_ACCESS = "AKIAEXAMPLE"


class _Result:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


@pytest.fixture(autouse=True)
def idem_store(tmp_path, monkeypatch):
    """Keep every test hermetic: never touch the real idempotency DB."""
    store = idempotency.IdempotencyStore(
        str(tmp_path / "idem.db"), window_seconds=3600
    )
    monkeypatch.setattr(idempotency, "store", store)
    return store


@pytest.fixture()
def capture_subprocess(monkeypatch):
    calls: list[tuple[list[str], dict | None]] = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs.get("env")))
        return _Result(returncode=0)

    monkeypatch.setattr(web_utils.subprocess, "run", fake_run)
    return calls


def _env_keys(calls, prefix="MC_HOST_"):
    return {
        k for _c, env in calls if env for k in env if k.startswith(prefix)
    }


def test_mc_upload_never_puts_secret_in_argv(tmp_path, capture_subprocess):
    f = tmp_path / "report.txt"
    f.write_text("hello")
    out = asyncio.run(
        web_utils.upload_to_s3(
            str(f), "my-bucket", access_key=_ACCESS, secret_key=_SECRET,
        )
    )
    assert "s3://my-bucket/report.txt" in out

    for cmd, _env in capture_subprocess:
        joined = " ".join(cmd)
        assert _SECRET not in joined, f"secret leaked into argv: {cmd}"
        assert _ACCESS not in joined, f"access key leaked into argv: {cmd}"

    # ...but it must be provided to mc via a per-call MC_HOST_ alias env var.
    mc_host = [
        env for _cmd, env in capture_subprocess
        if env and any(k.startswith("MC_HOST_") for k in env)
    ]
    assert mc_host, "credentials must be passed via MC_HOST_<alias> env var"
    for env in mc_host:
        key = next(k for k in env if k.startswith("MC_HOST_"))
        assert env[key].startswith(("http://", "https://"))
        assert _SECRET in env[key] and _ACCESS in env[key]


def test_mc_alias_is_random_per_call(tmp_path, capture_subprocess):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")

    asyncio.run(
        web_utils.upload_to_s3(
            str(tmp_path / "a.txt"), "bkt", key="a", access_key=_ACCESS, secret_key=_SECRET,
        )
    )
    first_aliases = _env_keys(capture_subprocess)
    asyncio.run(
        web_utils.upload_to_s3(
            str(tmp_path / "b.txt"), "bkt", key="b", access_key=_ACCESS, secret_key=_SECRET,
        )
    )
    all_aliases = _env_keys(capture_subprocess)
    assert len(all_aliases) >= 2, "each call must use its own random alias (B10)"
    assert first_aliases <= all_aliases


def test_upload_is_idempotent_for_identical_file(tmp_path, capture_subprocess):
    f = tmp_path / "same.txt"
    f.write_text("identical")

    asyncio.run(
        web_utils.upload_to_s3(
            str(f), "bkt", key="k", access_key=_ACCESS, secret_key=_SECRET,
        )
    )
    uploads_before = len([c for c, _e in capture_subprocess if "cp" in c])
    second = asyncio.run(
        web_utils.upload_to_s3(
            str(f), "bkt", key="k", access_key=_ACCESS, secret_key=_SECRET,
        )
    )
    uploads_after = len([c for c, _e in capture_subprocess if "cp" in c])
    assert uploads_before == 1
    assert uploads_after == 1, "identical upload must not be repeated (Phase 5)"
    assert idempotency.CACHED_NOTE in second


def test_upload_changed_content_uploads_again(tmp_path, capture_subprocess):
    f = tmp_path / "changes.txt"
    f.write_text("v1")
    asyncio.run(
        web_utils.upload_to_s3(
            str(f), "bkt", key="k", access_key=_ACCESS, secret_key=_SECRET,
        )
    )
    f.write_text("v2-different")
    asyncio.run(
        web_utils.upload_to_s3(
            str(f), "bkt", key="k", access_key=_ACCESS, secret_key=_SECRET,
        )
    )
    assert len([c for c, _e in capture_subprocess if "cp" in c]) == 2


def test_overwrite_false_refuses_existing_object(tmp_path, monkeypatch):
    f = tmp_path / "x.txt"
    f.write_text("data")

    def fake_run(cmd, **kwargs):
        # `mc stat <alias>/bucket/key` -> success means the object exists.
        if len(cmd) >= 2 and cmd[1] == "stat":
            return _Result(returncode=0)
        return _Result(returncode=0)

    monkeypatch.setattr(web_utils.subprocess, "run", fake_run)
    out = asyncio.run(
        web_utils.upload_to_s3(
            str(f), "bkt", key="k", access_key=_ACCESS, secret_key=_SECRET,
            overwrite=False,
        )
    )
    assert "already exists" in out
    assert "overwrite=True" in out


def test_upload_rejects_path_outside_allowed_roots():
    with pytest.raises(UserInputError):
        asyncio.run(
            web_utils.upload_to_s3(
                "/etc/hostname", "bkt", access_key=_ACCESS, secret_key=_SECRET,
            )
        )

