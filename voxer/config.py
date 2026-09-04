"""Environment-variable configuration for twitch-voxer.

All runtime settings are read from environment variables (populated from a
.env file via python-dotenv) into module-level constants at import time.

Required variables default to "" at import time; call validate_config()
once at startup (the composition root does this) to fail fast with a message
that lists ALL missing variables at once.  Deferring the check keeps
`import voxer.handler` (e.g. in tests) from requiring real credentials.
Optional variables fall back to sensible defaults.

The module is laid out as one contiguous block of constants followed by the
validation functions, so every reference inside a function points backwards.
"""

import os
from urllib.parse import urlparse

from dotenv import load_dotenv

# Load .env into os.environ before any constant below reads from it.
# Has no effect if the file does not exist (harmless in Docker/CI).
load_dotenv()

# ── Twitch API credentials ────────────────────────────────────────────────────
# These belong to the *bot* Twitch account's application (dev.twitch.tv console).
# They are the ONLY required settings: user tokens are obtained interactively
# via the browser OAuth flow on first startup and persisted to TOKEN_FILE.
CLIENT_ID: str = os.getenv("TWITCH_CLIENT_ID", "")
CLIENT_SECRET: str = os.getenv("TWITCH_CLIENT_SECRET", "")
# Optional seed tokens: when set AND no token file exists yet, they are added
# to the token store on startup so the browser flow can be skipped entirely
# (useful for restoring a deployment from a known-good token pair).
ACCESS_TOKEN: str = os.getenv("TWITCH_ACCESS_TOKEN", "")
REFRESH_TOKEN: str = os.getenv("TWITCH_REFRESH_TOKEN", "")
# Login name (slug) of the bot Twitch account, used to look up its numeric ID.
BOT_USERNAME: str = str(os.getenv("TWITCH_BOT_USERNAME", "worxbend"))

# Names of the environment variables that must be non-empty for the bot to run.
_REQUIRED_VARS: tuple[str, ...] = (
    "TWITCH_CLIENT_ID",
    "TWITCH_CLIENT_SECRET",
)

# ── Storage paths ─────────────────────────────────────────────────────────────
# pickledb files are JSON under the hood; paths are relative to the working dir
# unless overridden (Docker sets them to /data/…).
DB_PATH: str = str(os.getenv("VOXER_DB_PATH", "data/voices.json"))
AUDIO_DIR: str = str(os.getenv("VOXER_AUDIO_DIR", "audio"))
EMOTES_DB_PATH: str = str(os.getenv("VOXER_EMOTES_DB_PATH", "emotes/emotes.db"))
TIMESTAMPS_DB_PATH: str = str(
    os.getenv("VOXER_TIMESTAMPS_DB_PATH", "data/timestamps.json")
)
MESSAGES_PATH: str = str(os.getenv("VOXER_MESSAGES_PATH", "data/messages.json"))
# Directory of custom voice JSON files (*.json) loaded by TTSService at startup.
VOICES_DIR: str = str(os.getenv("VOXER_VOICES_DIR", "voices"))

# ── HTTP / WebSocket server ───────────────────────────────────────────────────
SERVER_HOST: str = str(os.getenv("VOXER_SERVER_HOST", "0.0.0.0"))
SERVER_PORT: int = int(os.getenv("VOXER_SERVER_PORT", "8080"))

