import asyncio
import logging
from pathlib import Path

from twitchio import eventsub

from .bot import VoxBot, get_user_id
from .config import (
    ACCESS_TOKEN, AUDIO_DIR, BOT_USERNAME, DB_PATH, EMOTE_SOUND_PATHS, EMOTES_DB_PATH,
    MESSAGES_PATH, REFRESH_TOKEN, SCHEDULER_INITIAL_DELAY, SCHEDULER_INTERVAL,
    SERVER_HOST, SERVER_PORT, VOICES_DIR,
)
from .handler import MessageHandler
from .log import setup_logging
from .scheduler import Scheduler
from .server import AudioServer
from .tts import TTSService

LOGGER: logging.Logger = logging.getLogger(__name__)


async def run() -> None:
    """Initialize and start the Twitch TTS bot with all components.

    Wires together: TTS service, audio server, message handler, Twitch bot, and scheduler.
    Runs bot, server, scheduler, and message handler in concurrent tasks via asyncio.gather().
    """
    setup_logging()

    audio_dir = Path(AUDIO_DIR)
    audio_dir.mkdir(exist_ok=True)
    LOGGER.info("Audio dir: %s", audio_dir.resolve())

    message_queue: asyncio.Queue = asyncio.Queue()

    tts = TTSService(voices_dir=Path(VOICES_DIR))
    server = AudioServer(audio_dir=audio_dir, host=SERVER_HOST, port=SERVER_PORT)
    handler = MessageHandler(
        tts=tts,
        db_path=DB_PATH,
        audio_dir=audio_dir,
        broadcast=server.broadcast,
        message_queue=message_queue,
        emotes_db_path=EMOTES_DB_PATH,
        emote_sound_paths=EMOTE_SOUND_PATHS,
    )

    bot_id = await get_user_id(BOT_USERNAME)
    subs: list[eventsub.SubscriptionPayload] = [
        eventsub.ChatMessageSubscription(broadcaster_user_id=bot_id, user_id=bot_id)
    ]
    LOGGER.info("Bot user ID: %s", bot_id)

    async with VoxBot(bot_id=bot_id, subs=subs, handler=handler, message_queue=message_queue) as bot:
        await bot.add_token(ACCESS_TOKEN, REFRESH_TOKEN)
        scheduler = Scheduler(
            send_chat=bot.send_chat,
            messages_path=Path(MESSAGES_PATH),
            interval=SCHEDULER_INTERVAL,
            initial_delay=SCHEDULER_INITIAL_DELAY,
        )
        await asyncio.gather(
            bot.start(load_tokens=False),
            server.serve(),
            scheduler.run(),
            handler.process_queue(),
        )


def main() -> None:
    """Entry point: run the async event loop."""
    asyncio.run(run())
