"""Twitch adapter layer for twitch-voxer.

VoxBot subclasses twitchio's AutoBot which handles:
  - EventSub WebSocket connection management
  - Automatic token refresh
  - Command prefix routing (prefix="!")

This module is intentionally thin: it translates raw Twitch events into
QueuedMessages and drops them onto the shared asyncio.Queue.  All business
logic (voice selection, TTS synthesis, language detection) lives in handler.py.

Subscriptions are registered via subscribe_for(), which is called:
  - from event_oauth_authorized, when a broadcaster completes the OAuth flow
    via the twitchio built-in /oauth route, and
  - from the composition root on every startup for the bot's own channel
    (Conduit subscriptions expire after 72h of downtime).

Authentication uses twitchio's built-in web adapter: /oauth starts the Twitch
authorization redirect and /oauth/callback receives the granted code.  Tokens
are persisted to config.TOKEN_FILE and auto-refreshed by twitchio.
"""

import asyncio
import logging
import time
import webbrowser
from collections import OrderedDict
from collections.abc import Iterable
from typing import Final
from urllib.parse import urlsplit

import twitchio
from twitchio import (
    ChatMessage,
    ChatMessageFragment,
    Client,
    Scopes,
    eventsub,
    MultiSubscribeError,
    MultiSubscribePayload,
)
from twitchio.authentication import UserTokenPayload, ValidateTokenPayload
from twitchio.user import Chatter, PartialUser
from twitchio.ext import commands
from twitchio.web import AiohttpAdapter

from . import config
from .events import (
    cheer_message,
    follow_message,
    gift_message,
    raid_message,
    resub_message,
    sub_message,
)
from .models import MessageKind, QueuedMessage
from .oauth import SecureOAuthAdapter
from .token_store import TokenFileLock, read_tokens, write_json_atomic

LOGGER: logging.Logger = logging.getLogger(__name__)

# OAuth scopes requested when a user authorizes the app via the browser flow.
# Passed to twitchio as the client default, so visiting /oauth without a
# ?scopes= query uses exactly this set.
#   user:read:chat / user:bot / channel:bot — receive chat via EventSub
#   user:write:chat                         — send scheduled messages to chat
#   moderator:read:followers                — follow events (+ fetch_emotes.py)
#   channel:read:subscriptions              — sub / resub / gift events
#   bits:read                               — cheer events
#   user:read:follows                       — fetch_emotes.py channel discovery
OAUTH_SCOPES: Scopes = Scopes(
    [
        "user:read:chat",
        "user:write:chat",
        "user:bot",
        "channel:bot",
        "moderator:read:followers",
        "channel:read:subscriptions",
        "bits:read",
        "user:read:follows",
    ]
)

# Twitch can omit the login name in rare event payloads (deleted accounts, and
# gifts/cheers the sender chose to hide).  Every announcement still needs some
# name to speak and to show on the overlay, so these two stand in: "unknown"
# where a name was expected but missing, "anonymous" where Twitch deliberately
# withheld it.
_UNKNOWN_USER: Final[str] = "unknown"
_ANONYMOUS_USER: Final[str] = "anonymous"

# HTTP 409 Conflict is what Twitch answers when the conduit subscription being
# requested is already active.  We re-subscribe on every boot (see
# subscribe_for), so this is the expected reply, not an error.
_SUBSCRIPTION_ALREADY_EXISTS: Final[int] = 409


