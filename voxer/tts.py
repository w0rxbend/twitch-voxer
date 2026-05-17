import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from supertonic import TTS

LOGGER: logging.Logger = logging.getLogger(__name__)


class TTSService:
    def __init__(self) -> None:
        LOGGER.debug("Initialising TTS engine...")
        self._tts = TTS(auto_download=True)
        self._voice_cache: dict[str, object] = {}
        LOGGER.info("TTS engine ready")

    def _voice_style(self, voice_name: str) -> object:
        if voice_name not in self._voice_cache:
            LOGGER.debug("Loading voice style: %s", voice_name)
            self._voice_cache[voice_name] = self._tts.get_voice_style(voice_name=voice_name)
        return self._voice_cache[voice_name]

    def save_wav(self, text: str, voice_name: str = "F3", lang: str = "uk") -> Path:
        LOGGER.debug("Synthesising [%s/%s]: %r", voice_name, lang, text)
        wav, _ = self._tts.synthesize(text, voice_style=self._voice_style(voice_name), lang=lang)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = Path(tmp.name)
        self._tts.save_audio(wav, str(path))
        LOGGER.debug("WAV saved: %s", path)
        return path

    async def play(self, wav_path: Path) -> None:
        LOGGER.debug("Playing locally: %s", wav_path.name)
        await asyncio.to_thread(subprocess.run, ["paplay", str(wav_path)])
        LOGGER.debug("Playback finished: %s", wav_path.name)

    async def to_mp3(self, wav_path: Path, mp3_path: Path) -> None:
        LOGGER.debug("Converting to MP3: %s", mp3_path.name)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(wav_path), str(mp3_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        LOGGER.debug("MP3 ready: %s", mp3_path.name)

    def speak(self, text: str, voice_name: str = "F3", lang: str = "uk") -> None:
        path = self.save_wav(text, voice_name, lang)
        try:
            subprocess.run(["paplay", str(path)])
        finally:
            os.unlink(path)
