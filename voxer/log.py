"""Logging configuration for twitch-voxer.

Sets up a single colorlog handler on the root logger so every module that
calls logging.getLogger(__name__) automatically inherits the coloured format.

Log level is controlled by the VOXER_LOG_LEVEL environment variable (default: INFO).
Noisy third-party loggers (websockets, uvicorn, asyncio) are quieted to WARNING
so they don't drown out the application's own output.
"""

import logging
import sys

import colorlog

from .config import LOG_LEVEL


def setup_logging(level: str = LOG_LEVEL) -> None:
    """Configure coloured logging with timestamp and module names.

    Called once at startup by voxer/app.py::run() before any other component
    initialises so all subsequent log output is consistently formatted.

    ``level`` is a level *name* such as "INFO" or "DEBUG"; it defaults to the
    value of VOXER_LOG_LEVEL.  An unrecognised name is reported on stderr and
    treated as INFO rather than being allowed to stop the program.
    """
    # getLevelNamesMapping() (Python 3.11+) returns only the real level names —
    # {"DEBUG": 10, "INFO": 20, ...}.  The previous code used
    # getattr(logging, level, logging.INFO), which looked up *any* attribute of
    # the logging module: config.py upper-cases VOXER_LOG_LEVEL, so
    # VOXER_LOG_LEVEL=basic_format became logging.BASIC_FORMAT, a format string,
    # and root.setLevel() then raised on it.  A plain typo like "INF0" was worse
    # in the other direction: it silently became INFO with nothing to show that
    # the setting had been ignored.
    resolved = logging.getLevelNamesMapping().get(level)
    if resolved is None:
        # Deliberately print() and not logging.warning(): the root logger is
        # configured a few lines below, so a log call made here would go through
        # logging's last-resort handler and look nothing like the rest of the
        # output — which is exactly the output someone is reading to find out
        # why their level setting did nothing.
        print(
            f"Unknown log level {level!r}; falling back to INFO. "
            f"Valid levels: {', '.join(logging.getLevelNamesMapping())}",
            file=sys.stderr,
        )
        resolved = logging.INFO

    # ColoredFormatter prepends ANSI colour codes based on log level.
    # %(name)-30s left-pads the logger name to 30 chars for column alignment.
    fmt = colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s  %(levelname)-8s%(reset)s  "
        "%(cyan)s%(name)-30s%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
        secondary_log_colors={
            # Also colour the message text itself for WARNING and above
            "message": {
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        },
    )
    handler = colorlog.StreamHandler()
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(resolved)
    # Clear any handlers added by earlier imports (e.g. basicConfig called by a library)
    root.handlers.clear()
    root.addHandler(handler)

    # Quiet down noisy third-party loggers that produce many low-value lines
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
