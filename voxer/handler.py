import asyncio
import logging
import random
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

import emoji as emoji_lib
import pickledb
from langdetect import detect, LangDetectException
from .tts import TTSService

LOGGER: logging.Logger = logging.getLogger(__name__)

_BUILTIN_VOICES: list[str] = ["M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"]
DEFAULT_LANG: str = "uk"
_ANNOUNCE_WINDOW_SECS: int = 300  # 5 minutes

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

_ANNOUNCEMENTS: dict[str, str] = {
    "en": "The user {username} says: {text}",
    "uk": "Користувач {username} каже: {text}",
}

_LINK_REPLACEMENTS: dict[str, str] = {
    "en": "see link in the chat",
    "uk": "дивіться посилання в чаті",
}

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_LAUGH_RE = re.compile(
    r"\b(?:"
    # English
    r"lo+l|lmf?ao|rofl|lel|kek+w?|x+d|w+w+|"
    r"a*ha+ha+(?:ha)*|he+he+(?:he)*|hi+hi+(?:hi)*|"
    # Ukrainian / common transliteration
    r"а*ха+ха+(?:ха)*|а+хах+|аза+з+|"
    r"хі-хі|хіхі+|ха-ха|хах(?:а+)?|"
    r"кек+|лол+|гаха+|ахах+|їхіхі+"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# Longer keys must appear first in the alternation so they are tried before their prefixes.
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
    # Latin abbreviations → Ukrainian
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
    # Cyrillic abbreviations
    "імхо": "на мою скромну думку",
    "афк": "від клавіатури",
    "гг": "гарна гра",
    "нз": "не знаю",
    "хз": "хто зна",
    "дк": "до речі",
}


def _build_abbrev_re(abbrevs: dict[str, str]) -> re.Pattern:
    keys = sorted(abbrevs, key=len, reverse=True)
    return re.compile(
        r"\b(?:" + "|".join(re.escape(k) for k in keys) + r")\b",
        re.IGNORECASE | re.UNICODE,
    )


_ABBREV_RE_EN: re.Pattern = _build_abbrev_re(_ABBREVS_EN)
_ABBREV_RE_UK: re.Pattern = _build_abbrev_re(_ABBREVS_UK)


_TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"


def _emoji_url(char: str) -> str:
    codepoints = "-".join(f"{ord(c):x}" for c in char if ord(c) != 0xFE0F)
    return f"{_TWEMOJI_BASE}/{codepoints}.png"


def _extract_emojis(text: str) -> tuple[str, list["EmoteItem"]]:
    found = emoji_lib.emoji_list(text)
    items = [EmoteItem(name=e["emoji"], url=_emoji_url(e["emoji"])) for e in found]
    clean = emoji_lib.replace_emoji(text, replace="").strip()
    return clean, items


@dataclass
class EmoteItem:
    name: str
    url: str


@dataclass
class BroadcastEvent:
    audio_url: str
    username: str
    emotes: list[EmoteItem] = field(default_factory=list)


class MessageKind(Enum):
    USER = auto()
    SYSTEM = auto()


@dataclass
class QueuedMessage:
    """A message waiting to be spoken via TTS."""
    username: str
    text: str
    kind: MessageKind = field(default=MessageKind.USER)
    emote_names: list[str] = field(default_factory=list)



def _is_bot(username: str) -> bool:
    lower = username.lower()
    return lower in _KNOWN_BOTS or "bot" in lower


def _normalize(text: str, lang: str) -> str:
    link_replacement = _LINK_REPLACEMENTS.get(lang, _LINK_REPLACEMENTS[DEFAULT_LANG])
    text, link_count = _URL_RE.subn(link_replacement, text)

    abbrevs = _ABBREVS_UK if lang == "uk" else _ABBREVS_EN
    abbrev_re = _ABBREV_RE_UK if lang == "uk" else _ABBREV_RE_EN
    text, abbrev_count = abbrev_re.subn(lambda m: abbrevs[m.group(0).lower()], text)

    text, laugh_count = _LAUGH_RE.subn("<laugh>,<laugh>,<laugh>", text)
    if link_count:
        LOGGER.debug("Replaced %d link(s)", link_count)
    if abbrev_count:
        LOGGER.debug("Expanded %d abbreviation(s)", abbrev_count)
    if laugh_count:
        LOGGER.debug("Applied %d <laugh> tag(s): %s", laugh_count, text)
        text = "<laugh>,<laugh>,<laugh>" + text
    return text


