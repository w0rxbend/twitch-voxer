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
        """Initialize the TTS engine and prepare the voice style cache."""
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
        """Synthesize text to speech and save as a temporary WAV file.

        Args:
            text: Text to synthesize.
            voice_name: Voice style identifier (default: "F3").
            lang: BCP-47 language code (default: "uk").

        Returns:
            Path to the generated WAV file (caller is responsible for deletion).
        """
        LOGGER.debug("Synthesising [%s/%s]: %r", voice_name, lang, text)
        wav, _ = self._tts.synthesize(text, voice_style=self._voice_style(voice_name), lang=lang)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = Path(tmp.name)
        self._tts.save_audio(wav, str(path))
        LOGGER.debug("WAV saved: %s", path)
        return path

    async def play(self, wav_path: Path) -> None:
        """Play a WAV file through the local audio device via paplay.

        Args:
            wav_path: Path to the WAV file to play.
        """
        LOGGER.debug("Playing locally: %s", wav_path.name)
        await asyncio.to_thread(subprocess.run, ["paplay", str(wav_path)])
        LOGGER.debug("Playback finished: %s", wav_path.name)

    async def to_mp3(self, wav_path: Path, mp3_path: Path) -> None:
        """Convert a WAV file to MP3 using ffmpeg.

        Args:
            wav_path: Source WAV file path.
            mp3_path: Destination MP3 file path.
        """
        LOGGER.debug("Converting to MP3: %s", mp3_path.name)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(wav_path), str(mp3_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        LOGGER.debug("MP3 ready: %s", mp3_path.name)

    async def speak(self, text: str, voice_name: str = "F3", lang: str = "uk") -> None:
        """Synthesize text and play it immediately through the local audio device.

        Args:
            text: Text to synthesize and play.
            voice_name: Voice style identifier (default: "F3").
            lang: BCP-47 language code (default: "uk").
        """
        path = self.save_wav(text, voice_name, lang)
        try:
            proc = await asyncio.create_subprocess_exec("paplay", str(path))
            await proc.wait()
        finally:
            os.unlink(path)
