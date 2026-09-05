"""Environment-variable configuration for twitch-voxer.

All runtime settings are read from environment variables (populated from a
.env file via python-dotenv) into module-level constants at import time.

Required variables default to "" at import time; call validate_config()
once at startup (the composition root does this) to fail fast with a message
that lists ALL missing variables at once.  Deferring the check keeps
`import voxer.handler` (e.g. in tests) from requiring real credentials.
Optional variables fall back to sensible defaults.

The module is laid out as the small parse helpers, then one contiguous block
of constants, then the validation functions, so every reference inside a
function points backwards.
"""

import os
import re
from ipaddress import ip_address, ip_network
from urllib.parse import urlparse

from dotenv import load_dotenv

# Load .env into os.environ before any constant below reads from it.
# Has no effect if the file does not exist (harmless in Docker/CI).
load_dotenv()


class ConfigError(RuntimeError):
    """A setting is missing, unparseable, or outside the range that can work.

    Everything raised by this module uses this type so that the entry point
    (voxer.app.main) can tell "the operator needs to edit their .env file"
    apart from every other RuntimeError the program might raise -- a failed
    speech-model download, for instance, which used to be reported as a
    configuration error and had its traceback thrown away.

    It subclasses RuntimeError rather than Exception so that any existing
    caller written to catch RuntimeError keeps working unchanged.
    """


# ── Parse helpers ─────────────────────────────────────────────────────────────
# Defined above the constants because every numeric and list setting below is
# built by calling one of them while this module is being imported.


def _env_int(
    name: str,
    default: str,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    """Read an environment variable as a whole number, or fail saying which one.

    Written the direct way, `int(os.getenv("VOXER_SERVER_PORT", "8080"))`
    reports a typo as `ValueError: invalid literal for int() with base 10:
    'eighty'`, raised from an `import` statement.  That message names neither
    the setting nor the file it came from, so whoever mistyped it in .env has
    to work out which numeric setting is at fault.  Here the ConfigError names
    the variable and echoes the value.

    `minimum` (and, for ports, `maximum`) reject numbers that parse fine but
    cannot work.  The default minimum of 1 suits every setting here except the
    scheduler's initial delay, where 0 legitimately means "post immediately".
    A value of 0 is otherwise never harmless: asyncio.Queue treats maxsize=0
    as *unbounded*, so accepting it would silently remove the backpressure the
    queue exists to provide, and port 0 asks the operating system for an
    arbitrary free port that nothing else could then connect to.
    """
    raw = os.getenv(name, default)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}, got {value}")
    return value


def _env_name_in_use(preferred: str, deprecated: str) -> str:
    """Return whichever of two names for the same setting the operator has set.

    A setting that has been renamed is readable under both names for a while,
    and everything downstream -- most importantly the error message _env_int
    produces for a bad value -- should talk about the name that is actually in
    the operator's .env file.  Naming the other one sends them looking for a
    variable they have never heard of.

    The preferred name wins when both are set, and is also what is returned
    when neither is, so a fresh install is told about the current name.

    Args:
        preferred: The current name for the setting.
        deprecated: The older name, still accepted.
    """
    if os.getenv(preferred) is None and os.getenv(deprecated) is not None:
        return deprecated
    return preferred


def _env_csv(name: str, default: str) -> list[str]:
    """Read a comma-separated environment variable as a list of trimmed items.

    Surrounding whitespace is stripped from each item and empty items are
    dropped, so a trailing comma or `a, b` rather than `a,b` does not produce
    an entry that matches nothing.  Case is left alone: only one caller wants
    case-insensitive matching, and it lowercases at its own call site.
    """
    return [
        item.strip() for item in os.getenv(name, default).split(",") if item.strip()
    ]


# ── Twitch API credentials ────────────────────────────────────────────────────
# These belong to the *bot* Twitch account's application (dev.twitch.tv console).
# Together with TWITCH_BOT_USERNAME below they are the only required settings:
# user tokens are obtained interactively via the browser OAuth flow on first
# startup and persisted to TOKEN_FILE.
CLIENT_ID: str = os.getenv("TWITCH_CLIENT_ID", "")
CLIENT_SECRET: str = os.getenv("TWITCH_CLIENT_SECRET", "")
# Optional seed tokens: when set AND no token file exists yet, they are added
# to the token store on startup so the browser flow can be skipped entirely
# (useful for restoring a deployment from a known-good token pair).
ACCESS_TOKEN: str = os.getenv("TWITCH_ACCESS_TOKEN", "")
REFRESH_TOKEN: str = os.getenv("TWITCH_REFRESH_TOKEN", "")
# Login name (slug) of the bot Twitch account, used to look up its numeric ID.
# There is deliberately no default.  This used to fall back to one specific
# person's Twitch handle, which meant anyone who cloned this repository and
# forgot the variable got no error at all: the bot resolved a stranger's login
# to a numeric account ID and then ran under that identity for the whole
# session.  Empty here, and rejected by validate_config() at startup instead.
BOT_USERNAME: str = os.getenv("TWITCH_BOT_USERNAME", "")

