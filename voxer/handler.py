"""Core business logic for processing Twitch chat messages into TTS audio.

Pipeline for a USER message:
  1. Bot-account filtering  — known bot usernames and "*bot*" patterns are skipped
                              (textnorm.is_bot).
  2. Emoji extraction       — Unicode emoji are stripped from the text and collected
                              as EmoteItems for the browser overlay (textnorm.extract_emojis).
  3. Emote-only short-circuit — if no text remains and the message contained only
                              Twitch emotes, play a random notification sound instead.
  4. Language detection     — langdetect classifies text as "uk" (Ukrainian) or "en"
                              (English); anything else falls back to "uk".
  5. Voice assignment       — each username gets a random voice on first message;
                              the assignment is persisted via VoiceStore.
  6. Text normalisation     — URLs replaced, abbreviations expanded, laugh tokens
                              converted to the TTS <laugh> expression tag
                              (textnorm.normalize).
  7. Announce-window check  — if more than the announce window has elapsed since
                              the user's last message, "username says:" is prepended
                              (AnnounceTracker.claim).
  8. WAV synthesis          — Supertonic TTS runs in a thread (it is CPU-bound).
  9. MP3 conversion         — ffmpeg converts the temporary WAV to MP3.
  10. WebSocket broadcast   — the MP3 URL, avatar URL, and emote list are pushed to
                              all connected browser clients.

SYSTEM messages (follows, subs, raids, cheers) skip steps 1-7 and go directly
to synthesis with a random voice in Ukrainian.

The pure text rules live in textnorm.py; persistence lives in stores.py.
This module owns only the orchestration of a message through the pipeline.
"""

import asyncio
import logging
import random
import shutil
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from langdetect import detect, LangDetectException

from .models import audio_url_for, BroadcastEvent, EmoteItem, MessageKind, QueuedMessage
from .stores import AnnounceTracker, EmoteStore, VoiceStore
from .textnorm import (
    DEFAULT_LANG,
    SUPPORTED_LANGS,
    extract_emojis,
    is_bot,
    normalize,
    rules_for,
)
from .tts import TTSService

LOGGER: logging.Logger = logging.getLogger(__name__)


