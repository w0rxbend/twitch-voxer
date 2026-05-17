import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

import pickledb

LOGGER: logging.Logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        send_chat: Callable[[str], Awaitable[None]],
        messages_path: Path,
        interval: int = 600,
        initial_delay: int = 10,
    ) -> None:
        self._send_chat = send_chat
        self._db = pickledb.PickleDB(str(messages_path))
        self._interval = interval
        self._initial_delay = initial_delay
        self._index = 0

    async def _load_messages(self) -> list[str]:
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
        LOGGER.info(
            "Scheduler ready — first message in %ds, then every %ds",
            self._initial_delay,
            self._interval,
        )
        await asyncio.sleep(self._initial_delay)
        while True:
            messages = await self._load_messages()
            if messages:
                text = messages[self._index % len(messages)]
                self._index += 1
                LOGGER.info("Posting scheduled message %d: %r", self._index, text[:60])
                await self._send_chat(text)
            await asyncio.sleep(self._interval)
