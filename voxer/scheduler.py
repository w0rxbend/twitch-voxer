import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

LOGGER: logging.Logger = logging.getLogger(__name__)

MESSAGES: list["ScheduledMessage"]


@dataclass
class ScheduledMessage:
    chat_text: str
    tts_text: str | None = None  # None = post to chat only, no TTS


MESSAGES = [
    ScheduledMessage(
        chat_text="Вітаємо всіх у нашій спільноті! Радий бачити вас тут! 👋",
        tts_text="Вітаємо всіх у нашій спільноті! Радий бачити вас тут!",
    ),
    ScheduledMessage(
        chat_text="Не забувай підписатись на діскорд! discord - link: https://discord.gg/TtxS5JTw2J",
        tts_text="Підписуйся на канал та вмикай сповіщення, щоб не пропустити стріми!",
    ),
    ScheduledMessage(
        chat_text="Підписуйся на канал та вмикай сповіщення, щоб не пропустити стріми! 🔔",
        tts_text="Підписуйся на канал та вмикай сповіщення, щоб не пропустити стріми!",
    ),
]


class Scheduler:
    def __init__(
        self,
        send_chat: Callable[[str], Awaitable[None]],
        handle_message: Callable[[str, str], Awaitable[None]],
        interval: int = 600,
        initial_delay: int = 10,
    ) -> None:
        self._send_chat = send_chat
        self._handle_message = handle_message
        self._interval = interval
        self._initial_delay = initial_delay
        self._index = 0

    async def run(self) -> None:
        LOGGER.info(
            "Scheduler ready — first message in %ds, then every %ds",
            self._initial_delay,
            self._interval,
        )
        await asyncio.sleep(self._initial_delay)
        while True:
            msg = MESSAGES[self._index % len(MESSAGES)]
            self._index += 1
            LOGGER.info(
                "Posting scheduled message %d/%d: %r",
                self._index,
                len(MESSAGES),
                msg.chat_text[:60],
            )
            await self._send_chat(msg.chat_text)

            await asyncio.sleep(self._interval)
