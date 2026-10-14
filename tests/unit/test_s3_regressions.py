"""Phase 1 regression tests for the S3/MinIO upload path (offline, B10).

* **B10** — ``upload_to_s3`` used a *fixed* ``mc`` alias name (concurrent
  uploads raced on it) and passed the secret key on the command line (visible
  to any local user via ``ps``). It must now use a per-call random alias and
  pass credentials via the ``MC_HOST_<alias>`` environment variable only.
"""

from __future__ import annotations

import asyncio

import pytest

from google_search_mcp.tools import web_utils

_SECRET = "S3CRET-please-do-not-leak"
_ACCESS = "AKIAEXAMPLE"


class _Result:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


@pytest.fixture()
def capture_subprocess(monkeypatch):
    calls: list[tuple[list[str], dict | None]] = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs.get("env")))
        return _Result(returncode=0)

    monkeypatch.setattr(web_utils.subprocess, "run", fake_run)
    return calls


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
    first_aliases = {
        k for _c, env in capture_subprocess if env for k in env if k.startswith("MC_HOST_")
    }
    asyncio.run(
        web_utils.upload_to_s3(
            str(tmp_path / "b.txt"), "bkt", key="b", access_key=_ACCESS, secret_key=_SECRET,
        )
    )
    all_aliases = {
        k for _c, env in capture_subprocess if env for k in env if k.startswith("MC_HOST_")
    }
    assert len(all_aliases) >= 2, "each call must use its own random alias (B10)"
    assert first_aliases <= all_aliases
