"""Logging configuration for twitch-voxer.

Sets up a single colorlog handler on the root logger so every module that
calls logging.getLogger(__name__) automatically inherits the coloured format.

Log level is controlled by the VOXER_LOG_LEVEL environment variable (default: INFO).
Noisy third-party loggers (websockets, uvicorn, asyncio) are quieted to WARNING
so they don't drown out the application's own output.
"""

import logging

import colorlog

from .config import LOG_LEVEL


def setup_logging() -> None:
    """Configure coloured logging with timestamp and module names.

    Called once at startup in voxer/__init__.py before any other component
    initialises so all subsequent log output is consistently formatted.
    """
    level = getattr(logging, LOG_LEVEL, logging.INFO)

    # ColoredFormatter prepends ANSI colour codes based on log level.
    # %(name)-30s left-pads the logger name to 30 chars for column alignment.
    fmt = colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s  %(levelname)-8s%(reset)s  "
        "%(cyan)s%(name)-30s%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
        secondary_log_colors={
            # Also colour the message text itself for WARNING and above
            "message": {
                "WARNING":  "yellow",
                "ERROR":    "red",
                "CRITICAL": "bold_red",
            },
        },
    )
    handler = colorlog.StreamHandler()
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    # Clear any handlers added by earlier imports (e.g. basicConfig called by a library)
    root.handlers.clear()
    root.addHandler(handler)

    # Quiet down noisy third-party loggers that produce many low-value lines
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
