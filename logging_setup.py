"""Logging configuration.

The agent writes to the console and to a size-capped, rotating file so an
always-on process cannot fill the disk. Handlers installed here are tagged and
replaced on re-configuration, which keeps repeated startup (and the test
suite) from duplicating log lines while leaving pytest's own handlers intact.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import Settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_HANDLER_MARKER = "_myaiagent_handler"
_MAX_BYTES = 2_000_000
_BACKUP_COUNT = 5


def configure_logging(settings: Settings) -> Path:
    """Install console + rotating file handlers on the root logger.

    Args:
        settings: Validated settings providing ``log_level`` and ``log_dir``.

    Returns:
        Absolute path of the log file that is written.
    """
    log_dir = settings.log_path
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "agent.log"

    level = getattr(logging, settings.log_level, logging.INFO)
    root = logging.getLogger()

    # Remove handlers from a previous call to this function only.
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    setattr(console, _HANDLER_MARKER, True)

    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_MARKER, True)

    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)

    # Uvicorn access logs are noisy for a personal agent; errors still surface.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return log_file
