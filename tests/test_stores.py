"""Unit tests for the pickledb persistence wrappers in voxer.stores."""

import asyncio
from pathlib import Path

import pickledb
import pytest

from voxer.stores import AnnounceTracker, EmoteStore, VoiceStore

# One emote cache entry per case the reader has to handle.  The three URLs of a
# complete entry deliberately differ from each other: lookup() is supposed to
# return the 2x one, and a bug that returned the 1x or 4x URL instead would
# still return a plausible-looking string, so only distinct values can catch it.
# "NoTwoX" is the entry an older or hand-edited cache file can contain — the
# fetcher always writes all three sizes, but the reader promises to treat a
# missing "url_2x" as an unknown emote rather than raising.
_EMOTE_SEED: dict[str, dict[str, str]] = {
    "PogChamp": {
        "url_1x": "https://cdn.example/emote/pog/1.0",
        "url_2x": "https://cdn.example/emote/pog/2.0",
        "url_4x": "https://cdn.example/emote/pog/3.0",
    },
    "Kappa": {
        "url_1x": "https://cdn.example/emote/kappa/1.0",
        "url_2x": "https://cdn.example/emote/kappa/2.0",
        "url_4x": "https://cdn.example/emote/kappa/3.0",
    },
    "NoTwoX": {
        "url_1x": "https://cdn.example/emote/broken/1.0",
        "url_4x": "https://cdn.example/emote/broken/3.0",
    },
}


@pytest.fixture
def voice_store(tmp_path: Path) -> VoiceStore:
    return VoiceStore(str(tmp_path / "voices.json"), ["M1", "F1"])


@pytest.fixture
def emote_db_path(tmp_path: Path) -> str:
    """Write a real emote cache file the way voxer/fetch_emotes.py writes it.

    The fetcher opens the cache with a plain ``with pickledb.PickleDB(...)``
    block and calls ``set()`` once per emote; leaving the block saves the file.
    Seeding through that same writer, instead of hand-writing the JSON these
    tests expect to read, is the whole point: if the format the fetcher
    produces ever stops matching what EmoteStore reads, these tests fail
    instead of passing against a guess that only exists in the test file.

    This is a synchronous fixture on purpose.  pickledb's methods are "dual"
    sync/async: they run immediately when no asyncio event loop is running, and
    return a coroutine when one is.  pytest sets fixtures up before it starts
    the event loop for an async test, so the sync form works here — calling the
    same code from inside an async test body would silently write nothing.
    """
    path = tmp_path / "emotes.db"
    with pickledb.PickleDB(str(path)) as db:
        for name, urls in _EMOTE_SEED.items():
            # Discard the return value: outside an event loop set() has already
            # done the work and hands back a plain bool, not an awaitable
            _ = db.set(name, urls)
    return str(path)


class TestVoiceStore:
    def test_empty_pool_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            VoiceStore(str(tmp_path / "voices.json"), [])

    async def test_assignment_is_stable(self, voice_store: VoiceStore) -> None:
        await voice_store.load()
        first = await voice_store.get_or_assign("alice")
        assert first in ("M1", "F1")
        # Same user always keeps the same voice
        for _ in range(5):
            assert await voice_store.get_or_assign("alice") == first

    async def test_assignment_survives_reload(self, tmp_path: Path) -> None:
        path = str(tmp_path / "voices.json")
        store = VoiceStore(path, ["M1", "F1"])
        await store.load()
        voice = await store.get_or_assign("bob")

        fresh = VoiceStore(path, ["M1", "F1"])
        await fresh.load()
        assert await fresh.get_or_assign("bob") == voice

    async def test_stale_voice_reassigned(self, tmp_path: Path) -> None:
        path = str(tmp_path / "voices.json")
        store = VoiceStore(path, ["OLD"])
        await store.load()
        assert await store.get_or_assign("carol") == "OLD"

        # The pool changed and "OLD" no longer exists — must reassign
        shrunk = VoiceStore(path, ["NEW"])
        await shrunk.load()
        assert await shrunk.get_or_assign("carol") == "NEW"

    async def test_missing_file_starts_empty(self, voice_store: VoiceStore) -> None:
        await voice_store.load()  # file does not exist yet — must not raise

    async def test_concurrent_assignment_single_voice(
        self, voice_store: VoiceStore
    ) -> None:
        await voice_store.load()
        voices = await asyncio.gather(
            *(voice_store.get_or_assign("dave") for _ in range(10))
        )
        assert len(set(voices)) == 1


