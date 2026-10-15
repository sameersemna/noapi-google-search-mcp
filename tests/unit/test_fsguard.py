"""Phase 7 tests for the filesystem guard (fsguard)."""

from __future__ import annotations

import os

import pytest

from google_search_mcp.core import fsguard
from google_search_mcp.errors import UserInputError


@pytest.fixture()
def rooted(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(fsguard.config, "ALLOWED_ROOTS", (str(root),))
    return root


def test_path_inside_root_allowed(rooted):
    inside = rooted / "notes.txt"
    inside.write_text("hi")
    assert fsguard.is_path_allowed(str(inside), must_exist=True) is True


def test_path_outside_root_rejected(rooted, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    assert fsguard.is_path_allowed(str(outside)) is False


def test_traversal_rejected(rooted):
    evil = rooted / ".." / "secret.txt"
    assert fsguard.is_path_allowed(str(evil)) is False


def test_symlink_escape_rejected(rooted, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    link = rooted / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")
    assert fsguard.is_path_allowed(str(link)) is False


def test_missing_file_reported_when_required(rooted):
    assert fsguard.is_path_allowed(str(rooted / "nope.txt"), must_exist=True) is False
    # ...but allowed when existence is not required
    assert fsguard.is_path_allowed(str(rooted / "nope.txt"), must_exist=False) is True


def test_empty_path_rejected(rooted):
    assert fsguard.is_path_allowed("") is False


def test_assert_raises_user_input_error(rooted, tmp_path):
    with pytest.raises(UserInputError) as ei:
        fsguard.assert_path_allowed(str(tmp_path / "elsewhere.txt"))
    assert "Refusing" in str(ei.value)


def test_no_roots_configured_means_unrestricted(monkeypatch):
    monkeypatch.setattr(fsguard.config, "ALLOWED_ROOTS", ())
    assert fsguard.is_path_allowed("/etc/hostname") is True


def test_default_root_is_home():
    # D5: default ALLOWED_ROOTS is the user's home.
    from google_search_mcp import config

    assert os.path.expanduser("~") in config.ALLOWED_ROOTS[0] or config.ALLOWED_ROOTS[0] == os.path.expanduser("~")
