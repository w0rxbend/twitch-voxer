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
from .handler import MessageHandler, MessageKind, QueuedMessage

LOGGER: logging.Logger = logging.getLogger(__name__)


async def get_user_id(username: str) -> str:
    """Fetch Twitch user ID by login name.

    Args:
        username: Twitch login name.

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
    def __init__(
        self,
        *,
        bot_id: str,
        subs: list[eventsub.SubscriptionPayload],
        handler: MessageHandler,
        message_queue: asyncio.Queue,
    ) -> None:
        """Initialize the Twitch bot with EventSub subscriptions and message queue.

        Args:
            bot_id: Twitch user ID of the bot account.
            subs: List of EventSub subscriptions to register.
            handler: MessageHandler instance (for type hinting only; not directly used).
            message_queue: asyncio.Queue for receiving chat messages from event loop.
        """
        self._handler = handler
        self._message_queue = message_queue
        super().__init__(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            bot_id=bot_id,
            owner_id=bot_id,
            prefix="!",
            subscriptions=subs,
            force_subscribe=True,
        )

    async def event_message(self, payload: ChatMessage) -> None:
        """Handle incoming Twitch chat message by enqueuing it for TTS processing.

        Args:
            payload: Chat message event from EventSub.
        """
        LOGGER.info("Received message: %s — %s", payload.chatter.name, payload.text)
        await self._message_queue.put(
            QueuedMessage(username=payload.chatter.name, text=payload.text)
        )
        LOGGER.debug("Queued message from %s", payload.chatter.name)
        await super().event_message(payload)

    async def event_oauth_authorized(self, payload: UserTokenPayload) -> None:
        """Handle OAuth token authorization and subscribe to chat and channel events.

        Args:
            payload: OAuth authorization payload from EventSub.
        """
        await self.add_token(payload.access_token, payload.refresh_token)
        subs: list[eventsub.SubscriptionPayload] = [
            eventsub.ChatMessageSubscription(
                broadcaster_user_id=payload.user_id,
                user_id=self.bot_id,
            ),
            eventsub.ChannelFollowSubscription(
                broadcaster_user_id=payload.user_id,
                moderator_user_id=self.bot_id,
            ),
            eventsub.ChannelSubscribeSubscription(
                broadcaster_user_id=payload.user_id,
            ),
            eventsub.ChannelSubscriptionGiftSubscription(
                broadcaster_user_id=payload.user_id,
            ),
            eventsub.ChannelSubscribeMessageSubscription(
                broadcaster_user_id=payload.user_id,
            ),
            eventsub.ChannelCheerSubscription(
                broadcaster_user_id=payload.user_id,
            ),
            eventsub.ChannelRaidSubscription(
                to_broadcaster_user_id=payload.user_id,
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

        Args:
            text: Message to send.
        """
        LOGGER.info("Sending to chat: %r", text)
        pu = self.create_partialuser(self.bot_id)
        await pu.send_message(sender=self.bot_id, message=text)

    async def event_ready(self) -> None:
        """Called when the bot is connected and ready to receive events."""
        LOGGER.info("Successfully logged in as: %s", self.bot_id)

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
