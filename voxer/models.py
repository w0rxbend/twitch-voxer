"""Shared data types passed between the bot, handler, and server layers.

These are plain data carriers with no behaviour.  They live in their own
module so that peripheral layers (server.py, bot.py) can depend on the data
shapes without importing the full business-logic module (handler.py) and
its heavy dependencies (langdetect, emoji, pickledb).
"""

from dataclasses import dataclass, field
from enum import Enum, auto


@dataclass
class EmoteItem:
    """A single emote or emoji to be displayed in the browser overlay."""
    name: str  # display name or raw emoji character
    url: str   # absolute URL to the image asset


@dataclass
class BroadcastEvent:
    """Payload sent over WebSocket to the browser overlay after synthesis."""
    audio_url: str          # relative URL served by AudioServer, e.g. "/audio/<uuid>.mp3"
    username: str           # chatter's Twitch login name
    avatar_url: str | None = None  # Twitch profile image URL, when available
    emotes: list[EmoteItem] = field(default_factory=list)  # emotes rendered alongside audio


class MessageKind(Enum):
    """Distinguishes chat messages from channel-event announcements."""
    USER = auto()    # regular chatter message — full pipeline
    SYSTEM = auto()  # follow/sub/raid/cheer — spoken directly, no announce window


@dataclass
class QueuedMessage:
    """A message waiting to be spoken via TTS."""
    username: str
    text: str
    kind: MessageKind = field(default=MessageKind.USER)
    emote_names: list[str] = field(default_factory=list)  # Twitch emote names from message fragments
    avatar_url: str | None = None
