"""Periodic chat message scheduler.

Posts rotating messages to Twitch chat on a fixed interval without TTS.
Messages are read from a pickledb file on every cycle, so the list can be
edited at runtime without restarting the bot.

Round-robin selection: an internal counter is incremented after each post
and the message is picked by `counter % len(messages)`, ensuring even
distribution across all entries regardless of how many are in the list.
"""

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

import pickledb

LOGGER: logging.Logger = logging.getLogger(__name__)


class Scheduler:
    """Posts rotating messages to Twitch chat at a configurable interval."""

    def __init__(
        self,
        send_chat: Callable[[str], Awaitable[None]],
        messages_path: Path,
        interval: int = 600,
        initial_delay: int = 10,
    ) -> None:
        """Initialize the scheduler with a chat callback and message database.

        Args:
            send_chat: Async callable that posts a message to Twitch chat.
                       Typically VoxBot.send_chat — injected to avoid circular imports.
            messages_path: Path to pickledb JSON file with a "messages" key containing
                           a list of strings to rotate through.
            interval: Seconds between scheduled messages (default: 600 = 10 min).
            initial_delay: Seconds to wait before the first message (default: 10).
                           Gives the EventSub connection time to establish before posting.
        """
        self._send_chat = send_chat
        self._db = pickledb.PickleDB(str(messages_path))
        self._interval = interval
        self._initial_delay = initial_delay
        # Position in the round-robin rotation; wraps via modulo on each use
        self._index = 0

    async def _load_messages(self) -> list[str]:
        """Load the current message list from the DB file.

        Re-reads the file on every call so edits to data/messages.json take effect
        on the next scheduled post without a bot restart.
        Returns an empty list (and logs a warning) if loading fails.
        """
        try:
            await self._db.load()
            messages = await self._db.get("messages")
            if not messages:
                LOGGER.warning("No messages found in DB")
                return []
            return messages
        except Exception as exc:
            LOGGER.error("Failed to load messages: %s", exc)
            return []

    async def run(self) -> None:
        """Continuously post scheduled messages to chat at the configured interval.

        Runs as one of the four concurrent tasks started by asyncio.gather() in __init__.py.
        The initial_delay gives the bot time to finish the EventSub handshake and token
        validation before attempting to post chat messages.
        """
        LOGGER.info(
            "Scheduler ready — first message in %ds, then every %ds",
            self._initial_delay,
            self._interval,
        )
        await asyncio.sleep(self._initial_delay)
        while True:
            messages = await self._load_messages()
            if messages:
                # Modulo wraps the counter back to 0 when it exceeds the list length,
                # producing a seamless round-robin even if the list changes size between cycles.
                text = messages[self._index % len(messages)]
                self._index += 1
                LOGGER.info("Posting scheduled message %d: %r", self._index, text[:60])
                await self._send_chat(text)
            await asyncio.sleep(self._interval)
