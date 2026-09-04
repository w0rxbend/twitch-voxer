"""Composition root for twitch-voxer.

This module is the single place that instantiates every component and wires
their dependencies together.  Nothing here contains business logic — it only
creates objects and connects them.

Startup order matters:
  1. Logging must be configured before anything else logs.
  2. TTSService downloads the model on first run, so it starts early.
  3. The three pickledb-backed stores are loaded before the handler starts
     draining the queue, so the first message already sees emote images and
     remembered voice assignments.
  4. bot_id is fetched before the bot socket opens so subscriptions can reference it.
  5. An asyncio.TaskGroup starts the long-running coroutines; the scheduler is
     added only after ensure_authorized() so it never posts to chat without a
     user token (which would 401 on every attempt).

Shutdown:
  This module owns SIGINT (Ctrl-C) and SIGTERM (`docker stop`) while the
  components are running.  A signal cancels run()'s own task, the TaskGroup
  cancels the tasks it started, and every `async with` block gets to run its
  exit code.  A second signal skips the politeness and ends the process.

Authorization flow:
  Only TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are required.  bot.start()
  brings up twitchio's web adapter (/oauth on OAUTH_PORT); ensure_authorized()
  loads persisted tokens from TOKEN_FILE, falls back to env seed tokens, and
  as a last resort opens the browser for the one-time OAuth grant.
"""

import asyncio
import logging
import signal
from pathlib import Path

from .bot import VoxBot, get_user_id
from .config import (
    ANNOUNCE_WINDOW_SECS,
    AUDIO_DIR,
    AUDIO_MAX_AGE_SECS,
    AUDIO_SWEEP_INTERVAL_SECS,
    BOT_USERNAME,
    DB_PATH,
    EMOTE_SOUND_PATHS,
    EMOTES_DB_PATH,
    MESSAGE_QUEUE_MAXSIZE,
    MESSAGES_PATH,
    NO_ANNOUNCE_USERS,
    SCHEDULER_EMPTY_RETRY_DELAY,
    SCHEDULER_INITIAL_DELAY,
    SERVER_HOST,
    SERVER_PORT,
    TIMESTAMPS_DB_PATH,
    TOKEN_FILE,
    VOICES_DIR,
    WS_SEND_TIMEOUT,
    validate_config,
)
from .handler import MessageHandler
from .log import setup_logging
from .models import QueuedMessage
from .scheduler import Scheduler
from .server import AudioServer, reap_audio, sweep_audio_dir
from .stores import AnnounceTracker, EmoteStore, VoiceStore
from .tts import TTSService

LOGGER: logging.Logger = logging.getLogger(__name__)


def _prepare_runtime_dirs(audio_dir: Path, token_file: Path) -> None:
    """Create the directories the bot writes into, before anything writes there.

    Two directories have to exist before startup can continue: the one MP3s are
    written to, and the one holding the OAuth token file that twitchio saves the
    first grant into.  Both are configurable (``VOXER_AUDIO_DIR`` and
    ``VOXER_TOKEN_FILE``), and a Docker deployment can point either at a path
    several levels deep on an empty volume.

    ``parents=True`` means "create any missing parent directories too", so a
    value like ``/data/voxer/audio`` works even when ``/data`` is empty; without
    it, Python raises ``FileNotFoundError``.  ``exist_ok=True`` means an already
    existing directory is not an error, which is the normal case on every run
    after the first.

    This lives in its own function rather than inline in :func:`run` so it can be
    tested: ``run()`` opens a Twitch WebSocket and downloads a ~100 MB speech
    model, so nothing inside it can be exercised directly.
    """
    audio_dir.mkdir(parents=True, exist_ok=True)
    token_file.parent.mkdir(parents=True, exist_ok=True)


