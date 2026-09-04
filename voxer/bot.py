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
import webbrowser

import twitchio
from twitchio import ChatMessage, Client, Scopes, eventsub, MultiSubscribePayload
from twitchio.authentication import UserTokenPayload, ValidateTokenPayload
from twitchio.user import Chatter
from twitchio.ext import commands
from twitchio.web import AiohttpAdapter

from . import config
from .config import CLIENT_ID, CLIENT_SECRET
from .events import (
    cheer_message,
    follow_message,
    gift_message,
    raid_message,
    resub_message,
    sub_message,
)
from .models import MessageKind, QueuedMessage

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
    async with Client(client_id=CLIENT_ID, client_secret=CLIENT_SECRET) as client:
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
    and built-in OAuth flow at /oauth/authorize.
    """

    def __init__(
        self,
        *,
        bot_id: str,
        subs: list[eventsub.SubscriptionPayload],
        message_queue: asyncio.Queue["QueuedMessage"],
    ) -> None:
        """Initialize the Twitch bot with EventSub subscriptions and message queue.

        Args:
            bot_id: Twitch user ID of the bot account.
            subs: List of EventSub subscriptions to register at connection time.
            message_queue: Queue for dispatching chat messages to the handler.
        """
        self._message_queue = message_queue
        self._avatar_url_cache: dict[str, str | None] = {}
        # Set once a user token for the bot's own account is available (loaded
        # from the token file, seeded from env, or granted via the browser
        # OAuth flow).  ensure_authorized() awaits it before chat-dependent
        # components start.
        self.bot_authorized: asyncio.Event = asyncio.Event()
        super().__init__(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            bot_id=bot_id,
            owner_id=bot_id,
            prefix="!",  # command prefix (no chat commands are defined yet)
            subscriptions=subs,
            # Default scope set used by the /oauth route when no ?scopes= is given
            scopes=OAUTH_SCOPES,
            # Built-in web server providing the /oauth and redirect routes.
            # Bind host/port come from config so Docker can bind 0.0.0.0.
            # The registered redirect URL is configured as one env var
            # (VOXER_OAUTH_REDIRECT_URL) and split into the domain/path
            # arguments the adapter expects.
            adapter=self._build_adapter(),
        )

    @staticmethod
    def _build_adapter() -> AiohttpAdapter:
        """Build the OAuth web adapter from the configured redirect URL.

        config.OAUTH_REDIRECT_URL (env VOXER_OAUTH_REDIRECT_URL) is the single
        source of truth for the URL registered in the Twitch dev console; it is
        split into the domain/redirect_path arguments the adapter expects.
        """
        domain, redirect_path = config.parse_redirect_url(config.OAUTH_REDIRECT_URL)
        return AiohttpAdapter(
            host=config.OAUTH_HOST,
            port=config.OAUTH_PORT,
            domain=domain,
            redirect_path=redirect_path,
        )

    # ── Token persistence ─────────────────────────────────────────────────────
    # twitchio persists tokens to ".tio.tokens.json" in the working directory by
    # default; these overrides point it at config.TOKEN_FILE instead, which
    # lives under the data dir so Docker keeps it in the /data volume.

    async def load_tokens(self, path: str | None = None, /) -> None:
        """Load stored user tokens from the configured token file."""
        await super().load_tokens(path or config.TOKEN_FILE)

    async def save_tokens(self, path: str | None = None, /) -> None:
        """Persist all user tokens to the configured token file."""
        await super().save_tokens(path or config.TOKEN_FILE)

    async def event_token_refreshed(self, payload: object) -> None:
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
            except Exception as exc:
                LOGGER.warning(
                    "Seed tokens are invalid or expired (%s) — "
                    "falling back to browser authorization",
                    exc,
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

        # The /oauth route always runs on the adapter.  With a public domain
        # in the redirect URL, the /oauth link uses it (HTTPS, per twitchio's
        # adapter); otherwise localhost works both on the host and through
        # Docker's published port.
        domain, _ = config.parse_redirect_url(config.OAUTH_REDIRECT_URL)
        if domain:
            url = f"https://{domain}/oauth"
        else:
            url = f"http://localhost:{config.OAUTH_PORT}/oauth"
        LOGGER.info("No stored Twitch token — starting one-time browser authorization")
        LOGGER.info("Authorize the bot here: %s", url)
        LOGGER.info(
            "The Twitch dev console must list this OAuth Redirect URL: %s",
            self.adapter.redirect_url,
        )
        try:
            opened = await asyncio.to_thread(webbrowser.open, url)
        except Exception:
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
        if cache_key in self._avatar_url_cache:
            return self._avatar_url_cache[cache_key]

        try:
            if chatter.id:
                user = await self.fetch_user(id=chatter.id)
            else:
                user = await self.fetch_user(login=chatter.name)
        except Exception as exc:
            LOGGER.warning("Could not fetch avatar for %s: %s", cache_key, exc)
            return None

        # fetch_user is typed `User | None` — a deleted or renamed account
        # resolves to nothing, and the overlay simply shows no avatar.
        avatar_url = user.profile_image.url if user is not None else None
        self._avatar_url_cache[cache_key] = avatar_url
        return avatar_url

    async def event_message(self, payload: ChatMessage) -> None:
        """Handle incoming Twitch chat message by enqueuing it for TTS processing.

        Twitch delivers each message as a list of typed fragments.
        We split them into:
          - text fragments  → joined into a single string for TTS
          - emote fragments → collected as names for image overlay lookup

        Args:
            payload: Chat message event from EventSub.
        """
        # Join text fragments (skip emote/cheermote fragments — those are handled separately)
        tts_text = " ".join(
            fragment.text for fragment in payload.fragments if fragment.type == "text"
        ).strip()
        # Collect emote names so the overlay can display their images
        emote_names = [
            fragment.text for fragment in payload.fragments if fragment.type == "emote"
        ]
        avatar_url = await self._get_avatar_url(payload.chatter)
        LOGGER.info(
            "Received message: %s — text=%r emotes=%r",
            payload.chatter.name,
            tts_text,
            emote_names,
        )
        # put_nowait, not put: when synthesis is backed up, dropping a chat line
        # is better than queueing it to be spoken long after it was sent.  The
        # drop is logged so a persistently overloaded bot is visible.
        try:
            self._message_queue.put_nowait(
                QueuedMessage(
                    username=payload.chatter.name or "unknown",
                    text=tts_text,
                    emote_names=emote_names,
                    avatar_url=avatar_url,
                )
            )
        except asyncio.QueueFull:
            LOGGER.warning(
                "Message queue full — dropping message from %s", payload.chatter.name
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
        await self.add_token(payload.access_token, payload.refresh_token)

        if payload.user_id is None:
            # Twitch guarantees user_id on the authorization-code grant; this
            # guard exists because the payload type marks it optional.
            LOGGER.warning("OAuth payload without user_id — skipping subscriptions")
            return

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

        await self.subscribe_for(payload.user_id)

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
        # Twitch answers an already-active conduit subscription with 409
        # Conflict — expected when re-subscribing on every boot, not a failure.
        duplicates = [e for e in resp.errors if e.error.status == 409]
        failures = [e for e in resp.errors if e.error.status != 409]
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
        resp: ValidateTokenPayload = await super().add_token(token, refresh)
        LOGGER.info("Added token for user: %s", resp.user_id)
        return resp

    async def send_chat(self, text: str) -> None:
        """Send a message to the bot's own Twitch channel.

        Used by the Scheduler to post scheduled community messages.
        Creates a PartialUser from the bot's own ID so no broadcaster token
        is needed — only the bot's chat:edit scope.

        Args:
            text: Message to send (max 500 chars enforced by Twitch API).
        """
        LOGGER.info("Sending to chat: %r", text)
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

        Unlike chat messages, this awaits room in the queue rather than dropping.
        Follows, subs, cheers and raids are rare and high-value: waiting out a
        backlog is better than silently losing a raid alert.

        Args:
            username: Display name for the overlay ("anonymous" for hidden gifters).
            text: Ready-to-speak announcement string from events.py.
        """
        await self._message_queue.put(
            QueuedMessage(username=username, text=text, kind=MessageKind.SYSTEM)
        )

    async def event_follow(self, payload: twitchio.ChannelFollow) -> None:
        """Announce a new channel follow via TTS.

        Args:
            payload: Follow event with the new follower's info.
        """
        # Twitch can omit the login name in rare payloads; fall back to a placeholder
        username = payload.user.name or "unknown"
        LOGGER.info("New follow from %s", username)
        await self._enqueue_system(username, follow_message(username))

    async def event_subscription(self, payload: twitchio.ChannelSubscribe) -> None:
        """Announce a new (non-gift) channel subscription via TTS.

        Gift subscriptions fire both this event AND event_subscription_gift.
        We skip them here so they are only announced once by the gift handler.

        Args:
            payload: Subscribe event with subscriber info and tier.
        """
        if payload.gift:
            return  # gift subscriptions are handled by event_subscription_gift
        # Twitch can omit the login name in rare payloads; fall back to a placeholder
        username = payload.user.name or "unknown"
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
        username = payload.user.name if payload.user else None
        display = username or "anonymous"
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
        # Twitch can omit the login name in rare payloads; fall back to a placeholder
        username = payload.user.name or "unknown"
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
        username = payload.user.name if payload.user else None
        display = username or "anonymous"
        LOGGER.info("Cheer from %s: %d bits", display, payload.bits)
        await self._enqueue_system(display, cheer_message(username, payload.bits))

    async def event_raid(self, payload: twitchio.ChannelRaid) -> None:
        """Announce an incoming raid via TTS.

        Args:
            payload: Raid event with raiding broadcaster info and viewer count.
        """
        raider = payload.from_broadcaster.name or "unknown"
        viewers = payload.viewer_count
        LOGGER.info("Raid from %s with %d viewers", raider, viewers)
        await self._enqueue_system(raider, raid_message(raider, viewers))