# The credentials that must be non-empty before anything can talk to Twitch,
# each paired with the environment variable it came from.  The pairing exists
# because the check and the error message need different halves: the check runs
# against the *values* the rest of the program consumes, while the message has
# to name the *variable* an operator edits in .env.
_REQUIRED_CREDENTIALS: tuple[tuple[str, str], ...] = (
    ("TWITCH_CLIENT_ID", CLIENT_ID),
    ("TWITCH_CLIENT_SECRET", CLIENT_SECRET),
)

# ── Storage paths ─────────────────────────────────────────────────────────────
# pickledb files are JSON under the hood; paths are relative to the working dir
# unless overridden (Docker sets them to /data/…).
DB_PATH: str = os.getenv("VOXER_DB_PATH", "data/voices.json")
AUDIO_DIR: str = os.getenv("VOXER_AUDIO_DIR", "audio")
EMOTES_DB_PATH: str = os.getenv("VOXER_EMOTES_DB_PATH", "emotes/emotes.db")
TIMESTAMPS_DB_PATH: str = os.getenv("VOXER_TIMESTAMPS_DB_PATH", "data/timestamps.json")
MESSAGES_PATH: str = os.getenv("VOXER_MESSAGES_PATH", "data/messages.json")
# Directory of custom voice JSON files (*.json) loaded by TTSService at startup.
VOICES_DIR: str = os.getenv("VOXER_VOICES_DIR", "voices")

# ── HTTP / WebSocket server ───────────────────────────────────────────────────
SERVER_HOST: str = os.getenv("VOXER_SERVER_HOST", "127.0.0.1").strip()
SERVER_PORT: int = _env_int("VOXER_SERVER_PORT", "8080", maximum=65535)
# Network listeners require a separate overlay credential, never a Twitch token.
OVERLAY_TOKEN: str = os.getenv("VOXER_OVERLAY_TOKEN", "")
ALLOWED_HOSTS: tuple[str, ...] = tuple(_env_csv("VOXER_ALLOWED_HOSTS", ""))
TRUSTED_PROXIES: tuple[str, ...] = tuple(_env_csv("VOXER_TRUSTED_PROXIES", ""))
MAX_WS_CLIENTS: int = _env_int("VOXER_MAX_WS_CLIENTS", "8", maximum=256)
MAX_PENDING_PER_CLIENT: int = _env_int(
    "VOXER_MAX_PENDING_PER_CLIENT", "64", maximum=1024
)
# How many seconds one overlay client may take to accept a WebSocket message
# before the server gives up on it and drops the connection.  Broadcasts are
# sent to clients one after another from the same task that drains the message
# queue, so a browser that stops reading its socket (a paused OBS source, a
# suspended laptop) holds up every message behind it: nothing raises, the send
# simply never finishes.  This is the deadline that turns that silent stall
# into a dropped client and one warning line.  Five seconds is far longer than
# a healthy client on the same machine ever needs, and far shorter than the
# ~40 s uvicorn's own keepalive takes to notice.
WS_SEND_TIMEOUT: int = _env_int("VOXER_WS_SEND_TIMEOUT", "5")
# How the overlay's audio directory is kept from growing without bound.  An MP3
# is normally deleted the moment the browser reports it finished playing, but a
# browser that crashes or is refreshed mid-clip never sends that report, so the
# file stays behind with nothing to remove it.  A background task sweeps the
# directory every AUDIO_SWEEP_INTERVAL_SECS seconds and deletes every MP3 that
# has not been written to for at least AUDIO_MAX_AGE_SECS.
#
# The age is the safety margin, and it is deliberately generous: a file younger
# than the cutoff may still be queued for synthesis, in flight to a browser or
# playing right now, and deleting one of those would cut a clip off mid-word.
# Five minutes is far longer than the longest plausible chat message plus the
# time it can spend waiting in the message queue, and still short enough that a
# multi-day stream never accumulates more than a handful of dead files.
AUDIO_SWEEP_INTERVAL_SECS: int = _env_int("VOXER_AUDIO_SWEEP_INTERVAL_SECS", "300")
AUDIO_MAX_AGE_SECS: int = _env_int("VOXER_AUDIO_MAX_AGE_SECS", "300")

