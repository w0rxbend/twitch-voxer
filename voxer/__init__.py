"""twitch-voxer — Twitch chat TTS bot with per-user voices and a browser overlay.

The package is organised as:
  app.py          — composition root (wires everything together, owns shutdown
                    signals; the entry point)
  bot.py          — Twitch adapter (twitchio AutoBot, EventSub, OAuth flow)
  handler.py      — message-to-audio pipeline orchestration
  textnorm.py     — pure text rules (bot filter, emoji, normalisation)
  stores.py       — pickledb persistence (voice assignments, announce windows)
  server.py       — Starlette HTTP + WebSocket server for the overlay
  scheduler.py    — periodic chat message poster
  tts.py          — Supertonic synthesis + ffmpeg MP3 conversion
  events.py       — randomised Ukrainian channel-event announcement strings
  config.py       — environment-variable configuration
  log.py          — root logger setup (colour handler, level resolution)
  models.py       — shared dataclasses + the helper that builds the /audio URL
  fetch_emotes.py — one-shot emote-cache builder, run by hand rather than by
                    the bot (`uv run voxer-fetch-emotes`)

Keeping this module import-light means `import voxer.models` (e.g. in tests)
does not pull in twitchio, uvicorn, or the TTS engine.
"""

__version__ = "0.1.0"
