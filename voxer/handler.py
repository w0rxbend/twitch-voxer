"""Core business logic for processing Twitch chat messages into TTS audio.

Pipeline for a USER message:
  1. Bot-account filtering  — known bot usernames and "*bot*" patterns are skipped.
  2. Emoji extraction       — Unicode emoji are stripped from the text and collected
                              as EmoteItems for the browser overlay.
  3. Emote-only short-circuit — if no text remains and the message contained only
                              Twitch emotes, play a random notification sound instead.
  4. Language detection     — langdetect classifies text as "uk" (Ukrainian) or "en"
                              (English); anything else falls back to "uk".
  5. Voice assignment       — each username gets a random voice on first message;
                              the assignment is persisted to voices.json via pickledb.
  6. Text normalisation     — URLs replaced, abbreviations expanded, laugh tokens
                              converted to the TTS <laugh> expression tag.
  7. Announce-window check  — if more than ANNOUNCE_WINDOW_SECS have elapsed since
                              the user's last message, "username says:" is prepended.
  8. WAV synthesis          — Supertonic TTS runs in a thread (it is CPU-bound).
  9. MP3 conversion         — ffmpeg converts the temporary WAV to MP3.
  10. WebSocket broadcast   — the MP3 URL and emote list are pushed to all connected
                              browser clients.

SYSTEM messages (follows, subs, raids, cheers) skip steps 1-7 and go directly
to synthesis with a random voice in Ukrainian.
"""

import asyncio
import logging
import random
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

import emoji as emoji_lib
import pickledb
from langdetect import detect, LangDetectException
from .tts import TTSService

LOGGER: logging.Logger = logging.getLogger(__name__)

# ── Voice pool ────────────────────────────────────────────────────────────────

# Built-in Supertonic voice identifiers; custom voices from voices/*.json are
# appended to this list at MessageHandler construction time.
_BUILTIN_VOICES: list[str] = ["M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"]

# Default language when detection fails or returns an unsupported code.
DEFAULT_LANG: str = "uk"

# ── Bot filtering ─────────────────────────────────────────────────────────────

# Well-known Twitch bot accounts that should never be read aloud.
# Any username that *contains* "bot" (case-insensitive) is also silently skipped.
_KNOWN_BOTS: frozenset[str] = frozenset({
    "streamelements",
    "nightbot",
    "moobot",
    "streamlabs",
    "wizebot",
    "fossabot",
    "botisimo",
    "phantombot",
    "cloudbot",
    "sery_bot",
    "soundalerts",
    "dixperstats",
})

# ── i18n announcement templates ───────────────────────────────────────────────

# Templates used to prefix messages with "username says:" when the announce
# window has elapsed.  Keyed by language code detected from the message text.
_ANNOUNCEMENTS: dict[str, str] = {
    "en": "The user {username} says: {text}",
    "uk": "Користувач {username} каже: {text}",
}

# Replacement text for URLs, chosen based on the detected language of the message.
_LINK_REPLACEMENTS: dict[str, str] = {
    "en": "see link in the chat",
    "uk": "дивіться посилання в чаті",
}

# ── Regular expressions ───────────────────────────────────────────────────────

