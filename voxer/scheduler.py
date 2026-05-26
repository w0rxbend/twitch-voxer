"""Periodic chat message scheduler.

Posts random messages to Twitch chat without TTS. Messages are read from a
pickledb file on every cycle, so the list can be edited at runtime without
restarting the bot.
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import pickledb

LOGGER: logging.Logger = logging.getLogger(__name__)
SECONDS_PER_HOUR = 3600.0
DEFAULT_FREQUENCY_PER_HOUR = 1.0


@dataclass(frozen=True)
class ScheduledMessage:
    text: str
    frequency_per_hour: float


class Scheduler:
    """Posts random scheduled messages to Twitch chat."""

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
                           message objects with text and frequency_per_hour.
            interval: Fallback retry delay when no messages are available.
            initial_delay: Seconds to wait before the first message (default: 10).
                           Gives the EventSub connection time to establish before posting.
        """
        self._send_chat = send_chat
        self._db = pickledb.PickleDB(str(messages_path))
        self._interval = interval
        self._initial_delay = initial_delay
        self._sent_count = 0

    def _parse_message(self, raw: Any, index: int) -> ScheduledMessage | None:
        if isinstance(raw, str):
            return ScheduledMessage(raw, DEFAULT_FREQUENCY_PER_HOUR)

        if not isinstance(raw, dict):
            LOGGER.warning("Skipping scheduled message %d: expected string or object", index)
            return None

        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            LOGGER.warning("Skipping scheduled message %d: missing text", index)
            return None

        frequency = raw.get("frequency_per_hour", DEFAULT_FREQUENCY_PER_HOUR)
        try:
            frequency_per_hour = float(frequency)
        except (TypeError, ValueError):
            LOGGER.warning(
                "Skipping scheduled message %d: invalid frequency_per_hour=%r",
                index,
                frequency,
            )
            return None

        if frequency_per_hour <= 0:
            LOGGER.warning(
                "Skipping scheduled message %d: frequency_per_hour must be positive",
                index,
            )
            return None

        return ScheduledMessage(text.strip(), frequency_per_hour)

    async def _load_messages(self) -> list[ScheduledMessage]:
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
            if not isinstance(messages, list):
                LOGGER.warning("Messages DB key must contain a list")
                return []
            parsed = [
                message
                for index, raw in enumerate(messages, start=1)
                if (message := self._parse_message(raw, index)) is not None
            ]
            if not parsed:
                LOGGER.warning("No valid scheduled messages found in DB")
            return parsed
        except Exception as exc:
            LOGGER.error("Failed to load messages: %s", exc)
            return []

    def _choose_message(self, messages: list[ScheduledMessage]) -> ScheduledMessage:
        weights = [message.frequency_per_hour for message in messages]
        return random.choices(messages, weights=weights, k=1)[0]

    def _delay_for(self, messages: list[ScheduledMessage]) -> float:
        total_frequency_per_hour = sum(message.frequency_per_hour for message in messages)
        if total_frequency_per_hour <= 0:
            return float(self._interval)
        return SECONDS_PER_HOUR / total_frequency_per_hour

    async def run(self) -> None:
        """Continuously post random scheduled messages to chat.

        Runs as one of the four concurrent tasks started by asyncio.gather() in __init__.py.
        The initial_delay gives the bot time to finish the EventSub handshake and token
        validation before attempting to post chat messages.
        """
        LOGGER.info(
            "Scheduler ready — first message in %ds, fallback retry every %ds",
            self._initial_delay,
            self._interval,
        )
        await asyncio.sleep(self._initial_delay)
        while True:
            messages = await self._load_messages()
            if messages:
                message = self._choose_message(messages)
                self._sent_count += 1
                delay = self._delay_for(messages)
                LOGGER.info(
                    "Posting scheduled message %d (%.2f/hour, next in %.0fs): %r",
                    self._sent_count,
                    message.frequency_per_hour,
                    delay,
                    message.text[:60],
                )
                await self._send_chat(message.text)
            else:
                delay = float(self._interval)
            await asyncio.sleep(delay)
