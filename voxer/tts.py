"""Text-to-Speech infrastructure layer.

Wraps the Supertonic TTS engine and handles the two-stage audio pipeline:

  Stage 1 — WAV synthesis (TTSService.save_wav)
    Supertonic is a synchronous, CPU-bound library.  Callers run this method
    via asyncio.to_thread() to avoid blocking the event loop.

  Stage 2 — MP3 conversion (TTSService.to_mp3)
    ffmpeg is invoked as an async subprocess.  The resulting MP3 is served
    by AudioServer and streamed to the browser overlay via WebSocket.

Voice styles are cached in a dict so the model does not re-load style weights
on every message — loading is expensive on the first call per style name.
"""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from supertonic import TTS

LOGGER: logging.Logger = logging.getLogger(__name__)


class TTSService:
    """Thin wrapper around Supertonic TTS with a voice-style cache."""

    def __init__(self, voices_dir: Path | None = None) -> None:
        """Initialize the TTS engine and prepare the voice style cache.

        On the very first run, Supertonic downloads the model weights (~100 MB).
        Subsequent runs load from the local cache and are much faster.

        Args:
            voices_dir: Optional directory of custom voice JSON files to preload.
                        Each *.json file becomes a named voice in the pool.
        """
        LOGGER.debug("Initialising TTS engine...")
        # auto_download=True lets Supertonic fetch the model on first use
        self._tts = TTS(auto_download=True)
        # Map of voice_name → style object, populated lazily or at init time
        self._voice_cache: dict[str, Any] = {}
        self._custom_voice_names: list[str] = []
        if voices_dir is not None:
            self._load_custom_voices(voices_dir)
        LOGGER.info("TTS engine ready")

    def _load_custom_voices(self, voices_dir: Path) -> None:
        """Load all *.json voice style files from voices_dir into the cache.

        Each file's stem (filename without extension) becomes the voice name.
        Files that fail to load are skipped with a warning — one bad file should
        not prevent the others from loading.
        """
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
        """Names of successfully loaded custom voices (read-only snapshot)."""
        return list(self._custom_voice_names)

    def _voice_style(self, voice_name: str) -> Any:
        """Return the cached style object for voice_name, loading it on first access."""
        if voice_name not in self._voice_cache:
            LOGGER.debug("Loading voice style: %s", voice_name)
            self._voice_cache[voice_name] = self._tts.get_voice_style(voice_name=voice_name)
        return self._voice_cache[voice_name]

    def save_wav(self, text: str, voice_name: str = "F3", lang: str = "uk") -> Path:
        """Synthesize text to speech and save as a temporary WAV file.

        This method is synchronous and CPU-bound.  Callers must run it via
        asyncio.to_thread() to avoid blocking the event loop.

        The caller is responsible for deleting the returned WAV file.
        MessageHandler._synthesize_and_broadcast() does this in a finally block.

        Args:
            text: Text to synthesize (may include Supertonic expression tags like <laugh>).
            voice_name: Voice style identifier (default: "F3").
            lang: BCP-47 language code (default: "uk" for Ukrainian).

        Returns:
            Path to the generated temporary WAV file.
        """
        LOGGER.debug("Synthesising [%s/%s]: %r", voice_name, lang, text)
        wav, _ = self._tts.synthesize(text, voice_style=self._voice_style(voice_name), lang=lang)
        # Use a named temp file with delete=False so we can return the path;
        # the caller is responsible for cleanup.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = Path(tmp.name)
        self._tts.save_audio(wav, str(path))
        LOGGER.debug("WAV saved: %s", path)
        return path

    async def to_mp3(self, wav_path: Path, mp3_path: Path) -> None:
        """Convert a WAV file to MP3 using ffmpeg as an async subprocess.

        ffmpeg is called with -y (overwrite output) because mp3_path is a new
        UUID-named file that should not already exist, but -y is a safe guard.
        stdout/stderr are suppressed; errors are surfaced via the return code.

        Args:
            wav_path: Source WAV file path (will be deleted by the caller).
            mp3_path: Destination MP3 file path (served by AudioServer).

        Raises:
            RuntimeError: If ffmpeg exits with a non-zero return code.
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
