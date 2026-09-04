"""Unit tests for the ffmpeg wrapper in voxer.tts.

`TTSService.to_mp3` is a static method, so these tests call it directly on the
class.  That is deliberate: constructing a real TTSService starts Supertonic,
which downloads roughly 100 MB of model weights on a cold machine, and none of
that is needed to check how the subprocess is driven.

Instead of running the real ffmpeg (which may not be installed, and whose exact
wording differs between builds), each test writes a tiny shell script into
tmp_path and passes it as `ffmpeg_bin=`.  The script decides the exit code and
what lands on stderr, so the tests can pin the failure path exactly.
"""

import asyncio
import stat
from pathlib import Path

import pytest

from voxer.tts import FFMPEG_STDERR_LINES, TTSService


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
