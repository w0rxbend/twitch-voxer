"""Bounded JSON stores with atomic writes outside the event loop.

Files retain the original pickledb JSON shape. Voice assignments are durable
on creation; disposable announcement timestamps are checkpointed periodically.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)
MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_VOICE_USERS = 50_000
MAX_ANNOUNCE_USERS = 10_000
MAX_EMOTES = 50_000
VOICE_RETRY_INTERVAL_SECS = 5.0


def read_json_object(path: Path) -> dict[str, Any]:
    """Read a size-limited JSON object; callers decide how to handle failures."""
    with path.open("rb") as source:
        data = source.read(MAX_STORE_BYTES + 1)
    if len(data) > MAX_STORE_BYTES:
        raise ValueError(f"JSON store exceeds {MAX_STORE_BYTES} bytes")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("JSON store must contain an object")
    return value


def _write_json(path: Path, values: dict[str, Any]) -> None:
    """Replace a complete file atomically, with restrictive file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            json.dump(values, target, ensure_ascii=True, allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def _save(path: Path, values: dict[str, Any]) -> None:
    # Keep the owner's lock until the writer finishes, even during shutdown;
    # otherwise a cancelled write could later replace a newer checkpoint.
    writer = asyncio.create_task(asyncio.to_thread(_write_json, path, dict(values)))
    try:
        await asyncio.shield(writer)
    except asyncio.CancelledError:
        while not writer.done():
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                # Repeated cancellation must not release the store lock while
                # the native writer can still replace a newer snapshot.
                continue
            except Exception:
                break
        try:
            writer.result()
        except Exception:
            LOGGER.exception("JSON write failed during cancellation")
        raise


async def _load_or_start_empty(path: Path, label: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(read_json_object, path)
    except (OSError, ValueError, UnicodeError) as exc:
        LOGGER.warning("Could not load %s DB, starting empty: %s", label, exc)
        return {}


class VoiceStore:
    """Persistent, case-insensitive voice assignments with bounded growth."""

    def __init__(self, db_path: str, voices: list[str]) -> None:
        if not voices:
            raise ValueError("VoiceStore needs a non-empty voice pool")
        self._path = Path(db_path)
        self._voices = list(dict.fromkeys(voices))
        self._voice_set = frozenset(self._voices)
        self._assignments: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._dirty = False
        self._retry_at = 0.0

    async def _flush_locked(self, *, force: bool = False) -> None:
        if not self._dirty or (not force and time.monotonic() < self._retry_at):
            return
        try:
            await _save(self._path, self._assignments)
        except OSError:
            self._retry_at = time.monotonic() + VOICE_RETRY_INTERVAL_SECS
            LOGGER.exception("Could not persist voice assignments; will retry")
        else:
            self._dirty = False
            self._retry_at = 0.0

    async def flush(self) -> None:
        """Retry dirty assignments during orderly shutdown, bypassing backoff."""
        async with self._lock:
            await self._flush_locked(force=True)

    async def load(self) -> None:
        values = await _load_or_start_empty(self._path, "voice")
        self._assignments = {
            name.casefold(): voice
            for name, voice in values.items()
            if isinstance(name, str) and len(name) <= 100 and isinstance(voice, str)
        }

    async def get_or_assign(self, username: str) -> str:
        username = username.casefold()
        async with self._lock:
            voice = self._assignments.get(username)
            if voice in self._voice_set:
                await self._flush_locked()
                return voice
            if (
                username not in self._assignments
                and len(self._assignments) >= MAX_VOICE_USERS
            ):
                # Preserve existing assignments when the store is full. New
                # viewers get a repeatable voice without growing the file.
                await self._flush_locked()
                digest = hashlib.sha256(username.encode("utf-8")).digest()
                return self._voices[
                    int.from_bytes(digest[:8], "big") % len(self._voices)
                ]
            voice = random.choice(self._voices)
            self._assignments[username] = voice
            self._dirty = True
            await self._flush_locked()
            return voice


class EmoteStore:
    """Read-only, validated emote name to HTTPS image URL cache."""

    def __init__(self, db_path: str | None) -> None:
        self._path = Path(db_path) if db_path else None
        self._emotes: dict[str, str] = {}

    async def load(self) -> None:
        self._emotes.clear()
        if self._path is None:
            return
        values = await _load_or_start_empty(self._path, "emote")
        for name, entry in values.items():
            if len(self._emotes) >= MAX_EMOTES:
                break
            if (
                not isinstance(name, str)
                or len(name) > 100
                or not isinstance(entry, dict)
            ):
                continue
            url = entry.get("url_2x")
            if not isinstance(url, str) or len(url) > 2048:
                continue
            try:
                parsed = urlsplit(url)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    continue
            except ValueError:
                continue
            self._emotes[name] = url
        LOGGER.info("Loaded %d emotes from %s", len(self._emotes), self._path)

    def lookup(self, name: str) -> str | None:
        return self._emotes.get(name)


class AnnounceTracker:
    """Bounded announcement windows, checkpointed at most every 30 seconds."""

    def __init__(
        self, db_path: str, window_secs: int, *, flush_interval_secs: float = 30.0
    ) -> None:
        if window_secs < 0 or not math.isfinite(window_secs):
            raise ValueError("Announcement window must be finite and nonnegative")
        if flush_interval_secs < 0 or not math.isfinite(flush_interval_secs):
            raise ValueError("Flush interval must be finite and nonnegative")
        self._path = Path(db_path)
        self._window_secs = window_secs
        self._flush_interval_secs = flush_interval_secs
        self._timestamps: OrderedDict[str, Any] = OrderedDict()
        self._lock = asyncio.Lock()
        self._last_flush = time.monotonic()
        self._dirty = False

    async def load(self) -> None:
        values = await _load_or_start_empty(self._path, "timestamp")
        self._timestamps.clear()
        now = time.time()
        for name, value in values.items():
            if not isinstance(name, str) or len(name) > 100:
                self._dirty = True
                continue
            try:
                if isinstance(value, bool):
                    raise ValueError("boolean timestamp")
                timestamp = float(value)
                if not math.isfinite(timestamp) or timestamp < 0 or timestamp > now:
                    raise ValueError("invalid timestamp")
            except TypeError, ValueError, OverflowError:
                LOGGER.warning(
                    "Unreadable last-seen value for %s; treating as new", name
                )
                self._dirty = True
                continue
            self._timestamps[name.casefold()] = str(timestamp)
        while len(self._timestamps) > MAX_ANNOUNCE_USERS:
            self._timestamps.popitem(last=False)
            self._dirty = True

    async def _flush_locked(self) -> None:
        if not self._dirty:
            return
        try:
            await _save(self._path, self._timestamps)
        except OSError, ValueError:
            LOGGER.exception("Could not checkpoint announcement timestamps")
        else:
            self._dirty = False
        self._last_flush = time.monotonic()

    async def flush(self) -> None:
        """Persist pending timestamps; the composition root calls this at shutdown."""
        async with self._lock:
            await self._flush_locked()

    async def claim(self, username: str) -> bool:
        username = username.casefold()
        async with self._lock:
            now = time.time()
            last = self._timestamps.get(username)
            announce = True
            if last is not None:
                try:
                    previous = float(last)
                    if not math.isfinite(previous) or previous > now or previous < 0:
                        raise ValueError("invalid timestamp")
                    announce = now - previous > self._window_secs
                except TypeError, ValueError, OverflowError:
                    LOGGER.warning(
                        "Unreadable last-seen value for %s; treating as new", username
                    )
            self._timestamps[username] = str(now)
            self._timestamps.move_to_end(username)
            while len(self._timestamps) > MAX_ANNOUNCE_USERS:
                self._timestamps.popitem(last=False)
            self._dirty = True
            if time.monotonic() - self._last_flush >= self._flush_interval_secs:
                await self._flush_locked()
            return announce
