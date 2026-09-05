"""Private, atomic credential storage with one refresh owner per token file."""

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path


class TokenFileBusyError(RuntimeError):
    """Another process owns the credentials and may be rotating them."""


class TokenFileLock:
    """Hold a nonblocking OS lock, released automatically on process exit."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(f"{self.path}.lock", flags, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise TokenFileBusyError(
                f"Tokens in {self.path} are managed by another process. "
                "Stop the bot before refreshing emotes, then restart it."
            ) from exc
        self._fd = fd

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "TokenFileLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def read_tokens(path: str | Path) -> dict:
    """Read TwitchIO's JSON mapping without treating malformed data as tokens."""
    try:
        with Path(path).open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Token file must contain a JSON object")
    return data


def write_json_atomic(path: str | Path, data: Mapping, *, mode: int = 0o600) -> None:
    """Publish complete JSON using an unpredictable, private sibling temp file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.chmod(temporary, mode)
            json.dump(dict(data), handle, ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
