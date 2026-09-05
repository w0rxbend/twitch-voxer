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
import asyncio
import datetime
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voxer.bot import classify_subscribe_errors, oauth_start_url, split_fragments
from voxer.bot import OAUTH_SCOPES, VoxBot
from voxer import config
from voxer.models import MessageKind
from voxer.soundboard import SOUNDS
from twitchio.ext import commands
from twitchio.authentication import ValidateTokenPayload


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
        assert oauth_start_url(redirect_url, 4343) == redirect_url.removesuffix(
            "/callback"
        )

    def test_the_registered_redirect_port_is_authoritative(self) -> None:
        url = oauth_start_url("http://localhost:4343/oauth/callback", 9999)
        assert url == "http://localhost:4343/oauth"

    def test_public_domain_gives_an_https_link_and_ignores_the_port(self) -> None:
        """A public deployment is reached through its domain, over HTTPS.

        twitchio's adapter builds its own URLs with https for any non-localhost
        domain, and Twitch only permits plain http for localhost, so a public
        deployment has no http option.  The internal bind port is invisible from
        outside (a reverse proxy terminates TLS on 443), so it must not appear.
        """
        url = oauth_start_url("https://bot.example.org/oauth/callback", 4343)
        assert url == "https://bot.example.org/oauth"


def make_bot(queue_size: int = 4) -> VoxBot:
    """Exercise event admission without starting TwitchIO's network lifecycle."""
    bot = object.__new__(VoxBot)
    bot._bot_id = "123"
    bot._message_queue = asyncio.Queue(maxsize=queue_size)
    bot._max_message_chars = 500
    bot._user_cooldown_secs = 2
    bot._seen_events = OrderedDict()
    bot._user_last_message = OrderedDict()
    bot._avatar_url_cache = OrderedDict()
    bot._avatar_pending = set()
    bot._token_admission_lock = asyncio.Lock()
    bot._http = SimpleNamespace(_app_token="existing-app", _tokens={})
    bot.fetch_user = AsyncMock(return_value=None)
    return bot


def chat(text: str = "hello", *, user_id: str = "456", event_id: str = "message"):
    return SimpleNamespace(
        id=event_id,
        fragments=[StubFragment("text", text)],
        chatter=SimpleNamespace(id=user_id, name="viewer"),
        broadcaster=SimpleNamespace(id="123"),
        source_broadcaster=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["full", "oversized", "self", "command", "other-channel", "shared-chat"]
)
async def test_dropped_chat_does_not_fetch_an_avatar(reason, monkeypatch):
    bot = make_bot(queue_size=1)
    payload = chat()
    monkeypatch.setattr(commands.AutoBot, "event_message", AsyncMock())
    if reason == "full":
        bot._message_queue.put_nowait(object())
    elif reason == "oversized":
        payload.fragments = [StubFragment("text", "x" * 501)]
    elif reason == "self":
        payload.chatter.id = "123"
    elif reason == "command":
        payload.fragments = [StubFragment("text", "!help")]
    elif reason == "other-channel":
        payload.broadcaster.id = "789"
    else:
        payload.source_broadcaster = SimpleNamespace(id="789")
    await bot.event_message(payload)
    bot.fetch_user.assert_not_awaited()
    assert bot._message_queue.qsize() == (1 if reason == "full" else 0)


