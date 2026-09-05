"""Unit tests for the token-file handling shared by the bot and fetch_emotes.

These tests cover the parts of the auth flow that run without a network:
reading the twitchio-format token file, preferring a still-valid access token
over a refresh (which would rotate the pair), writing rotated pairs back
atomically, and tolerating missing or malformed files.  They also pin the one
other thing fetch_emotes decides at import time without a network: where its
emote cache is written.
"""

import json
from pathlib import Path

import pytest
import requests

from voxer import config, fetch_emotes
from voxer.config import parse_redirect_url, validate_redirect_url


class TestOutputFile:
    def test_output_file_follows_configured_emote_db_path(self) -> None:
        """The writer must target the same file the bot reads.

        voxer/app.py builds its EmoteStore from config.EMOTES_DB_PATH, so if
        this script wrote somewhere else, setting VOXER_EMOTES_DB_PATH would
        split the two apart.  EmoteStore starts empty (with only a warning)
        when its file is missing, so that split would never raise — emotes
        would just stop appearing in the overlay.
        """
        assert fetch_emotes.OUTPUT_FILE == Path(config.EMOTES_DB_PATH)


class TestParseRedirectUrl:
    def test_default_localhost_url(self) -> None:
        assert parse_redirect_url("http://localhost:4343/oauth/callback") == (
            None,
            "oauth/callback",
        )

    def test_loopback_ip(self) -> None:
        assert parse_redirect_url("http://127.0.0.1:4343/oauth/callback") == (
            None,
            "oauth/callback",
        )

    def test_public_domain(self) -> None:
        assert parse_redirect_url("https://bot.example.org/oauth/callback") == (
            "bot.example.org",
            "oauth/callback",
        )

    def test_custom_path(self) -> None:
        assert parse_redirect_url("https://bot.example.org/auth/cb") == (
            "bot.example.org",
            "auth/cb",
        )

    def test_missing_path_falls_back_to_default(self) -> None:
        assert parse_redirect_url("https://bot.example.org") == (
            "bot.example.org",
            "oauth/callback",
        )

    def test_empty_string_uses_defaults(self) -> None:
        assert parse_redirect_url("") == (None, "oauth/callback")


class TestValidateRedirectUrl:
    def test_default_localhost_http_accepted(self) -> None:
        validate_redirect_url("http://localhost:4343/oauth/callback")

    def test_loopback_http_accepted(self) -> None:
        validate_redirect_url("http://127.0.0.1:4343/oauth/callback")

    def test_public_https_accepted(self) -> None:
        validate_redirect_url("https://bot.example.org/oauth/callback")

    def test_public_http_rejected(self) -> None:
        # Twitch only allows plain http for localhost redirect URLs
        with pytest.raises(RuntimeError, match="https"):
            validate_redirect_url("http://bot.example.org/oauth/callback")

    def test_missing_scheme_rejected(self) -> None:
        with pytest.raises(RuntimeError, match="full http"):
            validate_redirect_url("bot.example.org/oauth/callback")

    def test_garbage_rejected(self) -> None:
        with pytest.raises(RuntimeError):
            validate_redirect_url("not a url")


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "tokens.json"
    monkeypatch.setattr(fetch_emotes, "TOKEN_FILE", path)
    return path


def write_tokens(path: Path, entries: dict) -> None:
    path.write_text(json.dumps(entries))


def entry(token: str, refresh: str) -> dict:
    return {"user_id": "123", "token": token, "refresh": refresh, "last_validated": "x"}


class TestRefreshFromTokenFile:
    def test_missing_file_returns_none(self, token_file: Path) -> None:
        with requests.Session() as session:
            assert fetch_emotes.refresh_from_token_file(session) is None

    def test_malformed_file_returns_none(self, token_file: Path) -> None:
        token_file.write_text("{not json")
        with requests.Session() as session:
            assert fetch_emotes.refresh_from_token_file(session) is None

    def test_valid_stored_token_used_without_rotation(
        self, token_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_tokens(token_file, {"123": entry("live-token", "refresh-a")})
        # Fully scoped: the stored token can do everything the script needs
        monkeypatch.setattr(
            fetch_emotes,
            "validate_token_scopes",
            lambda s, t, **_: set(fetch_emotes.SCOPES),
        )

        def no_refresh(session: object, refresh: str) -> None:
            raise AssertionError("must not refresh while the stored token is valid")

        monkeypatch.setattr(fetch_emotes, "_refresh_grant", no_refresh)
        with requests.Session() as session:
            assert fetch_emotes.refresh_from_token_file(session) == "live-token"
        # The file is untouched — no rotation happened
        assert json.loads(token_file.read_text())["123"]["refresh"] == "refresh-a"

    def test_under_scoped_stored_token_is_refreshed_not_returned(
        self, token_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token that validates but lacks a scope must not be accepted.

        It is alive, so a plain "is it valid?" check would return it, and the
        caller would then reject it for the missing scope and fall through to
        the browser flow — even though the refresh token on disk would have
        produced a fully scoped token.
        """
        write_tokens(token_file, {"123": entry("partial-token", "refresh-a")})
        monkeypatch.setattr(
            fetch_emotes,
            "validate_token_scopes",
            lambda s, t, **_: (
                set(fetch_emotes.SCOPES)
                if t == "new-token"
                else {fetch_emotes.SCOPES[0]}
            ),
        )
        monkeypatch.setattr(
            fetch_emotes,
            "_refresh_grant",
            lambda s, r: {"access_token": "new-token", "refresh_token": "refresh-b"},
        )
        with requests.Session() as session:
            assert fetch_emotes.refresh_from_token_file(session) == "new-token"

    def test_dead_token_refreshed_and_rotated_pair_written_back(
        self, token_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_tokens(token_file, {"123": entry("dead-token", "refresh-a")})
        monkeypatch.setattr(
            fetch_emotes,
            "validate_token_scopes",
            lambda s, t, **_: set(fetch_emotes.SCOPES) if t == "new-token" else set(),
        )
        monkeypatch.setattr(
            fetch_emotes,
            "_refresh_grant",
            lambda s, r: {"access_token": "new-token", "refresh_token": "refresh-b"},
        )
        with requests.Session() as session:
            assert fetch_emotes.refresh_from_token_file(session) == "new-token"
        # The rotated pair replaced the stale one on disk
        stored = json.loads(token_file.read_text())["123"]
        assert stored["token"] == "new-token"
        assert stored["refresh"] == "refresh-b"

    def test_dead_refresh_token_returns_none(
        self, token_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_tokens(token_file, {"123": entry("dead-token", "dead-refresh")})
        monkeypatch.setattr(
            fetch_emotes, "validate_token_scopes", lambda s, t, **_: set()
        )
        monkeypatch.setattr(fetch_emotes, "_refresh_grant", lambda s, r: None)
        with requests.Session() as session:
            assert fetch_emotes.refresh_from_token_file(session) is None

    def test_write_is_atomic_no_tmp_left_behind(
        self, token_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_tokens(token_file, {"123": entry("dead-token", "refresh-a")})
        monkeypatch.setattr(
            fetch_emotes,
            "validate_token_scopes",
            lambda s, t, **_: set(fetch_emotes.SCOPES) if t == "t" else set(),
        )
        monkeypatch.setattr(
            fetch_emotes,
            "_refresh_grant",
            lambda s, r: {"access_token": "t", "refresh_token": "r"},
        )
        with requests.Session() as session:
            fetch_emotes.refresh_from_token_file(session)
        assert not token_file.with_suffix(".tmp").exists()
        # The final file parses — a torn write would have corrupted it
        json.loads(token_file.read_text())
