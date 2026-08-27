"""Environment-variable configuration for twitch-voxer.

All runtime settings are read from environment variables (populated from a
.env file via python-dotenv) into module-level constants at import time.

Required variables default to "" at import time; call validate_required()
once at startup (the composition root does this) to fail fast with a message
that lists ALL missing variables at once.  Deferring the check keeps
`import voxer.handler` (e.g. in tests) from requiring real credentials.
Optional variables fall back to sensible defaults.
"""

import os

from dotenv import load_dotenv

# Load .env into os.environ before any constant below reads from it.
# Has no effect if the file does not exist (harmless in Docker/CI).
load_dotenv()

# ── Twitch API credentials (all required) ────────────────────────────────────
# These belong to the *bot* Twitch account, not the broadcaster's account.
CLIENT_ID: str     = os.getenv("TWITCH_CLIENT_ID", "")
CLIENT_SECRET: str = os.getenv("TWITCH_CLIENT_SECRET", "")
# Access + refresh tokens are stored from the initial OAuth flow.
# twitchio handles automatic token refresh using the refresh token.
ACCESS_TOKEN: str  = os.getenv("TWITCH_ACCESS_TOKEN", "")
REFRESH_TOKEN: str = os.getenv("TWITCH_REFRESH_TOKEN", "")

# Names of the environment variables that must be non-empty for the bot to run.
_REQUIRED_VARS: tuple[str, ...] = (
    "TWITCH_CLIENT_ID",
    "TWITCH_CLIENT_SECRET",
    "TWITCH_ACCESS_TOKEN",
    "TWITCH_REFRESH_TOKEN",
)


def validate_required() -> None:
    """Raise RuntimeError listing every required environment variable that is unset.

    Called once by the composition root before any component starts, so a
    misconfigured deployment fails immediately with a complete list instead
    of one variable at a time.
    """
    missing = [key for key in _REQUIRED_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(
            "Required environment variables are not set: " + ", ".join(missing)
        )
# Login name (slug) of the bot Twitch account, used to look up its numeric ID.
BOT_USERNAME: str  = str(os.getenv("TWITCH_BOT_USERNAME", "worxbend"))

# ── Storage paths ─────────────────────────────────────────────────────────────
# pickledb files are JSON under the hood; paths are relative to the working dir
# unless overridden (Docker sets them to /data/…).
DB_PATH: str            = str(os.getenv("VOXER_DB_PATH", "data/voices.json"))
AUDIO_DIR: str          = str(os.getenv("VOXER_AUDIO_DIR", "audio"))
EMOTES_DB_PATH: str     = str(os.getenv("VOXER_EMOTES_DB_PATH", "emotes/emotes.db"))
TIMESTAMPS_DB_PATH: str = str(os.getenv("VOXER_TIMESTAMPS_DB_PATH", "data/timestamps.json"))
MESSAGES_PATH: str      = str(os.getenv("VOXER_MESSAGES_PATH", "data/messages.json"))
# Directory of custom voice JSON files (*.json) loaded by TTSService at startup.
VOICES_DIR: str         = str(os.getenv("VOXER_VOICES_DIR", "voices"))

# ── HTTP / WebSocket server ───────────────────────────────────────────────────
SERVER_HOST: str = str(os.getenv("VOXER_SERVER_HOST", "0.0.0.0"))
SERVER_PORT: int = int(os.getenv("VOXER_SERVER_PORT", "8080"))

# ── Scheduler ─────────────────────────────────────────────────────────────────
# How long to wait before sending the first scheduled message (lets the bot
# finish its EventSub handshake before posting to chat).
SCHEDULER_INITIAL_DELAY: int = int(os.getenv("VOXER_SCHEDULER_INITIAL_DELAY", "10"))
# How long to wait before re-checking when the message list is empty or invalid.
# The normal posting cadence is NOT this value — it is derived from each
# message's frequency_per_hour in data/messages.json.
# VOXER_SCHEDULER_INTERVAL is accepted as a deprecated alias of the new name.
SCHEDULER_EMPTY_RETRY_DELAY: int = int(
    os.getenv(
        "VOXER_SCHEDULER_EMPTY_RETRY_DELAY",
        os.getenv("VOXER_SCHEDULER_INTERVAL", "600"),
    )
)

# ── Announcement behaviour ────────────────────────────────────────────────────
# Time window (seconds) during which a user's name is NOT re-announced.
# After this window elapses, the next message prepends "username says:".
ANNOUNCE_WINDOW_SECS: int = int(os.getenv("VOXER_ANNOUNCE_WINDOW_SECS", "300"))

# Usernames that never receive the "username says:" prefix (e.g. the bot itself).
# Comma-separated list; comparison is case-insensitive.
# Defaults to the bot's own login to avoid self-announcement loops.
NO_ANNOUNCE_USERS: frozenset[str] = frozenset(
    u.strip().lower()
    for u in os.getenv("VOXER_NO_ANNOUNCE_USERS", BOT_USERNAME).split(",")
    if u.strip()
)

# Comma-separated list of MP3 files played for emote-only messages.
# Each file is picked at random; falls back to silence if the list is empty.
EMOTE_SOUND_PATHS: list[str] = [
    p.strip()
    for p in os.getenv(
        "VOXER_EMOTE_SOUND_PATHS",
        "emotes/slack-message.mp3,emotes/discord.mp3",
    ).split(",")
    if p.strip()
]

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL: str = str(os.getenv("VOXER_LOG_LEVEL", "INFO")).upper()
