"""Twitch adapter layer for twitch-voxer.

VoxBot subclasses twitchio's AutoBot which handles:
  - EventSub WebSocket connection management
  - Automatic token refresh
  - Command prefix routing (prefix="!")

This module is intentionally thin: it translates raw Twitch events into
QueuedMessages and drops them onto the shared asyncio.Queue.  All business
logic (voice selection, TTS synthesis, language detection) lives in handler.py.

Subscriptions are registered in two places:
  - __init__: the initial ChatMessageSubscription for the bot's own channel,
    required for the EventSub handshake before any user authenticates.
  - event_oauth_authorized: per-broadcaster subscriptions added after a user
    completes the OAuth flow via the twitchio built-in /oauth/authorize route.
"""

import asyncio
import logging

import twitchio
from twitchio import ChatMessage, Client, eventsub, MultiSubscribePayload
from twitchio.authentication import UserTokenPayload, ValidateTokenPayload
from twitchio.ext import commands

from .config import CLIENT_ID, CLIENT_SECRET
from .events import (
    cheer_message,
    follow_message,
    gift_message,
    raid_message,
    resub_message,
    sub_message,
)
from .handler import MessageKind, QueuedMessage

LOGGER: logging.Logger = logging.getLogger(__name__)


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
        await client.login()
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
        super().__init__(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            bot_id=bot_id,
            owner_id=bot_id,
            prefix="!",           # command prefix (no chat commands are defined yet)
            subscriptions=subs,
            force_subscribe=True,  # re-subscribe even if already active on Twitch's side
        )

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
        LOGGER.info("Received message: %s — text=%r emotes=%r", payload.chatter.name, tts_text, emote_names)
        await self._message_queue.put(
            QueuedMessage(username=payload.chatter.name, text=tts_text, emote_names=emote_names)
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

        # Full set of per-broadcaster subscriptions.
        # Each maps to a VoxBot.event_* handler below.
        subs: list[eventsub.SubscriptionPayload] = [
            eventsub.ChatMessageSubscription(
                broadcaster_user_id=payload.user_id,
                user_id=self.bot_id,
            ),
            eventsub.ChannelFollowSubscription(
                broadcaster_user_id=payload.user_id,
                moderator_user_id=self.bot_id,  # follow events require a moderator ID
            ),
            eventsub.ChannelSubscribeSubscription(
                broadcaster_user_id=payload.user_id,
            ),
            eventsub.ChannelSubscriptionGiftSubscription(
                broadcaster_user_id=payload.user_id,
            ),
            eventsub.ChannelSubscribeMessageSubscription(
                # Fires for resubs that include a message (distinct from plain resubs)
                broadcaster_user_id=payload.user_id,
            ),
            eventsub.ChannelCheerSubscription(
                broadcaster_user_id=payload.user_id,
            ),
            eventsub.ChannelRaidSubscription(
                to_broadcaster_user_id=payload.user_id,  # incoming raids only
            ),
        ]
        LOGGER.info("Subscribing for user: %s", payload.user_id)
        resp: MultiSubscribePayload = await self.multi_subscribe(subs)
        if resp.errors:
            LOGGER.warning(
                "Failed to subscribe to: %r, for user: %s", resp.errors, payload.user_id
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
    # Each handler builds a human-readable announcement string (via events.py),
    # wraps it in a SYSTEM-kind QueuedMessage, and enqueues it.
    # SYSTEM messages skip language detection and the announce-window check —
    # they are always spoken in Ukrainian with a random voice.

    async def event_follow(self, payload: twitchio.ChannelFollow) -> None:
        """Announce a new channel follow via TTS.

        Args:
            payload: Follow event with the new follower's info.
        """
        username = payload.user.name
        LOGGER.info("New follow from %s", username)
        text = follow_message(username)
        await self._message_queue.put(
            QueuedMessage(username=username, text=text, kind=MessageKind.SYSTEM)
        )

    async def event_subscription(self, payload: twitchio.ChannelSubscribe) -> None:
        """Announce a new (non-gift) channel subscription via TTS.

        Gift subscriptions fire both this event AND event_subscription_gift.
        We skip them here so they are only announced once by the gift handler.

        Args:
            payload: Subscribe event with subscriber info and tier.
        """
        if payload.gift:
            return  # gift subscriptions are handled by event_subscription_gift
        username = payload.user.name
        LOGGER.info("New subscription from %s (tier %s)", username, payload.tier)
        text = sub_message(username)
        await self._message_queue.put(
            QueuedMessage(username=username, text=text, kind=MessageKind.SYSTEM)
        )

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
        text = gift_message(username, payload.total)
        await self._message_queue.put(
            QueuedMessage(username=display, text=text, kind=MessageKind.SYSTEM)
        )

    async def event_subscription_message(
        self, payload: twitchio.ChannelSubscriptionMessage
    ) -> None:
        """Announce a resubscription with a message via TTS.

        This fires when a returning subscriber includes a chat message with their
        resub notification (cumulative month count is tracked by Twitch).

        Args:
            payload: Resub event with subscriber info and cumulative month count.
        """
        username = payload.user.name
        LOGGER.info("Resub from %s (%d months)", username, payload.cumulative_months)
        text = resub_message(username, payload.cumulative_months)
        await self._message_queue.put(
            QueuedMessage(username=username, text=text, kind=MessageKind.SYSTEM)
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
        text = cheer_message(username, payload.bits)
        await self._message_queue.put(
            QueuedMessage(username=display, text=text, kind=MessageKind.SYSTEM)
        )

    async def event_raid(self, payload: twitchio.ChannelRaid) -> None:
        """Announce an incoming raid via TTS.

        Args:
            payload: Raid event with raiding broadcaster info and viewer count.
        """
        raider = payload.from_broadcaster.name
        viewers = payload.viewer_count
        LOGGER.info("Raid from %s with %d viewers", raider, viewers)
        text = raid_message(raider, viewers)
        await self._message_queue.put(
            QueuedMessage(username=raider, text=text, kind=MessageKind.SYSTEM)
        )