class MessageHandler:
    """Orchestrates the full message-to-audio pipeline.

    Owns:
      - VoiceStore for persistent voice assignment
      - AnnounceTracker for announce-window bookkeeping
      - EmoteStore for emote name → image URL lookups
      - reference to TTSService for synthesis
      - reference to server.broadcast for WebSocket delivery
    """

    def __init__(
        self,
        *,
        tts: TTSService,
        voice_store: VoiceStore,
        announce_tracker: AnnounceTracker,
        emote_store: EmoteStore,
        audio_dir: Path,
        broadcast: Callable[[BroadcastEvent], Awaitable[None]],
        message_queue: asyncio.Queue["QueuedMessage"],
        emote_sound_paths: list[str] | None = None,
        no_announce_users: frozenset[str] | None = None,
    ) -> None:
        """Initialize the message handler.

        Args:
            tts: TTSService instance for voice synthesis.
            voice_store: Persistent username → voice assignments.
            announce_tracker: Announce-window bookkeeping per username.
            emote_store: Emote name → image URL lookups for the overlay.
            audio_dir: Directory for storing generated MP3 files.
            broadcast: Async callable to broadcast audio via WebSocket to connected clients.
            message_queue: Queue for receiving messages from the bot.
            emote_sound_paths: MP3 files to pick from randomly for emote-only messages.
            no_announce_users: Usernames that never get the announcement prefix.
        """
        LOGGER.debug("Initialising MessageHandler (audio_dir=%s)", audio_dir)
        self._tts = tts
        self._voice_store = voice_store
        self._announce_tracker = announce_tracker
        self._emote_store = emote_store
        self._audio_dir = audio_dir
        self._broadcast = broadcast
        self._message_queue = message_queue

        # Filter out configured sound paths that don't exist on disk at startup
        self._emote_sounds: list[Path] = [
            p for raw in (emote_sound_paths or []) if (p := Path(raw)).exists()
        ]
        self._no_announce_users: frozenset[str] = no_announce_users or frozenset()
        LOGGER.info("MessageHandler ready")

    async def preload_resources(self) -> None:
        """Load the three stores, which need I/O that cannot be awaited in __init__.

        Called once by the composition root before the message queue starts
        draining.  Each store tolerates a missing or broken file on its own, so
        a failure here degrades a feature (no emote images, forgotten voice
        assignments) rather than aborting startup.

        The stores are loaded once here rather than re-read on every message:
        this process is their sole reader and writer, so per-message reloads
        were pure disk I/O waste on the hot path.
        """
        await self._emote_store.load()
        await self._voice_store.load()
        await self._announce_tracker.load()

    async def _detect_lang(self, text: str) -> str:
        """Detect the language of text, returning "uk" or "en".

        langdetect is a synchronous, CPU-bound library — run in a thread so the
        event loop is not blocked during detection.  Any language other than "en"
        falls back to "uk" (the primary stream language).
        """
        try:
            lang = await asyncio.to_thread(detect, text)
            resolved = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
            LOGGER.debug("Detected lang: %s -> %s", lang, resolved)
            return resolved
        except LangDetectException:
            LOGGER.debug("Lang detection failed, defaulting to %s", DEFAULT_LANG)
            return DEFAULT_LANG

    def _resolve_emotes(
        self, emote_names: list[str], emoji_items: list[EmoteItem]
    ) -> list[EmoteItem]:
        """Merge Twitch emotes (resolved via the emote store) with Unicode emoji.

        Twitch emote names the store cannot resolve are silently dropped — the
        overlay simply won't show an image for them.
        """
        resolved = [
            EmoteItem(name=name, url=url)
            for name in emote_names
            if (url := self._emote_store.lookup(name)) is not None
        ]
        return resolved + emoji_items

    def _new_mp3_path(self) -> Path:
        """Return a fresh, unused path for one clip inside audio_dir.

        The name is a random UUID because several messages can be in flight at
        once: a name derived from the chatter or the text would collide, and one
        message would overwrite audio another was still playing.
        """
        return self._audio_dir / f"{uuid.uuid4()}.mp3"

    async def _publish(
        self,
        mp3_path: Path,
        *,
        username: str,
        avatar_url: str | None,
        emotes: list[EmoteItem],
    ) -> None:
        """Announce a finished MP3 to every connected overlay, or delete it.

        Once this returns, the file's whole future is the browser's: it lives in
        audio_dir until a client plays it and sends {"done": "<name>.mp3"} back,
        which is what makes server.py unlink it.  So if the broadcast itself
        fails, nobody will ever ask for that deletion and the file would sit
        there forever — hence the unlink on the way out.  BaseException rather
        than Exception, because a cancelled task must not leak a file either.
        """
        event = BroadcastEvent(
            audio_url=audio_url_for(mp3_path.name),
            username=username,
            avatar_url=avatar_url,
            emotes=emotes,
        )
        LOGGER.info(
            "Broadcasting audio for %s -> %s (emotes: %s)",
            username,
            mp3_path.name,
            [e.name for e in emotes],
        )
        try:
            await self._broadcast(event)
        except BaseException:
            mp3_path.unlink(missing_ok=True)
            raise

    async def _synthesize_and_broadcast(
        self,
        *,
        username: str,
        final_text: str,
        voice: str,
        lang: str,
        emotes: list[EmoteItem],
        avatar_url: str | None,
    ) -> None:
        """Synthesise final_text to MP3 and push the result to all WebSocket clients.

        Steps:
          1. Synthesise WAV via TTSService (runs in a thread — CPU-bound).
          2. Convert WAV → MP3 via ffmpeg (async subprocess).
          3. Delete the temporary WAV file (always, even on ffmpeg error).
          4. Hand the finished MP3 to _publish(), which announces it or, if the
             broadcast fails, deletes it again.
        """
        # Synthesis is synchronous and CPU-bound; run it off the event loop
        wav_path = await asyncio.to_thread(
            self._tts.save_wav, final_text, voice_name=voice, lang=lang
        )
        mp3_path = self._new_mp3_path()
        try:
            await self._tts.to_mp3(wav_path, mp3_path)
        except BaseException:
            # ffmpeg may have written a partial MP3 before failing — remove it
            # so broken files don't accumulate in audio_dir
            mp3_path.unlink(missing_ok=True)
            raise
        finally:
            # Always clean up the temporary WAV even if ffmpeg fails
            wav_path.unlink()

        await self._publish(
            mp3_path, username=username, avatar_url=avatar_url, emotes=emotes
        )

    async def _handle_system(self, message: QueuedMessage) -> None:
        """Synthesise a channel-event announcement directly, bypassing all user checks.

        Uses a random voice from the pool.  Synthesised in DEFAULT_LANG, which is
        Ukrainian, because all event announcement strings in events.py are written
        in Ukrainian.  Naming the constant rather than repeating the literal "uk"
        keeps this site in step with the language the rest of the pipeline falls
        back to, instead of quietly disagreeing with it after a future change.
        """
        LOGGER.info("Announcing system event for %s", message.username)
        await self._synthesize_and_broadcast(
            username=message.username,
            final_text=message.text,
            voice=self._voice_store.random_voice(),
            lang=DEFAULT_LANG,
            emotes=self._resolve_emotes(message.emote_names, []),
            avatar_url=message.avatar_url,
        )

    async def _handle_emote_only(
        self, message: QueuedMessage, emotes: list[EmoteItem]
    ) -> None:
        """Play a random notification sound for a message with no spoken text.

        Fires when emoji removal left nothing to synthesise — the message was
        pure emotes/emoji.  The emotes still show in the overlay alongside the
        notification sound.  With no resolvable emotes or no configured sounds,
        the message is silently skipped.
        """
        if not (emotes and self._emote_sounds):
            LOGGER.info("Skipping emote-only from %s", message.username)
            return
        sound = random.choice(self._emote_sounds)
        LOGGER.info("Emote-only from %s — playing %s", message.username, sound.name)
        mp3_path = self._new_mp3_path()
        # Copy rather than move so the source sound file is preserved for reuse.
        # Runs in a thread — file I/O would otherwise block the event loop.
        try:
            await asyncio.to_thread(shutil.copy2, sound, mp3_path)
        except BaseException:
            # copy2 can fail part-way (a full disk, a cancelled task) and leave a
            # truncated file behind, exactly as a failed ffmpeg run can — clear
            # it up the same way rather than leaving an unplayable orphan.
            mp3_path.unlink(missing_ok=True)
            raise
        await self._publish(
            mp3_path,
            username=message.username,
            avatar_url=message.avatar_url,
            emotes=emotes,
        )

    async def _handle_user(self, message: QueuedMessage) -> None:
        """Process a regular chat message through the full normalisation pipeline.

        See module docstring for the complete step-by-step description.
        """
        if is_bot(message.username):
            LOGGER.info("Skipping bot account: %s", message.username)
            return
        LOGGER.info("Handling message from %s", message.username)

        # Remove Unicode emoji from the text and collect them as overlay items
        clean_text, emoji_items = extract_emojis(message.text)
        # One merged overlay list: Twitch emotes (cache-resolved) + Unicode emoji
        emotes = self._resolve_emotes(message.emote_names, emoji_items)

        if not clean_text.strip():
            await self._handle_emote_only(message, emotes)
            return

        lang = await self._detect_lang(clean_text)
        voice = await self._voice_store.get_or_assign(message.username)
        normalized = normalize(clean_text, lang)

        # The timestamp is always recorded; the prefix is applied only when the
        # announce window elapsed AND the user is not on the no-announce list.
        announce = await self._announce_tracker.claim(message.username)
        if announce and message.username.lower() not in self._no_announce_users:
            final_text = rules_for(lang).announcement.format(
                username=message.username, text=normalized
            )
            LOGGER.debug("Announcing prefix for %s (outside window)", message.username)
        else:
            final_text = normalized

        await self._synthesize_and_broadcast(
            username=message.username,
            final_text=final_text,
            voice=voice,
            lang=lang,
            emotes=emotes,
            avatar_url=message.avatar_url,
        )

    async def handle(self, message: QueuedMessage) -> None:
        """Process a queued message via TTS and broadcast to connected clients.

        Dispatches on message.kind:
          - SYSTEM: speaks text directly with a random voice in Ukrainian — used for
            channel events (follow, sub, raid, cheer, etc.).
          - USER: applies bot filtering, language detection, persistent voice assignment,
            text normalisation, and the "username says:" announcement prefix.

        Args:
            message: The queued message to process.
        """
        if message.kind is MessageKind.SYSTEM:
            await self._handle_system(message)
        else:
            await self._handle_user(message)

    async def process_queue(self) -> None:
        """Continuously drain the message queue, invoking handle() for each QueuedMessage.

        Runs as one of the long-running tasks in the composition root's asyncio.TaskGroup.
        Errors in handle() are logged and swallowed so a bad message never kills the loop.
        task_done() is always called so Queue.join() (if ever used) doesn't hang.
        """
        while True:
            msg: QueuedMessage = await self._message_queue.get()
            try:
                LOGGER.debug(
                    "Processing queued message from %s (%s)",
                    msg.username,
                    msg.kind.name,
                )
                await self.handle(msg)
            except Exception:
                LOGGER.exception("Error processing message from %s", msg.username)
            finally:
                self._message_queue.task_done()
