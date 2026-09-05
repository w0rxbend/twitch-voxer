"""Core business logic for processing Twitch chat messages into TTS audio.

Pipeline for a USER message:
  1. Bot-account filtering  — known bot usernames and "*bot*" patterns are skipped
                              (textnorm.is_bot).
  2. Emoji extraction       — Unicode emoji are stripped from the text and collected
                              as EmoteItems for the browser overlay (textnorm.extract_emojis).
  3. Emote-only short-circuit — when step 2 leaves no speakable text at all —
                              a message of only Twitch emotes, only Unicode
                              emoji, or only whitespace — a random notification
                              sound is played instead, and the emotes still go
                              to the overlay.  Nothing at all is played when no
                              emote resolved to an image, or when no sound
                              files are configured (VOXER_EMOTE_SOUND_PATHS).
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
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import partial
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
    limit_speech,
    rules_for,
    sanitize_text,
)
from .tts import TTSService

LOGGER: logging.Logger = logging.getLogger(__name__)


class MessageHandler:
    """Orchestrates the full message-to-audio pipeline.

    Every collaborator is handed in by the composition root; this class creates
    none of them and manages the lifecycle of none of them:
      - VoiceStore for persistent voice assignment
      - AnnounceTracker for announce-window bookkeeping
      - EmoteStore for emote name → image URL lookups
      - TTSService for synthesis
      - server.broadcast for WebSocket delivery

    The three stores arrive already loaded, and are read from memory for the
    rest of the process's life rather than re-read per message: this process is
    their sole reader and writer, so a reload on the hot path would be disk I/O
    that can never return anything new.
    """

    def __init__(
        self,
        *,
        tts: TTSService,
        voice_store: VoiceStore,
        announce_tracker: AnnounceTracker,
        emote_store: EmoteStore,
        audio_dir: Path,
        broadcast: Callable[[BroadcastEvent], Awaitable[int]],
        message_queue: asyncio.Queue["QueuedMessage"],
        emote_sound_paths: list[str] | None = None,
        sound_paths: dict[str, Path] | None = None,
        no_announce_users: frozenset[str] | None = None,
        max_text_chars: int = 500,
        max_speech_chars: int = 1000,
        max_message_age_secs: float = 60.0,
        overlay_available: Callable[[], bool] | None = None,
    ) -> None:
        """Initialize the message handler.

        Args:
            tts: TTSService instance for voice synthesis.
            voice_store: Persistent username → voice assignments.
            announce_tracker: Announce-window bookkeeping per username.
            emote_store: Emote name → image URL lookups for the overlay.
            audio_dir: Directory for storing generated MP3 files.
            broadcast: Async callable that pushes the event to every connected
                       overlay and returns how many of them received it.
            message_queue: Queue for receiving messages from the bot.
            emote_sound_paths: MP3 files to pick from randomly for emote-only messages.
            sound_paths: Predefined soundboard names mapped to reusable MP3 files.
            no_announce_users: Usernames that never get the announcement prefix.
                               Matched case-insensitively; see below.
        """
        LOGGER.debug("Initialising MessageHandler (audio_dir=%s)", audio_dir)
        self._tts = tts
        self._voice_store = voice_store
        self._announce_tracker = announce_tracker
        self._emote_store = emote_store
        self._audio_dir = audio_dir
        self._broadcast = broadcast
        self._message_queue = message_queue
        self._max_text_chars = max_text_chars
        self._max_speech_chars = max_speech_chars
        self._max_message_age_secs = max_message_age_secs
        self._overlay_available = overlay_available
        self._sound_paths = dict(sound_paths or {})
        self._synthesis_lock = asyncio.Lock()
        self._language_lock = asyncio.Lock()

        # Filter out configured sound paths that don't exist on disk at startup
        self._emote_sounds: list[Path] = [
            p
            for raw in (emote_sound_paths or [])
            if (p := Path(raw)).is_file() and p.suffix.lower() == ".mp3"
        ]
        # Lower-cased here, at the boundary, because the comparison further down
        # lower-cases the incoming username and would otherwise never match a
        # capitalised entry.  config.py already lower-cases what it passes in, so
        # nothing changes for the running bot; the point is that the guarantee
        # now belongs to this class.  Before, a caller that built the set any
        # other way — a test, a future second composition root — silently got the
        # opposite behaviour, with no error to say why the prefix kept appearing.
        self._no_announce_users: frozenset[str] = frozenset(
            user.lower() for user in (no_announce_users or ())
        )
        LOGGER.info("MessageHandler ready")

    async def _detect_lang(self, text: str) -> str:
        """Detect the language of text, returning "uk" or "en".

        langdetect is a synchronous, CPU-bound library — run in a thread so the
        event loop is not blocked during detection.  Any language other than "en"
        falls back to "uk" (the primary stream language).
        """
        try:
            async with self._language_lock:
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
        return (resolved + emoji_items)[:32]

    @staticmethod
    async def _file_job(function, *args, cleanup_path: Path | None = None, **kwargs):
        """Dispose of thread output after cancellation, when writing is finished."""
        worker = asyncio.get_running_loop().run_in_executor(
            None, partial(function, *args, **kwargs)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:

            def discard_result(completed: asyncio.Future) -> None:
                try:
                    result = completed.result()
                except BaseException:
                    if cleanup_path is not None:
                        cleanup_path.unlink(missing_ok=True)
                else:
                    path = cleanup_path if cleanup_path is not None else Path(result)
                    path.unlink(missing_ok=True)

            worker.add_done_callback(discard_result)
            raise

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

        When at least one browser receives the event, the file's whole future is
        that browser's: it lives in audio_dir until the clip has played and the
        client sends {"done": "<name>.mp3"} back, which is what makes server.py
        unlink it.

        Two cases mean that message is never coming, and both end with the file
        deleted here instead of sitting in audio_dir forever:

          - the broadcast itself failed, so nothing was told about the file.
            Caught as BaseException rather than Exception, because a cancelled
            task must not leak a file either.
          - the broadcast succeeded but reached nobody.  This is the ordinary
            state of the bot whenever the overlay is closed — the stream is
            offline, or OBS has not been started — so every message spoken in
            that state used to add one more permanently orphaned MP3, on a
            Docker volume, with nothing to log it and nothing to clean it up
            until the next restart.
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
            delivered = await self._broadcast(event)
        except BaseException:
            mp3_path.unlink(missing_ok=True)
            raise
        if delivered == 0:
            LOGGER.debug(
                "No overlay client received %s — deleting it now", mp3_path.name
            )
            mp3_path.unlink(missing_ok=True)

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
        async with self._synthesis_lock:
            wav_path = await self._file_job(
                self._tts.save_wav,
                limit_speech(final_text, self._max_speech_chars),
                voice_name=voice,
                lang=lang,
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
            wav_path.unlink(missing_ok=True)

        await self._publish(
            mp3_path, username=username, avatar_url=avatar_url, emotes=emotes
        )

    async def _handle_system(self, message: QueuedMessage) -> None:
        """Synthesise a channel-event announcement directly, bypassing all user checks.

        The voice is picked at random from the TTS engine's own pool.  That pool
        is asked for directly rather than through VoiceStore, because a channel
        event has no persistent identity to remember: nothing about this pick is
        ever written down or looked up again.  VoiceStore exists to remember
        which voice belongs to which chatter, so asking it for a throwaway value
        it never stores read as if the pick mattered beyond this one
        announcement.  TTSService is the object that knows which voices exist
        (built-ins plus anything loaded from the voices directory), so the
        question is now asked where the answer lives.

        Synthesised in DEFAULT_LANG, which is Ukrainian, because all event
        announcement strings in events.py are written in Ukrainian.  Naming the
        constant rather than repeating the literal "uk" keeps this site in step
        with the language the rest of the pipeline falls back to, instead of
        quietly disagreeing with it after a future change.
        """
        LOGGER.info("Announcing system event for %s", message.username)
        await self._synthesize_and_broadcast(
            username=message.username,
            final_text=message.text,
            voice=random.choice(self._tts.voice_names),
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
        await self._play_sound(message, sound, emotes)

    async def _play_sound(
        self, message: QueuedMessage, sound: Path, emotes: list[EmoteItem]
    ) -> None:
        """Copy a reusable clip into the normal overlay playback lifecycle."""
        mp3_path = self._new_mp3_path()
        # Copy rather than move so the source sound file is preserved for reuse.
        # Runs in a thread — file I/O would otherwise block the event loop.
        #
        # copyfile, not copy2: copy2 additionally copies the source's metadata,
        # including its modification time.  The notification sounds ship with
        # the project, so their modification time is whenever the repository was
        # checked out or the Docker image was built — days or months ago.  A
        # clip stamped with that time is born older than AUDIO_MAX_AGE_SECS, and
        # server.reap_audio, which decides what is abandoned by looking at
        # exactly that timestamp, would delete it on its next pass, quite
        # possibly while the browser was still playing it.  copyfile leaves the
        # new file stamped "now", which is what the clip's age actually is.
        try:
            await self._file_job(
                shutil.copyfile, sound, mp3_path, cleanup_path=mp3_path
            )
        except BaseException:
            # The copy can fail part-way (a full disk, a cancelled task) and
            # leave a truncated file behind, exactly as a failed ffmpeg run can
            # — clear it up the same way rather than leaving an unplayable
            # orphan.
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
        normalized = normalize(clean_text, lang, max_chars=self._max_speech_chars)
        if not normalized.strip():
            return

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
        if time.monotonic() - message.enqueued_at > self._max_message_age_secs:
            LOGGER.debug("Skipping stale queued message")
            return
        if self._overlay_available is not None and not self._overlay_available():
            return
        message = replace(
            message,
            username=sanitize_text(message.username, 100),
            text=sanitize_text(message.text, self._max_text_chars),
            emote_names=message.emote_names[:32],
        )
        if message.kind is MessageKind.SYSTEM:
            if not message.text:
                return
            await self._handle_system(message)
        elif message.kind is MessageKind.SOUND:
            if is_bot(message.username):
                return
            sound = self._sound_paths.get(message.text)
            if sound is not None:
                await self._play_sound(
                    message, sound, self._resolve_emotes(message.emote_names, [])
                )
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