class MessageHandler:
    def __init__(
        self,
        tts: TTSService,
        db_path: str,
        audio_dir: Path,
        broadcast,
        message_queue: asyncio.Queue,
        emotes_db_path: str | None = None,
        emote_sound_paths: list[str] | None = None,
        timestamps_db_path: str = "timestamps.json",
        no_announce_users: frozenset[str] | None = None,
    ) -> None:
        """Initialize message handler with TTS service and database.

        Args:
            tts: TTSService instance for voice synthesis.
            db_path: Path to pickledb file storing username → voice assignments.
            audio_dir: Directory for storing generated MP3 files.
            broadcast: Async function to broadcast audio via WebSocket to connected clients.
            message_queue: asyncio.Queue for receiving messages from the bot.
            emotes_db_path: Optional path to pickledb file storing emote name → image URLs.
            emote_sound_paths: MP3 files to pick from randomly for emote-only messages.
            timestamps_db_path: Path to pickledb file storing username → last-message timestamp.
        """
        LOGGER.debug("Initialising MessageHandler (db=%s, audio_dir=%s)", db_path, audio_dir)
        self._tts = tts
        self._voices: list[str] = _BUILTIN_VOICES + tts.custom_voice_names
        LOGGER.info("Voice pool (%d): %s", len(self._voices), self._voices)
        self._db = pickledb.PickleDB(db_path)
        self._ts_db = pickledb.PickleDB(timestamps_db_path)
        self._audio_dir = audio_dir
        self._broadcast = broadcast
        self._message_queue = message_queue
        self._emotes: dict[str, dict] = self._load_emotes(emotes_db_path)
        self._emote_sounds: list[Path] = [
            p for raw in (emote_sound_paths or []) if (p := Path(raw)).exists()
        ]
        self._no_announce_users: frozenset[str] = no_announce_users or frozenset()
        LOGGER.info("MessageHandler ready")

    @staticmethod
    def _load_emotes(path: str | None) -> dict[str, dict]:
        if not path:
            return {}
        try:
            import json
            data = json.loads(Path(path).read_bytes())
            LOGGER.info("Loaded %d emotes from %s", len(data), path)
            return data
        except (FileNotFoundError, ValueError) as exc:
            LOGGER.warning("Could not load emotes DB (%s): %s", path, exc)
            return {}

    async def _get_or_assign_voice(self, username: str) -> str:
        await self._db.load()
        voice = await self._db.get(username)
        if not voice:
            voice = random.choice(self._voices)
            await self._db.set(username, voice)
            LOGGER.info("New chatter %s — assigned voice %s", username, voice)
            await self._db.save()
        else:
            LOGGER.debug("Voice for %s: %s", username, voice)
        return voice

    async def _detect_lang(self, text: str) -> str:
        try:
            lang = detect(text)
            resolved = lang if lang in _ANNOUNCEMENTS else DEFAULT_LANG
            LOGGER.debug("Detected lang: %s -> %s", lang, resolved)
            return resolved
        except LangDetectException:
            LOGGER.debug("Lang detection failed, defaulting to %s", DEFAULT_LANG)
            return DEFAULT_LANG

    async def _should_announce(self, username: str) -> bool:
        """Return True if more than _ANNOUNCE_WINDOW_SECS have passed since the user's last message."""
        await self._ts_db.load()
        last = await self._ts_db.get(username)
        if not last:
            return True
        return (time.time() - float(last)) > _ANNOUNCE_WINDOW_SECS

    async def _record_message(self, username: str) -> None:
        """Persist the current timestamp as the user's last-message time."""
        await self._ts_db.set(username, str(time.time()))
        await self._ts_db.save()

    async def handle(self, message: QueuedMessage) -> None:
        """Process a queued message via TTS and broadcast to connected clients.

        Dispatches on message.kind:
          - USER: applies bot filtering, language detection, persistent voice assignment,
            text normalisation, and the "username says:" announcement prefix.
          - SYSTEM: speaks text directly with a random voice in Ukrainian — used for
            channel events (follow, sub, raid, cheer, etc.).

        Args:
            message: The queued message to process.
        """
        emoji_items: list[EmoteItem] = []

        if message.kind is MessageKind.SYSTEM:
            LOGGER.info("Announcing system event for %s", message.username)
            voice = random.choice(self._voices)
            final_text = message.text
            lang = "uk"
        else:
            if _is_bot(message.username):
                LOGGER.info("Skipping bot account: %s", message.username)
                return
            LOGGER.info("Handling message from %s", message.username)
            clean_text, emoji_items = _extract_emojis(message.text)

            if not clean_text.strip():
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
            if message.username.lower() not in self._no_announce_users and await self._should_announce(message.username):
                final_text = _ANNOUNCEMENTS[lang].format(username=message.username, text=normalized)
                LOGGER.debug("Announcing prefix for %s (outside window)", message.username)
            else:
                final_text = normalized
            await self._record_message(message.username)

        twitch_emote_items = [
            EmoteItem(name=name, url=self._emotes[name]["url_2x"])
            for name in message.emote_names
            if name in self._emotes
        ]
        all_emotes = twitch_emote_items + emoji_items

        wav_path = self._tts.save_wav(final_text, voice_name=voice, lang=lang)
        mp3_path = self._audio_dir / f"{uuid.uuid4()}.mp3"
        try:
            await self._tts.to_mp3(wav_path, mp3_path)
        finally:
            wav_path.unlink()

        event = BroadcastEvent(
            audio_url=f"/audio/{mp3_path.name}",
            username=message.username,
            emotes=all_emotes,
        )
        LOGGER.info("Broadcasting audio for %s -> %s (emotes: %s)", message.username, mp3_path.name, [e.name for e in all_emotes])
        await self._broadcast(event)

    async def process_queue(self) -> None:
        """Continuously drain the message queue, invoking handle() for each QueuedMessage."""
        while True:
            try:
                msg: QueuedMessage = await self._message_queue.get()
                LOGGER.debug("Processing queued message from %s (%s)", msg.username, msg.kind.name)
                await self.handle(msg)
            except Exception as exc:
                LOGGER.error("Error processing message: %s", exc)
            finally:
                self._message_queue.task_done()
