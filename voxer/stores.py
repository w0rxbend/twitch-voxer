"""Small persistence wrappers around pickledb files.

Each store owns exactly one pickledb file.  The read-write stores (VoiceStore,
AnnounceTracker) also own an asyncio.Lock, which keeps their check-then-update
sequences atomic by construction; EmoteStore is read-only after load and needs
none.  Extracted from MessageHandler so the handler holds behaviour, not
storage plumbing, and so each store can be constructed and tested on its own.
"""

import asyncio
import logging
import random
import time

import pickledb

LOGGER: logging.Logger = logging.getLogger(__name__)


async def _load_or_start_empty(db: pickledb.PickleDB, label: str) -> None:
    """Load a pickledb file, tolerating a missing or corrupt file.

    A broken DB file must not abort startup — the store simply starts empty
    and the next save() rewrites the file cleanly.
    """
    try:
        await db.load()
    except (FileNotFoundError, ValueError, OSError) as exc:
        LOGGER.warning("Could not load %s DB, starting empty: %s", label, exc)


class VoiceStore:
    """Persistent username → voice-name assignments.

    Assigns a random voice from the pool on a user's first message and keeps
    that assignment across restarts.  The lock serialises concurrent reads and
    writes to the same pickledb file, which is not async-safe on its own.
    """

    def __init__(self, db_path: str, voices: list[str]) -> None:
        """Create the store.

        Args:
            db_path: Path of the pickledb JSON file holding the assignments.
            voices: Pool of valid voice names to assign from.
        """
        if not voices:
            raise ValueError("VoiceStore needs a non-empty voice pool")
        self._db = pickledb.PickleDB(db_path)
        self._voices = list(voices)
        self._lock = asyncio.Lock()

    def random_voice(self) -> str:
        """Pick a voice from the pool at random.

        Used for a brand-new chatter's assignment and for channel-event
        announcements, which are never tied to a persistent identity.
        """
        return random.choice(self._voices)

    async def load(self) -> None:
        """Load the DB file once at startup (missing/corrupt files start empty)."""
        await _load_or_start_empty(self._db, "voice")

    async def get_or_assign(self, username: str) -> str:
        """Return the voice assigned to username, creating one if this is a new chatter.

        A persisted voice that is no longer in the pool (e.g. a custom voice
        whose JSON file was deleted or renamed) is replaced with a fresh
        assignment — otherwise synthesis would crash on every future message
        from that user.
        """
        async with self._lock:
            voice = await self._db.get(username)
            if voice not in self._voices:
                if voice:
                    LOGGER.warning(
                        "Voice %r for %s no longer exists — reassigning",
                        voice,
                        username,
                    )
                voice = self.random_voice()
                await self._db.set(username, voice)
                LOGGER.info("New chatter %s — assigned voice %s", username, voice)
                await self._db.save()
            else:
                LOGGER.debug("Voice for %s: %s", username, voice)
        return voice


class EmoteStore:
    """Read-only Twitch emote name → image URL cache.

    The underlying file is built by voxer/fetch_emotes.py and holds entries
    like {"PogChamp": {"url_1x": ..., "url_2x": ..., "url_4x": ...}}.  It is
    loaded once at startup and never written, so unlike the other stores it
    needs no lock.  A missing or unreadable file leaves the cache empty, which
    only means the overlay shows no images.
    """

    def __init__(self, db_path: str | None) -> None:
        """Create the store.

        Args:
            db_path: Path of the emote cache file, or None to disable lookups
                     entirely (the store then stays permanently empty).
        """
        self._db_path = db_path
        self._emotes: dict[str, dict] = {}

    async def load(self) -> None:
        """Load the emote cache once at startup (a broken file starts empty)."""
        if not self._db_path:
            return
        db = pickledb.PickleDB(self._db_path)
        await _load_or_start_empty(db, "emote")
        try:
            for key in await db.all():
                value = await db.get(key)
                if value is not None:
                    self._emotes[key] = value
        except (FileNotFoundError, ValueError, OSError) as exc:
            # Partial reads keep whatever was accumulated — some emotes beat none
            LOGGER.warning("Stopped reading emote DB early: %s", exc)
        LOGGER.info("Loaded %d emotes from %s", len(self._emotes), self._db_path)

    def lookup(self, name: str) -> str | None:
        """Return the 2x image URL for an emote name, or None if unknown.

        An entry missing "url_2x" is treated as unknown rather than raising —
        callers drop unresolvable emotes from the overlay list.
        """
        entry = self._emotes.get(name)
        return entry.get("url_2x") if entry else None


class AnnounceTracker:
    """Tracks each user's last-message time to rate-limit name announcements."""

    def __init__(self, db_path: str, window_secs: int) -> None:
        """Create the tracker.

        Args:
            db_path: Path of the pickledb JSON file holding username → timestamp.
            window_secs: Seconds of silence before a user's name is re-announced.
        """
        self._db = pickledb.PickleDB(db_path)
        self._window_secs = window_secs
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Load the DB file once at startup (missing/corrupt files start empty)."""
        await _load_or_start_empty(self._db, "timestamp")

    async def claim(self, username: str) -> bool:
        """Atomically check the announce window and record the message timestamp.

        Returns True when more than window_secs have passed since the user's
        last message (or this is their first message), meaning the caller
        should prepend the "username says:" prefix.  The timestamp is always
        updated, even when no announcement is due.

        Owning the lock here (rather than relying on callers to pair a
        separate check and update under it) makes the check-then-update atomic
        by construction, so two concurrent messages from the same user cannot
        both claim the prefix.

        A stored value that cannot be read as a number (a hand-edited file, a
        half-written save) is reported and treated as "never seen", which
        announces the user and — because the write below happens either way —
        replaces the unreadable value with a good one.  Raising instead would
        be worse than useless: the write would never be reached, so the entry
        could never repair itself and every later message from that user would
        fail the same way, leaving them permanently un-announced.
        """
        async with self._lock:
            # One timestamp for the whole operation.  The comparison and the
            # value written down are the same instant conceptually, so reading
            # the clock twice can only introduce a small disagreement between
            # them for no benefit.
            now = time.time()
            last = await self._db.get(username)
            announce = True
            if last:
                try:
                    announce = (now - float(last)) > self._window_secs
                except TypeError, ValueError:
                    LOGGER.warning(
                        "Unreadable last-seen value %r for %s — treating as new",
                        last,
                        username,
                    )
            # The timestamp goes to disk as a string rather than a number
            # because that is the shape every existing timestamps.json already
            # holds.  Writing numbers instead would not let the float() call
            # above go away — files written by older versions still contain
            # strings — so it would only widen what the reader must accept,
            # forever, in exchange for nothing.
            await self._db.set(username, str(now))
            await self._db.save()
        return announce
