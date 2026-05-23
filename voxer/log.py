import logging

import colorlog

from .config import LOG_LEVEL


def setup_logging() -> None:
    """Configure coloured logging with timestamp and module names.

    Log level is controlled by the VOXER_LOG_LEVEL environment variable (default: INFO).
    Quiets noisy third-party loggers (websockets, uvicorn, asyncio).
    """
    level = getattr(logging, LOG_LEVEL, logging.INFO)
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
    root.handlers.clear()
    root.addHandler(handler)

    # Quiet down noisy third-party loggers
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
