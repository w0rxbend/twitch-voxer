"""Unit tests for the pickledb persistence wrappers in voxer.stores."""

import asyncio
from pathlib import Path

import pytest

from voxer.stores import AnnounceTracker, VoiceStore


@pytest.fixture
def voice_store(tmp_path: Path) -> VoiceStore:
    return VoiceStore(str(tmp_path / "voices.json"), ["M1", "F1"])


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
