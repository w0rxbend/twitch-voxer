"""Unit tests for voxer.tts: custom-voice loading and the ffmpeg wrapper.

Neither group constructs the real speech engine.  Supertonic downloads roughly
100 MB of model weights on a cold machine, which is far too much to pay for
tests that never synthesise a single word.

  - `TTSService.to_mp3` is a static method, so those tests call it straight on
    the class.  Rather than running the real ffmpeg (which may not be installed,
    and whose exact wording differs between builds), each test writes a tiny
    shell script into tmp_path and passes it as `ffmpeg_bin=`.  The script
    decides the exit code and what lands on stderr, so the failure path can be
    pinned exactly.

  - The voice-loading tests construct a real TTSService but hand it a stub
    engine through the `engine=` keyword argument, so `__init__` skips building
    Supertonic.  The stub implements only `get_voice_style_from_path`, which is
    the single engine method involved in loading custom voices.
"""

import asyncio
import logging
import stat
from pathlib import Path

import pytest

from voxer.tts import BUILTIN_VOICES, FFMPEG_STDERR_LINES, TTSService


def make_fake_ffmpeg(tmp_path: Path, *, exit_code: int, stderr: str = "") -> Path:
    """Write an executable stand-in for ffmpeg and return its path.

    The script ignores the arguments it is given, prints `stderr` on file
    descriptor 2, and exits with `exit_code`.
    """
    script = tmp_path / "fake-ffmpeg"
    body = "#!/bin/sh\n"
    if stderr:
        # printf rather than echo: echo's handling of backslashes and of a
        # leading "-" differs between shells, printf's does not.
        body += f"printf '%s' {shell_quote(stderr)} >&2\n"
    body += f"exit {exit_code}\n"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def shell_quote(text: str) -> str:
    """Wrap text in single quotes so /bin/sh treats it as one literal word."""
    return "'" + text.replace("'", "'\\''") + "'"


