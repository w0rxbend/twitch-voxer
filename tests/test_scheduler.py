"""Unit tests for voxer.scheduler: message parsing, delay math, and one cycle.

The cycle tests all drive ``Scheduler._run_once()`` rather than ``run()``.
``run()`` is an endless loop whose only remaining job is to sleep for as long
as the previous cycle asked for, so entering it from a test would mean either
waiting out real time or patching the clock.  ``_run_once()`` returns that
number instead of sleeping on it, which makes every question these tests ask
answerable without a single sleep.

The message list is seeded by writing JSON straight to a file under
``tmp_path``.  That is deliberately the same thing an operator does — the
scheduler re-reads ``data/messages.json`` on every cycle precisely so the file
can be hand-edited while the bot runs — so the tests exercise the real reader
against the real input shape, including the malformed shapes.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from voxer.scheduler import DEFAULT_FREQUENCY_PER_HOUR, ScheduledMessage, Scheduler


def make_scheduler(
    tmp_path: Path,
    send_chat: Callable[[str], Awaitable[None]] | None = None,
) -> Scheduler:
    """Build a Scheduler reading (and posting to) nothing but the test's own files.

    With no ``send_chat`` the scheduler is given one that fails the test if it
    is ever awaited, which is what the parsing and delay-math tests want: they
    assert about pure computation and must not reach chat at all.  Cycle tests
    pass their own recorder instead.
    """

    async def never_called(text: str) -> None:  # pragma: no cover - never called
        raise AssertionError("send_chat must not be called in unit tests")

    return Scheduler(
        send_chat=send_chat if send_chat is not None else never_called,
        messages_path=tmp_path / "messages.json",
        empty_retry_delay=600,
    )


def write_messages(tmp_path: Path, messages: Any) -> None:
    """Write the file the scheduler reads, exactly as an operator would.

    ``messages`` is stored under the "messages" key with no validation, so a
    test can put a non-list or a list of junk there and see what the loader
    makes of it.
    """
    (tmp_path / "messages.json").write_text(json.dumps({"messages": messages}))


class Recorder:
    """A stand-in for VoxBot.send_chat that remembers what it was asked to post.

    ``fails_with`` makes every call raise, which is how the tests reproduce a
    transient Twitch API failure without a network.
    """

    def __init__(self, fails_with: Exception | None = None) -> None:
        self.posted: list[str] = []
        self._fails_with = fails_with

    async def __call__(self, text: str) -> None:
        self.posted.append(text)
        if self._fails_with is not None:
            raise self._fails_with


class TestParseMessage:
    def test_rate_with_infinite_interval_is_rejected(self, tmp_path) -> None:
        scheduler = make_scheduler(tmp_path)
        assert (
            scheduler._parse_message({"text": "hello", "frequency_per_hour": 5e-324}, 1)
            is None
        )

    def test_very_low_finite_rate_is_preserved(self, tmp_path) -> None:
        scheduler = make_scheduler(tmp_path)
        message = scheduler._parse_message(
            {"text": "hello", "frequency_per_hour": 1e-100}, 1
        )
        assert message == ScheduledMessage("hello", 1e-100)
        assert scheduler._delay_for([message]) == 3600 / 1e-100

    @pytest.mark.parametrize(
        "frequency", [float("nan"), float("inf"), "NaN", "Infinity", True, 10**1000]
    )
    def test_nonfinite_or_boolean_frequency_is_rejected(
        self, tmp_path, frequency
    ) -> None:
        assert (
            make_scheduler(tmp_path)._parse_message(
                {"text": "hello", "frequency_per_hour": frequency}, 1
            )
            is None
        )

    @pytest.mark.parametrize("text", ["", "   ", "x" * 501])
    def test_invalid_plain_strings_are_rejected(self, tmp_path, text) -> None:
        assert make_scheduler(tmp_path)._parse_message(text, 1) is None

    def test_plain_string(self, tmp_path: Path) -> None:
        msg = make_scheduler(tmp_path)._parse_message("hello", 1)
        assert msg == ScheduledMessage("hello", DEFAULT_FREQUENCY_PER_HOUR)

    def test_object_with_frequency(self, tmp_path: Path) -> None:
        raw = {"text": "follow me", "frequency_per_hour": 2.5}
        msg = make_scheduler(tmp_path)._parse_message(raw, 1)
        assert msg == ScheduledMessage("follow me", 2.5)

    def test_missing_text_rejected(self, tmp_path: Path) -> None:
        assert (
            make_scheduler(tmp_path)._parse_message({"frequency_per_hour": 1}, 1)
            is None
        )

    def test_blank_text_rejected(self, tmp_path: Path) -> None:
        assert make_scheduler(tmp_path)._parse_message({"text": "   "}, 1) is None

    def test_invalid_frequency_rejected(self, tmp_path: Path) -> None:
        raw = {"text": "x", "frequency_per_hour": "often"}
        assert make_scheduler(tmp_path)._parse_message(raw, 1) is None

    def test_negative_frequency_rejected(self, tmp_path: Path) -> None:
        raw = {"text": "x", "frequency_per_hour": -1}
        assert make_scheduler(tmp_path)._parse_message(raw, 1) is None

    def test_non_dict_rejected(self, tmp_path: Path) -> None:
        assert make_scheduler(tmp_path)._parse_message(42, 1) is None


class TestDelayFor:
    def test_unrepresentable_delay_uses_finite_fallback(self, tmp_path) -> None:
        assert (
            make_scheduler(tmp_path)._delay_for([ScheduledMessage("bad", 5e-324)])
            == 600.0
        )

    def test_extreme_total_frequency_has_a_safe_minimum_delay(self, tmp_path) -> None:
        messages = [ScheduledMessage("a", 1e308), ScheduledMessage("b", 1e308)]
        scheduler = make_scheduler(tmp_path)
        assert scheduler._delay_for(messages) == 30.0
        assert scheduler._choose_message(messages) in messages

    def test_delay_is_hour_over_total_frequency(self, tmp_path: Path) -> None:
        messages = [ScheduledMessage("a", 1.0), ScheduledMessage("b", 3.0)]
        # 4 messages/hour in total → one every 900 seconds
        assert make_scheduler(tmp_path)._delay_for(messages) == 900.0

    def test_zero_frequency_falls_back_to_retry_delay(self, tmp_path: Path) -> None:
        assert make_scheduler(tmp_path)._delay_for([]) == 600.0


class TestChooseMessage:
    def test_single_message_always_chosen(self, tmp_path: Path) -> None:
        only = ScheduledMessage("solo", 1.0)
        assert make_scheduler(tmp_path)._choose_message([only]) is only

    def test_zero_weight_message_never_chosen(self, tmp_path: Path) -> None:
        # frequency_per_hour feeds random.choices weights directly, so a
        # zero-frequency message must never win against a positive one
        messages = [
            ScheduledMessage("never", 0.0),
            ScheduledMessage("always", 5.0),
        ]
        scheduler = make_scheduler(tmp_path)
        chosen = {scheduler._choose_message(messages).text for _ in range(100)}
        assert chosen == {"always"}


class TestLoadMessages:
    """The reader that turns data/messages.json into ScheduledMessage objects.

    Everything here goes through a real file, because the loader's whole job is
    to survive whatever an operator's text editor left behind.  It must never
    raise: run() calls it with no try of its own, and run() is a task in the
    composition root's TaskGroup, where an escaping exception cancels the
    sibling tasks and takes the whole bot down with it.
    """

    async def test_missing_file_yields_no_messages(self, tmp_path: Path) -> None:
        # Nothing is written, so messages.json does not exist at all — the
        # state of a fresh install before the operator has created it.
        assert await make_scheduler(tmp_path)._load_messages() == []

    async def test_non_list_messages_key_yields_no_messages(
        self, tmp_path: Path
    ) -> None:
        # A plausible hand-edit: an object keyed by name instead of an array.
        write_messages(tmp_path, {"first": "hello", "second": "world"})
        assert await make_scheduler(tmp_path)._load_messages() == []

    async def test_all_entries_invalid_yields_no_messages(self, tmp_path: Path) -> None:
        write_messages(tmp_path, [42, {"text": "   "}, {"frequency_per_hour": 2}])
        assert await make_scheduler(tmp_path)._load_messages() == []

    async def test_invalid_entries_are_dropped_and_the_rest_survive(
        self, tmp_path: Path
    ) -> None:
        # One bad entry must not cost the operator the good ones around it.
        write_messages(
            tmp_path,
            [
                "plain string",
                42,
                {"text": "weighted", "frequency_per_hour": 3},
                {"frequency_per_hour": 1},
            ],
        )
        assert await make_scheduler(tmp_path)._load_messages() == [
            ScheduledMessage("plain string", DEFAULT_FREQUENCY_PER_HOUR),
            ScheduledMessage("weighted", 3.0),
        ]

    async def test_the_file_is_re_read_on_every_call(self, tmp_path: Path) -> None:
        # The scheduler promises that editing data/messages.json takes effect on
        # the next cycle without restarting the bot.  That promise is only kept
        # because the loader re-reads the file every time it is called, so an
        # edit between two calls must show up in the second one.
        scheduler = make_scheduler(tmp_path)
        write_messages(tmp_path, ["before"])
        assert await scheduler._load_messages() == [
            ScheduledMessage("before", DEFAULT_FREQUENCY_PER_HOUR)
        ]

        write_messages(tmp_path, ["after"])
        assert await scheduler._load_messages() == [
            ScheduledMessage("after", DEFAULT_FREQUENCY_PER_HOUR)
        ]


class TestRunOnce:
    """One scheduling cycle: read the list, post at most one message, say when next."""

    async def test_posts_one_message_and_returns_the_derived_delay(
        self, tmp_path: Path
    ) -> None:
        write_messages(tmp_path, [{"text": "hello chat", "frequency_per_hour": 2}])
        recorder = Recorder()
        scheduler = make_scheduler(tmp_path, send_chat=recorder)

        delay = await scheduler._run_once()

        assert recorder.posted == ["hello chat"]
        # 2 messages/hour in total → the next cycle is 1800 seconds away.  The
        # cadence comes from the message list, never from empty_retry_delay.
        assert delay == 1800.0

    async def test_an_empty_list_posts_nothing_and_waits_the_retry_delay(
        self, tmp_path: Path
    ) -> None:
        # No file at all, so there is nothing to post.  The cycle must still
        # return a sane wait rather than spinning, and must not touch chat —
        # make_scheduler's default send_chat fails the test if it is awaited.
        delay = await make_scheduler(tmp_path)._run_once()

        assert delay == 600.0

    async def test_a_failing_send_is_logged_and_the_cycle_still_returns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # This is the most important behaviour in the module.  run() is a task
        # in the composition root's TaskGroup: if a transient Twitch failure
        # (network blip, token refresh, HTTP 500) escaped from here, the
        # TaskGroup would cancel every sibling task and the bot would exit.
        write_messages(tmp_path, [{"text": "hello chat", "frequency_per_hour": 4}])
        recorder = Recorder(fails_with=RuntimeError("twitch said no"))
        scheduler = make_scheduler(tmp_path, send_chat=recorder)

        with caplog.at_level(logging.ERROR, logger="voxer.scheduler"):
            delay = await scheduler._run_once()

        assert recorder.posted == ["hello chat"]
        # The failure changes nothing about the schedule: 4/hour is still 900s.
        assert delay == 900.0
        assert "Failed to post scheduled message" in caplog.text
        # LOGGER.exception attaches the traceback, so the underlying cause is
        # in the log too — a silent "failed to post" would be undiagnosable.
        assert "twitch said no" in caplog.text
