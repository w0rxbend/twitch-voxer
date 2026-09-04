"""Periodic chat message scheduler.

Posts random messages to Twitch chat without TTS. Messages are read from a
pickledb file on every cycle, so the list can be edited at runtime without
restarting the bot.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pickledb

LOGGER: logging.Logger = logging.getLogger(__name__)
SECONDS_PER_HOUR = 3600.0
DEFAULT_FREQUENCY_PER_HOUR = 1.0
# How much of a message's text the "posting" log line shows.  A scheduled
# message can be a long paragraph, and the log line exists to identify which
# message went out, not to reproduce it, so it is truncated to keep one cycle
# on one line.
LOG_TEXT_PREVIEW_CHARS: Final[int] = 60


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
        empty_retry_delay: int = 600,
        initial_delay: int = 10,
    ) -> None:
        """Initialize the scheduler with a chat callback and message database.

        Args:
            send_chat: Async callable that posts a message to Twitch chat.
                       Typically VoxBot.send_chat — injected to avoid circular imports.
            messages_path: Path to pickledb JSON file with a "messages" key containing
                           message objects with text and frequency_per_hour.
            empty_retry_delay: Seconds to wait before re-checking when the message
                               list is empty or invalid.  The normal posting cadence
                               is NOT this value — it is derived from each message's
                               frequency_per_hour.
            initial_delay: Seconds to wait before the first message (default: 10).
                           Gives the EventSub connection time to establish before posting.
        """
        self._send_chat = send_chat
        self._db = pickledb.PickleDB(str(messages_path))
        self._empty_retry_delay = empty_retry_delay
        self._initial_delay = initial_delay
        # Counts attempts, not successes: it is incremented before the send and
        # never rolled back when the send fails, so a run in which every post
        # errors still logs attempt 1, 2, 3…  The name says so to stop anyone
        # reading the log as proof that messages reached chat.
        self._post_attempts = 0

    def _parse_message(self, raw: Any, index: int) -> ScheduledMessage | None:
        if isinstance(raw, str):
            return ScheduledMessage(raw, DEFAULT_FREQUENCY_PER_HOUR)

        if not isinstance(raw, dict):
            LOGGER.warning(
                "Skipping scheduled message %d: expected string or object", index
            )
            return None

        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            LOGGER.warning("Skipping scheduled message %d: missing text", index)
            return None

        frequency = raw.get("frequency_per_hour", DEFAULT_FREQUENCY_PER_HOUR)
        try:
            frequency_per_hour = float(frequency)
        except TypeError, ValueError:
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
        except Exception:
            # Deliberately broad: run() calls this with no try of its own, and
            # run() is a task in the composition root's TaskGroup — an escaping
            # exception would cancel its siblings and take the whole bot down.
            # Log with a traceback so a bug here is diagnosable rather than
            # showing up only as a scheduler that silently stops posting.
            LOGGER.exception("Failed to load messages")
            return []

    def _choose_message(self, messages: list[ScheduledMessage]) -> ScheduledMessage:
        weights = [message.frequency_per_hour for message in messages]
        return random.choices(messages, weights=weights, k=1)[0]

    def _delay_for(self, messages: list[ScheduledMessage]) -> float:
        total_frequency_per_hour = sum(
            message.frequency_per_hour for message in messages
        )
        if total_frequency_per_hour <= 0:
            return float(self._empty_retry_delay)
        return SECONDS_PER_HOUR / total_frequency_per_hour

    async def _run_once(self) -> float:
        """Run exactly one scheduling cycle and report how long to wait after it.

        One cycle is: re-read the message list from disk, and — if the list has
        anything usable in it — pick one message by weight and try to post it.
        At most one message is posted per cycle.

        This is a separate method from run() so that it can be tested. run() is
        an endless loop around asyncio.sleep, which a test cannot enter without
        either waiting for real time to pass or patching the clock; one cycle
        is an ordinary coroutine that hands the waiting back to its caller.

        Returns:
            Seconds the caller should wait before running the next cycle: the
            cadence derived from the messages that were just read, or the
            fallback retry delay when there was nothing to post.
        """
        messages = await self._load_messages()
        if not messages:
            return float(self._empty_retry_delay)

        message = self._choose_message(messages)
        self._post_attempts += 1
        delay = self._delay_for(messages)
        LOGGER.info(
            "Posting scheduled message (attempt %d, %.2f/hour, next in %.0fs): %r",
            self._post_attempts,
            message.frequency_per_hour,
            delay,
            message.text[:LOG_TEXT_PREVIEW_CHARS],
        )
        try:
            await self._send_chat(message.text)
        except Exception:
            # A transient Twitch API failure (network blip, token refresh,
            # 500) must not kill the scheduler — and, via the TaskGroup
            # cancelling its siblings, the whole application.
            # Log and try again next cycle.
            LOGGER.exception("Failed to post scheduled message")
        return delay

    async def run(self) -> None:
        """Continuously post random scheduled messages to chat.

        Runs as one of the long-running tasks in the composition root's
        asyncio.TaskGroup (voxer/app.py).
        The initial_delay gives the bot time to finish the EventSub handshake and token
        validation before attempting to post chat messages.
        """
        LOGGER.info(
            "Scheduler ready — first message in %ds, fallback retry every %ds",
            self._initial_delay,
            self._empty_retry_delay,
        )
        await asyncio.sleep(self._initial_delay)
        while True:
            await asyncio.sleep(await self._run_once())