# ── OAuth / token persistence ─────────────────────────────────────────────────
# twitchio's built-in web adapter serves the OAuth flow:
#   http://localhost:<OAUTH_PORT>/oauth          — starts the Twitch authorization
#   http://localhost:<OAUTH_PORT>/oauth/callback — must be registered as an OAuth
#                                                  Redirect URL in the Twitch dev console
# In Docker the bind host must be 0.0.0.0 so the published port is reachable.
OAUTH_HOST: str = os.getenv("VOXER_OAUTH_HOST", "localhost")
OAUTH_PORT: int = _env_int("VOXER_OAUTH_PORT", "4343", maximum=65535)
# Full OAuth redirect URL — the URL registered as "OAuth Redirect URL" in the
# Twitch dev console.  Default: http://localhost:<OAUTH_PORT>/oauth/callback.
# Override for a reverse-proxy setup, e.g. https://bot.example.org/oauth/callback
# (a non-localhost host implies HTTPS, which Twitch requires anyway).
OAUTH_REDIRECT_URL: str = (
    os.getenv("VOXER_OAUTH_REDIRECT_URL", "")
    or f"http://localhost:{OAUTH_PORT}/oauth/callback"
)
# Where user access/refresh tokens are persisted between runs (twitchio JSON
# format).  Lives under the data dir so Docker keeps it in the /data volume.
TOKEN_FILE: str = os.getenv("VOXER_TOKEN_FILE", "data/tokens.json")

# ── Scheduler ─────────────────────────────────────────────────────────────────
# How long to wait before sending the first scheduled message (lets the bot
# finish its EventSub handshake before posting to chat).
SCHEDULER_INITIAL_DELAY: int = _env_int(
    "VOXER_SCHEDULER_INITIAL_DELAY", "10", minimum=0
)
# How long to wait before re-checking when the message list is empty or invalid.
# The normal posting cadence is NOT this value — it is derived from each
# message's frequency_per_hour in data/messages.json.
# VOXER_SCHEDULER_INTERVAL is accepted as a deprecated alias of the new name.
# Which of the two names is in use is worked out first, so that a bad value is
# reported under the variable the operator actually set.  Resolving the alias
# into _env_int's `default` argument instead would have told someone upgrading
# from an older release that VOXER_SCHEDULER_EMPTY_RETRY_DELAY was at fault --
# a variable they have never heard of and cannot find in their .env file --
# which is the exact confusion _env_int exists to prevent.
SCHEDULER_EMPTY_RETRY_DELAY: int = _env_int(
    _env_name_in_use("VOXER_SCHEDULER_EMPTY_RETRY_DELAY", "VOXER_SCHEDULER_INTERVAL"),
    "600",
)

# ── Message queue ─────────────────────────────────────────────────────────────
# How many messages may wait for synthesis at once.  One message costs a full
# TTS run plus an ffmpeg conversion — seconds each — so an unbounded queue would
# let a chat burst push the overlay minutes behind live chat.  When the queue is
# full, new *chat* messages are dropped (a line read out a minute late is worth
# nothing); channel events wait for room instead, because they are rare and
# losing a raid alert is worse than a short delay.
# 0 is rejected rather than treated as "no limit": asyncio.Queue reads maxsize=0
# as unbounded, which would turn the paragraph above into a comment describing
# something the code no longer does.
MESSAGE_QUEUE_MAXSIZE: int = _env_int("VOXER_MESSAGE_QUEUE_MAXSIZE", "20")
MAX_MESSAGE_CHARS: int = _env_int("VOXER_MAX_MESSAGE_CHARS", "500", maximum=500)
MAX_SPEECH_CHARS: int = _env_int("VOXER_MAX_SPEECH_CHARS", "1000", maximum=2000)
MAX_MESSAGE_AGE_SECS: int = _env_int("VOXER_MAX_MESSAGE_AGE_SECS", "60")
USER_COOLDOWN_SECS: int = _env_int("VOXER_USER_COOLDOWN_SECS", "2", minimum=0)

# ── Announcement behaviour ────────────────────────────────────────────────────
# Time window (seconds) during which a user's name is NOT re-announced.
# After this window elapses, the next message prepends "username says:".
ANNOUNCE_WINDOW_SECS: int = _env_int("VOXER_ANNOUNCE_WINDOW_SECS", "300")

# Usernames that never receive the "username says:" prefix (e.g. the bot itself).
# Comma-separated list; comparison is case-insensitive.
# Defaults to the bot's own login to avoid self-announcement loops.
NO_ANNOUNCE_USERS: frozenset[str] = frozenset(
    user.lower() for user in _env_csv("VOXER_NO_ANNOUNCE_USERS", BOT_USERNAME)
)

