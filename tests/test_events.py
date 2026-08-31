"""Unit tests for the channel-event announcement builders in voxer.events."""

from voxer.events import (
    cheer_message,
    follow_message,
    gift_message,
    raid_message,
    resub_message,
    sub_message,
)


def test_follow_contains_username() -> None:
    assert "some_user" in follow_message("some_user")


def test_sub_contains_username() -> None:
    assert "some_user" in sub_message("some_user")


def test_resub_contains_username_and_months() -> None:
    text = resub_message("some_user", 7)
    assert "some_user" in text
    assert "7" in text


def test_gift_with_named_gifter() -> None:
    text = gift_message("generous_user", 5)
    assert "generous_user" in text
    assert "5" in text


def test_gift_anonymous_has_no_placeholder() -> None:
    text = gift_message(None, 3)
    assert "{" not in text
    assert "None" not in text


def test_cheer_contains_bits() -> None:
    assert "100" in cheer_message("some_user", 100)


def test_raid_contains_raider_and_viewers() -> None:
    text = raid_message("raider_user", 42)
    assert "raider_user" in text
    assert "42" in text
