"""Unit tests for the environment-variable parse helpers in voxer.config.

`config.py` turns environment variables into module-level constants while it
is being imported, which means a bad value crashes the process before any
logging exists.  The two helpers tested here exist so that such a crash names
the variable that is wrong, and so that values which parse as numbers but
cannot work (a queue size of 0, a port above 65535) are rejected at startup
instead of quietly changing what the program does.

The helpers read `os.environ` when they are called, not when the module is
imported, so these tests can drive them directly with `monkeypatch.setenv`
without reimporting anything.

The startup validators are tested here too.  They read module *constants*,
which were built once during import, so those tests replace the constants with
`monkeypatch.setattr` rather than the environment — that is also what keeps
them giving the same answer on a machine with a filled-in .env and in CI,
which has none.
"""

import os

import pytest

from voxer import config
from voxer.config import _env_csv, _env_int


class TestEnvInt:
    def test_uses_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VOXER_TEST_INT", raising=False)
        assert _env_int("VOXER_TEST_INT", "8080") == 8080

    def test_reads_the_environment_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXER_TEST_INT", "9000")
        assert _env_int("VOXER_TEST_INT", "8080") == 9000

    @pytest.mark.parametrize("raw", ["eighty", "", "8080.0", "8 080", "0x10"])
    def test_rejects_non_numbers_naming_the_variable(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """The whole point of the helper: the error must be findable.

        Before this helper the same mistake produced `ValueError: invalid
        literal for int() with base 10: 'eighty'` raised from an import
        statement, which names neither the setting nor the file it came from.
        """
        monkeypatch.setenv("VOXER_TEST_INT", raw)
        with pytest.raises(RuntimeError) as excinfo:
            _env_int("VOXER_TEST_INT", "8080")
        message = str(excinfo.value)
        assert "VOXER_TEST_INT" in message
        assert repr(raw) in message

    def test_rejects_a_value_below_the_default_minimum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0 is the dangerous case: asyncio.Queue reads maxsize=0 as unbounded,
        so accepting it would silently delete the queue's backpressure."""
        monkeypatch.setenv("VOXER_TEST_INT", "0")
        with pytest.raises(RuntimeError, match="VOXER_TEST_INT must be at least 1"):
            _env_int("VOXER_TEST_INT", "20")

    def test_rejects_a_negative_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXER_TEST_INT", "-5")
        with pytest.raises(RuntimeError, match="must be at least 1"):
            _env_int("VOXER_TEST_INT", "20")

    def test_minimum_zero_accepts_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The scheduler's initial delay may legitimately be 0 (post at once)."""
        monkeypatch.setenv("VOXER_TEST_INT", "0")
        assert _env_int("VOXER_TEST_INT", "10", minimum=0) == 0

    def test_rejects_a_value_above_the_maximum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VOXER_TEST_INT", "70000")
        with pytest.raises(RuntimeError, match="must be at most 65535"):
            _env_int("VOXER_TEST_INT", "8080", maximum=65535)

    @pytest.mark.parametrize("raw", ["1", "65535"])
    def test_accepts_both_ends_of_the_port_range(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """The bounds are inclusive, so a real port never has to be nudged."""
        monkeypatch.setenv("VOXER_TEST_INT", raw)
        assert _env_int("VOXER_TEST_INT", "8080", maximum=65535) == int(raw)

    def test_a_bad_default_is_reported_the_same_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deprecated VOXER_SCHEDULER_INTERVAL alias is passed in as the
        *default*, so a bad value there must be caught too rather than sliding
        past the parse and reaching int() unchecked."""
        monkeypatch.delenv("VOXER_TEST_INT", raising=False)
        with pytest.raises(RuntimeError, match="must be a whole number"):
            _env_int("VOXER_TEST_INT", "ten minutes")


class TestEnvCsv:
    def test_uses_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VOXER_TEST_CSV", raising=False)
        assert _env_csv("VOXER_TEST_CSV", "a.mp3,b.mp3") == ["a.mp3", "b.mp3"]

    def test_splits_and_strips_each_item(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXER_TEST_CSV", " alice , bob ")
        assert _env_csv("VOXER_TEST_CSV", "") == ["alice", "bob"]

    @pytest.mark.parametrize("raw", ["", "   ", ",", " , , "])
    def test_blank_input_yields_no_items(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """An empty item would match nothing but still occupy a slot; callers
        treat an empty list as "this feature is off", so it must stay empty."""
        monkeypatch.setenv("VOXER_TEST_CSV", raw)
        assert _env_csv("VOXER_TEST_CSV", "fallback") == []

    def test_trailing_comma_does_not_add_an_empty_item(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VOXER_TEST_CSV", "alice,bob,")
        assert _env_csv("VOXER_TEST_CSV", "") == ["alice", "bob"]

    def test_case_is_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Lowercasing belongs to the one caller that wants it
        (NO_ANNOUNCE_USERS); file paths are case-sensitive on Linux."""
        monkeypatch.setenv("VOXER_TEST_CSV", "Emotes/Slack-Message.mp3")
        assert _env_csv("VOXER_TEST_CSV", "") == ["Emotes/Slack-Message.mp3"]


class TestConstantsAreParsed:
    """The constants really do go through the helpers.

    These read the values `import voxer.config` produced for whatever
    environment the test run has, so they pin the wiring rather than any one
    setting: a constant that went back to a bare `int(os.getenv(...))` or an
    inline split would stop satisfying them.
    """

    @pytest.mark.parametrize(
        "name", ["SERVER_PORT", "OAUTH_PORT", "MESSAGE_QUEUE_MAXSIZE"]
    )
    def test_bounded_integers_are_in_range(self, name: str) -> None:
        value = getattr(config, name)
        assert isinstance(value, int)
        assert 1 <= value <= 65535

    @pytest.mark.parametrize(
        "name", ["ANNOUNCE_WINDOW_SECS", "SCHEDULER_EMPTY_RETRY_DELAY"]
    )
    def test_delays_are_at_least_one_second(self, name: str) -> None:
        assert getattr(config, name) >= 1

    def test_initial_delay_may_be_zero_but_not_negative(self) -> None:
        assert config.SCHEDULER_INITIAL_DELAY >= 0

    def test_no_announce_users_are_lowercased_and_trimmed(self) -> None:
        """Comparison against a Twitch login is case-insensitive, which only
        works if the stored side is already lowercase."""
        for user in config.NO_ANNOUNCE_USERS:
            assert user == user.strip().lower()
            assert user != ""

    def test_emote_sound_paths_have_no_blank_or_padded_entries(self) -> None:
        for path in config.EMOTE_SOUND_PATHS:
            assert path == path.strip()
            assert path != ""

    def test_bot_username_has_no_built_in_default(self) -> None:
        """The login name must come from the environment or be empty.

        It used to default to one specific person's Twitch handle, so a clone
        of this repository that forgot the variable started successfully and
        ran as that stranger's account.  Comparing the constant against the
        raw environment pins that away: if the literal default came back, this
        fails on any machine where TWITCH_BOT_USERNAME is unset — which is
        every CI run, since CI has no .env file.
        """
        assert config.BOT_USERNAME == os.environ.get("TWITCH_BOT_USERNAME", "")


class TestValidateCredentials:
    """The two Twitch application credentials, checked as values not as names.

    Every test replaces `_REQUIRED_CREDENTIALS` rather than the environment,
    because that tuple is built once while the module is imported.  Doing it
    this way also means these tests give the same answer on a developer
    machine with a filled-in .env and in CI, which has none.
    """

    def test_reports_every_missing_credential_in_one_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Someone who left both blank should learn both on the first restart,
        not discover the second one after fixing the first."""
        monkeypatch.setattr(
            config,
            "_REQUIRED_CREDENTIALS",
            (("TWITCH_CLIENT_ID", ""), ("TWITCH_CLIENT_SECRET", "")),
        )
        with pytest.raises(RuntimeError) as excinfo:
            config.validate_credentials()
        message = str(excinfo.value)
        assert "TWITCH_CLIENT_ID" in message
        assert "TWITCH_CLIENT_SECRET" in message

    def test_names_only_the_credential_that_is_actually_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            config,
            "_REQUIRED_CREDENTIALS",
            (("TWITCH_CLIENT_ID", "an-id"), ("TWITCH_CLIENT_SECRET", "")),
        )
        with pytest.raises(RuntimeError) as excinfo:
            config.validate_credentials()
        message = str(excinfo.value)
        assert "TWITCH_CLIENT_SECRET" in message
        assert "TWITCH_CLIENT_ID" not in message

    def test_accepts_credentials_that_are_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            config,
            "_REQUIRED_CREDENTIALS",
            (("TWITCH_CLIENT_ID", "an-id"), ("TWITCH_CLIENT_SECRET", "a-secret")),
        )
        config.validate_credentials()

    def test_checks_the_values_in_use_not_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The environment variables are set here, yet the constants built from
        them are empty — which is precisely the disagreement the old check
        could not see, because it called os.getenv() a second time instead of
        looking at the values the rest of the program consumes."""
        monkeypatch.setenv("TWITCH_CLIENT_ID", "present-in-the-environment")
        monkeypatch.setenv("TWITCH_CLIENT_SECRET", "present-in-the-environment")
        monkeypatch.setattr(
            config,
            "_REQUIRED_CREDENTIALS",
            (("TWITCH_CLIENT_ID", ""), ("TWITCH_CLIENT_SECRET", "")),
        )
        with pytest.raises(RuntimeError, match="TWITCH_CLIENT_ID"):
            config.validate_credentials()


class TestValidateConfig:
    """The startup check the composition root runs before anything else.

    The fixture below makes every rule pass, so each test can break exactly
    one of them and know which rule produced the error.
    """

    @pytest.fixture(autouse=True)
    def _a_working_configuration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            config,
            "_REQUIRED_CREDENTIALS",
            (("TWITCH_CLIENT_ID", "an-id"), ("TWITCH_CLIENT_SECRET", "a-secret")),
        )
        monkeypatch.setattr(config, "BOT_USERNAME", "examplebot")
        monkeypatch.setattr(
            config, "OAUTH_REDIRECT_URL", "http://localhost:4343/oauth/callback"
        )

    def test_accepts_a_complete_configuration(self) -> None:
        config.validate_config()

    def test_rejects_an_empty_bot_username(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason this setting is required at all: without a name there is
        no account to run as, and the old fallback picked one silently."""
        monkeypatch.setattr(config, "BOT_USERNAME", "")
        with pytest.raises(RuntimeError, match="TWITCH_BOT_USERNAME"):
            config.validate_config()

    def test_missing_credentials_are_reported_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Order matters for the message someone reads: with nothing filled in
        at all, the credentials are the first thing to go and get, so they are
        what the error should be about."""
        monkeypatch.setattr(
            config,
            "_REQUIRED_CREDENTIALS",
            (("TWITCH_CLIENT_ID", ""), ("TWITCH_CLIENT_SECRET", "")),
        )
        monkeypatch.setattr(config, "BOT_USERNAME", "")
        with pytest.raises(RuntimeError) as excinfo:
            config.validate_config()
        message = str(excinfo.value)
        assert "TWITCH_CLIENT_ID" in message
        assert "TWITCH_BOT_USERNAME" not in message

    def test_still_rejects_an_unusable_redirect_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The redirect-URL rule was there before and must survive the two new
        checks being added in front of it."""
        monkeypatch.setattr(
            config, "OAUTH_REDIRECT_URL", "http://bot.example.org/oauth/callback"
        )
        with pytest.raises(RuntimeError, match="VOXER_OAUTH_REDIRECT_URL"):
            config.validate_config()
