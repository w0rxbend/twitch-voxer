import asyncio
import logging
import random
import re
import uuid
from pathlib import Path

import pickledb
from langdetect import detect, LangDetectException

from .tts import TTSService

LOGGER: logging.Logger = logging.getLogger(__name__)

VOICES: list[str] = ["M1", "M2", "M3", "F1", "F2", "F3"]
DEFAULT_LANG: str = "uk"

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
    ) -> None:
        LOGGER.debug("Initialising MessageHandler (db=%s, audio_dir=%s)", db_path, audio_dir)
        self._tts = tts
        self._db = pickledb.PickleDB(db_path)
        self._audio_dir = audio_dir
        self._broadcast = broadcast
        self._message_queue = message_queue
        LOGGER.info("MessageHandler ready")

    async def _get_or_assign_voice(self, username: str) -> str:
        await self._db.load()
        voice = await self._db.get(username)
        if not voice:
            voice = random.choice(VOICES)
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

    async def handle(self, username: str, text: str) -> None:
        if _is_bot(username):
            LOGGER.info("Skipping bot account: %s", username)
            return

        LOGGER.info("Handling message from %s", username)
        lang = await self._detect_lang(text)
        voice = await self._get_or_assign_voice(username)
        announced = _ANNOUNCEMENTS[lang].format(username=username, text=_normalize(text, lang))

        wav_path = self._tts.save_wav(announced, voice_name=voice, lang=lang)
        mp3_path = self._audio_dir / f"{uuid.uuid4()}.mp3"
        try:
            await self._tts.to_mp3(wav_path, mp3_path)
            # await self._tts.play(wav_path)
        finally:
            wav_path.unlink()

        LOGGER.info("Broadcasting audio for %s -> %s", username, mp3_path.name)
        await self._broadcast(url=f"/audio/{mp3_path.name}", username=username)

    async def _process_queue(self) -> None:
        while True:
            try:
                username, text = await self._message_queue.get()
                LOGGER.debug("Processing queued message from %s", username)
                await self.handle(username, text)
            except Exception as exc:
                LOGGER.error("Error processing message: %s", exc)
            finally:
                self._message_queue.task_done()
