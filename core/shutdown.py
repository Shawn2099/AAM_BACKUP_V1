"""Remote NAS shutdown via Windows shutdown.exe, with the result verified (plan WP10).

Command: shutdown /s /m \\\\<server_ip> /t <wol.shutdown_delay_seconds> /f
Staff can cancel within the delay by running `shutdown /a` on the NAS.

Real shutdown.exe facts (Windows Server 2016 + Windows 11, verified 2026-10-15):
  - the exit code is the Win32 error; the reason is on stderr as
    `<host>: <text>(<code>)`; stdout stays empty;
  - 0 = accepted, 1190 = a shutdown is already scheduled, 5 = access denied,
    1707 = the network address is invalid.

"Accepted" is not "off": the command only schedules the shutdown, and staff
may cancel it. So after an accepted command (0 or 1190) port 445 is polled
until it stays closed for `closed_probes` consecutive probes, within
`verify_timeout_s` counted from the command (it must outlast the delay).

Outcomes:
  VERIFIED      accepted and the NAS stopped answering on 445
  NOT_VERIFIED  accepted, but the NAS still answers (cancelled or ignored)
  FAILED        the command was refused or did not complete
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from loguru import logger

from core.run_artefacts import RunArtefacts
from core.smb_ready import tcp_port_open
from core.tool_runner import run_tool

VERIFIED = "VERIFIED"
NOT_VERIFIED = "NOT_VERIFIED"
FAILED = "FAILED"

_ACCEPTED = 0
_ALREADY_SCHEDULED = 1190  # ERROR_SHUTDOWN_IS_SCHEDULED
_SMB_PORT = 445


@dataclass
class ShutdownResult:
    outcome: str
    server_ip: str
    exit_code: int | None
    detail: str
    elapsed_s: float

    @property
    def verified(self) -> bool:
        return self.outcome == VERIFIED

    def to_metrics(self) -> dict:
        return asdict(self)


def shutdown_server(
    server_ip: str,
    *,
    delay_s: int,
    command_timeout_s: float,
    verify_timeout_s: float,
    poll_interval_s: float,
    connect_timeout_s: float,
    closed_probes: int,
    artefacts: RunArtefacts,
    clock=time.monotonic,
    sleep=time.sleep,
    tcp_probe=None,
) -> ShutdownResult:
    """Schedule the NAS shutdown and verify that the NAS actually went down."""
    probe = tcp_probe or tcp_port_open
    start = clock()
    argv = ["shutdown", "/s", "/m", f"\\\\{server_ip}", "/t", str(delay_s), "/f"]
    res = run_tool("shutdown", argv, timeout_s=command_timeout_s, artefacts=artefacts)

    if not res.completed:
        detail = f"shutdown command did not complete: {res.describe_failure()}"
        logger.error(f"NAS shutdown FAILED for {server_ip}: {detail}")
        return ShutdownResult(FAILED, server_ip, None, detail, round(clock() - start, 1))

    tool_text = (res.stderr_text().strip() or res.stdout_text().strip())
    if res.exit_code == _ALREADY_SCHEDULED:
        note = "a shutdown was already scheduled on the NAS"
        logger.info(f"{server_ip}: {note} (1190) - verifying it goes down")
    elif res.exit_code == _ACCEPTED:
        note = f"shutdown accepted ({delay_s}s delay)"
        logger.info(f"{server_ip}: {note} - verifying it goes down")
    else:
        detail = f"shutdown refused (exit {res.exit_code}): {tool_text or 'no message'}"
        logger.error(f"NAS shutdown FAILED for {server_ip}: {detail}")
        return ShutdownResult(FAILED, server_ip, res.exit_code, detail,
                              round(clock() - start, 1))

    closed_in_a_row = 0
    while True:
        is_open, why = probe(server_ip, _SMB_PORT, connect_timeout_s)
        elapsed = clock() - start
        closed_in_a_row = 0 if is_open else closed_in_a_row + 1
        logger.debug(f"{server_ip}: t+{elapsed:.0f}s port {_SMB_PORT} "
                     f"{'open' if is_open else 'closed'} ({why})")
        if closed_in_a_row >= closed_probes:
            detail = f"{note}; NAS stopped answering on port {_SMB_PORT} after {elapsed:.0f}s"
            logger.info(f"NAS shutdown VERIFIED for {server_ip}: {detail}")
            return ShutdownResult(VERIFIED, server_ip, res.exit_code, detail, round(elapsed, 1))
        if elapsed >= verify_timeout_s:
            break
        sleep(poll_interval_s)

    detail = (f"{note}, but the NAS is still reachable on port {_SMB_PORT} after "
              f"{verify_timeout_s:.0f}s - the shutdown may have been cancelled "
              "(shutdown /a) or ignored")
    logger.error(f"NAS shutdown NOT VERIFIED for {server_ip}: {detail}")
    return ShutdownResult(NOT_VERIFIED, server_ip, res.exit_code, detail,
                          round(clock() - start, 1))


def shutdown_from_config(config, artefacts: RunArtefacts) -> ShutdownResult:
    """shutdown_server with every timing from the `wol` config section."""
    w = config.wol
    return shutdown_server(
        w.server_ip,
        delay_s=w.shutdown_delay_seconds,
        command_timeout_s=w.shutdown_command_timeout_seconds,
        verify_timeout_s=w.shutdown_verify_timeout_seconds,
        poll_interval_s=w.ping_interval_seconds,
        connect_timeout_s=w.smb_connect_timeout_seconds,
        closed_probes=w.shutdown_verify_closed_probes,
        artefacts=artefacts,
    )
