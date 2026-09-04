"""Entry point for running the bot: `python main.py` or `uv run main.py`.

The import is guarded because voxer.config reads and parses every setting
while it is being imported, so a typo such as VOXER_SERVER_PORT=eighty fails
before main() exists to be called.  Without the guard the operator's first
sight of the problem is a six-frame traceback whose last line happens to be
the helpful sentence; with it they get the sentence on its own.

ConfigError cannot be imported for the `except` clause here -- importing it
means importing the very module that is failing -- so its RuntimeError base
class is caught instead.  Nothing else is imported at this point, so a
RuntimeError arriving here can only have come from reading the configuration.
"""

try:
    from voxer.app import main
except RuntimeError as exc:
    raise SystemExit(f"Configuration error: {exc}") from exc

if __name__ == "__main__":
    main()
