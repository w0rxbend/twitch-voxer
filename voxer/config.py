import os

from dotenv import load_dotenv

load_dotenv()

CLIENT_ID: str = str(os.getenv("TWITCH_CLIENT_ID"))
CLIENT_SECRET: str = str(os.getenv("TWITCH_CLIENT_SECRET"))
ACCESS_TOKEN: str = str(os.getenv("TWITCH_ACCESS_TOKEN"))
REFRESH_TOKEN: str = str(os.getenv("TWITCH_REFRESH_TOKEN"))
BOT_USERNAME: str = str(os.getenv("TWITCH_BOT_USERNAME", "worxbend"))
DB_PATH: str = str(os.getenv("VOXER_DB_PATH", "voices.json"))
AUDIO_DIR: str = str(os.getenv("VOXER_AUDIO_DIR", "audio"))
SERVER_HOST: str = str(os.getenv("VOXER_SERVER_HOST", "0.0.0.0"))
SERVER_PORT: int = int(os.getenv("VOXER_SERVER_PORT", "8080"))
SCHEDULER_INTERVAL: int = int(os.getenv("VOXER_SCHEDULER_INTERVAL", "600"))
SCHEDULER_INITIAL_DELAY: int = int(os.getenv("VOXER_SCHEDULER_INITIAL_DELAY", "10"))
MESSAGES_PATH: str = str(os.getenv("VOXER_MESSAGES_PATH", "messages.json"))
VOICES_DIR: str = str(os.getenv("VOXER_VOICES_DIR", "voices"))
