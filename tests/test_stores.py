"""Unit tests for the pickledb persistence wrappers in voxer.stores."""

import asyncio
import json
import time
import threading
from pathlib import Path

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
    """Seed the JSON cache format shared with the emote fetcher."""
    path = tmp_path / "emotes.db"
    path.write_text(json.dumps(_EMOTE_SEED))
    return str(path)


class TestVoiceStore:
    async def test_repeated_cancellation_cannot_publish_an_older_snapshot(
        self, tmp_path, monkeypatch
    ) -> None:
        from voxer import stores

        original_write = stores._write_json
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        snapshots = []

        def delayed_write(path, values):
            if "second" not in values:
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5)
            original_write(path, values)
            snapshots.append(dict(values))

        monkeypatch.setattr(stores, "_write_json", delayed_write)
        path = tmp_path / "voices.json"
        store = VoiceStore(str(path), ["M1"])
        first = asyncio.create_task(store.get_or_assign("first"))
        second = None
        try:
            await asyncio.wait_for(started.wait(), 2)
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)
            second = asyncio.create_task(store.get_or_assign("second"))
            await asyncio.sleep(0)
            assert not first.done(), "The store must own its writer until it exits"
            assert not second.done()
        finally:
            release.set()
            await asyncio.gather(
                first, *([second] if second else []), return_exceptions=True
            )
        assert first.cancelled()
        assert snapshots == [{"first": "M1"}, {"first": "M1", "second": "M1"}]
        assert json.loads(path.read_text()) == snapshots[-1]

    async def test_returning_user_retries_failed_assignment_write(
        self, tmp_path, monkeypatch
    ) -> None:
        from voxer import stores

        original_write = stores._write_json
        attempts = 0

        def transient_failure(path, values):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("temporary disk failure")
            original_write(path, values)

        monkeypatch.setattr(stores, "_write_json", transient_failure)
        monkeypatch.setattr(stores, "VOICE_RETRY_INTERVAL_SECS", 0)
        path = tmp_path / "voices.json"
        store = VoiceStore(str(path), ["M1", "F1"])
        await store.load()
        assigned = await store.get_or_assign("alice")
        assert not path.exists()
        assert await store.get_or_assign("alice") == assigned
        assert json.loads(path.read_text()) == {"alice": assigned}
        assert attempts == 2

    async def test_persistent_write_failures_back_off_but_shutdown_retries(
        self, tmp_path, monkeypatch
    ) -> None:
        from voxer import stores

        attempts = 0

        def disk_failure(path, values):
            nonlocal attempts
            attempts += 1
            raise OSError("disk unavailable")

        monkeypatch.setattr(stores, "_write_json", disk_failure)
        monkeypatch.setattr(stores, "VOICE_RETRY_INTERVAL_SECS", 60)
        store = VoiceStore(str(tmp_path / "voices.json"), ["M1"])
        await store.load()
        for _ in range(10):
            assert await store.get_or_assign("alice") == "M1"
        assert attempts == 1
        await store.flush()
        assert attempts == 2

    async def test_case_changes_keep_the_same_assignment(
        self, voice_store: VoiceStore
    ) -> None:
        await voice_store.load()
        first = await voice_store.get_or_assign("Alice")
        assert await voice_store.get_or_assign("alice") == first

    async def test_full_store_preserves_existing_users(
        self, tmp_path, monkeypatch
    ) -> None:
        from voxer import stores

        monkeypatch.setattr(stores, "MAX_VOICE_USERS", 1)
        path = tmp_path / "voices.json"
        store = VoiceStore(str(path), ["M1", "F1"])
        await store.load()
        existing = await store.get_or_assign("first")
        fallback = await store.get_or_assign("new")
        assert await store.get_or_assign("new") == fallback
        assert json.loads(path.read_text()) == {"first": existing}

    async def test_atomic_save_failure_preserves_previous_file(
        self, tmp_path, monkeypatch
    ) -> None:
        from voxer import stores

        path = tmp_path / "voices.json"
        store = VoiceStore(str(path), ["M1"])
        await store.load()
        await store.get_or_assign("first")

        def failed_replace(source, destination):
            raise OSError("disk failure")

        monkeypatch.setattr(stores.os, "replace", failed_replace)
        assert await store.get_or_assign("second") == "M1"
        assert json.loads(path.read_text()) == {"first": "M1"}
        assert list(tmp_path.glob("*.tmp")) == []

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
    @pytest.mark.parametrize(
        "bad",
        [float("nan"), float("inf"), float("-inf"), {"nested": float("nan")}, True],
    )
    async def test_unaccessed_invalid_timestamp_cannot_poison_checkpoint(
        self, tmp_path, bad
    ) -> None:
        path = tmp_path / "ts.json"
        path.write_text(json.dumps({"healthy": str(time.time()), "bad": bad}))
        tracker = AnnounceTracker(str(path), window_secs=300)
        await tracker.load()
        assert await tracker.claim("healthy") is False
        await tracker.flush()
        values = json.loads(path.read_text())
        assert set(values) == {"healthy"}
        assert float(values["healthy"]) > 0

    async def test_flush_persists_batched_timestamps(self, tmp_path) -> None:
        path = tmp_path / "ts.json"
        tracker = AnnounceTracker(str(path), window_secs=300)
        await tracker.load()
        await tracker.claim("Alice")
        assert not path.exists()
        await tracker.flush()
        fresh = AnnounceTracker(str(path), window_secs=300)
        await fresh.load()
        assert await fresh.claim("alice") is False

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "9999999999999"])
    async def test_nonfinite_or_future_timestamp_self_heals(
        self, tmp_path, bad
    ) -> None:
        path = tmp_path / "ts.json"
        path.write_text(json.dumps({"alice": bad}))
        tracker = AnnounceTracker(str(path), window_secs=300)
        await tracker.load()
        assert await tracker.claim("alice") is True
        assert await tracker.claim("alice") is False

    async def test_timestamp_cache_is_bounded(self, tmp_path, monkeypatch) -> None:
        from voxer import stores

        monkeypatch.setattr(stores, "MAX_ANNOUNCE_USERS", 2)
        path = tmp_path / "ts.json"
        tracker = AnnounceTracker(str(path), window_secs=300)
        await tracker.load()
        for username in ["one", "two", "three"]:
            await tracker.claim(username)
        await tracker.flush()
        assert set(json.loads(path.read_text())) == {"two", "three"}

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
        Path(path).write_text(json.dumps({"alice": junk}))

        tracker = AnnounceTracker(path, window_secs=300)
        await tracker.load()

        with caplog.at_level("WARNING"):
            assert await tracker.claim("alice") is True
        assert "alice" in caplog.text

        # Well inside the 300 s window: a False here can only mean the first
        # claim replaced the junk with a timestamp it can now read back
        assert await tracker.claim("alice") is False


class TestEmoteStore:
    @pytest.mark.parametrize(
        "entry",
        [
            42,
            "string",
            [],
            {"url_2x": 42},
            {"url_2x": "javascript:alert(1)"},
            {"url_2x": "https://[bad"},
        ],
    )
    async def test_malformed_emotes_are_ignored(self, tmp_path, entry) -> None:
        path = tmp_path / "emotes.json"
        path.write_text(json.dumps({"Broken": entry}))
        store = EmoteStore(str(path))
        await store.load()
        assert store.lookup("Broken") is None

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
