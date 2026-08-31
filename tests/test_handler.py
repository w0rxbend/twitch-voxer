"""Unit tests for the message-to-audio pipeline in voxer.handler.

Everything is driven through the public `MessageHandler.handle()` so the tests
pin observable behaviour rather than the private step methods.  Two things in
the pipeline have to be faked or the tests would be slow and flaky:

  - TTSService, because real synthesis downloads a ~100 MB model and takes
    seconds per message.  The fake records what it was asked to say, which is
    how the announce-prefix assertions are made.
  - langdetect, because it is randomised and unreliable on short chat strings;
    a message that detects as "uk" in one run and "en" in the next would pick a
    different announcement template and make assertions flap.
"""

from pathlib import Path

import pytest

from voxer import handler as handler_module
from voxer.handler import MessageHandler
from voxer.models import BroadcastEvent, MessageKind, QueuedMessage
from voxer.stores import AnnounceTracker, EmoteStore, VoiceStore


class FakeTTS:
    """Stands in for TTSService, recording every synthesis request.

    save_wav must create a real file: _synthesize_and_broadcast unlinks the WAV
    in a finally block, so returning a bare path would fail with
    FileNotFoundError for reasons unrelated to the behaviour under test.
    """

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self._counter = 0
        self.calls: list[dict] = []

    def save_wav(self, text: str, *, voice_name: str, lang: str) -> Path:
        self.calls.append({"text": text, "voice_name": voice_name, "lang": lang})
        self._counter += 1
        wav = self._tmp_path / f"synth-{self._counter}.wav"
        wav.touch()
        return wav

    async def to_mp3(self, wav_path: Path, mp3_path: Path) -> None:
        mp3_path.touch()

    @property
    def spoken_text(self) -> str:
        """The text passed to the most recent synthesis call."""
        return self.calls[-1]["text"]


@pytest.fixture
def audio_dir(tmp_path: Path) -> Path:
    path = tmp_path / "audio"
    path.mkdir()
    return path


@pytest.fixture
def fake_tts(tmp_path: Path) -> FakeTTS:
    return FakeTTS(tmp_path)


@pytest.fixture
def broadcasts() -> list[BroadcastEvent]:
    return []


