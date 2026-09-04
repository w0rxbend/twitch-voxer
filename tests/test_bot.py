"""Unit tests for the decidable parts of the Twitch adapter.

`voxer/bot.py` is mostly a twitchio subclass: opening a real `VoxBot` needs a
Twitch application, a WebSocket connection and a live OAuth flow, so the class
itself cannot be exercised here.  What *can* be exercised is the handful of
module-level functions it delegates its actual decisions to — splitting a chat
message into speakable text and emote names, telling a routine "already
subscribed" reply apart from a real subscription failure, and choosing which
URL a human should open to authorize the bot.

Those functions take plain data, so the payloads below are hand-built stubs
holding only the attributes the function reads.  That is deliberate: twitchio's
real payload classes are constructed from raw Twitch JSON plus an HTTP client,
and building one would test twitchio's parser rather than our logic.
"""

from dataclasses import dataclass, field

import pytest

from voxer.bot import classify_subscribe_errors, oauth_start_url, split_fragments


@dataclass(frozen=True)
class StubFragment:
    """One entry of a chat message's `fragments` list.

    Twitch tags every fragment with a type ("text", "emote", "cheermote",
    "mention") and puts its literal characters in `text`; for an emote fragment
    those characters are the emote's name, e.g. "Kappa".
    """

    type: str
    text: str


@dataclass(frozen=True)
class StubHTTPError:
    """The `error` half of a failed subscription: an HTTP status code."""

    status: int


@dataclass(frozen=True)
class StubSubscribeError:
    """One entry of `MultiSubscribePayload.errors`.

    `name` is not part of the real twitchio type; it is here only so a failed
    assertion says which stub came back in the wrong list.
    """

    name: str
    error: StubHTTPError = field(default_factory=lambda: StubHTTPError(500))


def _error(name: str, status: int) -> StubSubscribeError:
    return StubSubscribeError(name=name, error=StubHTTPError(status))


class TestSplitFragments:
    def test_text_only_message_is_joined_and_stripped(self) -> None:
        fragments = [StubFragment("text", " hello "), StubFragment("text", "world ")]
        assert split_fragments(fragments) == ("hello  world", [])

    def test_emote_names_are_collected_and_not_spoken(self) -> None:
        """An emote-only message must produce no speech at all.

        Reading "Kappa Kappa Kappa" aloud is noise, so the emote names go to the
        overlay and the spoken text stays empty — which is what lets the
        handler decide to show the emotes without synthesising anything.
        """
        fragments = [StubFragment("emote", "Kappa"), StubFragment("emote", "PogChamp")]
        assert split_fragments(fragments) == ("", ["Kappa", "PogChamp"])

    def test_mixed_message_keeps_both_halves_in_order(self) -> None:
        fragments = [
            StubFragment("text", "look"),
            StubFragment("emote", "Kappa"),
            StubFragment("text", "at this"),
            StubFragment("emote", "PogChamp"),
        ]
        assert split_fragments(fragments) == ("look at this", ["Kappa", "PogChamp"])

    @pytest.mark.parametrize("kind", ["cheermote", "mention"])
    def test_other_fragment_kinds_are_dropped_entirely(self, kind: str) -> None:
        """Only "text" is spoken and only "emote" is displayed.

        Twitch also sends "cheermote" fragments (the "Cheer100" tokens that
        trigger a bits animation) and "mention" fragments.  Neither is speakable
        text and neither is an emote the overlay can look up, so both must fall
        out of both halves rather than leak into the spoken line.
        """
        fragments = [StubFragment("text", "hi"), StubFragment(kind, "Cheer100")]
        assert split_fragments(fragments) == ("hi", [])

    def test_no_fragments_yields_empty_results(self) -> None:
        assert split_fragments([]) == ("", [])


class TestClassifySubscribeErrors:
    def test_conflict_status_is_a_duplicate_not_a_failure(self) -> None:
        """HTTP 409 means "you are already subscribed to that", which is fine.

        The bot re-registers its own channel on every boot because conduit
        subscriptions expire after 72 hours of downtime.  On every restart after
        the first, Twitch therefore answers 409 Conflict for each one.  Counting
        those as failures would print a warning on every single start.
        """
        errors = [_error("chat", 409), _error("follow", 409)]
        duplicates, failures = classify_subscribe_errors(errors)
        assert [e.name for e in duplicates] == ["chat", "follow"]
        assert failures == []

    @pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
    def test_any_other_status_is_a_real_failure(self, status: int) -> None:
        duplicates, failures = classify_subscribe_errors([_error("chat", status)])
        assert duplicates == []
        assert [e.name for e in failures] == ["chat"]

    def test_mixed_batch_is_split_keeping_input_order(self) -> None:
        errors = [
            _error("chat", 409),
            _error("follow", 403),
            _error("cheer", 409),
            _error("raid", 500),
        ]
        duplicates, failures = classify_subscribe_errors(errors)
        assert [e.name for e in duplicates] == ["chat", "cheer"]
        assert [e.name for e in failures] == ["follow", "raid"]

    def test_no_errors_yields_two_empty_lists(self) -> None:
        """The all-succeeded case must log neither the debug nor the warning."""
        assert classify_subscribe_errors([]) == ([], [])


class TestOauthStartUrl:
    @pytest.mark.parametrize(
        "redirect_url",
        [
            "http://localhost:4343/oauth/callback",
            "http://127.0.0.1:4343/oauth/callback",
        ],
    )
    def test_local_redirect_url_gives_a_localhost_link_on_the_bound_port(
        self, redirect_url: str
    ) -> None:
        """With no public domain, twitchio's adapter serves on its own port.

        "localhost" is used rather than the configured bind host because the
        bind host is often 0.0.0.0 (so Docker can publish the port), and
        0.0.0.0 is an address to listen on, not one a browser can visit.
        """
        assert oauth_start_url(redirect_url, 4343) == "http://localhost:4343/oauth"

    def test_the_port_argument_is_the_one_used(self) -> None:
        url = oauth_start_url("http://localhost:4343/oauth/callback", 9999)
        assert url == "http://localhost:9999/oauth"

    def test_public_domain_gives_an_https_link_and_ignores_the_port(self) -> None:
        """A public deployment is reached through its domain, over HTTPS.

        twitchio's adapter builds its own URLs with https for any non-localhost
        domain, and Twitch only permits plain http for localhost, so a public
        deployment has no http option.  The internal bind port is invisible from
        outside (a reverse proxy terminates TLS on 443), so it must not appear.
        """
        url = oauth_start_url("https://bot.example.org/oauth/callback", 4343)
        assert url == "https://bot.example.org/oauth"

    def test_the_redirect_path_does_not_leak_into_the_start_url(self) -> None:
        """These are two different routes on the same adapter.

        The redirect URL is where Twitch sends the browser *back* with a code;
        the start URL is where the human begins.  A custom redirect path must
        not be pasted onto the start link.
        """
        url = oauth_start_url("https://bot.example.org/twitch/cb", 4343)
        assert url == "https://bot.example.org/oauth"
