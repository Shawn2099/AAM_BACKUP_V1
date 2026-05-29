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


_bridge_configured = False


def configure_prefect_bridge():
    """Forward Loguru messages to the active Prefect run logger if running under Prefect.

    Idempotent — safe to call on every flow run. Only adds the sink once.
    """
    global _bridge_configured
    if _bridge_configured:
        return
    _bridge_configured = True

    from prefect.context import FlowRunContext, TaskRunContext

    _cached_logger = None
    _logger_initialized = False

    def _get_prefect_logger():
        nonlocal _cached_logger, _logger_initialized
        if _logger_initialized:
            return _cached_logger
        _logger_initialized = True
        from prefect import get_run_logger
        try:
            _cached_logger = get_run_logger()
        except Exception:
            _logger_initialized = False
        return _cached_logger

    def prefect_sink(message):
        if FlowRunContext.get() or TaskRunContext.get():
            try:
                prefect_logger = _get_prefect_logger()
                if prefect_logger is None:
                    return
                msg_str = message.record["message"]
                level = message.record["level"].name
                if level == "INFO":
                    prefect_logger.info(msg_str)
                elif level == "WARNING":
                    prefect_logger.warning(msg_str)
                elif level == "ERROR":
                    prefect_logger.error(msg_str)
                elif level == "CRITICAL":
                    prefect_logger.critical(msg_str)
                else:
                    prefect_logger.debug(msg_str)
            except Exception:
                logger.opt(depth=1, exception=False).debug("Prefect bridge failed to forward message")

    logger.add(prefect_sink, level="INFO")