# ── OAuth / token persistence ─────────────────────────────────────────────────
# twitchio's built-in web adapter serves the OAuth flow:
#   http://localhost:<OAUTH_PORT>/oauth          — starts the Twitch authorization
#   http://localhost:<OAUTH_PORT>/oauth/callback — must be registered as an OAuth
#                                                  Redirect URL in the Twitch dev console
# In Docker the bind host must be 0.0.0.0 so the published port is reachable.
OAUTH_HOST: str = str(os.getenv("VOXER_OAUTH_HOST", "localhost"))
OAUTH_PORT: int = int(os.getenv("VOXER_OAUTH_PORT", "4343"))
# Full OAuth redirect URL — the URL registered as "OAuth Redirect URL" in the
# Twitch dev console.  Default: http://localhost:<OAUTH_PORT>/oauth/callback.
# Override for a reverse-proxy setup, e.g. https://bot.example.org/oauth/callback
# (a non-localhost host implies HTTPS, which Twitch requires anyway).
OAUTH_REDIRECT_URL: str = (
    str(os.getenv("VOXER_OAUTH_REDIRECT_URL", ""))
    or f"http://localhost:{OAUTH_PORT}/oauth/callback"
)
# Where user access/refresh tokens are persisted between runs (twitchio JSON
# format).  Lives under the data dir so Docker keeps it in the /data volume.
TOKEN_FILE: str = str(os.getenv("VOXER_TOKEN_FILE", "data/tokens.json"))

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

# ── Message queue ─────────────────────────────────────────────────────────────
# How many messages may wait for synthesis at once.  One message costs a full
# TTS run plus an ffmpeg conversion — seconds each — so an unbounded queue would
# let a chat burst push the overlay minutes behind live chat.  When the queue is
# full, new *chat* messages are dropped (a line read out a minute late is worth
# nothing); channel events wait for room instead, because they are rare and
# losing a raid alert is worse than a short delay.
MESSAGE_QUEUE_MAXSIZE: int = int(os.getenv("VOXER_MESSAGE_QUEUE_MAXSIZE", "20"))

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


# ── Startup validation ────────────────────────────────────────────────────────
# Defined after every constant above so each reference below reads backwards,
# the way the module is read.


def parse_redirect_url(url: str) -> tuple[str | None, str]:
    """Split an OAuth redirect URL into twitchio adapter arguments.

    twitchio's web adapter takes a `domain` (public host, forces HTTPS) and a
    `redirect_path`; it does not take a full URL.  This translates:
      http://localhost:4343/oauth/callback      → (None, "oauth/callback")
      https://bot.example.org/oauth/callback    → ("bot.example.org", "oauth/callback")

    A localhost/127.0.0.1 host maps to no domain (the adapter then serves on
    its bind host/port directly); anything else becomes the public domain.
    """
    parsed = urlparse(url if "//" in url else f"//{url}")
    host = parsed.hostname or ""
    path = parsed.path.strip("/") or "oauth/callback"
    if host in ("", "localhost", "127.0.0.1"):
        return None, path
    return host, path


def validate_redirect_url(url: str) -> None:
    """Reject a redirect URL that could never complete the OAuth flow.

    Called from validate_config() so a misconfigured deployment fails at
    startup with a clear message instead of during the browser flow.  Rules:
      - the URL must be http:// or https:// with a host;
      - a non-localhost host must use https:// — Twitch only allows plain
        http for localhost redirect URLs, and twitchio's adapter builds the
        code-exchange redirect URI with https for any public domain, so both
        URIs would mismatch byte-for-byte otherwise.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RuntimeError(
            f"VOXER_OAUTH_REDIRECT_URL must be a full http(s) URL, got: {url!r}"
        )
    is_localhost = parsed.hostname in ("localhost", "127.0.0.1")
    if not is_localhost and parsed.scheme != "https":
        raise RuntimeError(
            "VOXER_OAUTH_REDIRECT_URL must use https:// for a non-localhost "
            f"host (Twitch requires it, and the authorization flow builds an "
            f"https redirect URI for public domains), got: {url!r}"
        )


def validate_config() -> None:
    """Check the whole configuration, raising RuntimeError on the first problem.

    Two rules are applied, in order:
      1. every required environment variable is set — reported as one list, so
         a misconfigured deployment learns about all of them at once rather
         than one restart at a time;
      2. the OAuth redirect URL is one Twitch could actually redirect to.

    Called once by the composition root before any component starts.
    """
    missing = [key for key in _REQUIRED_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(
            "Required environment variables are not set: " + ", ".join(missing)
        )
    validate_redirect_url(OAUTH_REDIRECT_URL)