def split_fragments(
    fragments: Iterable[ChatMessageFragment],
) -> tuple[str, list[str]]:
    """Split a chat message's fragments into speakable text and emote names.

    Twitch does not deliver a chat message as one string.  It delivers a list of
    typed fragments: ``text`` for ordinary words, ``emote`` for a channel or
    global emote, plus ``cheermote`` and ``mention`` kinds we do not use here.

    Only ``text`` fragments are worth speaking — reading "Kappa Kappa Kappa"
    aloud is noise — so they are joined with single spaces and stripped.  The
    ``emote`` fragments carry the emote's name in their ``text`` field, and are
    collected separately so the overlay can look up and display their images.

    Args:
        fragments: The fragment list from a chat-message event payload.

    Returns:
        A ``(text, emote_names)`` pair.  Either half can be empty: an emote-only
        message has no text, and a plain sentence has no emotes.
    """
    text_parts: list[str] = []
    emote_names: list[str] = []
    for fragment in fragments:
        if fragment.type == "text":
            text_parts.append(fragment.text)
        elif fragment.type == "emote":
            emote_names.append(fragment.text)
    return " ".join(text_parts).strip(), emote_names


def classify_subscribe_errors(
    errors: Iterable[MultiSubscribeError],
) -> tuple[list[MultiSubscribeError], list[MultiSubscribeError]]:
    """Separate "already subscribed" replies from genuine subscription failures.

    ``multi_subscribe`` reports every subscription it could not create as an
    error, but one of those "errors" is routine: asking for a subscription that
    is already active answers HTTP 409 Conflict, which happens on every restart
    because we re-register the bot's own channel each boot.  Treating 409 like a
    real failure would print a warning on every single start and train the
    operator to ignore the one message that matters.

    Args:
        errors: The ``errors`` list from a ``MultiSubscribePayload``.

    Returns:
        A ``(duplicates, failures)`` pair, each preserving the input order:
        duplicates are the 409s (log at debug), failures are everything else
        (log at warning).
    """
    duplicates: list[MultiSubscribeError] = []
    failures: list[MultiSubscribeError] = []
    for error in errors:
        if error.error.status == _SUBSCRIPTION_ALREADY_EXISTS:
            duplicates.append(error)
        else:
            failures.append(error)
    return duplicates, failures


