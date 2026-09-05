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

import os
import json
import asyncio
import threading
import time
from pathlib import Path

import pytest

from voxer import handler as handler_module
from voxer.handler import MessageHandler
from voxer.models import BroadcastEvent, EmoteItem, MessageKind, QueuedMessage
from voxer.stores import AnnounceTracker, EmoteStore, VoiceStore


class FakeTTS:
    """Stands in for TTSService, recording every synthesis request.

    save_wav must create a real file: _synthesize_and_broadcast unlinks the WAV
    in a finally block, so returning a bare path would fail with
    FileNotFoundError for reasons unrelated to the behaviour under test.

    voice_names is the pool the real engine exposes, and channel-event
    announcements pick from it at random.  The names here deliberately differ
    from the pool given to VoiceStore in build_handler below — in the running
    bot the two are the same list, because the composition root passes
    tts.voice_names straight into VoiceStore, but keeping them apart in the
    tests is what lets an assertion tell which of the two objects a voice
    actually came from.
    """

    VOICE_NAMES: list[str] = ["ENGINE_A", "ENGINE_B"]

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self._counter = 0
        self.calls: list[dict] = []

    @property
    def voice_names(self) -> list[str]:
        return list(self.VOICE_NAMES)

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


async def _seeded_emote_store(
    tmp_path: Path, emotes: dict[str, dict[str, str]] | None
) -> EmoteStore:
    """Build an unloaded store over the fetcher's JSON cache format."""
    if emotes is None:
        return EmoteStore(None)
    path = tmp_path / "emotes.db"
    path.write_text(json.dumps(emotes))
    return EmoteStore(str(path))


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

    Pass `emotes` to give the handler a populated Twitch emote cache; leaving it
    unset keeps the store permanently empty, which is what every test that does
    not care about emote images wants.

    Pass `broadcast_raises` to make delivery to the overlay fail — that is how a
    real WebSocket fan-out failure is simulated, since the handler only ever sees
    `broadcast` as an awaitable it calls.

    Pass `delivered` to say how many overlay clients received the event, which
    is what the real `AudioServer.broadcast` returns.  The default of 1 stands
    for the ordinary case of one OBS browser source being connected; 0 is the
    equally ordinary case of the stream being offline with nothing listening.
    """

    async def _build(
        *,
        emote_sound_paths: list[str] | None = None,
        sound_paths: dict[str, Path] | None = None,
        no_announce_users: frozenset[str] | None = None,
        announce_window_secs: int = 300,
        emotes: dict[str, dict[str, str]] | None = None,
        broadcast_raises: type[BaseException] | None = None,
        delivered: int = 1,
    ) -> MessageHandler:
        async def capture(event: BroadcastEvent) -> int:
            broadcasts.append(event)
            if broadcast_raises is not None:
                raise broadcast_raises("broadcast failed")
            return delivered

        voice_store = VoiceStore(str(tmp_path / "voices.json"), ["M1"])
        announce_tracker = AnnounceTracker(
            str(tmp_path / "ts.json"), announce_window_secs
        )
        emote_store = await _seeded_emote_store(tmp_path, emotes)
        # Reading each store's file is awaited I/O, so it cannot happen in a
        # constructor.  Whoever builds the stores loads them: in the running bot
        # that is voxer.app.run(), and here it is this factory.  Loading them in
        # the same order and at the same point the composition root does means
        # the handler these tests drive starts in the state it starts in for
        # real — in particular with the seeded emote cache already in memory,
        # which is what makes emote lookups resolve.
        await emote_store.load()
        await voice_store.load()
        await announce_tracker.load()

        return MessageHandler(
            tts=fake_tts,
            voice_store=voice_store,
            announce_tracker=announce_tracker,
            emote_store=emote_store,
            audio_dir=audio_dir,
            broadcast=capture,
            message_queue=None,  # handle() is called directly; the queue is unused
            emote_sound_paths=emote_sound_paths,
            sound_paths=sound_paths,
            no_announce_users=no_announce_users,
        )

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

    async def test_emote_only_clip_is_stamped_now_not_with_the_source_time(
        self, build_handler, broadcasts, tmp_path, audio_dir
    ) -> None:
        """The copied clip must carry its own age, not the source sound's.

        The notification sounds ship with the project, so their modification
        time is whenever the repository was checked out — long in the past.
        server.reap_audio decides which clips have been abandoned by reading
        that same timestamp, so a clip that inherited the source's time would
        be older than the reaper's threshold from the moment it existed and
        would be deleted on the next sweep, possibly mid-playback.
        """
        sound = tmp_path / "ping.mp3"
        sound.write_bytes(b"fake-mp3")
        # Backdate the source by a day, the way a checked-out asset looks.
        old = time.time() - 86_400
        os.utime(sound, (old, old))
        instance = await build_handler(emote_sound_paths=[str(sound)])

        await instance.handle(QueuedMessage(username="alice", text="🎉"))

        played = audio_dir / Path(broadcasts[0].audio_url).name
        assert time.time() - played.stat().st_mtime < 60

    async def test_emote_only_is_skipped_without_configured_sounds(
        self, build_handler, broadcasts
    ) -> None:
        instance = await build_handler(emote_sound_paths=[])
        await instance.handle(QueuedMessage(username="alice", text="🎉"))
        assert broadcasts == []


class TestSoundboard:
    async def test_sound_uses_overlay_without_speech_or_announcement_state(
        self, build_handler, broadcasts, fake_tts, tmp_path, audio_dir
    ):
        sound = tmp_path / "pop.mp3"
        sound.write_bytes(b"downloaded-pop")
        old = time.time() - 86400
        os.utime(sound, (old, old))
        instance = await build_handler(sound_paths={"pop": sound})
        for _ in range(2):
            await instance.handle(
                QueuedMessage(
                    "alice", "pop", kind=MessageKind.SOUND, avatar_url="https://avatar"
                )
            )
        assert len(broadcasts) == 2
        assert broadcasts[0].audio_url != broadcasts[1].audio_url
        assert fake_tts.calls == []
        assert not (tmp_path / "ts.json").exists()
        assert not (tmp_path / "voices.json").exists()
        for event in broadcasts:
            played = audio_dir / Path(event.audio_url).name
            assert played.read_bytes() == sound.read_bytes()
            assert time.time() - played.stat().st_mtime < 60
            assert event.username == "alice"
            assert event.avatar_url == "https://avatar"

    @pytest.mark.parametrize("reason", ["missing", "stale", "offline", "bot"])
    async def test_unplayable_sound_does_not_fall_back_to_tts(
        self, reason, build_handler, broadcasts, fake_tts, tmp_path, audio_dir
    ):
        sound = tmp_path / "pop.mp3"
        sound.write_bytes(b"pop")
        instance = await build_handler(sound_paths={"pop": sound})
        message = QueuedMessage("alice", "pop", kind=MessageKind.SOUND)
        if reason == "missing":
            message.text = "../other"
        elif reason == "stale":
            message.enqueued_at = time.monotonic() - 1000
        elif reason == "offline":
            instance._overlay_available = lambda: False
        else:
            message.username = "nightbot"
        await instance.handle(message)
        assert broadcasts == fake_tts.calls == list(audio_dir.iterdir()) == []

    @pytest.mark.parametrize("outcome", ["unheard", "error", "cancelled"])
    async def test_sound_cleanup_preserves_source(
        self, outcome, build_handler, tmp_path, audio_dir
    ):
        sound = tmp_path / "pop.mp3"
        sound.write_bytes(b"pop")
        error = {"error": RuntimeError, "cancelled": asyncio.CancelledError}.get(
            outcome
        )
        instance = await build_handler(
            sound_paths={"pop": sound}, broadcast_raises=error, delivered=0
        )
        message = QueuedMessage("alice", "pop", kind=MessageKind.SOUND)
        if error is not None:
            with pytest.raises(error):
                await instance.handle(message)
        else:
            await instance.handle(message)
        assert list(audio_dir.iterdir()) == []
        assert sound.read_bytes() == b"pop"


class TestEmoteResolution:
    # The three sizes differ so a bug that picked the 1x or 4x URL instead of
    # the 2x one still fails here — same-looking URLs would hide it.
    _CACHE = {
        "Kappa": {
            "url_1x": "https://cdn.example/emote/kappa/1.0",
            "url_2x": "https://cdn.example/emote/kappa/2.0",
            "url_4x": "https://cdn.example/emote/kappa/3.0",
        }
    }

    async def test_known_emote_reaches_the_overlay_and_unknown_is_dropped(
        self, build_handler, broadcasts
    ) -> None:
        """A cached Twitch emote name becomes an image the overlay can render.

        Twitch tells the bot which emotes a message used, but only by name
        ("Kappa"); the image URL comes from the local cache file the emote
        fetcher builds.  This is the one path that turns those names into
        pictures on stream, so both halves of it are pinned here: a name the
        cache knows about arrives as an EmoteItem carrying the 2x URL, and a
        name it does not know is left out of the list rather than crashing the
        message or reaching the browser with a missing URL.
        """
        instance = await build_handler(emotes=self._CACHE)

        await instance.handle(
            QueuedMessage(
                username="alice",
                text="hello there",
                emote_names=["Kappa", "NotInTheCache"],
            )
        )

        assert len(broadcasts) == 1
        assert broadcasts[0].emotes == [
            EmoteItem(name="Kappa", url=self._CACHE["Kappa"]["url_2x"])
        ]


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

    async def test_a_capitalised_no_announce_entry_still_matches(
        self, build_handler, fake_tts
    ) -> None:
        """The handler lower-cases the list itself, so a caller need not.

        config.py happens to lower-case the value it reads from the environment,
        which used to be the only reason this worked at all.  Anyone building
        the set another way got a list that silently never matched, because the
        comparison lower-cases the incoming username but not the entries.  The
        handler now normalises what it is handed, so both sides agree.
        """
        instance = await build_handler(no_announce_users=frozenset({"Alice"}))
        await instance.handle(QueuedMessage(username="alice", text="hello there"))
        assert fake_tts.spoken_text == "hello there"


class TestVoiceSource:
    """Which object each kind of message asks for a voice.

    Two different questions look alike here and must not be confused.  A chat
    message needs the voice this particular chatter has been given and keeps
    forever, which is VoiceStore's job because it is the thing that writes the
    answer to disk.  A channel-event announcement (a follow, a raid) needs any
    voice at all, once, and nothing about the pick is ever stored or read back
    — so it comes from the TTS engine, which is the object that knows which
    voices exist.  The fake engine and the store are given different pools
    precisely so these assertions can tell the two apart.
    """

    async def test_channel_event_voice_comes_from_the_engine_pool(
        self, build_handler, fake_tts
    ) -> None:
        instance = await build_handler()
        await instance.handle(
            QueuedMessage(
                username="alice", text="alice just followed!", kind=MessageKind.SYSTEM
            )
        )
        assert fake_tts.calls[-1]["voice_name"] in FakeTTS.VOICE_NAMES

    async def test_user_voice_still_comes_from_the_store(
        self, build_handler, fake_tts
    ) -> None:
        """A chat message keeps using the persisted assignment, not the pool.

        build_handler gives VoiceStore the single-voice pool ["M1"], so the
        assignment it hands out is knowable in advance.
        """
        instance = await build_handler()
        await instance.handle(QueuedMessage(username="alice", text="hello there"))
        assert fake_tts.calls[-1]["voice_name"] == "M1"


class TestDispatch:
    async def test_stale_messages_do_not_synthesize(
        self, build_handler, fake_tts
    ) -> None:
        instance = await build_handler()
        await instance.handle(
            QueuedMessage("alice", "too late", enqueued_at=time.monotonic() - 61)
        )
        assert fake_tts.calls == []

    async def test_expanded_speech_is_bounded(self, build_handler, fake_tts) -> None:
        instance = await build_handler()
        await instance.handle(QueuedMessage("alice", "icymi " * 1000))
        assert len(fake_tts.spoken_text) <= 1000

    async def test_no_overlay_skips_inference(self, build_handler, fake_tts) -> None:
        instance = await build_handler()
        instance._overlay_available = lambda: False
        await instance.handle(QueuedMessage("alice", "hello"))
        assert fake_tts.calls == []

    async def test_cancelled_synthesis_deletes_late_wav(
        self, build_handler, fake_tts, tmp_path, monkeypatch
    ) -> None:
        entered = asyncio.Event()
        release = threading.Event()
        written = threading.Event()
        loop = asyncio.get_running_loop()
        wav = tmp_path / "cancelled.wav"

        def slow_synthesis(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=5)
            wav.touch()
            written.set()
            return wav

        monkeypatch.setattr(fake_tts, "save_wav", slow_synthesis)
        instance = await build_handler()
        pending = asyncio.create_task(instance.handle(QueuedMessage("alice", "hello")))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            release.set()
        assert await asyncio.to_thread(written.wait, 5)
        for _ in range(100):
            if not wav.exists():
                break
            await asyncio.sleep(0.01)
        assert not wav.exists()

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


class TestBroadcastFailureLeavesNoOrphans:
    """A failed broadcast must not leave an MP3 nobody will ever delete.

    Generated MP3s are deleted by exactly one mechanism: the browser plays the
    clip and sends {"done": "<name>.mp3"} back over the WebSocket, and the
    server unlinks it then.  If the broadcast never reaches a browser, that
    message never arrives, so a file left behind at this point stays in
    audio_dir until somebody deletes it by hand.  Both paths that produce a
    clip — real synthesis and the copied notification sound for an emote-only
    message — are checked, because they used to clean up differently.
    """

    async def test_synthesis_path_removes_the_mp3(
        self, build_handler, audio_dir
    ) -> None:
        instance = await build_handler(broadcast_raises=RuntimeError)

        with pytest.raises(RuntimeError):
            await instance.handle(QueuedMessage(username="alice", text="hello there"))

        assert list(audio_dir.iterdir()) == []

    async def test_emote_only_path_removes_the_mp3(
        self, build_handler, audio_dir, tmp_path
    ) -> None:
        sound = tmp_path / "ping.mp3"
        sound.write_bytes(b"fake-mp3")
        instance = await build_handler(
            emote_sound_paths=[str(sound)], broadcast_raises=RuntimeError
        )

        with pytest.raises(RuntimeError):
            await instance.handle(QueuedMessage(username="alice", text="🎉"))

        assert list(audio_dir.iterdir()) == []
        # The copied-from source sound is not the handler's to delete
        assert sound.exists()

    async def test_a_half_written_copy_is_removed(
        self, build_handler, audio_dir, tmp_path, monkeypatch
    ) -> None:
        """A copy that dies part-way leaves no truncated MP3 behind.

        shutil.copyfile writes the destination incrementally, so a full disk
        or a cancelled task can leave a file that exists but is unplayable.  The
        real copy cannot be made to fail on demand, so it is replaced with one
        that writes a few bytes and then raises, which is the same state on
        disk.
        """
        sound = tmp_path / "ping.mp3"
        sound.write_bytes(b"fake-mp3")

        def half_write(src, dst) -> None:
            Path(dst).write_bytes(b"trunc")
            raise OSError("No space left on device")

        monkeypatch.setattr(handler_module.shutil, "copyfile", half_write)
        instance = await build_handler(emote_sound_paths=[str(sound)])

        with pytest.raises(OSError):
            await instance.handle(QueuedMessage(username="alice", text="🎉"))

        assert list(audio_dir.iterdir()) == []


class TestUndeliveredAudioIsDeleted:
    """A clip nobody received is deleted straight away, not left behind.

    This is the same orphan problem as the class above, arriving by the quieter
    route: the broadcast works perfectly, it just reaches nobody, because the
    overlay is closed.  There is exactly one thing that ever deletes a generated
    MP3 in normal operation — a browser plays it and sends {"done": "<name>"}
    back — so with no browser attached that message is never coming and the file
    stays in the audio directory forever.  A bot left running while the stream
    is offline used to add one file per spoken message, on a Docker volume, with
    nothing logged and nothing cleaning up until the next restart.

    `broadcast` returning the number of clients it reached is what makes the
    difference visible to the handler at all; both paths that produce a clip are
    checked here because both hand their file to the same _publish.
    """

    async def test_synthesis_path_removes_the_unheard_mp3(
        self, build_handler, broadcasts, audio_dir
    ) -> None:
        instance = await build_handler(delivered=0)

        await instance.handle(QueuedMessage(username="alice", text="hello there"))

        # The event was still built and sent — this is not "nothing happened"
        assert len(broadcasts) == 1
        assert list(audio_dir.iterdir()) == []

    async def test_emote_only_path_removes_the_unheard_mp3(
        self, build_handler, broadcasts, audio_dir, tmp_path
    ) -> None:
        sound = tmp_path / "ping.mp3"
        sound.write_bytes(b"fake-mp3")
        instance = await build_handler(emote_sound_paths=[str(sound)], delivered=0)

        await instance.handle(QueuedMessage(username="alice", text="🎉"))

        assert len(broadcasts) == 1
        assert list(audio_dir.iterdir()) == []
        # The copied-from source sound is not the handler's to delete
        assert sound.exists()
