import logging

from twitchio import ChatMessage, Client, eventsub, MultiSubscribePayload
from twitchio.authentication import UserTokenPayload, ValidateTokenPayload
from twitchio.ext import commands

from .config import CLIENT_ID, CLIENT_SECRET
from .handler import MessageHandler

LOGGER: logging.Logger = logging.getLogger(__name__)


async def get_user_id(username: str) -> str:
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
    ) -> None:
        self._handler = handler
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
        LOGGER.info("Received message: %s — %s", payload.chatter.name, payload.text)
        await self._handler.handle(payload.chatter.name, payload.text)
        await super().event_message(payload)

    async def event_oauth_authorized(self, payload: UserTokenPayload) -> None:
        await self.add_token(payload.access_token, payload.refresh_token)
        subs: list[eventsub.SubscriptionPayload] = [
            eventsub.ChatMessageSubscription(
                broadcaster_user_id=payload.user_id,
                user_id=self.bot_id,
            ),
        ]
        LOGGER.info("Subscribing for user: %s", payload.user_id)
        resp: MultiSubscribePayload = await self.multi_subscribe(subs)
        if resp.errors:
            LOGGER.warning(
                "Failed to subscribe to: %r, for user: %s", resp.errors, payload.user_id
            )

    async def add_token(self, token: str, refresh: str) -> ValidateTokenPayload:
        resp: ValidateTokenPayload = await super().add_token(token, refresh)
        LOGGER.info("Added token for user: %s", resp.user_id)
        return resp

    async def send_chat(self, text: str) -> None:
        LOGGER.info("Sending to chat: %r", text)
        pu = self.create_partialuser(self.bot_id)
        await pu.send_message(sender=self.bot_id, message=text)
        
    async def event_ready(self) -> None:
        LOGGER.info("Successfully logged in as: %s", self.bot_id)
