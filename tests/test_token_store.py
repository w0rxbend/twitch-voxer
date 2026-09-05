"""Credential writes must preserve privacy and survive failed publication."""

import json
import os
import stat

import pytest

from voxer import token_store
from voxer.token_store import TokenFileBusyError, TokenFileLock, write_json_atomic


def test_private_atomic_write_creates_parent_and_replaces_old_content(tmp_path):
    target = tmp_path / "nested" / "tokens.json"
    write_json_atomic(target, {"token": "first"})
    write_json_atomic(target, {"token": "second"})
    assert json.loads(target.read_text()) == {"token": "second"}
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(target.parent.iterdir()) == [target]


def test_failed_replace_preserves_previous_tokens_and_removes_temp(
    tmp_path, monkeypatch
):
    target = tmp_path / "tokens.json"
    target.write_text('{"token":"previous"}')

    def fail_replace(*_args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(token_store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        write_json_atomic(target, {"token": "new"})
    assert json.loads(target.read_text()) == {"token": "previous"}
    assert list(tmp_path.iterdir()) == [target]


def test_token_file_has_one_refresh_owner(tmp_path):
    target = tmp_path / "tokens.json"
    with TokenFileLock(target):
        with pytest.raises(TokenFileBusyError, match="Stop the bot"):
            with TokenFileLock(target):
                pytest.fail("second owner acquired the same token file")
    with TokenFileLock(target):
        pass
