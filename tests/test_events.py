"""Unit tests for the channel-event announcement builders in voxer.events.

Two kinds of test live here.  The per-function tests below check the public
behaviour: the right name or number ends up in the returned string.  Because
each builder picks one template at random out of several, a single call only
ever exercises one of them, so a typo in any of the others would slip through
on most runs.  The template sweep at the bottom closes that hole by formatting
every template in every list on every run.
"""

import pytest

from voxer import events
from voxer.events import (
    cheer_message,
    follow_message,
    gift_message,
    raid_message,
    resub_message,
    sub_message,
)

# Each entry pairs one template list with exactly the placeholder names the
# function that uses it passes to str.format() — no more.  Formatting every
# list against one shared superset of keys would be easier and would test
# nothing: the failure that actually reaches production is a template using a
# placeholder its own caller never supplies (a stray "{username}" in the
# anonymous-gift list, or "{month}" where the caller passes "months"), and only
# the narrow key set turns that into a KeyError here instead of a crash live.
_TEMPLATE_LISTS: list[tuple[str, list[str], dict[str, object]]] = [
    ("_FOLLOW", events._FOLLOW, {"username": "some_user"}),
    ("_SUBSCRIBE", events._SUBSCRIBE, {"username": "some_user"}),
    ("_RESUB", events._RESUB, {"username": "some_user", "months": 7}),
    ("_GIFT", events._GIFT, {"username": "some_user", "count": 5}),
    ("_GIFT_ANONYMOUS", events._GIFT_ANONYMOUS, {"count": 5}),
    ("_RAID", events._RAID, {"raider": "raider_user", "viewers": 42}),
    ("_CHEER", events._CHEER, {"username": "some_user", "bits": 100}),
    ("_CHEER_ANONYMOUS", events._CHEER_ANONYMOUS, {"bits": 100}),
]


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


def test_cheer_anonymous_has_no_placeholder() -> None:
    # An anonymous cheer has no username to substitute, so it must come from
    # the dedicated list rather than leaving a "{username}" or the literal
    # word "None" in a string the overlay is about to read out loud
    text = cheer_message(None, 100)
    assert "{" not in text
    assert "None" not in text
    assert "100" in text


@pytest.mark.parametrize(
    ("list_name", "templates", "keys"),
    _TEMPLATE_LISTS,
    ids=[name for name, _, _ in _TEMPLATE_LISTS],
)
def test_every_template_formats_with_its_own_keys(
    list_name: str, templates: list[str], keys: dict[str, object]
) -> None:
    """Every template in one list must format cleanly with that list's keys.

    "Cleanly" means two things.  First, str.format() must not raise: a
    KeyError here means a template names a placeholder its caller never passes,
    which live would crash the announcement instead of speaking it.  Second, no
    "{" may survive: a placeholder that was misspelled as, say, "{ username}"
    does not raise, it just stays in the text and gets read out verbatim.
    """
    assert templates, f"{list_name} is empty — the sweep would pass vacuously"
    for template in templates:
        try:
            text = template.format(**keys)
        except (KeyError, IndexError) as exc:
            pytest.fail(
                f"{list_name} template {template!r} uses placeholder {exc} "
                f"which its caller does not supply (available: {sorted(keys)})"
            )
        assert "{" not in text, (
            f"{list_name} template {template!r} left an unfilled placeholder: {text!r}"
        )