class TestToMp3:
    async def test_success_does_not_raise(self, tmp_path: Path) -> None:
        """A converter that exits 0 is treated as success."""
        fake = make_fake_ffmpeg(tmp_path, exit_code=0, stderr="just the usual banner")
        await TTSService.to_mp3(
            tmp_path / "in.wav", tmp_path / "out.mp3", ffmpeg_bin=str(fake)
        )

    async def test_failure_surfaces_stderr(self, tmp_path: Path) -> None:
        """A non-zero exit raises, and ffmpeg's own explanation is in the message.

        Before this, the error said only "ffmpeg failed (exit 1)", so an
        operator had to rebuild the command by hand to find out why.
        """
        message = "Unknown encoder 'libmp3lame'"
        fake = make_fake_ffmpeg(
            tmp_path, exit_code=1, stderr=f"banner line\n{message}\n"
        )
        with pytest.raises(RuntimeError) as excinfo:
            await TTSService.to_mp3(
                tmp_path / "in.wav", tmp_path / "out.mp3", ffmpeg_bin=str(fake)
            )
        text = str(excinfo.value)
        assert message in text
        assert "exit 1" in text
        assert "out.mp3" in text

    async def test_failure_quotes_only_the_last_lines(self, tmp_path: Path) -> None:
        """Only the tail of stderr is quoted, because the banner comes first."""
        noise = "\n".join(f"banner {i}" for i in range(40))
        fake = make_fake_ffmpeg(
            tmp_path, exit_code=1, stderr=f"{noise}\nNo space left on device\n"
        )
        with pytest.raises(RuntimeError) as excinfo:
            await TTSService.to_mp3(
                tmp_path / "in.wav", tmp_path / "out.mp3", ffmpeg_bin=str(fake)
            )
        text = str(excinfo.value)
        assert "No space left on device" in text
        assert "banner 0" not in text
        # The tail is exactly FFMPEG_STDERR_LINES lines: the failure line plus
        # the banner lines immediately above it, and nothing older.
        assert f"banner {40 - FFMPEG_STDERR_LINES + 1}" in text
        assert f"banner {40 - FFMPEG_STDERR_LINES}" not in text

    async def test_failure_without_stderr_still_raises(self, tmp_path: Path) -> None:
        """A converter that fails silently still produces a usable error."""
        fake = make_fake_ffmpeg(tmp_path, exit_code=3)
        with pytest.raises(RuntimeError, match="no stderr output"):
            await TTSService.to_mp3(
                tmp_path / "in.wav", tmp_path / "out.mp3", ffmpeg_bin=str(fake)
            )

    async def test_large_stderr_does_not_deadlock(self, tmp_path: Path) -> None:
        """More stderr than a pipe buffer holds must not hang the conversion.

        A pipe holds around 64 KB before the writing process blocks.  The old
        code used DEVNULL, where that could not happen; with a real pipe it can,
        so this writes several times a buffer's worth to prove the wrapper keeps
        reading instead of waiting for a process that is waiting for it.

        The timeout is what makes this a test rather than a hang: a regression
        back to `await proc.wait()` would otherwise block the suite forever.
        """
        fake = tmp_path / "chatty-ffmpeg"
        fake.write_text(
            "#!/bin/sh\n"
            "i=0\n"
            "while [ $i -lt 5000 ]; do\n"
            "  echo 'ffmpeg is very talkative about its build flags' >&2\n"
            "  i=$((i + 1))\n"
            "done\n"
            "echo 'Invalid data found when processing input' >&2\n"
            "exit 1\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        with pytest.raises(RuntimeError, match="Invalid data found"):
            async with asyncio.timeout(30):
                await TTSService.to_mp3(
                    tmp_path / "in.wav", tmp_path / "out.mp3", ffmpeg_bin=str(fake)
                )


class StubEngine:
    """Stands in for the Supertonic engine while custom voices are loaded.

    A real engine reads a JSON file and returns an opaque "voice style" object
    that only the engine itself understands.  TTSService never looks inside that
    object — it stores it in a cache and hands it back to the engine later — so
    the stub can return a plain string and nothing downstream can tell.

    Args:
        failing_stems: Filenames (without the .json extension) that this engine
                       refuses to load, standing in for a corrupt or malformed
                       voice file.
    """

    def __init__(self, *, failing_stems: frozenset[str] = frozenset()) -> None:
        self._failing_stems = failing_stems
        # Every path the engine was asked to load, in the order it was asked.
        self.requested: list[Path] = []

    def get_voice_style_from_path(self, path: Path) -> str:
        self.requested.append(path)
        if path.stem in self._failing_stems:
            raise ValueError(f"not a voice file: {path.name}")
        return f"style-for-{path.stem}"


def write_voice_files(directory: Path, *names: str) -> None:
    """Create one empty <name>.json file per name.

    The contents do not matter: the stub engine decides what loading a file
    does, and the real engine is never involved.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / f"{name}.json").write_text("{}")


class TestCustomVoiceLoading:
    def test_voice_names_are_file_stems_after_the_builtins(
        self, tmp_path: Path
    ) -> None:
        """Each *.json file adds one voice, named after the file, at the end.

        Order matters to nothing functionally, but the built-ins coming first
        is what the docstring on `voice_names` promises, and app.py feeds that
        list straight into VoiceStore as the pool of assignable voices.
        """
        voices_dir = tmp_path / "voices"
        write_voice_files(voices_dir, "narrator", "robot")
        # A non-JSON file in the same directory must be ignored rather than
        # becoming a voice called "readme".
        (voices_dir / "readme.txt").write_text("these are my voices")

        service = TTSService(voices_dir, engine=StubEngine())

        assert service.voice_names == [*BUILTIN_VOICES, "narrator", "robot"]

    def test_one_unloadable_file_does_not_stop_the_others(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A file the engine rejects is skipped; its siblings still load.

        This is the promise the whole voice pool rests on: one hand-edited JSON
        file with a stray comma must cost that one voice, not every voice.
        """
        voices_dir = tmp_path / "voices"
        write_voice_files(voices_dir, "alpha", "broken", "charlie")
        engine = StubEngine(failing_stems=frozenset({"broken"}))

        with caplog.at_level(logging.WARNING, logger="voxer.tts"):
            service = TTSService(voices_dir, engine=engine)

        assert service.voice_names == [*BUILTIN_VOICES, "alpha", "charlie"]
        # The engine was still asked for all three, so the skip happened at the
        # failure rather than by abandoning the loop.
        assert [path.stem for path in engine.requested] == [
            "alpha",
            "broken",
            "charlie",
        ]
        assert "broken" in caplog.text

    def test_empty_dir_yields_only_the_builtins(self, tmp_path: Path) -> None:
        """A directory that exists but holds no voice files is not an error."""
        voices_dir = tmp_path / "voices"
        voices_dir.mkdir()

        service = TTSService(voices_dir, engine=StubEngine())

        assert service.voice_names == BUILTIN_VOICES

    def test_missing_dir_warns_and_yields_only_the_builtins(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A configured directory that does not exist says so out loud.

        This is the behaviour change in this commit.  Path.glob() on a missing
        directory yields nothing and raises nothing, so a mistyped
        VOXER_VOICES_DIR used to log a cheerful "Loading custom voices from:"
        and then load none, with the built-in pool masking the mistake.
        """
        missing = tmp_path / "typo" / "voices"

        with caplog.at_level(logging.WARNING, logger="voxer.tts"):
            service = TTSService(missing, engine=StubEngine())

        assert service.voice_names == BUILTIN_VOICES
        assert "Voices dir does not exist" in caplog.text
        assert str(missing.resolve()) in caplog.text

    def test_no_voices_dir_never_touches_the_engine(self) -> None:
        """Omitting voices_dir leaves the built-in pool and loads nothing."""
        engine = StubEngine()

        service = TTSService(engine=engine)

        assert service.voice_names == BUILTIN_VOICES
        assert engine.requested == []
