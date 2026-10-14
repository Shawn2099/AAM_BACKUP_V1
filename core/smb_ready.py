"""Shared NAS / SMB readiness check (plan WP10, owner issue 2026-10-14).

Replaces three divergent probes (lan_preflight: hard-coded 2.0 s x 4 tries
~ 15 s; wol: hard-coded 5 s connect; audit: none). A NAS cold boot (POST +
disk spin-up + OS + SMB service) routinely takes minutes, so every timing
here comes from config, with generous defaults.

"Ready" means ALL of:
  1. TCP 445 accepts a connection (within `connect_timeout_s`), and
  2. a real SMB operation succeeds (stat of `probe_path`, e.g. the
     destination mount canary) — a bare TCP accept is not enough: a hung
     or still-booting NAS can accept connections and serve nothing
     (audit finding L-5), and
  3. after the first success, the share stays usable through
     `stability_wait_s` (re-checked once at the end).

Every attempt is logged with elapsed time so the real cold-boot time can
be measured on the client box and the config tuned (plan WP17).
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field

from loguru import logger


# Keep only the most recent attempts in the result (a zero/low poll interval
# must not grow an unbounded list); the summary always states the totals.
_TIMELINE_MAX = 50


@dataclass
class ReadinessResult:
    ready: bool
    host: str
    elapsed_s: float
    attempts: int
    reason: str
    timeline: list[str] = field(default_factory=list)


def tcp_port_open(host: str, port: int, timeout_s: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, "tcp open"
    except OSError as e:  # timeout, refused, unreachable, DNS
        return False, f"tcp {port}: {type(e).__name__}: {e}"


def smb_path_usable(probe_path: str | None) -> tuple[bool, str]:
    """A real SMB round-trip: stat the probe path (or list the share root)."""
    if not probe_path:
        return True, "no probe path configured"
    try:
        os.stat(probe_path)
        return True, "smb stat ok"
    except FileNotFoundError:
        # The share answers (it could tell us the file is absent) - SMB works.
        # Whether the canary must exist is the canary guard's decision (G5).
        return True, "smb answered (probe path absent)"
    except OSError as e:
        return False, f"smb stat failed: {type(e).__name__}: {e}"


def wait_for_smb_ready(
    host: str,
    *,
    probe_path: str | None,
    port: int = 445,
    connect_timeout_s: float,
    max_wait_s: float,
    poll_interval_s: float,
    stability_wait_s: float,
    min_attempts: int = 1,
    clock=time.monotonic,
    sleep=time.sleep,
    tcp_probe=None,
) -> ReadinessResult:
    """Poll until the SMB share is genuinely usable or `max_wait_s` passes.

    At least `min_attempts` probes are made even if `max_wait_s` is tiny,
    so a misconfiguration can never skip the check entirely.
    """
    probe = tcp_probe or tcp_port_open
    start = clock()
    attempts = 0
    timeline: list[str] = []
    last_reason = "not probed"
    while True:
        attempts += 1
        ok, why = probe(host, port, connect_timeout_s)
        if ok:
            ok, why = smb_path_usable(probe_path)
        elapsed = clock() - start
        if len(timeline) >= _TIMELINE_MAX:
            del timeline[0]
        timeline.append(f"t+{elapsed:.0f}s attempt {attempts}: {'OK' if ok else 'no'} ({why})")
        last_reason = why
        if ok:
            if stability_wait_s > 0:
                logger.info(f"{host}: SMB answered after {elapsed:.0f}s; waiting "
                            f"{stability_wait_s:.0f}s for the NAS to settle")
                sleep(stability_wait_s)
                ok2, why2 = probe(host, port, connect_timeout_s)
                if ok2:
                    ok2, why2 = smb_path_usable(probe_path)
                elapsed = clock() - start
                timeline.append(f"t+{elapsed:.0f}s stability re-check: "
                                f"{'OK' if ok2 else 'no'} ({why2})")
                if not ok2:
                    last_reason = f"share dropped during stability wait: {why2}"
                    if elapsed >= max_wait_s:
                        break
                    sleep(poll_interval_s)
                    continue
            logger.info(f"{host}: SMB ready after {elapsed:.0f}s ({attempts} attempt(s))")
            return ReadinessResult(True, host, round(elapsed, 1), attempts, "ready", timeline)
        logger.debug(f"{host}: not ready yet (attempt {attempts}, t+{elapsed:.0f}s): {why}")
        if elapsed >= max_wait_s and attempts >= min_attempts:
            break
        sleep(poll_interval_s)
    elapsed = clock() - start
    reason = (f"{host} did not become usable over SMB within {max_wait_s:.0f}s "
              f"({attempts} attempt(s), each up to {connect_timeout_s:.0f}s); "
              f"last result: {last_reason}")
    logger.error(reason)
    return ReadinessResult(False, host, round(elapsed, 1), attempts, reason, timeline)
