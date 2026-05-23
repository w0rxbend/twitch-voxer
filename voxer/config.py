import os

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if value is None:
        raise RuntimeError(f"Required environment variable {key!r} is not set")
    return value


CLIENT_ID: str     = _require("TWITCH_CLIENT_ID")
CLIENT_SECRET: str = _require("TWITCH_CLIENT_SECRET")
ACCESS_TOKEN: str  = _require("TWITCH_ACCESS_TOKEN")
REFRESH_TOKEN: str = _require("TWITCH_REFRESH_TOKEN")
BOT_USERNAME: str  = str(os.getenv("TWITCH_BOT_USERNAME", "worxbend"))
DB_PATH: str       = str(os.getenv("VOXER_DB_PATH", "data/voices.json"))
AUDIO_DIR: str     = str(os.getenv("VOXER_AUDIO_DIR", "audio"))
SERVER_HOST: str   = str(os.getenv("VOXER_SERVER_HOST", "0.0.0.0"))
SERVER_PORT: int   = int(os.getenv("VOXER_SERVER_PORT", "8080"))
SCHEDULER_INTERVAL: int       = int(os.getenv("VOXER_SCHEDULER_INTERVAL", "600"))
SCHEDULER_INITIAL_DELAY: int  = int(os.getenv("VOXER_SCHEDULER_INITIAL_DELAY", "10"))
MESSAGES_PATH: str = str(os.getenv("VOXER_MESSAGES_PATH", "messages.json"))
VOICES_DIR: str    = str(os.getenv("VOXER_VOICES_DIR", "voices"))
EMOTES_DB_PATH: str     = str(os.getenv("VOXER_EMOTES_DB_PATH", "emotes/emotes.db"))
TIMESTAMPS_DB_PATH: str = str(os.getenv("VOXER_TIMESTAMPS_DB_PATH", "data/timestamps.json"))
ANNOUNCE_WINDOW_SECS: int = int(os.getenv("VOXER_ANNOUNCE_WINDOW_SECS", "300"))
LOG_LEVEL: str     = str(os.getenv("VOXER_LOG_LEVEL", "INFO")).upper()
NO_ANNOUNCE_USERS: frozenset[str] = frozenset(
    u.strip().lower()
    for u in os.getenv("VOXER_NO_ANNOUNCE_USERS", BOT_USERNAME).split(",")
    if u.strip()
)
EMOTE_SOUND_PATHS: list[str] = [
    p.strip()
    for p in os.getenv(
        "VOXER_EMOTE_SOUND_PATHS",
        "emotes/slack-message.mp3,emotes/discord.mp3",
    ).split(",")
    if p.strip()
]
