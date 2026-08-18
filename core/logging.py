"""Structured logging via Loguru — rotating daily, 30-day retention."""

import sys
from pathlib import Path

from loguru import logger

LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} | {message}"
)


def configure(log_dir: str | Path, log_retention_days: int = 30) -> None:
    """Configure Loguru with daily rotating file + stderr output.

    Args:
        log_dir: Directory for log files. Created if missing.
        log_retention_days: Days before log files are auto-deleted.
                            Override via config.maintenance.log_retention_days.
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
        retention=f"{log_retention_days} days",
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

    from prefect.context import FlowRunContext, TaskRunContext

    # F10: the old code cached the FIRST logger obtained by this process and
    # reused it for every later flow run. On the always-on agent process the
    # first flow run happens at night 1 — from night 2 on, every loguru line
    # was forwarded into the STALE run's logger (wrong run id in the Prefect
    # console, empty logs for the current run). Cache per active run id
    # instead, with a small eviction cap (the agent process lives for months).
    _logger_cache = {}
    _CACHE_MAX = 128

    def _get_prefect_logger():
        task_ctx = TaskRunContext.get()
        flow_ctx = FlowRunContext.get()
        if task_ctx is not None:
            key = ("task", task_ctx.task_run.id)
        elif flow_ctx is not None:
            key = ("flow", flow_ctx.flow_run.id)
        else:
            return None
        cached = _logger_cache.get(key)
        if cached is not None:
            return cached
        try:
            from prefect import get_run_logger
            cached = get_run_logger()
        except Exception:
            return None
        if len(_logger_cache) >= _CACHE_MAX:
            _logger_cache.pop(next(iter(_logger_cache)))  # evict oldest
        _logger_cache[key] = cached
        return cached

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
    _bridge_configured = True
