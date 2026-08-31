"""Unit tests for the audio-filename path-traversal guard in voxer.server."""

from pathlib import Path

from voxer.server import resolve_audio_file


def test_plain_filename_allowed(tmp_path: Path) -> None:
    resolved = resolve_audio_file(tmp_path, "abc.mp3")
    assert resolved == tmp_path.resolve() / "abc.mp3"


def test_parent_traversal_rejected(tmp_path: Path) -> None:
    assert resolve_audio_file(tmp_path, "../secret.txt") is None


def test_absolute_path_rejected(tmp_path: Path) -> None:
    assert resolve_audio_file(tmp_path, "/etc/passwd") is None


def test_subdirectory_rejected(tmp_path: Path) -> None:
    assert resolve_audio_file(tmp_path, "sub/dir.mp3") is None


def test_deep_traversal_rejected(tmp_path: Path) -> None:
    assert resolve_audio_file(tmp_path, "../../../../etc/passwd") is None
