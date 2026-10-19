"""Restart of the backup service from the dashboard ("Apply changes").

The agent (`launch.py`: scheduler + dashboard) runs under NSSM, whose AppExit default is
Restart: when the process ends, NSSM starts it again after `AppRestartDelay`. So the
dashboard does not need rights on the service manager - it ends the process the same way a
service stop does (Ctrl+C -> KeyboardInterrupt in the main thread, which `serve()` turns into
a clean shutdown) and NSSM brings it back with the new config.yaml.

Only a signed-in person can ask, the caller has already proved that no backup, audit or
rollover is running, and a process that has not ended after `force_exit_s` is ended hard
(nothing is running, so no data is at risk, and NSSM restarts it either way).

`BOOT_ID` changes with every process start: the browser polls /health until it differs, so
it knows the NEW process answers (not the old one that is still shutting down).
"""

from __future__ import annotations

import os
import signal
import threading
import time
import uuid

from loguru import logger

from core.time_utils import now_iso

BOOT_ID = uuid.uuid4().hex
STARTED_AT = now_iso()

_requested = threading.Event()


def restart_pending() -> bool:
    return _requested.is_set()


def request_restart(*, delay_s: float, force_exit_s: float, actor: str,
                    interrupt=None, hard_exit=None) -> bool:
    """Schedule the restart; False when one is already scheduled.

    `delay_s` lets the HTTP reply reach the browser first. `interrupt` / `hard_exit` are
    injectable for tests.
    """
    if _requested.is_set():
        return False
    _requested.set()
    interrupt = interrupt or (lambda: signal.raise_signal(signal.SIGINT))
    hard_exit = hard_exit or (lambda: os._exit(1))

    def _go() -> None:
        time.sleep(delay_s)
        logger.warning(f"Restarting the backup service now (applied settings, asked by {actor})")
        try:
            interrupt()
        except Exception as e:  # noqa: BLE001 - the hard exit below still ends the process
            logger.error(f"Clean shutdown signal failed: {e!r}")
        time.sleep(force_exit_s)
        logger.error(f"The service did not stop within {force_exit_s:g}s - ending the process")
        hard_exit()

    threading.Thread(target=_go, name="apply-restart", daemon=True).start()
    return True
