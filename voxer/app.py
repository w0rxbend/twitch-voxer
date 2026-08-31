"""Composition root for twitch-voxer.

This module is the single place that instantiates every component and wires
their dependencies together.  Nothing here contains business logic — it only
creates objects and connects them.

Startup order matters:
  1. Logging must be configured before anything else logs.
  2. TTSService downloads the model on first run, so it starts early.
  3. MessageHandler.preload_resources() must complete before messages arrive (loads emote DB).
  4. bot_id is fetched before the bot socket opens so subscriptions can reference it.
  5. An asyncio.TaskGroup starts the long-running coroutines; the scheduler is
     added only after ensure_authorized() so it never posts to chat without a
     user token (which would 401 on every attempt).

Authorization flow:
  Only TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are required.  bot.start()
  brings up twitchio's web adapter (/oauth on OAUTH_PORT); ensure_authorized()
  loads persisted tokens from TOKEN_FILE, falls back to env seed tokens, and
  as a last resort opens the browser for the one-time OAuth grant.
"""

import asyncio
import logging
from pathlib import Path

from .bot import VoxBot, get_user_id
from .config import (
    ANNOUNCE_WINDOW_SECS, AUDIO_DIR, BOT_USERNAME, DB_PATH, EMOTE_SOUND_PATHS,
    EMOTES_DB_PATH, MESSAGE_QUEUE_MAXSIZE, MESSAGES_PATH, NO_ANNOUNCE_USERS,
    SCHEDULER_EMPTY_RETRY_DELAY, SCHEDULER_INITIAL_DELAY, SERVER_HOST,
    SERVER_PORT, TIMESTAMPS_DB_PATH, TOKEN_FILE, VOICES_DIR, validate_config,
)
from .handler import MessageHandler
from .log import setup_logging
from .models import QueuedMessage
from .scheduler import Scheduler
from .server import AudioServer
from .stores import AnnounceTracker, EmoteStore, VoiceStore
from .tts import TTSService

LOGGER: logging.Logger = logging.getLogger(__name__)


async def run() -> None:
    """Initialize and start the Twitch TTS bot with all components.

    Wires together: TTS service, audio server, message handler, Twitch bot, and scheduler.
    Runs them concurrently in an asyncio.TaskGroup.
    """
    # Must happen first — every subsequent import uses logging
    setup_logging()

    # Fail fast with a complete list of missing credentials before any
    # component (which would fail later with a less helpful error) starts.
    validate_config()

    # Ensure the audio output directory exists before any MP3 is written there
    audio_dir = Path(AUDIO_DIR)
    audio_dir.mkdir(exist_ok=True)
    LOGGER.info("Audio dir: %s", audio_dir.resolve())

    # The token file's directory must exist before twitchio saves the first grant
    Path(TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)

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
    # It is bounded so the overlay cannot fall arbitrarily far behind live chat
    # during a burst — see MESSAGE_QUEUE_MAXSIZE for the drop policy.
    message_queue: asyncio.Queue[QueuedMessage] = asyncio.Queue(
        maxsize=MESSAGE_QUEUE_MAXSIZE
    )

    # TTSService downloads the Supertonic model on first run (~100 MB).
    # Custom voices from the voices/ dir are loaded here too.
    tts = TTSService(voices_dir=Path(VOICES_DIR))

    # AudioServer owns the Starlette app, WebSocket client set, and MP3 cleanup.
    server = AudioServer(audio_dir=audio_dir, host=SERVER_HOST, port=SERVER_PORT)

    # Persistence: each store owns one pickledb file.  The engine is the single
    # source of truth for which voices exist, so the pool comes from it.
    voice_store = VoiceStore(DB_PATH, tts.voice_names)
    announce_tracker = AnnounceTracker(TIMESTAMPS_DB_PATH, ANNOUNCE_WINDOW_SECS)
    emote_store = EmoteStore(EMOTES_DB_PATH)

    # MessageHandler is the core business logic layer.  server.broadcast is
    # passed in so the handler never imports the server directly (loose coupling).
    handler = MessageHandler(
        tts=tts,
        voice_store=voice_store,
        announce_tracker=announce_tracker,
        emote_store=emote_store,
        audio_dir=audio_dir,
        broadcast=server.broadcast,
        message_queue=message_queue,
        emote_sound_paths=EMOTE_SOUND_PATHS,
        no_announce_users=NO_ANNOUNCE_USERS,
    )
    # preload_resources() exists because `async def __init__` is not valid Python.
    # It loads the three pickledb-backed stores, which requires awaiting I/O.
    await handler.preload_resources()

    # Resolve the numeric Twitch user ID for the bot account.  Only needs the
    # app credentials, so it works before any user token exists.
    bot_id = await get_user_id(BOT_USERNAME)
    LOGGER.info("Bot user ID: %s", bot_id)

    # No constructor-time subscriptions: they are registered by subscribe_for()
    # once a user token exists (first run via the OAuth callback, later runs
    # right after ensure_authorized below).
    async with VoxBot(bot_id=bot_id, subs=[], message_queue=message_queue) as bot:
        scheduler = Scheduler(
            send_chat=bot.send_chat,
            messages_path=Path(MESSAGES_PATH),
            empty_retry_delay=SCHEDULER_EMPTY_RETRY_DELAY,
            initial_delay=SCHEDULER_INITIAL_DELAY,
        )

        # All long-running coroutines share one event loop.  None of them
        # return under normal operation.  TaskGroup (unlike asyncio.gather)
        # cancels the sibling tasks when one of them fails, so a fatal error
        # in any component shuts the whole app down cleanly.
        async with asyncio.TaskGroup() as tg:
            # bot.start() logs in, loads TOKEN_FILE, brings up the OAuth web
            # adapter, and then serves EventSub until shutdown.
            tg.create_task(bot.start())                   # Twitch EventSub WebSocket
            tg.create_task(server.serve())                # Starlette HTTP + WebSocket server
            tg.create_task(handler.process_queue())       # TTS synthesis loop

            # Wait for login + adapter, then block until a user token exists
            # (stored, env-seeded, or granted through the browser flow).
            await bot.wait_until_ready()
            await bot.ensure_authorized()

            # Conduit subscriptions expire after 72h of downtime — re-register
            # the bot's own channel on every boot (duplicates are tolerated).
            await bot.subscribe_for(bot_id)

            # The scheduler posts to chat, which needs the user token — start
            # it only after authorization so it never 401s.
            tg.create_task(scheduler.run())               # periodic chat message poster


def main() -> None:
    """Entry point: run the async event loop."""
    asyncio.run(run())
