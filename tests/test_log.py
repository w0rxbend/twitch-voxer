"""Unit tests for root-logger setup in voxer.log.

``setup_logging()`` is called once, at process start, and it takes ownership of
the root logger: it sets the level, throws away whatever handlers earlier
imports had installed, and adds exactly one of its own.  That makes it hostile
to a test suite, because pytest also attaches handlers to the root logger to
capture output.  The ``restore_root_logger`` fixture below therefore snapshots
the root logger (and the three third-party loggers the function quiets) before
each test and puts them back afterwards, so a test in this file cannot change
what any later test sees.

The level argument exists so these tests have something to pass.  In
production the single caller in ``voxer/app.py`` still calls
``setup_logging()`` with no arguments and gets ``config.LOG_LEVEL``.
"""

import logging
from collections.abc import Iterator

import pytest

from voxer.config import LOG_LEVEL
from voxer.log import setup_logging

_QUIETED = ("websockets", "uvicorn", "asyncio")


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    """Undo everything setup_logging() does to global logging state."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_quiet = {name: logging.getLogger(name).level for name in _QUIETED}
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_quiet.items():
            logging.getLogger(name).setLevel(level)


class TestLevelResolution:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("DEBUG", logging.DEBUG),
            ("INFO", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("CRITICAL", logging.CRITICAL),
        ],
    )
    def test_known_level_is_applied(self, name: str, expected: int) -> None:
        """Every real level name must end up on the root logger."""
        setup_logging(name)
        assert logging.getLogger().level == expected

    def test_unknown_level_falls_back_to_info(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A typo must warn on stderr and leave the bot running at INFO.

        "INF0" (digit zero) is the realistic mistake.  The old attribute-based
        lookup turned it into INFO with no sign that anything was wrong.
        """
        setup_logging("INF0")

        assert logging.getLogger().level == logging.INFO
        stderr = capsys.readouterr().err
        assert "INF0" in stderr
        assert "INFO" in stderr

    def test_module_attribute_is_not_a_level(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A name that is an attribute of the logging module but not a level.

        config.py upper-cases VOXER_LOG_LEVEL, so ``basic_format`` arrives here
        as ``BASIC_FORMAT``, which is a real attribute of the logging module
        holding a format string.  The old lookup returned that string and
        ``root.setLevel()`` raised on it, killing the process at startup.
        """
        setup_logging("BASIC_FORMAT")

        assert logging.getLogger().level == logging.INFO
        assert "BASIC_FORMAT" in capsys.readouterr().err

    def test_default_argument_is_the_configured_level(self) -> None:
        """Calling with no argument must behave exactly as app.py expects.

        LOG_LEVEL may be any string the environment supplies, so this asserts
        the two possible outcomes of resolving it rather than one fixed number.
        """
        setup_logging()

        expected = logging.getLevelNamesMapping().get(LOG_LEVEL, logging.INFO)
        assert logging.getLogger().level == expected


class TestHandlers:
    def test_root_ends_with_exactly_one_handler(self) -> None:
        """Two handlers would print every line twice.

        Handlers installed before the call (by a library that ran
        ``logging.basicConfig()`` on import, say) must be dropped, and calling
        the function again must not stack a second copy of our own handler.
        """
        root = logging.getLogger()
        root.addHandler(logging.NullHandler())

        setup_logging("INFO")
        assert len(root.handlers) == 1

        setup_logging("DEBUG")
        assert len(root.handlers) == 1
