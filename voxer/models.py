"""Shared data types passed between the bot, handler, and server layers.

These are plain data carriers with no behaviour.  They live in their own
module so that peripheral layers (server.py, bot.py) can depend on the data
shapes without importing the full business-logic module (handler.py) and
its heavy dependencies (langdetect, emoji, pickledb).

The single exception is ``audio_url_for()``: building the ``audio_url`` value
is part of that field's contract, and keeping the helper next to the field
means the producer (handler.py) and the route that has to make the URL
resolvable (server.py) cannot drift apart unnoticed.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Final


@dataclass
class EmoteItem:
    """A single emote or emoji to be displayed in the browser overlay."""

    name: str  # display name or raw emoji character
    url: str  # absolute URL to the image asset


# URL path prefix under which generated MP3s are served.  server.py mounts
# audio_dir here and handler.py builds every audio_url below from it, so
# renaming the mount is a one-line change instead of a silent break in the
# overlay: the browser would request a path nothing answers, and neither a
# test nor a type check would notice, because a URL is only ever a string.
AUDIO_URL_PREFIX: Final[str] = "/audio"


def audio_url_for(filename: str) -> str:
    """Return the overlay-facing URL for an MP3 sitting in the audio directory.

    ``filename`` is the bare name of the file ("<uuid>.mp3"), not a path.  The
    result is a relative URL such as "/audio/6c8e….mp3", which the browser
    resolves against whatever host it loaded the overlay page from.
    """
    return f"{AUDIO_URL_PREFIX}/{filename}"


@dataclass
class BroadcastEvent:
    """Payload sent over WebSocket to the browser overlay after synthesis."""

    # Always built with audio_url_for() above, e.g. "/audio/<uuid>.mp3"
    audio_url: str
    username: str  # chatter's Twitch login name
    avatar_url: str | None = None  # Twitch profile image URL, when available
    emotes: list[EmoteItem] = field(
        default_factory=list
    )  # emotes rendered alongside audio


class MessageKind(Enum):
    """Distinguishes chat messages from channel-event announcements."""

    USER = auto()  # regular chatter message — full pipeline
    SYSTEM = auto()  # follow/sub/raid/cheer — spoken directly, no announce window


@dataclass
class QueuedMessage:
    """A message waiting to be spoken via TTS."""

    username: str
    text: str
    kind: MessageKind = field(default=MessageKind.USER)
    emote_names: list[str] = field(
        default_factory=list
    )  # Twitch emote names from message fragments
    avatar_url: str | None = None