def oauth_start_url(redirect_url: str, oauth_port: int) -> str:
    """Build the URL a human opens to start the one-time OAuth authorization.

    This is not the redirect URL Twitch calls back on — it is the ``/oauth``
    route twitchio's own web adapter serves, which bounces the browser to
    Twitch's consent screen.

    Which host to use is decided by the *redirect* URL, because both have to be
    served by the same adapter.  When that URL names a public domain (anything
    other than localhost), twitchio's adapter serves the route under that domain
    over HTTPS, so the link must use it too.  When it does not, the adapter
    serves on its own bind host and port, and ``localhost`` is the address that
    works both on the host machine and through a published Docker port.

    Args:
        redirect_url: The value of ``VOXER_OAUTH_REDIRECT_URL``.
        oauth_port: The port the adapter binds, used only in the localhost case.

    Returns:
        An absolute URL ending in ``/oauth``.
    """
    parsed = urlsplit(redirect_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/oauth"
    return f"http://localhost:{oauth_port}/oauth"


def _display_name(user: PartialUser) -> str:
    """Return a user's login name, or a placeholder when Twitch omitted it.

    twitchio types ``name`` as ``str | None`` because Twitch can send an event
    for an account whose login is no longer resolvable.  Every caller here needs
    a real string (it is spoken aloud and rendered on the overlay), so the one
    fallback lives in this one place.
    """
    return user.name or _UNKNOWN_USER


async def get_user_id(username: str) -> str:
    """Fetch Twitch user ID by login name.

    Opens a short-lived API client, makes one GET /users call, then closes.
    Called once at startup to resolve BOT_USERNAME → numeric ID.

    Args:
        username: Twitch login name (slug, not display name).

    Returns:
        User ID string.

    Raises:
        ValueError: If user not found.
    """
    async with Client(
        client_id=config.CLIENT_ID, client_secret=config.CLIENT_SECRET
    ) as client:
        # load_tokens/save_tokens off: this throwaway client only needs an app
        # token, and the defaults would read AND rewrite ".tio.tokens.json" in
        # the working directory — a file this project never uses.
        await client.login(load_tokens=False, save_tokens=False)
        users = await client.fetch_users(logins=[username])
        if not users:
            raise ValueError(f"User not found: {username}")
        return users[0].id


class VoxBot(commands.AutoBot):
    """Twitch EventSub bot that feeds chat events into the TTS message queue.

    Inherits from AutoBot which manages the EventSub WebSocket, token storage,
    and the built-in OAuth flow: /oauth (the page a human opens to start the
    authorization) and /oauth/callback (the redirect target Twitch sends the
    granted code back to).
    """

    def __init__(
        self,
        *,
        bot_id: str,
        message_queue: asyncio.Queue["QueuedMessage"],
        max_message_chars: int = config.MAX_MESSAGE_CHARS,
        user_cooldown_secs: float = config.USER_COOLDOWN_SECS,
    ) -> None:
        """Initialize the Twitch bot with its message queue.

        Args:
            bot_id: Twitch user ID of the bot account.
            message_queue: Queue for dispatching chat messages to the handler.
        """
        self._message_queue = message_queue
        if max_message_chars < 1 or user_cooldown_secs < 0:
            raise ValueError("Message limit must be positive and cooldown nonnegative")
        self._max_message_chars = max_message_chars
        self._user_cooldown_secs = user_cooldown_secs
        self._avatar_url_cache: OrderedDict[str, tuple[float, str | None]] = (
            OrderedDict()
        )
        self._avatar_pending: set[str] = set()
        self._seen_events: OrderedDict[str, float] = OrderedDict()
        self._user_last_message: OrderedDict[str, float] = OrderedDict()
        self._token_owner: TokenFileLock | None = None
        self._token_save_lock = asyncio.Lock()
        self._token_admission_lock = asyncio.Lock()
        # Set once a user token for the bot's own account is available (loaded
        # from the token file, seeded from env, or granted via the browser
        # OAuth flow).  ensure_authorized() awaits it before chat-dependent
        # components start.
        self.bot_authorized: asyncio.Event = asyncio.Event()
        super().__init__(
            client_id=config.CLIENT_ID,
            client_secret=config.CLIENT_SECRET,
            bot_id=bot_id,
            owner_id=bot_id,
            prefix="!",  # command prefix (no chat commands are defined yet)
            # No constructor-time subscriptions.  Every subscription this bot
            # needs is per-broadcaster and requires a user token, so they are
            # all registered later by subscribe_for().
            subscriptions=[],
            # Default scope set used by the /oauth route when no ?scopes= is given
            scopes=OAUTH_SCOPES,
            # Built-in web server providing the /oauth and redirect routes.
            # Bind host/port come from config so Docker can bind 0.0.0.0.
            # The registered redirect URL is configured as one env var
            # (VOXER_OAUTH_REDIRECT_URL) and split into the domain/path
            # arguments the adapter expects.
            adapter=self._build_adapter(bot_id),
        )

    @staticmethod
    def _build_adapter(bot_id: str) -> AiohttpAdapter:
        """Build the OAuth web adapter from the configured redirect URL.

        config.OAUTH_REDIRECT_URL (env VOXER_OAUTH_REDIRECT_URL) is the single
        source of truth for the URL registered in the Twitch dev console; it is
        split into the domain/redirect_path arguments the adapter expects.
        """
        return SecureOAuthAdapter(
            redirect_url=config.OAUTH_REDIRECT_URL,
            expected_user_id=bot_id,
            client_id=config.CLIENT_ID,
            scopes=OAUTH_SCOPES,
            host=config.OAUTH_HOST,
            port=config.OAUTH_PORT,
        )

    # ── Token persistence ─────────────────────────────────────────────────────
    # twitchio persists tokens to ".tio.tokens.json" in the working directory by
    # default; these overrides point it at config.TOKEN_FILE instead, which
    # lives under the data dir so Docker keeps it in the /data volume.

    async def load_tokens(self, path: str | None = None, /) -> None:
        """Load stored user tokens from the configured token file."""
        target = path or config.TOKEN_FILE
        if self._token_owner is None:
            self._token_owner = TokenFileLock(target)
            self._token_owner.acquire()
        try:
            tokens = await asyncio.to_thread(read_tokens, target)
        except OSError, ValueError:
            LOGGER.warning(
                "Stored token file is unreadable; browser authorization required"
            )
            return
        entry = tokens.get(self.bot_id)
        if not isinstance(entry, dict):
            return
        token, refresh = entry.get("token"), entry.get("refresh")
        if not isinstance(token, str) or not isinstance(refresh, str):
            return
        try:
            await self.add_token(token, refresh)
        except Exception:
            LOGGER.warning(
                "Stored Twitch token is invalid or lacks required permissions"
            )
        else:
            await self.save_tokens(target)

    async def save_tokens(self, path: str | None = None, /) -> None:
        """Persist all user tokens to the configured token file."""
        async with self._token_save_lock:
            tokens = {key: dict(value) for key, value in self.tokens.items()}
            if not tokens:
                return
            target = path or config.TOKEN_FILE
            if self._token_owner is None:
                self._token_owner = TokenFileLock(target)
                self._token_owner.acquire()
            task = asyncio.create_task(
                asyncio.to_thread(write_json_atomic, target, tokens)
            )
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def close(self, **kwargs: object) -> None:
        """Release credential ownership only after TwitchIO finishes saving."""
        try:
            await super().close(**kwargs)
        finally:
            if self._token_owner is not None:
                self._token_owner.close()

    async def event_token_refreshed(self, _payload: object) -> None:
        """Persist tokens every time twitchio rotates a refresh token.

        Twitch rotates the refresh token on every refresh (~hourly).  Saving
        immediately means a crash never strands a stale refresh token on disk,
        which would force the user back through the browser flow.
        """
        await self.save_tokens()

    def _has_user_token(self) -> bool:
        """Return True when the managed token store holds a token for the bot account.

        Uses the read-only Client.tokens mapping (keyed by user id).
        """
        return self.bot_id in self.tokens

    async def ensure_authorized(self) -> None:
        """Block until a user token for the bot account exists.

        Resolution order:
          1. A token already loaded from the token file (normal restart) —
             returns immediately.
          2. TWITCH_ACCESS_TOKEN / TWITCH_REFRESH_TOKEN env seeds, if set —
             added to the store and persisted.
          3. The interactive browser flow: opens http://localhost:<port>/oauth
             (or logs the URL when no browser can be opened, e.g. in Docker)
             and waits until event_oauth_authorized fires for the bot account.
        """
        if self._has_user_token():
            LOGGER.info("Using stored token from %s", config.TOKEN_FILE)
            self.bot_authorized.set()
            return

        if config.ACCESS_TOKEN and config.REFRESH_TOKEN:
            # Seed tokens can be stale (Twitch rotates refresh tokens on every
            # refresh) or belong to the wrong account — both must fall through
            # to the browser flow rather than crash or silently half-work.
            LOGGER.info("Seeding token store from TWITCH_ACCESS_TOKEN/REFRESH_TOKEN")
            try:
                resp = await self.add_token(config.ACCESS_TOKEN, config.REFRESH_TOKEN)
            except Exception:
                LOGGER.warning(
                    "Seed tokens are invalid, expired or under-scoped; "
                    "falling back to browser authorization"
                )
            else:
                if resp.user_id == self.bot_id:
                    await self.save_tokens()
                    self.bot_authorized.set()
                    return
                LOGGER.warning(
                    "Seed tokens belong to %s (%s), not the bot account %s — "
                    "falling back to browser authorization",
                    resp.login,
                    resp.user_id,
                    self.bot_id,
                )

        # The /oauth route always runs on the adapter; see oauth_start_url for
        # why the public domain wins over localhost when one is configured.
        url = oauth_start_url(config.OAUTH_REDIRECT_URL, config.OAUTH_PORT)
        LOGGER.info("No stored Twitch token — starting one-time browser authorization")
        LOGGER.info("Authorize the bot here: %s", url)
        LOGGER.info(
            "The Twitch dev console must list this OAuth Redirect URL: %s",
            self.adapter.redirect_url,
        )
        try:
            opened = await asyncio.to_thread(webbrowser.open, url)
        except Exception as exc:
            # A headless machine (a Docker container, a server over SSH) has no
            # browser to open, which is normal here — the URL is logged just
            # above so it can be opened by hand.  Recording the reason at debug
            # keeps the normal path quiet while still answering "why did nothing
            # open?" for anyone who does expect a browser.
            LOGGER.debug("webbrowser.open failed: %s", exc)
            opened = False
        if not opened:
            LOGGER.info(
                "Could not open a browser automatically — open the URL above "
                "manually (from the machine whose browser you want to use)"
            )
        await self.bot_authorized.wait()
        LOGGER.info("Authorization complete — tokens saved to %s", config.TOKEN_FILE)

    async def _get_avatar_url(self, chatter: Chatter) -> str | None:
        """Fetch and cache the chatter's Twitch profile image URL.

        The cache is keyed by user ID (falling back to login name) so a chatter
        who sends many messages costs one GET /users call per run.  A failed
        lookup is not cached — the next message retries.
        """
        cache_key = str(chatter.id or chatter.name or "")
        if not cache_key:
            return None
        now = time.monotonic()
        cached = self._avatar_url_cache.get(cache_key)
        if cached is not None and cached[0] > now:
            self._avatar_url_cache.move_to_end(cache_key)
            return cached[1]
        if cache_key in self._avatar_pending or len(self._avatar_pending) >= 8:
            return None
        self._avatar_pending.add(cache_key)
        avatar_url = None
        lifetime = 3600
        try:
            async with asyncio.timeout(3):
                if chatter.id:
                    user = await self.fetch_user(id=chatter.id)
                else:
                    user = await self.fetch_user(login=chatter.name)
            avatar_url = user.profile_image.url if user is not None else None
        except Exception:
            lifetime = 30
            LOGGER.debug("Could not fetch avatar for %s", cache_key)
        finally:
            self._avatar_pending.discard(cache_key)
        self._avatar_url_cache[cache_key] = (now + lifetime, avatar_url)
        self._avatar_url_cache.move_to_end(cache_key)
        while len(self._avatar_url_cache) > 2048:
            self._avatar_url_cache.popitem(last=False)
        return avatar_url

    def _accept_event(self, payload: object) -> bool:
        """Reject other channels and redelivered EventSub notifications."""
        broadcaster = getattr(payload, "broadcaster", None) or getattr(
            payload, "to_broadcaster", None
        )
        if broadcaster is not None and str(broadcaster.id) != self.bot_id:
            return False
        metadata = getattr(payload, "metadata", None) or getattr(
            payload, "headers", None
        )
        receipt_id = getattr(metadata, "message_id", None)
        event_id = getattr(payload, "id", None) or receipt_id
        if not event_id:
            return True
        key = f"{type(payload).__name__}:{event_id}"
        now = time.monotonic()
        while self._seen_events and next(iter(self._seen_events.values())) <= now:
            self._seen_events.popitem(last=False)
        if key in self._seen_events:
            return False
        self._seen_events[key] = now + 600
        while len(self._seen_events) > 4096:
            self._seen_events.popitem(last=False)
        return True

    def _accept_chatter(self, user_id: str) -> bool:
        now = time.monotonic()
        previous = self._user_last_message.get(user_id)
        if previous is not None and now - previous < self._user_cooldown_secs:
            return False
        self._user_last_message[user_id] = now
        self._user_last_message.move_to_end(user_id)
        while len(self._user_last_message) > 4096:
            self._user_last_message.popitem(last=False)
        return True

    async def event_message(self, payload: ChatMessage) -> None:
        """Handle incoming Twitch chat message by enqueuing it for TTS processing.

        Twitch delivers each message as a list of typed fragments; see
        split_fragments for how they become speakable text plus emote names.

        Args:
            payload: Chat message event from EventSub.
        """
        if not self._accept_event(payload) or self._message_queue.full():
            return
        if str(payload.chatter.id) == self.bot_id:
            return
        source = getattr(payload, "source_broadcaster", None)
        if source is not None and str(source.id) != self.bot_id:
            return
        if (
            sum(len(fragment.text) for fragment in payload.fragments)
            > self._max_message_chars
        ):
            return
        tts_text, emote_names = split_fragments(payload.fragments)
        if tts_text.lstrip().startswith("!"):
            await super().event_message(payload)
            return
        if not tts_text and not emote_names:
            return
        if not self._accept_chatter(str(payload.chatter.id or payload.chatter.name)):
            return
        avatar_url = await self._get_avatar_url(payload.chatter)
        LOGGER.debug(
            "Received chat from %s (%d characters)", payload.chatter.name, len(tts_text)
        )
        # put_nowait, not put: when synthesis is backed up, dropping a chat line
        # is better than queueing it to be spoken long after it was sent.  The
        # drop is logged so a persistently overloaded bot is visible.
        try:
            self._message_queue.put_nowait(
                QueuedMessage(
                    username=_display_name(payload.chatter),
                    text=tts_text,
                    emote_names=emote_names,
                    avatar_url=avatar_url,
                )
            )
        except asyncio.QueueFull:
            LOGGER.debug(
                "Message queue full; dropping chat from %s", payload.chatter.name
            )
        # Call super() so twitchio can route any "!" prefixed commands
        await super().event_message(payload)

    async def event_oauth_authorized(self, payload: UserTokenPayload) -> None:
        """Handle OAuth token authorization and subscribe to chat and channel events.

        Fires when a broadcaster visits the twitchio built-in OAuth callback URL.
        We register all per-broadcaster EventSub subscriptions here because we
        now have the broadcaster's user_id from the validated token.

        Args:
            payload: OAuth authorization payload with user_id and tokens.
        """
        if payload.user_id != self.bot_id:
            # Twitch guarantees user_id on the authorization-code grant; this
            # guard exists because the payload type marks it optional.
            LOGGER.warning("Ignoring OAuth grant for an unconfigured account")
            return
        validated = await self.add_token(payload.access_token, payload.refresh_token)

        if payload.user_id == self.bot_id:
            # Unblock ensure_authorized() as soon as the token is in the store.
            # This must happen BEFORE the persistence/subscription steps below:
            # twitchio routes listener exceptions to event_error, so a failure
            # after this point is logged but must not leave startup hanging on
            # bot_authorized forever.
            self.bot_authorized.set()

        try:
            # Persist immediately — waiting for a clean shutdown risks losing
            # the grant (forcing the user back through the browser flow) on a
            # crash.  A failed write is loud but non-fatal: the token still
            # works for this run.
            await self.save_tokens()
        except OSError:
            LOGGER.exception(
                "Could not persist tokens to %s — authorization works for this "
                "run but will be required again on the next start",
                config.TOKEN_FILE,
            )

        if validated.user_id is not None:
            await self.subscribe_for(validated.user_id)

    async def subscribe_for(self, user_id: str) -> None:
        """Register all per-broadcaster EventSub subscriptions for one channel.

        Called from event_oauth_authorized when a broadcaster completes the
        OAuth flow, and from the composition root on every startup for the
        bot's own channel (Conduit subscriptions expire after 72 hours of
        downtime, so re-subscribing on boot keeps long gaps safe; Twitch
        answers still-active subscriptions with 409 Conflict, treated as
        success below).

        Args:
            user_id: Numeric Twitch user ID of the broadcaster to subscribe to.
        """
        if user_id != self.bot_id:
            raise ValueError("Only the configured bot channel may be subscribed")
        # Full set of per-broadcaster subscriptions.
        # Each maps to a VoxBot.event_* handler below.
        subs: list[eventsub.SubscriptionPayload] = [
            eventsub.ChatMessageSubscription(
                broadcaster_user_id=user_id,
                user_id=self.bot_id,
            ),
            eventsub.ChannelFollowSubscription(
                broadcaster_user_id=user_id,
                moderator_user_id=self.bot_id,  # follow events require a moderator ID
            ),
            eventsub.ChannelSubscribeSubscription(
                broadcaster_user_id=user_id,
            ),
            eventsub.ChannelSubscriptionGiftSubscription(
                broadcaster_user_id=user_id,
            ),
            eventsub.ChannelSubscribeMessageSubscription(
                # Fires for resubs that include a message (distinct from plain resubs)
                broadcaster_user_id=user_id,
            ),
            eventsub.ChannelCheerSubscription(
                broadcaster_user_id=user_id,
            ),
            eventsub.ChannelRaidSubscription(
                to_broadcaster_user_id=user_id,  # incoming raids only
            ),
        ]
        LOGGER.info("Subscribing for user: %s", user_id)
        resp: MultiSubscribePayload = await self.multi_subscribe(subs)
        duplicates, failures = classify_subscribe_errors(resp.errors)
        if duplicates:
            LOGGER.debug(
                "%d subscription(s) already active for user %s",
                len(duplicates),
                user_id,
            )
        if failures:
            LOGGER.warning(
                "Failed to subscribe to: %r, for user: %s", failures, user_id
            )

    async def add_token(self, token: str, refresh: str) -> ValidateTokenPayload:
        """Add or validate a Twitch OAuth token.

        Delegates to AutoBot which calls GET /validate and stores the token
        in its internal token store keyed by user_id.

        Args:
            token: Access token.
            refresh: Refresh token.

        Returns:
            Token validation response with user ID and expiration.
        """
        async with self._token_admission_lock:
            # TwitchIO mutates its managed store before returning validation:
            # app grants replace _app_token, while user grants replace their row.
            # Preserve the previous grants so rejecting an input cannot log the
            # running bot out. Keep TwitchIO's expired-token refresh behavior.
            previous_app_token = self._http._app_token
            previous_user_tokens = self._http._tokens.copy()
            resp: ValidateTokenPayload = await super().add_token(token, refresh)
            if (
                resp.user_id != self.bot_id
                or resp.client_id != config.CLIENT_ID
                or not set(OAUTH_SCOPES.selected).issubset(resp.scopes)
            ):
                if resp.user_id is None:
                    self._http._app_token = previous_app_token
                elif resp.user_id in previous_user_tokens:
                    self._http._tokens[resp.user_id] = previous_user_tokens[
                        resp.user_id
                    ]
                else:
                    self._http._tokens.pop(resp.user_id, None)
                raise ValueError(
                    "Twitch token has the wrong account, application or scopes"
                )
            LOGGER.info("Added token for user: %s", resp.user_id)
            return resp

    async def send_chat(self, text: str) -> None:
        """Send a message to the bot's own Twitch channel.

        Used by the Scheduler to post scheduled community messages.
        Creates a PartialUser from the bot's own ID so no broadcaster token
        is needed — only the bot's user:write:chat scope, which is the one
        Helix requires for POST /chat/messages and the one OAUTH_SCOPES above
        actually asks for.  (chat:edit is the legacy IRC scope and is not
        requested anywhere in this project; naming it here would mislead
        anyone trimming the scope list down to the minimum.)

        Args:
            text: Message to send (max 500 chars enforced by Twitch API).
        """
        if not text.strip() or len(text) > 500:
            raise ValueError("Outgoing chat must contain 1 to 500 characters")
        LOGGER.debug("Sending scheduled chat (%d characters)", len(text))
        pu = self.create_partialuser(self.bot_id)
        await pu.send_message(sender=self.bot_id, message=text)

    async def event_ready(self) -> None:
        """Called when the bot is connected and ready to receive events."""
        LOGGER.info("Successfully logged in as: %s", self.bot_id)

    # ── Channel event handlers ────────────────────────────────────────────────
    # Each handler builds a human-readable announcement string (via events.py)
    # and enqueues it via _enqueue_system.
    # SYSTEM messages skip language detection and the announce-window check —
    # they are always spoken in Ukrainian with a random voice.

    async def _enqueue_system(self, username: str, text: str) -> None:
        """Wrap an announcement in a SYSTEM-kind QueuedMessage and enqueue it.

        Overflow is dropped immediately so event bursts cannot create an
        unbounded population of coroutines waiting for queue capacity.

        Args:
            username: Display name for the overlay ("anonymous" for hidden gifters).
            text: Ready-to-speak announcement string from events.py.
        """
        try:
            self._message_queue.put_nowait(
                QueuedMessage(
                    username=username,
                    text=text[: self._max_message_chars],
                    kind=MessageKind.SYSTEM,
                )
            )
        except asyncio.QueueFull:
            LOGGER.debug("Message queue full; dropping announcement from %s", username)

    async def event_follow(self, payload: twitchio.ChannelFollow) -> None:
        """Announce a new channel follow via TTS.

        Args:
            payload: Follow event with the new follower's info.
        """
        if not self._accept_event(payload):
            return
        username = _display_name(payload.user)
        LOGGER.info("New follow from %s", username)
        await self._enqueue_system(username, follow_message(username))

    async def event_subscription(self, payload: twitchio.ChannelSubscribe) -> None:
        """Announce a new (non-gift) channel subscription via TTS.

        Gift subscriptions fire both this event AND event_subscription_gift.
        We skip them here so they are only announced once by the gift handler.

        Args:
            payload: Subscribe event with subscriber info and tier.
        """
        if not self._accept_event(payload) or payload.gift:
            return  # gift subscriptions are handled by event_subscription_gift
        username = _display_name(payload.user)
        LOGGER.info("New subscription from %s (tier %s)", username, payload.tier)
        await self._enqueue_system(username, sub_message(username))

    async def event_subscription_gift(
        self, payload: twitchio.ChannelSubscriptionGift
    ) -> None:
        """Announce a gift subscription event via TTS.

        The gifter may be anonymous (payload.user is None in that case).

        Args:
            payload: Gift subscription event with gifter info and gift count.
        """
        if not self._accept_event(payload):
            return
        username = payload.user.name if payload.user else None
        display = username or _ANONYMOUS_USER
        LOGGER.info("Gift sub from %s: %d subs", display, payload.total)
        await self._enqueue_system(display, gift_message(username, payload.total))

    async def event_subscription_message(
        self, payload: twitchio.ChannelSubscriptionMessage
    ) -> None:
        """Announce a resubscription with a message via TTS.

        This fires when a returning subscriber includes a chat message with their
        resub notification (cumulative month count is tracked by Twitch).

        Args:
            payload: Resub event with subscriber info and cumulative month count.
        """
        if not self._accept_event(payload):
            return
        username = _display_name(payload.user)
        LOGGER.info("Resub from %s (%d months)", username, payload.cumulative_months)
        await self._enqueue_system(
            username, resub_message(username, payload.cumulative_months)
        )

    async def event_cheer(self, payload: twitchio.ChannelCheer) -> None:
        """Announce a bits cheer event via TTS.

        The cheerer may be anonymous (payload.user is None).

        Args:
            payload: Cheer event with cheerer info and bit count.
        """
        if not self._accept_event(payload):
            return
        username = payload.user.name if payload.user else None
        display = username or _ANONYMOUS_USER
        LOGGER.info("Cheer from %s: %d bits", display, payload.bits)
        await self._enqueue_system(display, cheer_message(username, payload.bits))

    async def event_raid(self, payload: twitchio.ChannelRaid) -> None:
        """Announce an incoming raid via TTS.

        Args:
            payload: Raid event with raiding broadcaster info and viewer count.
        """
        if not self._accept_event(payload):
            return
        raider = _display_name(payload.from_broadcaster)
        viewers = payload.viewer_count
        LOGGER.info("Raid from %s with %d viewers", raider, viewers)
        await self._enqueue_system(raider, raid_message(raider, viewers))