async def _authorize_and_subscribe(bot: VoxBot, bot_id: str) -> None:
    """Take a freshly started bot all the way to "allowed to act on the channel".

    Three awaits that only make sense in this order, which is why they live
    together in one named function rather than as loose statements in the middle
    of the task group:

      1. ``wait_until_ready()`` — the bot has logged in and its OAuth web
         adapter is accepting requests.  Nothing below can happen before that.
      2. ``ensure_authorized()`` — block until a *user* token exists.  It is
         loaded from TOKEN_FILE, or seeded from the environment, or, on a first
         run, obtained by opening the browser for the one-time grant, which
         means this await can sit here for as long as it takes a human to click
         "Authorize".
      3. ``subscribe_for(bot_id)`` — register the EventSub subscriptions for the
         bot's own channel.  Conduit subscriptions expire after 72 hours of
         downtime, so this runs on every boot; re-registering something that is
         still alive is tolerated by Twitch and costs nothing.

    Returning from this function is the signal the caller waits for before
    starting anything that talks to chat.
    """
    await bot.wait_until_ready()
    await bot.ensure_authorized()
    await bot.subscribe_for(bot_id)


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

    # Both the audio output dir and the token file's dir must exist before
    # anything writes into them — see _prepare_runtime_dirs for why.
    audio_dir = Path(AUDIO_DIR)
    _prepare_runtime_dirs(audio_dir, Path(TOKEN_FILE))
    LOGGER.info("Audio dir: %s", audio_dir.resolve())

    # MP3s are normally deleted when the overlay reports playback done, but
    # files leak when no client is connected or the process is restarted
    # mid-playback.  Everything in audio_dir is ephemeral and nothing was
    # playing a moment ago, so the boot sweep takes every file regardless of
    # age — that is what sweep_audio_dir's default minimum age of 0 means.
    leftovers = sweep_audio_dir(audio_dir)
    if leftovers:
        LOGGER.info("Removed %d leftover audio file(s) from previous runs", leftovers)

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
    server = AudioServer(
        audio_dir=audio_dir,
        host=SERVER_HOST,
        port=SERVER_PORT,
        send_timeout=WS_SEND_TIMEOUT,
    )

    # Persistence: each store owns one pickledb file.  The engine is the single
    # source of truth for which voices exist, so the pool comes from it.
    voice_store = VoiceStore(DB_PATH, tts.voice_names)
    announce_tracker = AnnounceTracker(TIMESTAMPS_DB_PATH, ANNOUNCE_WINDOW_SECS)
    emote_store = EmoteStore(EMOTES_DB_PATH)

    # Reading a pickledb file is I/O, so it has to be awaited, and `async def
    # __init__` is not valid Python — a store cannot fill itself in its own
    # constructor.  The loads therefore happen here, right under the lines that
    # created the objects: whatever builds a component is what starts it.  They
    # must finish before the handler begins draining the queue, and it is easier
    # to see that they do when both facts are on the same screen.
    # Each store tolerates a missing or unreadable file on its own, so a failure
    # here costs a feature — no emote images, forgotten voice assignments —
    # rather than aborting startup.
    await emote_store.load()
    await voice_store.load()
    await announce_tracker.load()

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

    # Resolve the numeric Twitch user ID for the bot account.  Only needs the
    # app credentials, so it works before any user token exists.
    bot_id = await get_user_id(BOT_USERNAME)
    LOGGER.info("Bot user ID: %s", bot_id)

    # Subscriptions are not passed in here: they are registered by
    # subscribe_for() once a user token exists (first run via the OAuth
    # callback, later runs right after ensure_authorized below).
    async with VoxBot(bot_id=bot_id, message_queue=message_queue) as bot:
        scheduler = Scheduler(
            send_chat=bot.send_chat,
            messages_path=Path(MESSAGES_PATH),
            empty_retry_delay=SCHEDULER_EMPTY_RETRY_DELAY,
            initial_delay=SCHEDULER_INITIAL_DELAY,
        )

        # From here on, "stop the program" is this function's job.  It used to
        # belong to uvicorn by accident: uvicorn.Server.serve() replaces the
        # process-wide SIGINT/SIGTERM handlers with its own, and server.serve()
        # is one of the tasks started below.  On `docker stop` uvicorn restored
        # the default handler and re-sent SIGTERM to the process, which killed
        # it on the spot — inside the server task, before the task group
        # unwound and before the `async with VoxBot` above could run its exit
        # code (closing the EventSub session, cleaning up a half-written audio
        # file, stopping a running ffmpeg child).  Ctrl-C did the same and
        # printed a BaseExceptionGroup traceback on the way out.  The server no
        # longer touches signals at all (see _QuietServer in voxer/server.py),
        # and these handlers take over instead.
        loop = asyncio.get_running_loop()
        # asyncio.run() always drives run() inside a task, so this is never
        # None in practice; the fallback below keeps the type checker honest.
        main_task = asyncio.current_task()
        installed: list[signal.Signals] = []
        stopping = False

        def _shutdown(sig: signal.Signals) -> None:
            """Ask the whole application to stop, in response to one signal.

            Cancelling this coroutine's own task is enough to stop everything:
            asyncio.TaskGroup reacts to its parent being cancelled by
            cancelling every task it started, and it does not treat a cancelled
            child as a failure.  So the group finishes quietly, the `async
            with` blocks around it get to run their exit code, and the process
            leaves through the bottom of run() instead of being shot in the
            head halfway through a task.
            """
            nonlocal stopping
            if stopping:
                # A second signal means the polite request is taking too long —
                # something is refusing to let go of its cancellation.  Put the
                # operating system's default behaviour back for this signal and
                # send it to ourselves again, which ends the process at once.
                # Without this, an impatient operator's only remaining option
                # would be `kill -9`.
                LOGGER.warning("Received %s again — exiting immediately", sig.name)
                signal.signal(sig, signal.SIG_DFL)
                signal.raise_signal(sig)
                return
            stopping = True
            LOGGER.info("Received %s — shutting down", sig.name)
            if main_task is not None:
                main_task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _shutdown, sig)
            except NotImplementedError:
                # Windows' asyncio event loop cannot register signal handlers.
                # There, Ctrl-C keeps Python's default behaviour of raising
                # KeyboardInterrupt, which main() turns into the same one-line
                # message; there is no SIGTERM on Windows to worry about.
                LOGGER.debug("Event loop cannot handle %s here", sig.name)
            else:
                installed.append(sig)

        try:
            # All long-running coroutines share one event loop.  None of them
            # return under normal operation.  TaskGroup (unlike asyncio.gather)
            # cancels the sibling tasks when one of them fails, so a fatal error
            # in any component shuts the whole app down cleanly.
            async with asyncio.TaskGroup() as tg:
                # bot.start() logs in, loads TOKEN_FILE, brings up the OAuth web
                # adapter, and then serves EventSub until shutdown.
                tg.create_task(bot.start())  # Twitch EventSub WebSocket
                tg.create_task(server.serve())  # Starlette HTTP + WebSocket server
                tg.create_task(handler.process_queue())  # TTS synthesis loop

                # The boot sweep above only cleans up what a previous run left
                # behind.  This one keeps running: a browser that crashes or is
                # refreshed in the middle of a clip never sends the "done"
                # message that deletes it, so without a periodic sweep those
                # files stay in audio_dir for the rest of the stream.
                tg.create_task(
                    reap_audio(
                        audio_dir,
                        AUDIO_SWEEP_INTERVAL_SECS,
                        AUDIO_MAX_AGE_SECS,
                        server.outstanding_files,
                    )
                )

                # Log in, get a user token, register the channel subscriptions.
                await _authorize_and_subscribe(bot, bot_id)

                # The scheduler posts to chat, which needs the user token — start
                # it only after authorization so it never 401s.
                tg.create_task(scheduler.run())  # periodic chat message poster
        except* asyncio.CancelledError:
            # Only reachable when _shutdown above cancelled us, because nothing
            # else cancels this task.  A shutdown we asked for is not a crash,
            # so it is swallowed here; letting it escape would print the same
            # BaseExceptionGroup traceback this step exists to remove.  Errors
            # of any other type are not caught and still propagate.
            LOGGER.info("All components stopped")
        finally:
            # Hand the signals back before leaving, so that anything that runs
            # after this point (the bot's own shutdown, below) reacts to Ctrl-C
            # the ordinary way instead of calling a handler whose work is done.
            for sig in installed:
                loop.remove_signal_handler(sig)


def main() -> None:
    """Entry point: run the async event loop.

    The two clauses below exist so that the two endings an operator is likely
    to meet each produce one readable line rather than a stack trace.
    """
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        # A Ctrl-C that arrived before run() installed its own handlers — while
        # the speech model was downloading, for example.  Python's default
        # handler turns it into this exception.  Nothing is wrong, so there is
        # nothing to show a stack trace for.
        LOGGER.info("Shutting down")
    except RuntimeError as exc:
        # validate_config() reports an unusable configuration by raising
        # RuntimeError with a message naming the environment variable at fault.
        # That is something for the operator to fix in their .env file, not a
        # bug to debug, so print the sentence and exit non-zero (SystemExit
        # with a string prints it to stderr and sets the exit status to 1).
        raise SystemExit(f"Configuration error: {exc}") from exc