# Comma-separated list of MP3 files played for emote-only messages.
# Each file is picked at random; falls back to silence if the list is empty.
EMOTE_SOUND_PATHS: list[str] = _env_csv(
    "VOXER_EMOTE_SOUND_PATHS",
    "emotes/slack-message.mp3,emotes/discord.mp3",
)

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("VOXER_LOG_LEVEL", "INFO").upper()


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
    return parsed.netloc, path


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
    try:
        parsed = urlparse(url)
        # Accessing .port also validates malformed and out-of-range ports.
        parsed.port
    except ValueError:
        raise ConfigError(
            "VOXER_OAUTH_REDIRECT_URL has an invalid host or port"
        ) from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(
            f"VOXER_OAUTH_REDIRECT_URL must be a full http(s) URL, got: {url!r}"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(
            "VOXER_OAUTH_REDIRECT_URL cannot contain credentials, a query or a fragment"
        )
    is_localhost = parsed.hostname in ("localhost", "127.0.0.1")
    if not is_localhost and parsed.scheme != "https":
        raise ConfigError(
            "VOXER_OAUTH_REDIRECT_URL must use https:// for a non-localhost "
            f"host (Twitch requires it, and the authorization flow builds an "
            f"https redirect URI for public domains), got: {url!r}"
        )


def validate_credentials() -> None:
    """Check the Twitch application credentials, naming every missing one.

    All of them are reported in a single message, so an operator who left two
    variables blank learns about both at once instead of one restart at a time.

    The check reads CLIENT_ID and CLIENT_SECRET — the constants every other
    module actually consumes — rather than calling os.getenv() a second time.
    Re-reading the environment agreed with the constants only by coincidence:
    both happened to come from the same process environment, so any later
    stripping, aliasing or fallback applied while building a constant would
    have been invisible to the check, and the program could start with a
    credential the validator had approved under a different value.

    This is a separate function from validate_config() because the emote-fetch
    script (voxer.fetch_emotes) needs exactly these two values and nothing
    else: it runs its own local OAuth callback server and never signs in as
    the bot account, so enforcing the bot's settings there would reject a
    setup that works perfectly well.
    """
    missing = [name for name, value in _REQUIRED_CREDENTIALS if not value.strip()]
    if missing:
        raise ConfigError(
            "Required environment variables are not set: " + ", ".join(missing)
        )


def validate_overlay_access(host: str, token: str) -> None:
    """Require an independent, URL-safe credential for a network listener."""
    if not host:
        raise ConfigError("VOXER_SERVER_HOST must not be empty")
    try:
        is_loopback = ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.lower() == "localhost"
    if token and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ConfigError(
            "VOXER_OVERLAY_TOKEN must be 32-256 URL-safe characters; "
            "generate it with secrets.token_urlsafe(32)"
        )
    if not is_loopback and not token:
        raise ConfigError(
            "VOXER_OVERLAY_TOKEN is required when VOXER_SERVER_HOST "
            "listens beyond localhost"
        )


def validate_trusted_proxies(addresses: tuple[str, ...]) -> None:
    """Forwarded headers are opt-in and limited to explicit proxy addresses."""
    for address in addresses:
        try:
            network = ip_network(address, strict=False)
        except ValueError:
            raise ConfigError(
                "VOXER_TRUSTED_PROXIES must contain IP addresses or CIDR networks"
            ) from None
        if network.prefixlen == 0:
            raise ConfigError("VOXER_TRUSTED_PROXIES cannot trust every address")


def validate_config() -> None:
    """Check the whole configuration, raising ConfigError on the first problem.

    Three rules are applied, in order:
      1. the Twitch application credentials are present (validate_credentials);
      2. the bot account's login name is set;
      3. the OAuth redirect URL is one Twitch could actually redirect to.

    Called once by the composition root before any component starts.
    """
    validate_credentials()
    if not BOT_USERNAME:
        raise ConfigError(
            "TWITCH_BOT_USERNAME is not set: it must be the Twitch login name "
            "of the account this bot signs in as. There is no default, because "
            "a wrong one is silent — the login is resolved to a numeric Twitch "
            "account ID and used as the bot's identity for the whole run."
        )
    validate_redirect_url(OAUTH_REDIRECT_URL)
    redirect = urlparse(OAUTH_REDIRECT_URL)
    if (
        redirect.scheme == "http"
        and redirect.hostname in ("localhost", "127.0.0.1")
        and (redirect.port or 80) != OAUTH_PORT
    ):
        raise ConfigError(
            "VOXER_OAUTH_REDIRECT_URL port must match VOXER_OAUTH_PORT "
            "for the local authorization server"
        )
    if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", BOT_USERNAME):
        raise ConfigError("TWITCH_BOT_USERNAME must be a Twitch login name")
    validate_overlay_access(SERVER_HOST, OVERLAY_TOKEN)
    validate_trusted_proxies(TRUSTED_PROXIES)
