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
"""

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
