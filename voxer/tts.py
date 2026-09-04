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

# Voice identifiers that ship with the Supertonic engine itself.  Custom voices
# loaded from voices/*.json are appended to these by the voice_names property.
BUILTIN_VOICES: list[str] = ["M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"]

# How many trailing lines of ffmpeg's stderr to quote when conversion fails.
# ffmpeg opens every run with a long banner (version, build flags, the list of
# libraries it was compiled against); the sentence that says what actually went
# wrong is at the very end, so only the tail is worth putting in an exception.
FFMPEG_STDERR_LINES: int = 5


class TTSService:
    """Thin wrapper around Supertonic TTS with a voice-style cache."""

    def __init__(self, voices_dir: Path | None = None, *, engine: Any = None) -> None:
        """Initialize the TTS engine and prepare the voice style cache.

        On the very first run, Supertonic downloads the model weights (~100 MB).
        Subsequent runs load from the local cache and are much faster.

        Args:
            voices_dir: Optional directory of custom voice JSON files to preload.
                        Each *.json file becomes a named voice in the pool.
            engine: Optional pre-built synthesis engine to use instead of
                    constructing Supertonic.  Production never passes this;
                    tests pass a stand-in object so that checking how voices
                    are loaded does not download ~100 MB of model weights.
                    It is deliberately untyped rather than hidden behind a
                    Protocol: there is exactly one real engine, and the style
                    objects it returns are opaque to this class either way.
        """
        LOGGER.debug("Initialising TTS engine...")
        # auto_download=True lets Supertonic fetch the model on first use
        self._tts = engine if engine is not None else TTS(auto_download=True)
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
        # A directory that does not exist is the common symptom of a mistyped
        # VOXER_VOICES_DIR.  Path.glob() on a missing directory yields nothing
        # and raises nothing, so without this check the previous code announced
        # "Loading custom voices from: ..." and then silently loaded none, with
        # the built-in voice pool hiding the mistake.  Say so instead.
        if not voices_dir.is_dir():
            LOGGER.warning(
                "Voices dir does not exist, no custom voices: %s", voices_dir.resolve()
            )
            return
        LOGGER.info("Loading custom voices from: %s", voices_dir.resolve())
        for json_file in sorted(voices_dir.glob("*.json")):
            name = json_file.stem
            try:
                self._voice_cache[name] = self._tts.get_voice_style_from_path(json_file)
                self._custom_voice_names.append(name)
                LOGGER.info("Custom voice loaded: %s (%s)", name, json_file.resolve())
            except Exception as exc:
                # exc_info=True prints the traceback as well as the message: the
                # message alone from a malformed JSON file or a shape the engine
                # rejects rarely says which line of which file was the problem.
                LOGGER.warning(
                    "Failed to load custom voice %s: %s", name, exc, exc_info=True
                )

    @property
    def voice_names(self) -> list[str]:
        """Every voice this engine can speak with: built-ins plus custom voices.

        Returned as a fresh list so callers cannot mutate the engine's state.
        This is the single place that knows the full pool; the composition root
        hands it to VoiceStore, which owns assignment of a voice to a chatter.
        """
        return BUILTIN_VOICES + self._custom_voice_names

    def _voice_style(self, voice_name: str) -> Any:
        """Return the cached style object for voice_name, loading it on first access."""
        if voice_name not in self._voice_cache:
            LOGGER.debug("Loading voice style: %s", voice_name)
            self._voice_cache[voice_name] = self._tts.get_voice_style(
                voice_name=voice_name
            )
        return self._voice_cache[voice_name]

    def save_wav(self, text: str, *, voice_name: str, lang: str) -> Path:
        """Synthesize text to speech and save as a temporary WAV file.

        This method is synchronous and CPU-bound.  Callers must run it via
        asyncio.to_thread() to avoid blocking the event loop.

        The returned WAV belongs to the caller: nothing here or in the operating
        system's temp-file machinery deletes it, so the caller must unlink it
        once it is done with it (a try/finally is the reliable way, because the
        MP3 conversion in between can raise).

        Args:
            text: Text to synthesize (may include Supertonic expression tags like <laugh>).
            voice_name: Voice style identifier (e.g. "F3" or a custom voice name).
            lang: BCP-47 language code (e.g. "uk", "en").

        Returns:
            Path to the generated temporary WAV file.
        """
        LOGGER.debug("Synthesising [%s/%s]: %r", voice_name, lang, text)
        wav, _ = self._tts.synthesize(
            text, voice_style=self._voice_style(voice_name), lang=lang
        )
        # Use a named temp file with delete=False so we can return the path;
        # the caller is responsible for cleanup.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = Path(tmp.name)
        try:
            self._tts.save_audio(wav, str(path))
        except BaseException:
            # save_audio failed (disk full, codec error) — remove the temp file
            # ourselves, because the caller never gets a path to clean up
            path.unlink(missing_ok=True)
            raise
        LOGGER.debug("WAV saved: %s", path)
        return path

    @staticmethod
    async def to_mp3(
        wav_path: Path, mp3_path: Path, *, ffmpeg_bin: str = "ffmpeg"
    ) -> None:
        """Convert a WAV file to MP3 using ffmpeg as an async subprocess.

        ffmpeg is called with -y (overwrite output) because mp3_path is a new
        UUID-named file that should not already exist, but -y is a safe guard.
        stdout is discarded, but stderr is captured: it is where ffmpeg explains
        a failure (missing encoder, unreadable input, no space left on device),
        and the tail of it is quoted in the raised error so an operator reading
        the log does not have to reconstruct and re-run the command by hand.

        This is a static method: it uses no engine state, which also means the
        ffmpeg wrapper can be exercised without constructing a TTSService (that
        downloads ~100 MB of model weights).  Calling it through an instance,
        as MessageHandler does, keeps working unchanged.

        Args:
            wav_path: Source WAV file path (will be deleted by the caller).
            mp3_path: Destination MP3 file path (served by AudioServer).
            ffmpeg_bin: Executable to run.  Defaults to "ffmpeg" from PATH;
                        tests point it at a stand-in script.

        Raises:
            RuntimeError: If ffmpeg exits with a non-zero return code.  The
                message carries the exit code and the tail of ffmpeg's stderr.
        """
        LOGGER.debug("Converting to MP3: %s", mp3_path.name)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_bin,
            "-y",
            "-i",
            str(wav_path),
            str(mp3_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # communicate() rather than wait(): now that stderr is a pipe, its
        # operating-system buffer can fill up, and a process whose stderr is
        # full blocks forever waiting for someone to read it.  wait() never
        # reads, so it would deadlock; communicate() drains while it waits.
        _, err = await proc.communicate()
        if proc.returncode != 0:
            lines = [
                line.strip()
                for line in (err or b"").decode("utf-8", errors="replace").splitlines()
                if line.strip()
            ]
            detail = (
                " | ".join(lines[-FFMPEG_STDERR_LINES:])
                if lines
                else "no stderr output"
            )
            raise RuntimeError(
                f"ffmpeg failed (exit {proc.returncode}) for {mp3_path.name}: {detail}"
            )
        LOGGER.debug("MP3 ready: %s", mp3_path.name)
