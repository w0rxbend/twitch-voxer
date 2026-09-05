"""Command boundaries, flexible sound names, and bundled asset completeness."""

import pytest

from voxer.chat_commands import parse_commands
from voxer.soundboard import DEFAULT_SOUNDS_DIR, SOUNDS, load_sounds, resolve_sound


@pytest.mark.parametrize(
    "text, canonical",
    [
        ("!S   POP", "pop"),
        (' ! sound: "woof-woof"! ', "wan wan"),
        ("!s='doki_doki'", "doki doki"),
        ("!sound CHOO\tCHOO", "choo choo"),
        ("!sound choochoo", "choo choo"),
        ("!s arf—arf", "wan wan"),
        ("!s quezacotl", "zap"),
        ("!sound beep.", "beep"),
    ],
)
def test_flexible_sound_commands(text, canonical):
    commands = parse_commands(text)
    assert len(commands) == 1
    sound = resolve_sound(commands[0].argument)
    assert sound is not None
    assert sound.name == canonical


@pytest.mark.parametrize(
    "text",
    [
        "email!tts hi",
        "!ttsomething hi",
        "!soundtrack pop",
        "!!s pop",
        "pop",
        "!pop",
        r"\!tts hi",
    ],
)
def test_commands_do_not_match_embedded_words_or_plain_chat(text):
    assert parse_commands(text) == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("hey !tts message to TTS - ,", [("tts", "message to TTS - ,")]),
        (
            "!tts message !end !sound magic",
            [("tts", "message"), ("end", ""), ("sound", "magic")],
        ),
        (
            "before !tts One! !end ignored !s pop !tts Two.",
            [("tts", "One!"), ("end", "ignored"), ("s", "pop"), ("tts", "Two.")],
        ),
        ("!tts first\n!TTS second", [("tts", "first"), ("tts", "second")]),
        ("!tts !end !s", [("tts", ""), ("end", ""), ("s", "")]),
        (
            "!tts keep !unknown and !soundtrack as text",
            [("tts", "keep !unknown and !soundtrack as text")],
        ),
    ],
)
def test_inline_command_sections_preserve_text_and_offsets(text, expected):
    commands = parse_commands(text)
    assert [(command.name, command.argument) for command in commands] == expected
    for command in commands:
        assert text[command.start : command.end].strip() == command.argument


@pytest.mark.parametrize(
    "name",
    [
        "popping",
        "pop then bang",
        "../pop",
        "https://example.org/pop.mp3",
        '"pop',
        "pop\nbang",
        ".*",
    ],
)
def test_sounds_require_a_whole_known_alias(name):
    assert resolve_sound(name) is None


def test_all_predefined_sounds_are_bundled():
    paths = load_sounds(DEFAULT_SOUNDS_DIR)
    assert len(paths) == len(SOUNDS)
    assert {path.name for path in paths.values()} == {
        path.name for path in DEFAULT_SOUNDS_DIR.glob("*.mp3")
    }
    assert all(path.stat().st_size > 100 for path in paths.values())
    assert len({sound.filename for sound in SOUNDS}) == len(SOUNDS)


def test_missing_sounds_warn_and_unknown_files_are_ignored(tmp_path, caplog):
    (tmp_path / "pop.mp3").write_bytes(b"pop")
    (tmp_path / "unlisted.mp3").write_bytes(b"extra")
    assert load_sounds(tmp_path) == {"pop": tmp_path / "pop.mp3"}
    assert "zap.mp3" in caplog.text