# Matches http:// and https:// URLs so they can be replaced with spoken text.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Matches common laugh expressions in English and Ukrainian/Cyrillic.
# Each match is replaced with the TTS-native <laugh> expression tag.
# The tag is also *prepended* to the normalised text so the voice starts
# laughing before it reads the rest of the message (adds comedic timing).
_LAUGH_RE = re.compile(
    r"\b(?:"
    # English laugh variants: lol, lmao, rofl, kek, xD, ww, haha, hehe …
    r"lo+l|lmf?ao|rofl|lel|kek+w?|x+d|w+w+|"
    r"a*ha+ha+(?:ha)*|he+he+(?:he)*|hi+hi+(?:hi)*|"
    # Ukrainian / Cyrillic transliteration: хаха, азаз, хіхі, лол, кек …
    r"а*ха+ха+(?:ха)*|а+хах+|аза+з+|"
    r"хі-хі|хіхі+|ха-ха|хах(?:а+)?|"
    r"кек+|лол+|гаха+|ахах+|їхіхі+"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# ── Abbreviation expansion tables ─────────────────────────────────────────────
# Longer keys must appear first in the alternation so they are tried before their prefixes.
# Without this, "gg" would match inside "ggwp" and leave "wp" un-expanded.
# The ordering is enforced by _build_abbrev_re() which sorts by key length descending.

_ABBREVS_EN: dict[str, str] = {
    "ggwp": "good game well played",
    "glhf": "good luck have fun",
    "omfg": "oh my freaking god",
    "icymi": "in case you missed it",
    "afaik": "as far as I know",
    "fwiw": "for what it's worth",
    "yolo": "you only live once",
    "tldr": "too long didn't read",
    "goat": "greatest of all time",
    "imho": "in my honest opinion",
    "iirc": "if I recall correctly",
    "asap": "as soon as possible",
    "wtf": "what the f",
    "wth": "what the heck",
    "omg": "oh my god",
    "brb": "be right back",
    "afk": "away from keyboard",
    "imo": "in my opinion",
    "fyi": "for your information",
    "tbh": "to be honest",
    "tbf": "to be fair",
    "irl": "in real life",
    "ngl": "not gonna lie",
    "idk": "I don't know",
    "idc": "I don't care",
    "nvm": "never mind",
    "ofc": "of course",
    "smh": "shaking my head",
    "ikr": "I know right",
    "lmk": "let me know",
    "btw": "by the way",
    "ftw": "for the win",
    "jk": "just kidding",
    "rn": "right now",
    "gn": "good night",
    "gm": "good morning",
    "gg": "good game",
    "gj": "good job",
    "gl": "good luck",
    "hf": "have fun",
    "wp": "well played",
    "op": "overpowered",
    "npc": "non-player character",
    "pvp": "player versus player",
    "pve": "player versus environment",
    "fps": "first person shooter",
    "pov": "point of view",
    "eta": "estimated time of arrival",
    "dm": "direct message",
}

_ABBREVS_UK: dict[str, str] = {
    # Latin abbreviations expanded into Ukrainian
    "ggwp": "гарна гра, добре зіграно",
    "glhf": "удачі та гарної гри",
    "omfg": "о боже мій",
    "icymi": "якщо пропустили",
    "afaik": "наскільки я знаю",
    "fwiw": "якщо вам цікаво",
    "yolo": "живемо один раз",
    "tldr": "занадто довго, не читав",
    "goat": "найкращий всіх часів",
    "imho": "на мою скромну думку",
    "iirc": "якщо не помиляюсь",
    "asap": "якнайшвидше",
    "wtf": "що за чорт",
    "wth": "що за таке",
    "omg": "о боже",
    "brb": "зараз повернусь",
    "afk": "від клавіатури",
    "imo": "на мою думку",
    "fyi": "до відома",
    "tbh": "чесно кажучи",
    "tbf": "якщо чесно",
    "irl": "у реальному житті",
    "ngl": "чесно кажучи",
    "idk": "не знаю",
    "idc": "мені все одно",
    "nvm": "нічого",
    "ofc": "звичайно",
    "smh": "хитаю головою",
    "ikr": "та я розумію",
    "lmk": "дай знати",
    "btw": "до речі",
    "ftw": "для перемоги",
    "jk": "жартую",
    "rn": "прямо зараз",
    "gn": "на добраніч",
    "gm": "доброго ранку",
    "gg": "гарна гра",
    "gj": "гарна робота",
    "gl": "удачі",
    "hf": "гарної гри",
    "wp": "добре зіграно",
    "op": "перекачаний",
    "npc": "не ігровий персонаж",
    "pvp": "гравець проти гравця",
    "pve": "гравець проти середовища",
    "fps": "шутер від першої особи",
    "pov": "точка зору",
    "eta": "приблизний час прибуття",
    "dm": "особисте повідомлення",
    # Native Cyrillic abbreviations
    "імхо": "на мою скромну думку",
    "афк": "від клавіатури",
    "гг": "гарна гра",
    "нз": "не знаю",
    "хз": "хто зна",
    "дк": "до речі",
}


def _build_abbrev_re(abbrevs: dict[str, str]) -> re.Pattern:
    """Compile a word-boundary regex that matches all keys in the abbreviation dict.

    Keys are sorted longest-first so the alternation tries longer patterns before
    their shorter prefixes (e.g. "ggwp" before "gg").  Without this ordering,
    the engine would greedily match the shorter key, leaving the suffix un-expanded.
    """
    keys = sorted(abbrevs, key=len, reverse=True)
    return re.compile(
        r"\b(?:" + "|".join(re.escape(k) for k in keys) + r")\b",
        re.IGNORECASE | re.UNICODE,
    )


# Pre-compiled at module load time — avoids re-compiling on every message.
_ABBREV_RE_EN: re.Pattern = _build_abbrev_re(_ABBREVS_EN)
_ABBREV_RE_UK: re.Pattern = _build_abbrev_re(_ABBREVS_UK)

# ── Twemoji URL helpers ───────────────────────────────────────────────────────

# Base URL for Twemoji PNG assets (72×72 px).  Used to build image URLs for
# Unicode emoji so the browser overlay can display them alongside Twitch emotes.
_TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"


def _emoji_url(char: str) -> str:
    """Return the Twemoji PNG URL for a single emoji character.

    Variation Selector-16 (U+FE0F) is skipped when building the codepoint
    filename because Twemoji asset filenames omit it.
    Example: "❤️" (U+2764 U+FE0F) → "2764.png", not "2764-fe0f.png".
    """
    codepoints = "-".join(f"{ord(c):x}" for c in char if ord(c) != 0xFE0F)
    return f"{_TWEMOJI_BASE}/{codepoints}.png"


def _extract_emojis(text: str) -> tuple[str, list["EmoteItem"]]:
    """Strip Unicode emoji from text and return (clean_text, emoji_items).

    The emoji library identifies all emoji positions, builds EmoteItems with
    Twemoji image URLs, then returns a cleaned string with emoji removed.
    The items are later merged with Twitch emote items to form the overlay list.
    """
    found = emoji_lib.emoji_list(text)
    items = [EmoteItem(name=e["emoji"], url=_emoji_url(e["emoji"])) for e in found]
    clean = emoji_lib.replace_emoji(text, replace="").strip()
    return clean, items


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class EmoteItem:
    """A single emote or emoji to be displayed in the browser overlay."""
    name: str  # display name or raw emoji character
    url: str   # absolute URL to the image asset


@dataclass
class BroadcastEvent:
    """Payload sent over WebSocket to the browser overlay after synthesis."""
    audio_url: str          # relative URL served by AudioServer, e.g. "/audio/<uuid>.mp3"
    username: str           # chatter's Twitch login name
    emotes: list[EmoteItem] = field(default_factory=list)  # emotes rendered alongside audio


class MessageKind(Enum):
    """Distinguishes chat messages from channel-event announcements."""
    USER = auto()    # regular chatter message — full pipeline
    SYSTEM = auto()  # follow/sub/raid/cheer — spoken directly, no announce window


@dataclass
class QueuedMessage:
    """A message waiting to be spoken via TTS."""
    username: str
    text: str
    kind: MessageKind = field(default=MessageKind.USER)
    emote_names: list[str] = field(default_factory=list)  # Twitch emote names from message fragments


# ── Helper functions ──────────────────────────────────────────────────────────

def _is_bot(username: str) -> bool:
    """Return True if the username belongs to a known bot or contains 'bot'."""
    lower = username.lower()
    return lower in _KNOWN_BOTS or "bot" in lower


def _normalize(text: str, lang: str) -> str:
    """Apply text transformations to make a chat message more speakable.

    Transformations (applied in order):
      1. Replace URLs with a language-appropriate spoken phrase.
      2. Expand abbreviations using the language-specific lookup table.
      3. Replace laugh tokens with the TTS <laugh> tag and also prepend it
         so the voice opens with laughter before reading the rest.

    Args:
        text: Cleaned message text (emoji already stripped).
        lang: Detected language code ("uk" or "en").

    Returns:
        Normalised text ready for TTS synthesis.
    """
    link_replacement = _LINK_REPLACEMENTS.get(lang, _LINK_REPLACEMENTS[DEFAULT_LANG])
    text, link_count = _URL_RE.subn(link_replacement, text)

    abbrevs = _ABBREVS_UK if lang == "uk" else _ABBREVS_EN
    abbrev_re = _ABBREV_RE_UK if lang == "uk" else _ABBREV_RE_EN
    # subn with a lambda performs a case-insensitive lookup in the dict
    text, abbrev_count = abbrev_re.subn(lambda m: abbrevs[m.group(0).lower()], text)

    text, laugh_count = _LAUGH_RE.subn("<laugh>,<laugh>,<laugh>", text)
    if link_count:
        LOGGER.debug("Replaced %d link(s)", link_count)
    if abbrev_count:
        LOGGER.debug("Expanded %d abbreviation(s)", abbrev_count)
    if laugh_count:
        LOGGER.debug("Applied %d <laugh> tag(s): %s", laugh_count, text)
        # Prepend laugh so the voice starts laughing *before* reading the message
        text = "<laugh>,<laugh>,<laugh>" + text
    return text


# ── MessageHandler ────────────────────────────────────────────────────────────

class MessageHandler:
    """Orchestrates the full message-to-audio pipeline.

    Owns:
      - voice assignment DB (pickledb, protected by _voice_lock)
      - timestamp DB for announce-window tracking (pickledb, protected by _ts_lock)
      - emote lookup dict (loaded from emotes.db at startup via preload_resources)
      - reference to TTSService for synthesis
      - reference to server.broadcast for WebSocket delivery
    """

    def __init__(
        self,
        tts: TTSService,
        db_path: str,
        audio_dir: Path,
        broadcast: Callable[[BroadcastEvent], Awaitable[None]],
        message_queue: asyncio.Queue["QueuedMessage"],
        emotes_db_path: str | None = None,
        emote_sound_paths: list[str] | None = None,
        timestamps_db_path: str = "data/timestamps.json",
        no_announce_users: frozenset[str] | None = None,
        announce_window_secs: int = 300,
    ) -> None:
        """Initialize message handler with TTS service and database.

        Args:
            tts: TTSService instance for voice synthesis.
            db_path: Path to pickledb file storing username → voice assignments.
            audio_dir: Directory for storing generated MP3 files.
            broadcast: Async callable to broadcast audio via WebSocket to connected clients.
            message_queue: Queue for receiving messages from the bot.
            emotes_db_path: Optional path to pickledb file storing emote name → image URLs.
            emote_sound_paths: MP3 files to pick from randomly for emote-only messages.
            timestamps_db_path: Path to pickledb file storing username → last-message timestamp.
            no_announce_users: Usernames that never get the announcement prefix.
            announce_window_secs: Seconds of silence before re-announcing a user's name.
        """
        LOGGER.debug("Initialising MessageHandler (db=%s, audio_dir=%s)", db_path, audio_dir)
        self._tts = tts

        # Combine built-in Supertonic voices with any custom voices loaded by TTSService
        self._voices: list[str] = _BUILTIN_VOICES + tts.custom_voice_names
        LOGGER.info("Voice pool (%d): %s", len(self._voices), self._voices)

        # pickledb files are loaded/saved explicitly; we hold one instance per DB file
        self._db = pickledb.PickleDB(db_path)           # username → voice name
        self._ts_db = pickledb.PickleDB(timestamps_db_path)  # username → last-seen timestamp

        self._audio_dir = audio_dir
        self._broadcast = broadcast
        self._message_queue = message_queue
        self._emotes_db_path = emotes_db_path

        # In-memory emote cache populated by preload_resources() after construction
        self._emotes: dict[str, dict] = {}

        # Filter out configured sound paths that don't exist on disk at startup
        self._emote_sounds: list[Path] = [
            p for raw in (emote_sound_paths or []) if (p := Path(raw)).exists()
        ]
        self._no_announce_users: frozenset[str] = no_announce_users or frozenset()
        self._announce_window_secs = announce_window_secs

        # Separate locks because voice and timestamp DBs are independent files;
        # a single shared lock would unnecessarily serialize unrelated operations.
        self._voice_lock = asyncio.Lock()
        self._ts_lock = asyncio.Lock()
        LOGGER.info("MessageHandler ready")

    async def preload_resources(self) -> None:
        """Load async resources (emotes DB) that cannot be awaited in __init__.

        Called once by the composition root before the message queue starts draining.
        Failure to load emotes is non-fatal — the overlay simply won't show images.
        """
        if self._emotes_db_path:
            self._emotes = await self._load_emotes(self._emotes_db_path)

    async def _load_emotes(self, path: str) -> dict[str, dict]:
        """Load emote name → image URL mappings from the pickledb emote cache.

        The emotes DB is built by scripts/fetch_emotes.py and contains entries like:
          {"PogChamp": {"url_1x": "...", "url_2x": "...", "url_4x": "..."}}
        """
        try:
            db = pickledb.PickleDB(path)
            await db.load()
            keys: list[str] = await db.all()
            emotes: dict[str, dict] = {}
            for key in keys:
                value = await db.get(key)
                if value is not None:
                    emotes[key] = value
            LOGGER.info("Loaded %d emotes from %s", len(emotes), path)
            return emotes
        except (FileNotFoundError, ValueError, OSError) as exc:
            LOGGER.warning("Could not load emotes DB (%s): %s", path, exc)
            return {}

    async def _get_or_assign_voice(self, username: str) -> str:
        """Return the voice assigned to username, creating one if this is a new chatter.

        The lock serialises concurrent reads and writes to the same pickledb file,
        which is not thread-safe or async-safe on its own.
        """
        async with self._voice_lock:
            await self._db.load()
            voice = await self._db.get(username)
            if not voice:
                # First message from this user — pick and persist a random voice
                voice = random.choice(self._voices)
                await self._db.set(username, voice)
                LOGGER.info("New chatter %s — assigned voice %s", username, voice)
                await self._db.save()
            else:
                LOGGER.debug("Voice for %s: %s", username, voice)
        return voice

    async def _detect_lang(self, text: str) -> str:
        """Detect the language of text, returning "uk" or "en".

        langdetect is a synchronous, CPU-bound library — run in a thread so the
        event loop is not blocked during detection.  Any language other than "en"
        falls back to "uk" (the primary stream language).
        """
        try:
            lang = await asyncio.to_thread(detect, text)
            resolved = lang if lang in _ANNOUNCEMENTS else DEFAULT_LANG
            LOGGER.debug("Detected lang: %s -> %s", lang, resolved)
            return resolved
        except LangDetectException:
            LOGGER.debug("Lang detection failed, defaulting to %s", DEFAULT_LANG)
            return DEFAULT_LANG

    async def _should_announce(self, username: str) -> bool:
        """Return True if more than announce_window_secs have passed since the user's last message.

        Must be called inside _ts_lock to avoid a TOCTOU race with _record_message.
        """
        await self._ts_db.load()
        last = await self._ts_db.get(username)
        if not last:
            # First ever message from this user
            return True
        return (time.time() - float(last)) > self._announce_window_secs

    async def _record_message(self, username: str) -> None:
        """Persist the current timestamp as the user's last-message time.

        Must be called inside _ts_lock, immediately after _should_announce(),
        so the check and update are atomic with respect to other coroutines.
        """
        await self._ts_db.set(username, str(time.time()))
        await self._ts_db.save()

    async def _synthesize_and_broadcast(
        self,
        username: str,
        final_text: str,
        voice: str,
        lang: str,
        emote_names: list[str],
        emoji_items: list[EmoteItem],
    ) -> None:
        """Synthesise final_text to MP3 and push the result to all WebSocket clients.

        Steps:
          1. Resolve Twitch emote names to image URLs via the in-memory emote cache.
          2. Synthesise WAV via TTSService (runs in a thread — CPU-bound).
          3. Convert WAV → MP3 via ffmpeg (async subprocess).
          4. Delete the temporary WAV file (always, even on ffmpeg error).
          5. Build a BroadcastEvent and call server.broadcast().

        The MP3 is written to audio_dir with a UUID filename to avoid collisions
        from concurrent messages.  The browser client deletes it after playback
        by sending {"done": "filename.mp3"} over WebSocket.
        """
        # Resolve Twitch emote names to their 2x image URLs from the emote cache
        twitch_emote_items = [
            EmoteItem(name=name, url=self._emotes[name]["url_2x"])
            for name in emote_names
            if name in self._emotes
        ]
        all_emotes = twitch_emote_items + emoji_items

        # Synthesis is synchronous and CPU-bound; run it off the event loop
        wav_path = await asyncio.to_thread(
            self._tts.save_wav, final_text, voice_name=voice, lang=lang
        )
        mp3_path = self._audio_dir / f"{uuid.uuid4()}.mp3"
        try:
            await self._tts.to_mp3(wav_path, mp3_path)
        finally:
            # Always clean up the temporary WAV even if ffmpeg fails
            wav_path.unlink()

        event = BroadcastEvent(
            audio_url=f"/audio/{mp3_path.name}",
            username=username,
            emotes=all_emotes,
        )
        LOGGER.info(
            "Broadcasting audio for %s -> %s (emotes: %s)",
            username,
            mp3_path.name,
            [e.name for e in all_emotes],
        )
        await self._broadcast(event)

    async def _handle_system(self, message: QueuedMessage) -> None:
        """Synthesise a channel-event announcement directly, bypassing all user checks.

        Uses a random voice from the pool.  Always synthesised in Ukrainian because
        all event announcement strings in events.py are written in Ukrainian.
        """
        LOGGER.info("Announcing system event for %s", message.username)
        voice = random.choice(self._voices)
        await self._synthesize_and_broadcast(
            message.username, message.text, voice, "uk", message.emote_names, []
        )

    async def _handle_user(self, message: QueuedMessage) -> None:
        """Process a regular chat message through the full normalisation pipeline.

        See module docstring for the complete step-by-step description.
        """
        if _is_bot(message.username):
            LOGGER.info("Skipping bot account: %s", message.username)
            return
        LOGGER.info("Handling message from %s", message.username)

        # Remove Unicode emoji from the text and collect them as overlay items
        clean_text, emoji_items = _extract_emojis(message.text)

        if not clean_text.strip():
            # Message was emote-only (no spoken text remains after emoji removal).
            # Play a random notification sound and show emotes in the overlay.
            twitch_emote_items = [
                EmoteItem(name=name, url=self._emotes[name]["url_2x"])
                for name in message.emote_names
                if name in self._emotes
            ]
            all_emotes = twitch_emote_items + emoji_items
            if all_emotes and self._emote_sounds:
                sound = random.choice(self._emote_sounds)
                LOGGER.info("Emote-only from %s — playing %s", message.username, sound.name)
                mp3_path = self._audio_dir / f"{uuid.uuid4()}.mp3"
                # Copy rather than move so the source sound file is preserved for reuse
                shutil.copy2(sound, mp3_path)
                await self._broadcast(BroadcastEvent(
                    audio_url=f"/audio/{mp3_path.name}",
                    username=message.username,
                    emotes=all_emotes,
                ))
            else:
                LOGGER.info("Skipping emote-only from %s", message.username)
            return

        lang = await self._detect_lang(clean_text)
        voice = await self._get_or_assign_voice(message.username)
        normalized = _normalize(clean_text, lang)

        # Lock covers both the announce check and the timestamp update so that
        # two concurrent messages from the same user don't both get the prefix.
        async with self._ts_lock:
            if (
                message.username.lower() not in self._no_announce_users
                and await self._should_announce(message.username)
            ):
                final_text = _ANNOUNCEMENTS[lang].format(
                    username=message.username, text=normalized
                )
                LOGGER.debug("Announcing prefix for %s (outside window)", message.username)
            else:
                final_text = normalized
            await self._record_message(message.username)

        await self._synthesize_and_broadcast(
            message.username, final_text, voice, lang, message.emote_names, emoji_items
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

        Runs as one of the four concurrent tasks started by asyncio.gather() in __init__.py.
        Errors in handle() are logged and swallowed so a bad message never kills the loop.
        task_done() is always called so Queue.join() (if ever used) doesn't hang.
        """
        while True:
            msg: QueuedMessage = await self._message_queue.get()
            try:
                LOGGER.debug(
                    "Processing queued message from %s (%s)", msg.username, msg.kind.name
                )
                await self.handle(msg)
            except Exception:
                LOGGER.exception("Error processing message from %s", msg.username)
            finally:
                self._message_queue.task_done()
