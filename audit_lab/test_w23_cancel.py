"""WAVE 23 - final audit 2026-10-20 (A6 / A2 / A7): a CANCEL reaches the running tool and is recorded truthfully.

The production flow runs in a child process with the REAL robocopy / rclone and a REAL shared Prefect server
(the W22 rig). Mid-sync the cancel is delivered the way Prefect delivers it on Windows: `_thread.interrupt_main(
SIGTERM)` in the flow-run process (Prefect's own SIGTERM handler turns it into TerminationSignal). Proven here:

* the interrupt is seen while the tool is still running (run_tool polls, A6) - the child exits within Prefect's
  30 s grace, long before the throttled copy could finish by itself;
* no robocopy / rclone process is left behind;
* the run is recorded as <LEG>_FAILED (never RUNNING, COMPLETE or SKIPPED) with the 'interrupted' fault code AAM-SYS-02 (A2);
* backup.lock is released (A7) and the slot is free: the next run starts at once, completes, destination == source.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import psutil
import pytest

from audit_lab import labconf as L
from audit_lab.test_w6_flow import rig  # noqa: F401 - the production-wired rig fixture
from audit_lab.test_w22_hardkill import (CHILD, LEASE_WAIT_SECONDS, _drop_local_backend_partials,  # noqa: F401
                                         _no_blocks, _statuses, _tool_children, _tree, hardkill_rig,
                                         real_prefect_server)

pytestmark = pytest.mark.timeout(1800)

PREFECT_CANCEL_GRACE_SECONDS = 30      # runner/_cancellation_manager.py: the graceful window on Windows


def _run_child_until_mid_sync(rig, leg: str):
    from prefect.settings import PREFECT_API_URL

    trigger = rig["sandbox"] / f"cancel_{leg}.trigger"
    env = {k: v for k, v in os.environ.items() if k != "PREFECT_TEST_MODE"}
    env.update(PREFECT_API_URL=PREFECT_API_URL.value(), PYTHONIOENCODING="utf-8")
    spec = {"cfg": str(rig["slow_cfg"]), "rc": rig["rc"], "entry": leg, "ipg": 100, "audit_sleep": 600,
            "cancel_trigger": str(trigger)}
    from models.config import load_config
    lock = load_config(str(rig["cfg_path"])).paths.backup_lock_path
    log = rig["sandbox"] / f"child_cancel_{leg}.log"
    with log.open("wb") as out:
        child = subprocess.Popen([sys.executable, str(CHILD), json.dumps(spec)], env=env,
                                 cwd=str(rig["buckets"]), stdout=out, stderr=subprocess.STDOUT)
    proc = psutil.Process(child.pid)
    deadline, seen = time.monotonic() + 300, None
    tools: list[psutil.Process] = []
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise AssertionError(f"child ended before the cancel (exit {child.returncode}): "
                                 f"{log.read_text(errors='replace')[-2000:]}")
        tools = _tool_children(proc) or tools
        if lock.exists() and _tool_children(proc):
            seen = seen or time.monotonic()
            if time.monotonic() - seen >= 4:                 # really in the middle of the copy
                break
        else:
            seen = None
        time.sleep(0.5)
    else:
        raise AssertionError("the child never reached the middle of its sync: "
                             f"{log.read_text(errors='replace')[-2000:]}")
    return child, trigger, lock, [t.pid for t in _tool_children(proc)] or [t.pid for t in tools], log


@pytest.mark.parametrize("leg", ["cloud", "lan"])
def test_w23_cancel_mid_sync_stops_the_tool_records_failed_and_frees_everything(hardkill_rig, leg):
    rig = hardkill_rig
    flow = rig["flow"]
    child, trigger, lock, tool_pids, log = _run_child_until_mid_sync(rig, leg)
    assert tool_pids, "no rclone/robocopy was running at the moment of the cancel"

    trigger.write_text("cancel")                                   # Prefect: interrupt_main(SIGTERM)
    t0 = time.monotonic()
    try:
        child.wait(timeout=PREFECT_CANCEL_GRACE_SECONDS + 30)
    except subprocess.TimeoutExpired:
        child.kill()
        raise AssertionError("the cancelled run did not exit: " + log.read_text(errors="replace")[-2500:])
    exited_after = time.monotonic() - t0

    survivors = [p for p in tool_pids if psutil.pid_exists(p)]
    statuses = _statuses(rig["cfg_path"], leg)
    from core.manifest import ManifestDB
    from models.config import load_config
    cfg = load_config(str(rig["cfg_path"]))
    db = ManifestDB(cfg.paths.database_path)
    try:
        rows = [r for r in db.get_recent_runs(20) if r["mode"] == leg]
    finally:
        db.close()
    metrics = json.loads(rows[0]["extended_metrics"] or "{}") if rows else {}
    blocked, approvals = _no_blocks(rig["cfg_path"])

    L.record(f"W23.cancel_mid_{leg}", f"Prefect-style cancel mid {leg} sync",
             f"child exited {exited_after:.1f}s after the cancel | tool survivors={survivors} | rows={statuses} "
             f"| fault={metrics.get('fault_code')} | lock left={lock.exists()} | blocked={blocked}",
             "OK" if not survivors and statuses == [f"{leg.upper()}_FAILED"] else "ISSUE")
    assert exited_after < PREFECT_CANCEL_GRACE_SECONDS, "the cancel waited for the tool instead of stopping it"
    assert not survivors, f"the tool outlived the cancelled flow: {survivors}"
    assert statuses == [f"{leg.upper()}_FAILED"], statuses
    assert metrics.get("fault_code") == "AAM-SYS-02"
    assert not lock.exists(), "the cancelled run must release backup.lock"
    assert not blocked and approvals == 0

    # the slot is free at once: the next run starts without waiting, completes, and the copy converges
    if leg == "cloud":
        _drop_local_backend_partials(rig)
    t1 = time.monotonic()
    flow.backup(config_path=str(rig["cfg_path"]), mode=leg)
    waited = time.monotonic() - t1
    after = _statuses(rig["cfg_path"], leg)
    src = _tree(rig["src"])
    dest = _tree(rig["nas"]) if leg == "lan" else _tree(rig["buckets"] / "lab-bucket" / rig["src"].name)
    assert after[0].endswith("COMPLETE") and f"{leg.upper()}_FAILED" in after, after   # newest first
    assert dest == src, "after the next run the destination must equal the source"
    assert waited < LEASE_WAIT_SECONDS
