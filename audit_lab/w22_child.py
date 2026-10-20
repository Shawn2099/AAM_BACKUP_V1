"""Child process of WAVE 22 (plan I4): runs ONE production flow with the real robocopy/rclone
against the lab rig, so the parent test can hard-kill the whole process tree mid-run.

argv[1] = JSON {"cfg": config.yaml path, "rc": local rclone config, "entry": cloud|lan|audit|rollover,
                "ipg": robocopy /IPG ms (throttle), "audit_sleep": seconds}
Env: PREFECT_API_URL = the parent's ephemeral Prefect server (shared slot, as in production).
Lab-only seams, like labconf: local rclone backend, no /ZB, no WoL, no shutdown.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from test_support import sandbox_config  # noqa: E402

sandbox_config.install()

from audit_lab import labclock  # noqa: E402,F401
from audit_lab import labconf as L  # noqa: E402

spec = json.loads(sys.argv[1])
os.environ.pop("PREFECT_TEST_MODE", None)

import core.lan_sync as ls  # noqa: E402
import flow  # noqa: E402

_orig_build = ls.build_robocopy_command
# /IPG (throttle, so the kill lands mid-copy) cannot be combined with /MT
ls.build_robocopy_command = lambda s, d, c: [
    a for a in L.strip_zb(_orig_build(s, d, c)) if not a.upper().startswith("/MT")
] + [f"/IPG:{spec['ipg']}"]
L.patch_cloud_modules_to_local(spec["rc"])
flow.temp_rclone_config = lambda *a, **k: contextlib.nullcontext(spec["rc"])
flow.notify = lambda *a, **k: True

entry = spec["entry"]

# WAVE 23 (cancel): when the parent drops the trigger file, deliver the cancel exactly as Prefect's
# control listener does on Windows - `_thread.interrupt_main(SIGTERM)` in this (flow-run) process, whose
# SIGTERM handler (prefect.utilities.engine) raises TerminationSignal in the main thread.
if spec.get("cancel_trigger"):
    import _thread
    import signal
    import threading

    def _watch_for_cancel():
        while not Path(spec["cancel_trigger"]).exists():
            time.sleep(0.2)
        _thread.interrupt_main(signal.SIGTERM)

    threading.Thread(target=_watch_for_cancel, daemon=True).start()

if entry in ("cloud", "lan"):
    flow.backup(config_path=spec["cfg"], mode=entry)
elif entry == "audit":
    import core.integrity as integrity

    _orig_check = integrity._run_rclone_check

    def _slow_check(*a, **k):
        time.sleep(spec["audit_sleep"])      # the audit holds the slot, read-only, for a while
        return _orig_check(*a, **k)

    integrity._run_rclone_check = _slow_check
    flow.integrity_audit_flow(config_path=spec["cfg"])
elif entry == "rollover":
    flow.get_fy_prefix = lambda *a, **k: "FY27-28"
    flow.rollover_check_flow(config_path=spec["cfg"])
else:
    raise SystemExit(f"unknown entry {entry!r}")
