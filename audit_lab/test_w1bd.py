"""WAVE 1B/D â€” verification blind spot (size-only) + kill-mid-run behaviour.

B: same-size / different-content file â€” does the DAILY gate say "verified"?
D: kill the transfer mid-run with exit code 0 (the F-08 / TerminateProcess(pid,0)
   class) â€” is the result fail-closed?
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from audit_lab import labconf as L
from audit_lab.labconf import build_config, write_file

pytestmark = pytest.mark.timeout(300)


# â”€â”€ B â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _find_pid(exe_leaf: str, needle: str) -> int | None:
    import psutil
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            cmd = " ".join(p.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name.startswith(exe_leaf.lower()) and needle.lower() in cmd.lower():
            return p.pid
    return None


def _terminate_exit_0(pid: int) -> bool:
    h = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
    if not h:
        return False
    try:
        return ctypes.windll.ntdll.NtTerminateProcess(h, 0) == 0
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _make_big_source(src: Path, *, files: int = 4000, size: int = 4096) -> None:
    for i in range(files):
        write_file(src / "Clients" / f"f_{i:05d}.bin", b"Z" * size)


def test_w1d_kill_lan_with_exit_code_0(sandbox):
    """Kill robocopy mid-run with exit code 0 -> must NOT be LAN_COMPLETE.

    LAB FIX (this test was vacuous on this box): it built the PRODUCTION command,
    which carries /ZB. Without SeBackupPrivilege robocopy exits 16 before copying
    a single byte (finding C-2), so `pid` stayed None, the kill never happened,
    and the recorded verdict was an artefact of C-2 rather than a statement about
    the kill contract. The missing helper name is fixed above. /ZB is now stripped
    through the lab seam (as test_w1d2 already does) so the kill really happens
    mid-transfer, and the assertions below prove it.
    """
    import core.lan_sync as ls
    from core.lan_sync import run_lan_sync

    src = sandbox / "source" / "FY26-26"
    _make_big_source(src)
    dest = sandbox / "nas" / "FY26-26"
    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")

    _orig_build = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(_orig_build(s, d, c))
    box: dict = {}

    def _worker():
        box["res"] = run_lan_sync(str(src), str(dest), cfg.lan)

    try:
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        pid = None
        deadline = time.time() + 20
        while time.time() < deadline and pid is None:
            pid = _find_pid("Robocopy", str(sandbox))
            time.sleep(0.2)
        killed = _terminate_exit_0(pid) if pid else False
        t.join(timeout=180)
    finally:
        ls.build_robocopy_command = _orig_build
    res = box.get("res", {})

    dest_files = len([p for p in dest.rglob("*") if p.is_file()])
    L.record("D.kill_exit0.lan",
             "robocopy killed mid-run with exit code 0 is reported as success (F-08 forge)",
             f"found_robocopy_pid={pid} NtTerminateProcess(0)={killed} | "
             f"status={res.get('status')} exit_code={res.get('exit_code')} "
             f"termination={res.get('termination')} log_complete={res.get('log_complete')} "
             f"log_retained={res.get('log_retained')} | dest files copied={dest_files}/{4000}",
             "FAIL-CLOSED OK: status=" + str(res.get('status')) +
             " (kill detected, nothing falsely COMPLETE)"
             if res.get("status") == "LAN_SUSPECT" else
             f"FAIL-CLOSED BROKEN: status={res.get('status')} exit={res.get('exit_code')}")
    print(f"[D-lan] status={res.get('status')} exit={res.get('exit_code')} killed={killed}")
    # A6: the kill must actually have happened mid-transfer, and the verdict must
    # fail closed (never LAN_COMPLETE) with the forensic log retained.
    assert killed is True, "could not kill the robocopy process - experiment is vacuous"
    assert res.get("status") == "LAN_SUSPECT", res
    assert res.get("termination") == "abnormal", res
    assert res.get("log_complete") is False, res
    assert dest_files < 4000, (
        f"kill landed after the whole copy finished ({dest_files}/4000) - this no "
        "longer proves fail-closed behaviour")


def test_w1d_kill_cloud_with_exit_code_0(sandbox):
    """Kill rclone mid-run with exit code 0 -> what does run_cloud_sync report?"""
    import os as _os

    from core.cloud_sync import run_cloud_sync
    from audit_lab.labconf import write_local_rclone_config

    src = sandbox / "source" / "FY26-26"
    _make_big_source(src, files=3000)
    rc_path = write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc_path)
    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    cwd = _os.getcwd()
    _os.chdir(bucket_root)
    try:
        box: dict = {}

        def _worker():
            box["res"] = run_cloud_sync(
                source=str(src), bucket="lab-bucket", fy_prefix="FY26-26",
                gcs_key_path=str(sandbox / "lab-key.json"), project_number="000000000000",
                storage_class="STANDARD", bwlimit="1000M", retries=1, transfers=2,
                checkers=2, buffer_size="16M", timeout=200, max_duration_seconds=0)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        pid, deadline = None, time.time() + 30
        while time.time() < deadline and pid is None:
            pid = _find_pid("rclone", str(sandbox))
            time.sleep(0.2)
        killed = _terminate_exit_0(pid) if pid else False
        t.join(timeout=240)
        res = box.get("res", {})

        landed = len([p for p in (bucket_root / "lab-bucket" / "FY26-26").rglob("*") if p.is_file()])
        L.record("D.kill_exit0.cloud",
                 "rclone killed mid-run with exit code 0 is caught by run_cloud_sync itself",
                 f"found_rclone_pid={pid} NtTerminateProcess(0)={killed} | status={res.get('status')} "
                 f"exit_code={res.get('exit_code')} suspect_reason={(res.get('suspect_reason') or '')[:160]} | "
                 f"objects landed={landed}/3000",
                 f"transfer-level contract: status={res.get('status')} "
                 f"(the flow-level evidence gate is the real safeguard)")
        print(f"[D-cloud] status={res.get('status')} exit={res.get('exit_code')} landed={landed}/3000")
        # A7: the experiment must actually have killed rclone mid-transfer, and
        # run_cloud_sync must not implicitly claim a clean COMPLETE on exit 0.
        # (The flow-level evidence gate is the real backstop - proven in
        #  test_w4_pipeline.py::test_w4_cloud_killed_mid_sync_is_rejected_by_gate.)
        assert killed is True, "could not kill the rclone process - experiment is vacuous"
        assert landed < 3000, (
            f"kill landed after the whole transfer finished ({landed}/3000) - this "
            "no longer proves anything about run_cloud_sync's own verdict")
        assert res.get("status") in ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"), res
        assert res.get("exit_code") == 0, res
    finally:
        _os.chdir(cwd)
        for (mod, attr), orig in saved.items():
            setattr(mod, attr, orig)


