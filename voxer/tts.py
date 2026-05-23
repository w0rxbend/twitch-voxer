import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from supertonic import TTS

LOGGER: logging.Logger = logging.getLogger(__name__)


class TTSService:
    def __init__(self, voices_dir: Path | None = None) -> None:
        """Initialize the TTS engine and prepare the voice style cache.

        Args:
            voices_dir: Optional directory of custom voice JSON files to preload.
        """
        LOGGER.debug("Initialising TTS engine...")
        self._tts = TTS(auto_download=True)
        self._voice_cache: dict[str, Any] = {}
        self._custom_voice_names: list[str] = []
        if voices_dir is not None:
            self._load_custom_voices(voices_dir)
        LOGGER.info("TTS engine ready")

    def _load_custom_voices(self, voices_dir: Path) -> None:
        LOGGER.info("Loading custom voices from: %s", voices_dir.resolve())
        for json_file in sorted(voices_dir.glob("*.json")):
            name = json_file.stem
            try:
                self._voice_cache[name] = self._tts.get_voice_style_from_path(json_file)
                self._custom_voice_names.append(name)
                LOGGER.info("Custom voice loaded: %s (%s)", name, json_file.resolve())
            except Exception as exc:
                LOGGER.warning("Failed to load custom voice %s: %s", name, exc)

    @property
    def custom_voice_names(self) -> list[str]:
        """Names of successfully loaded custom voices."""
        return list(self._custom_voice_names)

    def _voice_style(self, voice_name: str) -> Any:
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
        returncode = await proc.wait()
        if returncode != 0:
            raise RuntimeError(f"ffmpeg failed (exit {returncode}) for {mp3_path.name}")
        LOGGER.debug("MP3 ready: %s", mp3_path.name)
