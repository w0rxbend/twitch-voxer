"""Unit tests for message parsing and delay math in voxer.scheduler."""

from pathlib import Path

from voxer.scheduler import DEFAULT_FREQUENCY_PER_HOUR, ScheduledMessage, Scheduler


def make_scheduler(tmp_path: Path) -> Scheduler:
    async def send_chat(text: str) -> None:  # pragma: no cover - never called here
        raise AssertionError("send_chat must not be called in unit tests")

    return Scheduler(
        send_chat=send_chat,
        messages_path=tmp_path / "messages.json",
        empty_retry_delay=600,
    )


class TestParseMessage:
    def test_plain_string(self, tmp_path: Path) -> None:
        msg = make_scheduler(tmp_path)._parse_message("hello", 1)
        assert msg == ScheduledMessage("hello", DEFAULT_FREQUENCY_PER_HOUR)

    def test_object_with_frequency(self, tmp_path: Path) -> None:
        raw = {"text": "follow me", "frequency_per_hour": 2.5}
        msg = make_scheduler(tmp_path)._parse_message(raw, 1)
        assert msg == ScheduledMessage("follow me", 2.5)

    def test_missing_text_rejected(self, tmp_path: Path) -> None:
        assert make_scheduler(tmp_path)._parse_message({"frequency_per_hour": 1}, 1) is None

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