@pytest.mark.asyncio
async def test_chat_deduplication_and_user_cooldown(monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(commands.AutoBot, "event_message", AsyncMock())
    await bot.event_message(chat())
    await bot.event_message(chat())
    await bot.event_message(chat(event_id="next"))
    await bot.event_message(chat(user_id="other", event_id="other"))
    assert bot._message_queue.qsize() == 2
    assert bot.fetch_user.await_count == 2


@pytest.mark.parametrize(
    "text, expected",
    [
        ("!tts Hello, world!", "Hello, world!"),
        ("  ! TTS\tHello\nworld!  ", "Hello\nworld!"),
        ("!tts: Привіт, чат!", "Привіт, чат!"),
        ("!tts = Read this", "Read this"),
        ("hey !tts message to TTS", "message to TTS"),
        ("before !tts say this !end ignore this", "say this"),
    ],
)
async def test_tts_command_queues_only_the_body(text, expected, monkeypatch):
    bot = make_bot()
    dispatch = AsyncMock()
    monkeypatch.setattr(commands.AutoBot, "event_message", dispatch)
    await bot.event_message(chat(text))
    message = bot._message_queue.get_nowait()
    assert message.kind is MessageKind.USER
    assert message.text == expected
    assert message.username == "viewer"
    dispatch.assert_not_awaited()


async def test_tts_command_preserves_emote_fragments(monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(commands.AutoBot, "event_message", AsyncMock())
    payload = chat()
    payload.fragments = [
        StubFragment("text", " !tts: hello "),
        StubFragment("emote", "Kappa"),
        StubFragment("text", " world"),
    ]
    await bot.event_message(payload)
    message = bot._message_queue.get_nowait()
    assert "!tts" not in message.text
    assert message.text.strip().startswith("hello")
    assert message.text.strip().endswith("world")
    assert message.emote_names == ["Kappa"]


async def test_inline_commands_queue_in_order_with_one_cooldown(monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(commands.AutoBot, "event_message", AsyncMock())
    await bot.event_message(
        chat("hey !tts message !end ignore this !sound magic !s pop")
    )
    queued = [bot._message_queue.get_nowait() for _ in range(3)]
    assert [(message.kind, message.text) for message in queued] == [
        (MessageKind.USER, "message"),
        (MessageKind.SOUND, "sparkle"),
        (MessageKind.SOUND, "pop"),
    ]
    assert bot.fetch_user.await_count == 1
    await bot.event_message(chat("!s bang", event_id="next"))
    assert bot._message_queue.empty()


async def test_each_tts_section_gets_only_its_own_emotes(monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(commands.AutoBot, "event_message", AsyncMock())
    payload = chat()
    payload.fragments = [
        StubFragment("emote", "Before"),
        StubFragment("text", " !tts first "),
        StubFragment("emote", "Kappa"),
        StubFragment("text", " !end "),
        StubFragment("emote", "Ignored"),
        StubFragment("text", " !tts second "),
        StubFragment("emote", "PogChamp"),
    ]
    await bot.event_message(payload)
    first, second = bot._message_queue.get_nowait(), bot._message_queue.get_nowait()
    assert (first.text, first.emote_names) == ("first", ["Kappa"])
    assert (second.text, second.emote_names) == ("second", ["PogChamp"])


@pytest.mark.parametrize("during_avatar", [False, True])
async def test_command_sequence_is_not_partially_enqueued(during_avatar, monkeypatch):
    bot = make_bot(queue_size=2)
    monkeypatch.setattr(commands.AutoBot, "event_message", AsyncMock())
    if during_avatar:

        async def lookup(chatter):
            bot._message_queue.put_nowait("another message")

        monkeypatch.setattr(bot, "_get_avatar_url", lookup)
    else:
        bot._message_queue.put_nowait("another message")
    await bot.event_message(chat("!tts hello !end !sound pop"))
    assert bot._message_queue.qsize() == 1
    assert bot._message_queue.get_nowait() == "another message"


async def test_invalid_command_section_does_not_swallow_valid_following_commands(
    monkeypatch,
):
    bot = make_bot()
    monkeypatch.setattr(commands.AutoBot, "event_message", AsyncMock())
    await bot.event_message(chat("!s missing !tts !end !sound magic"))
    message = bot._message_queue.get_nowait()
    assert (message.kind, message.text) == (MessageKind.SOUND, "sparkle")
    assert bot._message_queue.empty()


@pytest.mark.parametrize("prefix", ["!sound", "!s"])
@pytest.mark.parametrize(
    "name, canonical",
    [(name, sound.name) for sound in SOUNDS for name in (sound.name, *sound.aliases)],
)
async def test_every_sound_and_alias_is_available_to_viewers(
    prefix, name, canonical, monkeypatch
):
    bot = make_bot()
    dispatch = AsyncMock()
    monkeypatch.setattr(commands.AutoBot, "event_message", dispatch)
    await bot.event_message(chat(f"{prefix} {name}"))
    message = bot._message_queue.get_nowait()
    assert message.kind is MessageKind.SOUND
    assert message.text == canonical
    dispatch.assert_not_awaited()


@pytest.mark.parametrize(
    "text", ["!tts", "!tts : ", "!sound", "!s missing", "!s ../pop"]
)
async def test_invalid_audio_commands_do_not_consume_cooldown(text, monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(commands.AutoBot, "event_message", AsyncMock())
    await bot.event_message(chat(text))
    bot.fetch_user.assert_not_awaited()
    assert bot._message_queue.empty()
    await bot.event_message(chat("!s pop", event_id="valid"))
    assert bot._message_queue.qsize() == 1


@pytest.mark.parametrize("text", ["!tts Hello", "!sound pop", "!s woof woof"])
@pytest.mark.parametrize(
    "reason", ["full", "cooldown", "duplicate", "oversized", "shared"]
)
async def test_audio_commands_obey_message_admission(text, reason, monkeypatch):
    bot = make_bot(queue_size=1)
    monkeypatch.setattr(commands.AutoBot, "event_message", AsyncMock())
    payload = chat(text)
    if reason == "full":
        bot._message_queue.put_nowait(object())
    elif reason == "cooldown":
        bot._accept_chatter("456")
    elif reason == "duplicate":
        bot._accept_event(payload)
    elif reason == "oversized":
        bot._max_message_chars = 3
    else:
        payload.source_broadcaster = SimpleNamespace(id="another-channel")
    await bot.event_message(payload)
    bot.fetch_user.assert_not_awaited()
    assert bot._message_queue.qsize() == (1 if reason == "full" else 0)


@pytest.mark.asyncio
async def test_system_queue_overflow_does_not_wait():
    bot = make_bot(queue_size=1)
    await bot._enqueue_system("one", "first")
    await asyncio.wait_for(bot._enqueue_system("two", "second"), timeout=0.1)
    assert bot._message_queue.qsize() == 1


@pytest.mark.asyncio
async def test_system_redelivery_is_not_announced_twice():
    bot = make_bot()
    event = SimpleNamespace(
        metadata=SimpleNamespace(message_id="delivery"),
        broadcaster=SimpleNamespace(id="123"),
        user=SimpleNamespace(name="viewer"),
    )
    await bot.event_follow(event)
    await bot.event_follow(event)
    assert bot._message_queue.qsize() == 1


@pytest.mark.asyncio
async def test_avatar_misses_are_negative_cached_and_cache_is_bounded():
    bot = make_bot()
    bot.fetch_user.side_effect = RuntimeError("Twitch is unavailable")
    user = SimpleNamespace(id="456", name="viewer")
    assert await bot._get_avatar_url(user) is None
    assert await bot._get_avatar_url(user) is None
    bot.fetch_user.assert_awaited_once()
    bot._avatar_url_cache = OrderedDict(
        (str(i), (float("inf"), None)) for i in range(2048)
    )
    await bot._get_avatar_url(SimpleNamespace(id="new", name="new"))
    assert len(bot._avatar_url_cache) == 2048


def test_admission_memory_is_bounded():
    bot = make_bot()
    for index in range(4100):
        assert bot._accept_event(chat(event_id=str(index)))
        assert bot._accept_chatter(str(index))
    assert len(bot._seen_events) == len(bot._user_last_message) == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["user", "client", "scopes"])
async def test_invalid_oauth_tokens_are_removed(invalid, monkeypatch):
    bot = make_bot()
    validated = SimpleNamespace(
        user_id="123", client_id=config.CLIENT_ID, scopes=list(OAUTH_SCOPES)
    )
    if invalid == "user":
        validated.user_id = "someone-else"
    elif invalid == "client":
        validated.client_id = "another-application"
    else:
        validated.scopes = []
    monkeypatch.setattr(
        commands.AutoBot, "add_token", AsyncMock(return_value=validated)
    )
    bot.remove_token = AsyncMock()
    with pytest.raises(ValueError, match="wrong account"):
        await bot.add_token("access", "refresh")
    assert bot._http._app_token == "existing-app"
    assert bot._http._tokens == {}


def test_the_redirect_path_does_not_leak_into_the_start_url() -> None:
    """The callback path and browser authorization start route are independent."""
    url = oauth_start_url("https://bot.example.org/twitch/cb", 4343)
    assert url == "https://bot.example.org/oauth"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["app", "foreign-app", "under-scoped-user", "foreign-client-user"]
)
async def test_rejected_grant_preserves_existing_managed_tokens(kind, monkeypatch):
    """Exercise TwitchIO's real mutation before rejection, not a stubbed add_token."""
    bot = VoxBot(bot_id="123", message_queue=asyncio.Queue(maxsize=1))
    monkeypatch.setattr(bot, "save_tokens", AsyncMock())
    bot._http._app_token = "known-good-app"
    existing_user = {
        "user_id": "123",
        "token": "known-good-user",
        "refresh": "known-good-refresh",
        "last_validated": datetime.datetime.now().isoformat(),
    }
    bot._http._tokens["123"] = existing_user.copy()
    validated = ValidateTokenPayload(
        {
            "client_id": "other-application"
            if kind.startswith("foreign")
            else config.CLIENT_ID,
            "user_id": None if kind in ("app", "foreign-app") else "123",
            "login": "bot",
            "scopes": [] if kind == "under-scoped-user" else list(OAUTH_SCOPES),
            "expires_in": 36000,
        }
    )
    isolated = bot._http._ManagedHTTPClient__isolated
    monkeypatch.setattr(isolated, "validate_token", AsyncMock(return_value=validated))
    try:
        with pytest.raises(ValueError, match="wrong account"):
            await bot.add_token("rejected-token", "rejected-refresh")
        assert bot._http._app_token == "known-good-app"
        assert bot._http._tokens == {"123": existing_user}
    finally:
        await bot.close()