class TestAnnounceTracker:
    async def test_first_message_claims(self, tmp_path: Path) -> None:
        tracker = AnnounceTracker(str(tmp_path / "ts.json"), window_secs=300)
        await tracker.load()
        assert await tracker.claim("alice") is True

    async def test_second_message_within_window_does_not_claim(
        self, tmp_path: Path
    ) -> None:
        tracker = AnnounceTracker(str(tmp_path / "ts.json"), window_secs=300)
        await tracker.load()
        assert await tracker.claim("alice") is True
        assert await tracker.claim("alice") is False

    async def test_claim_after_window_elapsed(self, tmp_path: Path) -> None:
        # window_secs=0 means every gap exceeds the window
        tracker = AnnounceTracker(str(tmp_path / "ts.json"), window_secs=0)
        await tracker.load()
        assert await tracker.claim("alice") is True
        await asyncio.sleep(0.01)
        assert await tracker.claim("alice") is True

    async def test_users_are_independent(self, tmp_path: Path) -> None:
        tracker = AnnounceTracker(str(tmp_path / "ts.json"), window_secs=300)
        await tracker.load()
        assert await tracker.claim("alice") is True
        assert await tracker.claim("bob") is True

    # A timestamps file can hold something that is not a number: it is plain
    # JSON that a human can edit, and a save interrupted part-way through can
    # leave a half-written value behind.  A string that is not a number raises
    # ValueError when read as a float; a value of the wrong shape entirely
    # raises TypeError.  Both must be survivable.
    @pytest.mark.parametrize(
        "junk",
        ["not-a-float", {"written": "by an older version"}],
        ids=["unparsable-string", "wrong-type"],
    )
    async def test_unreadable_timestamp_self_heals(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        junk: object,
    ) -> None:
        """A junk stored value must announce the user, warn, and repair itself.

        The repair is the point.  The bad value is only overwritten if claim()
        gets as far as the write, so the second claim returning False proves
        that the first one wrote a good timestamp rather than failing before
        it.  Were the failure left to propagate, the entry would stay broken
        and every future message from that user would raise again — which the
        handler catches and logs, so the user would go silently un-announced
        forever with nothing but a repeating traceback to explain it.
        """
        path = str(tmp_path / "ts.json")
        seed = pickledb.PickleDB(path)
        # Inside an async test pickledb's dual sync/async methods return
        # awaitables, so these must be awaited or nothing reaches the file
        await seed.set("alice", junk)
        await seed.save()

        tracker = AnnounceTracker(path, window_secs=300)
        await tracker.load()

        with caplog.at_level("WARNING"):
            assert await tracker.claim("alice") is True
        assert "alice" in caplog.text

        # Well inside the 300 s window: a False here can only mean the first
        # claim replaced the junk with a timestamp it can now read back
        assert await tracker.claim("alice") is False


class TestEmoteStore:
    async def test_load_reads_every_seeded_emote(self, emote_db_path: str) -> None:
        store = EmoteStore(emote_db_path)
        await store.load()
        # Every complete entry the fetcher wrote must come back out again
        assert store.lookup("PogChamp") is not None
        assert store.lookup("Kappa") is not None

    async def test_lookup_returns_the_2x_url(self, emote_db_path: str) -> None:
        store = EmoteStore(emote_db_path)
        await store.load()
        assert store.lookup("PogChamp") == _EMOTE_SEED["PogChamp"]["url_2x"]

    async def test_lookup_without_2x_url_is_unknown(self, emote_db_path: str) -> None:
        # The overlay drops emotes it cannot resolve; an entry that happens to
        # lack the size we want must therefore read as "unknown", not raise
        store = EmoteStore(emote_db_path)
        await store.load()
        assert store.lookup("NoTwoX") is None

    async def test_lookup_unknown_name_is_none(self, emote_db_path: str) -> None:
        store = EmoteStore(emote_db_path)
        await store.load()
        assert store.lookup("NeverSeenThisEmote") is None

    async def test_missing_file_starts_empty(self, tmp_path: Path) -> None:
        # A deployment that never ran the fetcher has no cache file at all.
        # That is not an error: the overlay simply shows no emote images.
        store = EmoteStore(str(tmp_path / "does-not-exist.db"))
        await store.load()
        assert store.lookup("PogChamp") is None

    async def test_none_path_disables_lookups(self) -> None:
        # db_path=None is how the app turns the emote feature off entirely
        store = EmoteStore(None)
        await store.load()
        assert store.lookup("PogChamp") is None
