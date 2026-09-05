"""Predefined sound names, aliases, and local MP3 assets."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Sound:
    name: str
    filename: str
    aliases: tuple[str, ...] = ()


SOUNDS: tuple[Sound, ...] = (
    Sound("pop", "pop.mp3"),
    Sound("zap", "zap.mp3", ("quezacotl",)),
    Sound("sparkle", "sparkle.mp3", ("magic",)),
    Sound("ding", "ding.mp3"),
    Sound("crunch", "crunch.mp3"),
    Sound("chirp", "chirp.mp3"),
    Sound("choo choo", "choo-choo.mp3"),
    Sound("splash", "splash.mp3"),
    Sound("tweet", "tweet.mp3"),
    Sound("boing", "boing.mp3"),
    Sound("hush", "hush.mp3", ("shhh",)),
    Sound("ribbit", "ribbit.mp3", ("croak",)),
    Sound("doki doki", "doki-doki.mp3"),
    Sound(
        "wan wan",
        "wan-wan.mp3",
        ("goodboy", "goodgirl", "arf arf", "bark bark", "woof woof"),
    ),
    Sound("noted", "noted.mp3"),
    Sound("bang", "bang.mp3"),
    Sound("beep", "beep.mp3"),
    Sound("wow", "wow.mp3", ("anime wow",)),
    Sound("gong", "gong.mp3", ("asian gong",)),
    Sound("aww", "aww.mp3"),
    Sound("bruh", "bruh.mp3"),
    Sound("buzzer", "buzzer.mp3"),
    Sound("chime", "chime.mp3", ("ding2",)),
    Sound("call", "call.mp3", ("discord call",)),
    Sound("leave", "leave.mp3", ("discord leave",)),
    Sound("discord", "discord.mp3", ("discord notification", "discord ping")),
    Sound("join", "join.mp3", ("discord join",)),
    Sound("wrong", "wrong.mp3", ("incorrect", "loud buzzer")),
    Sound("fart", "fart.mp3"),
    Sound("gunshot", "gunshot.mp3", ("shot",)),
    Sound("iphone", "iphone.mp3", ("iphone notification",)),
    Sound("meow2", "meow2.mp3", ("meow 2",)),
    Sound("quack", "quack.mp3"),
    Sound("meow", "meow.mp3", ("meow1", "meow 1")),
    Sound("evil", "evil.mp3", ("evil laugh", "muhehehe")),
    Sound("nana", "nana.mp3", ("na na na",)),
    Sound("nope", "nope.mp3"),
    Sound("hellnah", "hellnah.mp3", ("hell nah",)),
    Sound("omg", "omg.mp3", ("oh my god",)),
    Sound("ohno", "ohno.mp3", ("oh no", "oh no laugh")),
    Sound("punch", "punch.mp3"),
    Sound("rizz", "rizz.mp3"),
    Sound("shocked", "shocked.mp3", ("shock",)),
    Sound("thunder", "thunder.mp3"),
    Sound("wait", "wait.mp3", ("wait wait", "what the hell")),
    Sound("champions", "champions.mp3", ("we are the champions",)),
    Sound("wetfart", "wetfart.mp3", ("wet fart",)),
    Sound("whip", "whip.mp3"),
    Sound("womp", "womp.mp3", ("womp womp", "womp womp womp")),
)
DEFAULT_SOUNDS_DIR = Path(__file__).parent / "sounds"
_PATTERNS = tuple(
    (
        sound,
        re.compile(
            r"\s*(?P<quote>[\"']?)(?:"
            + "|".join(
                r"[\s_\-–—]*".join(re.escape(word) for word in name.split())
                for name in (sound.name, *sound.aliases)
            )
            + r")(?P=quote)[.!?]*\s*",
            re.IGNORECASE,
        ),
    )
    for sound in SOUNDS
)


def resolve_sound(name: str) -> Sound | None:
    """Match whole aliases with optional quotes, word separators and punctuation."""
    return next(
        (sound for sound, pattern in _PATTERNS if pattern.fullmatch(name)), None
    )


def load_sounds(directory: Path) -> dict[str, Path]:
    """Load only predefined filenames; warn about missing assets at startup."""
    paths = {}
    for sound in SOUNDS:
        path = directory / sound.filename
        if path.is_file():
            paths[sound.name] = path
        else:
            logging.getLogger(__name__).warning("Soundboard clip missing: %s", path)
    return paths
