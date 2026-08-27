"""Composition root for twitch-voxer.

This module is the single place that instantiates every component and wires
their dependencies together.  Nothing here contains business logic — it only
creates objects and connects them.

Startup order matters:
  1. Logging must be configured before anything else logs.
  2. TTSService downloads the model on first run, so it starts early.
  3. MessageHandler.preload_resources() must complete before messages arrive (loads emote DB).
  4. bot_id is fetched before the bot socket opens so subscriptions can reference it.
  5. An asyncio.TaskGroup starts all four long-running coroutines concurrently.
"""

import asyncio
import logging
from pathlib import Path

from twitchio import eventsub

from .bot import VoxBot, get_user_id
from .config import (
    ACCESS_TOKEN, ANNOUNCE_WINDOW_SECS, AUDIO_DIR, BOT_USERNAME, DB_PATH,
    EMOTE_SOUND_PATHS, EMOTES_DB_PATH, MESSAGES_PATH, NO_ANNOUNCE_USERS,
    REFRESH_TOKEN, SCHEDULER_EMPTY_RETRY_DELAY, SCHEDULER_INITIAL_DELAY,
    SERVER_HOST, SERVER_PORT, TIMESTAMPS_DB_PATH, VOICES_DIR, validate_required,
)
from .handler import MessageHandler
from .log import setup_logging
from .models import QueuedMessage
from .scheduler import Scheduler
from .server import AudioServer
from .tts import TTSService

LOGGER: logging.Logger = logging.getLogger(__name__)


async def run() -> None:
    """Initialize and start the Twitch TTS bot with all components.

    Wires together: TTS service, audio server, message handler, Twitch bot, and scheduler.
    Runs bot, server, scheduler, and message handler concurrently in an asyncio.TaskGroup.
    """
    # Must happen first — every subsequent import uses logging
    setup_logging()

    # Fail fast with a complete list of missing credentials before any
    # component (which would fail later with a less helpful error) starts.
    validate_required()

    # Ensure the audio output directory exists before any MP3 is written there
    audio_dir = Path(AUDIO_DIR)
    audio_dir.mkdir(exist_ok=True)
    LOGGER.info("Audio dir: %s", audio_dir.resolve())

    # MP3s are normally deleted when the overlay reports playback done, but
    # files leak when no client is connected or the process is restarted
    # mid-playback.  Everything in audio_dir is ephemeral, so sweep it at boot.
    stale = list(audio_dir.glob("*.mp3"))
    for mp3 in stale:
        mp3.unlink(missing_ok=True)
    if stale:
        LOGGER.info("Removed %d leftover audio file(s) from previous runs", len(stale))

    # Single shared queue: VoxBot puts QueuedMessages, MessageHandler drains them.
    # Using a queue decouples fast Twitch event arrival from slow TTS synthesis.
    message_queue: asyncio.Queue[QueuedMessage] = asyncio.Queue()

    # TTSService downloads the Supertonic model on first run (~100 MB).
    # Custom voices from the voices/ dir are loaded here too.
    tts = TTSService(voices_dir=Path(VOICES_DIR))

    # AudioServer owns the Starlette app, WebSocket client set, and MP3 cleanup.
    server = AudioServer(audio_dir=audio_dir, host=SERVER_HOST, port=SERVER_PORT)

    # MessageHandler is the core business logic layer.  server.broadcast is
    # passed in so the handler never imports the server directly (loose coupling).
    handler = MessageHandler(
        tts=tts,
        db_path=DB_PATH,
        audio_dir=audio_dir,
        broadcast=server.broadcast,
        message_queue=message_queue,
        emotes_db_path=EMOTES_DB_PATH,
        emote_sound_paths=EMOTE_SOUND_PATHS,
        timestamps_db_path=TIMESTAMPS_DB_PATH,
        no_announce_users=NO_ANNOUNCE_USERS,
        announce_window_secs=ANNOUNCE_WINDOW_SECS,
    )
    # preload_resources() exists because `async def __init__` is not valid Python.
    # It loads the emotes pickledb, which requires awaiting I/O.
    await handler.preload_resources()

    # Resolve the numeric Twitch user ID for the bot account.
    # We need this before opening the EventSub socket so we can
    # reference it in the initial ChatMessageSubscription.
    bot_id = await get_user_id(BOT_USERNAME)
    # Subscribe to chat on the bot's own channel at startup so the bot can
    # hear its own messages (and so the EventSub handshake succeeds).
    # Per-channel subscriptions for other users are added in event_oauth_authorized.
    subs: list[eventsub.SubscriptionPayload] = [
        eventsub.ChatMessageSubscription(broadcaster_user_id=bot_id, user_id=bot_id)
    ]
    LOGGER.info("Bot user ID: %s", bot_id)

    async with VoxBot(bot_id=bot_id, subs=subs, message_queue=message_queue) as bot:
        # Register the stored OAuth token so the bot can authenticate immediately
        # without going through the browser OAuth flow on every start.
        await bot.add_token(ACCESS_TOKEN, REFRESH_TOKEN)

        scheduler = Scheduler(
            send_chat=bot.send_chat,
            messages_path=Path(MESSAGES_PATH),
            empty_retry_delay=SCHEDULER_EMPTY_RETRY_DELAY,
            initial_delay=SCHEDULER_INITIAL_DELAY,
        )

        # All four coroutines run concurrently on the same event loop.
        # None of them return under normal operation.  TaskGroup (unlike
        # asyncio.gather) cancels the sibling tasks when one of them fails,
        # so a fatal error in any component shuts the whole app down cleanly.
        async with asyncio.TaskGroup() as tg:
            tg.create_task(bot.start(load_tokens=False))  # Twitch EventSub WebSocket
            tg.create_task(server.serve())                # Starlette HTTP + WebSocket server
            tg.create_task(scheduler.run())               # periodic chat message poster
            tg.create_task(handler.process_queue())       # TTS synthesis loop


def main() -> None:
    """Entry point: run the async event loop."""
    asyncio.run(run())
