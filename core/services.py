"""Windows service control through sc.exe (plan WP13).

Real sc.exe facts (trial box, Windows Server 2016, 2026-10-15):
  - every message, errors included, goes to STDOUT; the exit code is the
    Win32 error (1060 = no such service). The old watchdog logged stderr
    only, so the production 1051 failure was logged with an empty reason.
  - `sc queryex <svc>`: `STATE : 4  RUNNING` and `PID : 1100` lines.
  - AamBackupAgent DEPENDS ON AamPrefectServer: stopping the server while
    the agent runs is refused with 1051, so a restart stops the agent first.

Decision logic (when to restart, when killing is allowed) stays in the
watchdog; this module only executes and reports.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import psutil
from loguru import logger

from core.tool_runner import run_capture

RUNNING = "RUNNING"
STOPPED = "STOPPED"
STOP_PENDING = "STOP_PENDING"
START_PENDING = "START_PENDING"

_ALREADY_RUNNING = 1056   # ERROR_SERVICE_ALREADY_RUNNING
_NOT_ACTIVE = 1062        # ERROR_SERVICE_NOT_ACTIVE (already stopped)


@dataclass
class ServiceStatus:
    state: str            # SCM state name, '' when it could not be determined
    pid: int | None
    detail: str


@dataclass
class ControlResult:
    ok: bool
    exit_code: int | None
    detail: str


def _one_line(text: str) -> str:
    return " ".join(text.split())


# SCM state codes (winsvc.h) -> the English state names the callers compare with. sc.exe localizes the LABELS
# (`STATE` is `ZUSTAND` on a German system, `ETAT` on a French one), never the numeric code.
_STATE_BY_CODE = {1: STOPPED, 2: START_PENDING, 3: STOP_PENDING, 4: RUNNING,
                  5: "CONTINUE_PENDING", 6: "PAUSE_PENDING", 7: "PAUSED"}
_LABEL_NUMBER_NAME = re.compile(r"^\s*[^:]+?\s*:\s*(\d+)\s+([A-Za-z_]+)\s*$")


def _parse_queryex(output: str) -> tuple[str, int | None]:
    """(state, pid) from `sc queryex` text, whatever the language of the labels: the state is the first
    `<label> : <number> <NAME>` line whose number is a state code (the TYPE line has 10/20/110/120; a
    driver's `1 KERNEL_DRIVER` is skipped by name), the PID the first bare `<label> : <integer>` line after it."""
    lines = output.splitlines()
    state_at = None
    for exact in (True, False):
        for i, line in enumerate(lines):
            m = _LABEL_NUMBER_NAME.match(line)
            if not m or int(m.group(1)) not in _STATE_BY_CODE:
                continue
            name = m.group(2).upper()
            code_name = _STATE_BY_CODE[int(m.group(1))]
            if (name == code_name) if exact else ("DRIVER" not in name and "PROCESS" not in name):
                state_at = i
                break
        if state_at is not None:
            break
    if state_at is None:
        return "", None
    state = _STATE_BY_CODE[int(_LABEL_NUMBER_NAME.match(lines[state_at]).group(1))]
    pid = None
    for line in lines[state_at + 1:]:
        _label, sep, value = line.partition(":")
        if sep and value.strip().isdigit():
            pid = int(value.strip()) or None
            break
    return state, pid


def query(service: str, *, timeout_s: float) -> ServiceStatus:
    """`sc queryex` -> state + PID (PID 0 = no process -> None)."""
    res = run_capture(["sc", "queryex", service], timeout_s=timeout_s)
    if not res.completed:
        return ServiceStatus("", None, res.describe_failure())
    state, pid = _parse_queryex(res.output)
    if not state:
        return ServiceStatus("", None, f"sc queryex exit {res.exit_code}: {_one_line(res.output)}")
    return ServiceStatus(state, pid, "")


def _control(verb: str, service: str, ok_codes: tuple[int, ...], timeout_s: float) -> ControlResult:
    res = run_capture(["sc", verb, service], timeout_s=timeout_s)
    if not res.completed:
        return ControlResult(False, None, res.describe_failure())
    detail = _one_line(res.output)
    ok = res.exit_code == 0 or res.exit_code in ok_codes
    (logger.info if ok else logger.error)(f"sc {verb} {service}: exit {res.exit_code} {detail}")
    return ControlResult(ok, res.exit_code, detail)


def stop(service: str, *, timeout_s: float) -> ControlResult:
    return _control("stop", service, (_NOT_ACTIVE,), timeout_s)


def start(service: str, *, timeout_s: float) -> ControlResult:
    return _control("start", service, (_ALREADY_RUNNING,), timeout_s)


def wait_for(service: str, targets: set[str], *, timeout_s: float, poll_s: float,
             sc_timeout_s: float, clock=time.monotonic, sleep=time.sleep) -> ServiceStatus:
    """Poll until the state is one of `targets` or `timeout_s` passes; returns the last status."""
    start_t = clock()
    while True:
        status = query(service, timeout_s=sc_timeout_s)
        if status.state in targets or clock() - start_t >= timeout_s:
            return status
        sleep(poll_s)


def kill_process_tree(pid: int) -> str:
    """Kill a service host process (nssm.exe) and its children; returns what happened."""
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.Error as e:
        return f"PID {pid} not killable: {type(e).__name__}: {e}"
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=30)
    return f"killed PID {pid} and {len(procs) - 1} child process(es); {len(alive)} still alive"
