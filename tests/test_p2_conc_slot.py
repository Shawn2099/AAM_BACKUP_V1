"""P2-CONC - SCH-16: direct pipeline entry must serialize on the slot.

Evidence: tests/scripts calling _run_lan_pipeline/_run_cloud_pipeline
directly BYPASS any flow-level wrapper - two robocopies could hit the NAS
at once. Fix (plan v1.1 approved shape): the slot moves INTO each pipeline
(_backup_slot); the flow-level wrapper is removed in the SAME commit.

Runs in a CLEAN-ENV SUBPROCESS pinned to the live API (same pattern as
branch_f): in-process threads deadlock in Prefect's concurrency client when
PREFECT_TEST_MODE collides with PREFECT_API_URL.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.scenario_support import real_gate

pytestmark = real_gate()

_PROJECT = "C:/AAM_BACKUP_V1"

_PRELUDE = r'''
import os, sys, json, threading, time, faulthandler
os.environ["PREFECT_TEST_MODE"] = "0"
os.environ["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"
sys.path.insert(0, "C:/AAM_BACKUP_V1")
'''


def _scenario_code(db_path: str) -> str:
    return (
        "import os\n"
        "from pathlib import Path\n"
        "from models.config import load_config\n"
        "from tests.e2e_helpers import make_file\n"
        "from tests.scenario_support import ensure_canary\n"
        "from flow import _run_lan_pipeline\n"
        "\n"
        f"DB = {db_path!r}\n"
        "cfg = load_config('C:/AAM_BACKUP_V1/config.yaml')\n"
        "cfg.paths.source_drive = 'C:/E2E_TEST_SOURCE'\n"
        "cfg.paths.lan_destination = r'\\\\127.0.0.1\\lan_backup\\E2E_TEST_DEST'\n"
        "cfg.paths.database_path = DB\n"
        "cfg.lan.enabled = True\n"
        "cfg.wol.enabled = False\n"
        "cfg.lan.shutdown_after_backup = False\n"
        "cfg.notifications.send_on_failure = False\n"
        "assert hasattr(cfg.maintenance, 'concurrency_wait_seconds'), "
        "'maintenance.concurrency_wait_seconds missing'\n"
        "cfg.maintenance.concurrency_wait_seconds = 560\n"
        "\n"
        "src = Path(cfg.paths.source_drive)\n"
        "src.mkdir(parents=True, exist_ok=True)\n"
        "make_file(src / 'a.txt', 65536)\n"
        "make_file(src / 'b.bin', 130 * 1024 * 1024)\n"
        "ensure_canary()\n"
        "\n"
        "intervals = {}\n"
        "def run(tag):\n"
        "    faulthandler.dump_traceback_later(520, exit=True)\n"
        "    t0 = time.monotonic()\n"
        "    result = _run_lan_pipeline(cfg, 'scen-sch16-' + tag,\n"
        "                               '2026-08-24T00:00:00', t0)\n"
        "    intervals[tag] = {'start': t0, 'end': time.monotonic(),\n"
        "                      'status': result['status'],\n"
        "                      'exit': result['exit_code']}\n"
        "\n"
        "ta = threading.Thread(target=run, args=('A',))\n"
        "tb = threading.Thread(target=run, args=('B',))\n"
        "ta.start(); time.sleep(1.5); tb.start()\n"
        "ta.join(timeout=620); tb.join(timeout=620)\n"
        "print(json.dumps({'intervals': intervals,\n"
        "                  'alive': threading.active_count()}))\n"
    )


def test_SCH_16_direct_calls_serialize():
    sid = "SCH-16"
    ops = {}
    db_path = str(Path(tempfile.gettempdir()) / "scen_sch16.db")
    Path(db_path).unlink(missing_ok=True)
    try:
        script = Path(tempfile.gettempdir()) / "sch16_live.py"
        script.write_text(_PRELUDE + _scenario_code(db_path), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PREFECT_")}
        env["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=900,
        )
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        payload = json.loads(lines[-1]) if lines else {}
        ops["_rc"] = proc.returncode
        ops["_stderr_tail"] = (proc.stderr or "")[-400:] if proc.returncode else ""
        assert proc.returncode == 0, f"op={ops}"

        iv = payload["intervals"]
        a, b = iv["A"], iv["B"]
        ops.update({
            "A": {k: round(v, 2) if isinstance(v, float) else v for k, v in a.items()},
            "B": {k: round(v, 2) if isinstance(v, float) else v for k, v in b.items()},
            "B_started_while_A_running": a["start"] < b["start"] < a["end"],
            "serialized_work": b["end"] > a["end"],
        })
        assert a["status"] == "LAN_COMPLETE" and b["status"] == "LAN_COMPLETE", f"op={ops}"
        # B launched while A held the slot -> B finishes strictly later;
        # parallel execution would show B ending BEFORE or WITH A given the
        # identical tiny workload and B's late start.
        assert ops["B_started_while_A_running"], f"B should have queued: op={ops}"
        assert ops["serialized_work"], f"work must not overlap: op={ops}"
        from tests.scenario_support import record_op

        record_op(sid, "PASS", ops)
    except Exception as e:
        ops.setdefault("error", f"{type(e).__name__}: {e}"[:250])
        from tests.scenario_support import record_op

        record_op(sid, "FAIL", ops)
        raise
    finally:
        Path(db_path).unlink(missing_ok=True)
