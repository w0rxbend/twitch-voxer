import asyncio
import logging
import random
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


def _is_bot(username: str) -> bool:
    lower = username.lower()
    return lower in _KNOWN_BOTS or "bot" in lower


class MessageHandler:
    def __init__(
        self,
        tts: TTSService,
        db_path: str,
        audio_dir: Path,
        broadcast,
    ) -> None:
        LOGGER.debug("Initialising MessageHandler (db=%s, audio_dir=%s)", db_path, audio_dir)
        self._tts = tts
        self._db = pickledb.PickleDB(db_path)
        self._audio_dir = audio_dir
        self._broadcast = broadcast
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
        announced = _ANNOUNCEMENTS[lang].format(username=username, text=text)

        wav_path = self._tts.save_wav(announced, voice_name=voice, lang=lang)
        mp3_path = self._audio_dir / f"{uuid.uuid4()}.mp3"
        try:
            await self._tts.to_mp3(wav_path, mp3_path)
            # await self._tts.play(wav_path)
        finally:
            wav_path.unlink()

        LOGGER.info("Broadcasting audio for %s -> %s", username, mp3_path.name)
        await self._broadcast(url=f"/audio/{mp3_path.name}", username=username)
