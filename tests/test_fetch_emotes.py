"""Network bounds, refresh ownership and cache publication regression tests."""

import json
from unittest.mock import Mock

import pytest
import requests

from voxer import fetch_emotes
from voxer.token_store import TokenFileBusyError, TokenFileLock


def response(data, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    result._content_consumed = True
    return result


@pytest.mark.parametrize("kind", ["app", "refresh"])
def test_token_secrets_are_sent_in_body_with_a_timeout(kind):
    session = Mock(spec=requests.Session)
    session.post.return_value = response(
        {"access_token": "access", "refresh_token": "refresh"}
    )
    if kind == "app":
        assert fetch_emotes.get_app_token(session) == "access"
    else:
        assert fetch_emotes._refresh_grant(session, "secret-refresh") is not None
    args, kwargs = session.post.call_args
    assert args == ("https://id.twitch.tv/oauth2/token",)
    assert "params" not in kwargs
    assert "client_secret" in kwargs["data"]
    assert kwargs["timeout"] == fetch_emotes.HTTP_TIMEOUT
    assert kwargs["allow_redirects"] is False


def test_token_validation_rejects_another_application(monkeypatch):
    session = Mock(spec=requests.Session)
    session.get.return_value = response(
        {"client_id": "other", "user_id": "123", "scopes": fetch_emotes.SCOPES}
    )
    monkeypatch.setattr(fetch_emotes, "CLIENT_ID", "configured")
    assert fetch_emotes.validate_token_scopes(session, "access") == set()


def test_transient_refresh_errors_are_not_treated_as_revocation():
    session = Mock(spec=requests.Session)
    session.post.return_value = response({}, status=503)
    with pytest.raises(requests.HTTPError):
        fetch_emotes._refresh_grant(session, "refresh")


def test_validation_binds_the_token_to_its_stored_user_id(monkeypatch):
    session = Mock(spec=requests.Session)
    session.get.return_value = response(
        {"client_id": "application", "user_id": "other", "scopes": fetch_emotes.SCOPES}
    )
    monkeypatch.setattr(fetch_emotes, "CLIENT_ID", "application")
    assert (
        fetch_emotes.validate_token_scopes(session, "access", expected_user_id="123")
        == set()
    )


def test_rate_limited_reads_honor_retry_after_with_a_finite_budget(monkeypatch):
    session = Mock(spec=requests.Session)
    limited = response({}, status=429)
    limited.headers["Retry-After"] = "2"
    session.get.side_effect = [limited, response({"data": []})]
    sleep = Mock()
    monkeypatch.setattr(fetch_emotes.time, "sleep", sleep)
    assert fetch_emotes.fetch_global_emotes(session, "access") == []
    sleep.assert_called_once_with(2)
    assert session.get.call_count == 2


def test_pagination_rejects_repeated_cursors_and_bounds_requests():
    session = Mock(spec=requests.Session)
    session.get.return_value = response(
        {"data": [{"id": "1"}], "pagination": {"cursor": "same"}}
    )
    with pytest.raises(RuntimeError, match="repeated pagination cursor"):
        fetch_emotes.paginate(session, "https://api.twitch.tv/helix/users", "token", {})
    assert session.get.call_count == 2
    assert session.get.call_args.kwargs["timeout"] == fetch_emotes.HTTP_TIMEOUT


def test_live_tokens_remain_readable_while_bot_owns_refresh(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"123": {"token": "access", "refresh": "refresh"}}))
    monkeypatch.setattr(fetch_emotes, "TOKEN_FILE", path)
    monkeypatch.setattr(
        fetch_emotes,
        "validate_token_scopes",
        lambda *_args, **_kwargs: set(fetch_emotes.SCOPES),
    )
    with TokenFileLock(path):
        assert fetch_emotes.refresh_from_token_file(Mock()) == "access"


def test_downloader_does_not_rotate_tokens_owned_by_bot(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"123": {"token": "access", "refresh": "refresh"}}))
    monkeypatch.setattr(fetch_emotes, "TOKEN_FILE", path)
    monkeypatch.setattr(
        fetch_emotes, "validate_token_scopes", lambda *_args, **_kwargs: set()
    )
    refresh = Mock()
    monkeypatch.setattr(fetch_emotes, "_refresh_grant", refresh)
    with TokenFileLock(path):
        with pytest.raises(TokenFileBusyError, match="Stop the bot"):
            fetch_emotes.refresh_from_token_file(Mock())
    refresh.assert_not_called()


def test_refreshed_token_must_have_required_scopes(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"123": {"token": "access", "refresh": "refresh"}}))
    monkeypatch.setattr(fetch_emotes, "TOKEN_FILE", path)
    monkeypatch.setattr(
        fetch_emotes, "validate_token_scopes", lambda *_args, **_kwargs: set()
    )
    monkeypatch.setattr(
        fetch_emotes,
        "_refresh_grant",
        lambda *_args: {"access_token": "new", "refresh_token": "rotated"},
    )
    assert fetch_emotes.refresh_from_token_file(Mock()) is None
    assert json.loads(path.read_text())["123"]["refresh"] == "rotated"


def test_failed_token_write_never_prints_refresh_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fetch_emotes, "TOKEN_FILE", tmp_path / "tokens.json")

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(fetch_emotes, "write_json_atomic", fail)
    with pytest.raises(OSError, match="disk full"):
        fetch_emotes._write_tokens_atomically({}, "do-not-log-this-secret")
    assert "do-not-log-this-secret" not in capsys.readouterr().out


def test_cache_is_published_as_complete_json_without_stale_entries(
    tmp_path, monkeypatch
):
    target = tmp_path / "emotes.json"
    target.write_text('{"stale":{}}')
    monkeypatch.setattr(fetch_emotes, "OUTPUT_FILE", target)
    monkeypatch.setattr(fetch_emotes.config, "validate_credentials", lambda: None)
    monkeypatch.setattr(fetch_emotes, "get_app_token", lambda *_: "app")
    monkeypatch.setattr(fetch_emotes, "get_user_token", lambda *_: "user")
    monkeypatch.setattr(
        fetch_emotes, "get_current_user", lambda *_: {"id": "123", "login": "bot"}
    )
    monkeypatch.setattr(fetch_emotes, "fetch_followed_ids", lambda *_: [])
    monkeypatch.setattr(fetch_emotes, "fetch_follower_ids", lambda *_: [])
    monkeypatch.setattr(fetch_emotes, "fetch_channel_emotes", lambda *_: [])
    images = {
        "url_1x": "https://cdn/1",
        "url_2x": "https://cdn/2",
        "url_4x": "https://cdn/4",
    }
    monkeypatch.setattr(
        fetch_emotes,
        "fetch_global_emotes",
        lambda *_: [{"name": "Kappa", "images": images}],
    )
    fetch_emotes.main([])
    assert json.loads(target.read_text()) == {"Kappa": images}
