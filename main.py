import asyncio
import logging
import os
import random
import subprocess
import wave
from cmd import Cmd
from queue import Empty
from typing import TYPE_CHECKING

import twitchio
from dotenv import load_dotenv
from piper import PiperVoice, SynthesisConfig
from twitchio import ChatMessage, eventsub
from twitchio.ext import commands

LOGGER: logging.Logger = logging.getLogger("Bot")

load_dotenv()
CLIENT_ID: str = str(
    os.getenv("TWITCH_CLIENT_ID")
)  # The CLIENT ID from the Twitch Dev Console
CLIENT_SECRET: str = str(
    os.getenv("TWITCH_CLIENT_SECRET")
)  # The CLIENT SECRET from the Twitch Dev Console


VOICE = PiperVoice.load("./uk_UA-ukrainian_tts-medium.onnx")


async def get_user_id(username: str):
    async with twitchio.Client(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET
    ) as client:
        await client.login()
        user = await client.fetch_users(logins=[username])
        try:
            return user[0].id
        except IndexError:
            raise "Something went wrong"


class VoxBot(commands.AutoBot):
    def __init__(
        self, *, bot_id: str, subs: list[eventsub.SubscriptionPayload]
    ) -> None:
        self.subs = subs
        self.arg_bot_id = bot_id
        super().__init__(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            bot_id=bot_id,
            owner_id=bot_id,
            prefix="!",
            subscriptions=subs,
            force_subscribe=True,
        )

    async def setup_hook(self) -> None:
        # Add our component which contains our commands...
        # subs_result = await self.multi_subscribe(self.subs)
        # if subs_result.errors:
        #     LOGGER.error(subs_result.errors)
        # LOGGER.info("Subscription established")
        pass

    async def generate_wav(self, text: str):
        conf = SynthesisConfig(
            noise_scale=1.0,  # more audio variation
            noise_w_scale=1.0,  # more speaking variation
            speaker_id=random.choice([0, 1, 2]),
        )
        with wave.open("test.wav", "wb") as wav_file:
            VOICE.synthesize_wav(text.lower(), wav_file, conf)
        subprocess.run(["paplay", "test.wav"], capture_output=False)

    async def event_message(self, payload: twitchio.ChatMessage) -> None:
        LOGGER.info(f"Received Message: {payload.chatter.name} - {payload.text}")
        await self.generate_wav(payload.text)
        await super().event_message(payload)

    async def event_oauth_authorized(
        self, payload: twitchio.authentication.UserTokenPayload
    ) -> None:
        await self.add_token(payload.access_token, payload.refresh_token)

        # A list of subscriptions we would like to make to the newly authorized channel...
        subs: list[eventsub.SubscriptionPayload] = [
            eventsub.ChatMessageSubscription(
                broadcaster_user_id=payload.user_id,
                user_id=self.bot_id,
            ),
        ]

        LOGGER.info("Trying to subscribe..")
        resp: twitchio.MultiSubscribePayload = await self.multi_subscribe(subs)
        if resp.errors:
            LOGGER.warning(
                "Failed to subscribe to: %r, for user: %s", resp.errors, payload.user_id
            )

    async def add_token(
        self, token: str, refresh: str
    ) -> twitchio.authentication.ValidateTokenPayload:
        # Make sure to call super() as it will add the tokens interally and return us some data...
        resp: twitchio.authentication.ValidateTokenPayload = await super().add_token(
            token, refresh
        )
        LOGGER.info("Added token to the database for user: %s", resp.user_id)
        return resp

    async def event_ready(self) -> None:
        LOGGER.info("Successfully logged in as: %s", self.bot_id)


async def runner():
    twitchio.utils.setup_logging(level=logging.INFO)
    at: str = str(os.getenv("TWITCH_ACCESS_TOKEN"))
    rf: str = str(os.getenv("TWITCH_REFRESH_TOKEN"))

    bot_id = await get_user_id("worxbend")
    subs: list[eventsub.SubscriptionPayload] = [
        eventsub.ChatMessageSubscription(broadcaster_user_id=bot_id, user_id=bot_id)
    ]
    LOGGER.info(f"User ID {bot_id}")
    async with VoxBot(bot_id=bot_id, subs=subs) as bot:
        await bot.add_token(at, rf)
        await bot.start(load_tokens=False)
    LOGGER.info("Can we spawn something here as the another coroutine")


def main():
    asyncio.run(runner())


if __name__ == "__main__":
    main()
