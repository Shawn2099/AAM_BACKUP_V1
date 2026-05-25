"""Structured logging via Loguru — rotating daily, 30-day retention."""

import sys
from pathlib import Path

from loguru import logger

LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} | {message}"
)


def configure(log_dir: str | Path) -> None:
    """Configure Loguru with daily rotating file + stderr output.

    Args:
        log_dir: Directory for log files. Created if missing.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()

    logger.add(
        sys.stderr,
        format=LOG_FORMAT,
        level="INFO",
        colorize=True,
    )

    logger.add(
        log_dir / "backup_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="30 days",
        encoding="utf-8",
        level="DEBUG",
        format=LOG_FORMAT,
        enqueue=True,
    )