@pytest.fixture(autouse=True)
def fixed_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin language detection to English so template choice is deterministic."""
    monkeypatch.setattr(handler_module, "detect", lambda text: "en")


@pytest.fixture
def build_handler(
    tmp_path: Path,
    audio_dir: Path,
    fake_tts: FakeTTS,
    broadcasts: list[BroadcastEvent],
):
    """Return a factory building a ready-to-use MessageHandler.

    The stores are real and backed by tmp_path — they are cheap, and using the
    real ones means the announce-window behaviour is exercised rather than
    re-implemented in a stub.
    """

    async def _build(
        *,
        emote_sound_paths: list[str] | None = None,
        no_announce_users: frozenset[str] | None = None,
        announce_window_secs: int = 300,
    ) -> MessageHandler:
        async def capture(event: BroadcastEvent) -> None:
            broadcasts.append(event)

        instance = MessageHandler(
            tts=fake_tts,
            voice_store=VoiceStore(str(tmp_path / "voices.json"), ["M1"]),
            announce_tracker=AnnounceTracker(
                str(tmp_path / "ts.json"), announce_window_secs
            ),
            emote_store=EmoteStore(None),
            audio_dir=audio_dir,
            broadcast=capture,
            message_queue=None,  # handle() is called directly; the queue is unused
            emote_sound_paths=emote_sound_paths,
            no_announce_users=no_announce_users,
        )
        await instance.preload_resources()
        return instance

    return _build


class TestBotFiltering:
    async def test_bot_message_is_dropped(self, build_handler, broadcasts) -> None:
        instance = await build_handler()
        await instance.handle(QueuedMessage(username="somebot", text="hello"))
        assert broadcasts == []

    async def test_system_event_bypasses_the_bot_filter(
        self, build_handler, broadcasts, fake_tts
    ) -> None:
        """A channel event is announced even when the name looks like a bot.

        SYSTEM messages carry announcement text this app generated itself, so
        the chatter-name filter must not apply to them.
        """
        instance = await build_handler()
        await instance.handle(
            QueuedMessage(
                username="somebot", text="somebot joined", kind=MessageKind.SYSTEM
            )
        )
        assert len(broadcasts) == 1
        # Event strings in events.py are written in Ukrainian
        assert fake_tts.calls[-1]["lang"] == "uk"


class TestEmoteOnlyMessages:
    async def test_emote_only_plays_a_sound_without_synthesis(
        self, build_handler, broadcasts, fake_tts, tmp_path, audio_dir
    ) -> None:
        sound = tmp_path / "ping.mp3"
        sound.write_bytes(b"fake-mp3")
        instance = await build_handler(emote_sound_paths=[str(sound)])

        await instance.handle(QueuedMessage(username="alice", text="🎉"))

        assert len(broadcasts) == 1
        assert broadcasts[0].emotes  # the emoji still reaches the overlay
        assert fake_tts.calls == []  # nothing was spoken
        # The sound is copied, not moved — it must survive for the next message
        assert sound.exists()
        played = audio_dir / Path(broadcasts[0].audio_url).name
        assert played.exists()

    async def test_emote_only_is_skipped_without_configured_sounds(
        self, build_handler, broadcasts
    ) -> None:
        instance = await build_handler(emote_sound_paths=[])
        await instance.handle(QueuedMessage(username="alice", text="🎉"))
        assert broadcasts == []


class TestAnnouncePrefix:
    async def test_first_message_is_prefixed_and_the_next_is_not(
        self, build_handler, fake_tts
    ) -> None:
        instance = await build_handler()

        await instance.handle(QueuedMessage(username="alice", text="hello there"))
        assert "alice" in fake_tts.spoken_text
        assert fake_tts.spoken_text != "hello there"

        # Still inside the announce window — speak the bare message
        await instance.handle(QueuedMessage(username="alice", text="second one"))
        assert fake_tts.spoken_text == "second one"

    async def test_prefix_returns_after_the_window_elapses(
        self, build_handler, fake_tts
    ) -> None:
        # A zero-second window means every gap counts as elapsed
        instance = await build_handler(announce_window_secs=0)
        await instance.handle(QueuedMessage(username="alice", text="hello"))
        await instance.handle(QueuedMessage(username="alice", text="hello again"))
        assert "alice" in fake_tts.spoken_text

    async def test_no_announce_users_are_never_prefixed(
        self, build_handler, fake_tts
    ) -> None:
        """The no-announce list is matched case-insensitively.

        The configured entry is lowercase but Twitch display names are not, so
        a mixed-case username must still match.
        """
        instance = await build_handler(no_announce_users=frozenset({"alice"}))
        await instance.handle(QueuedMessage(username="Alice", text="hello there"))
        assert fake_tts.spoken_text == "hello there"


class TestDispatch:
    async def test_system_message_is_spoken_verbatim(
        self, build_handler, fake_tts
    ) -> None:
        """SYSTEM text is ready to speak — no prefix, even on first sight."""
        instance = await build_handler()
        await instance.handle(
            QueuedMessage(
                username="alice", text="alice just followed!", kind=MessageKind.SYSTEM
            )
        )
        assert fake_tts.spoken_text == "alice just followed!"

    async def test_user_message_is_broadcast_with_its_audio_url(
        self, build_handler, broadcasts, audio_dir
    ) -> None:
        instance = await build_handler()
        await instance.handle(QueuedMessage(username="alice", text="hello there"))

        assert len(broadcasts) == 1
        event = broadcasts[0]
        assert event.username == "alice"
        assert event.audio_url.startswith("/audio/")
        assert (audio_dir / Path(event.audio_url).name).exists()
