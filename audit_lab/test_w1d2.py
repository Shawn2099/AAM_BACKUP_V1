"""WAVE 1B-bis / D-bis — corrected experiments.

* B2: cloud copy has same size + same mtime but DIFFERENT content (pure silent
  corruption at the destination) -> daily gate vs weekly audit.
* D2: kill robocopy mid-run with exit code 0, using a command WITHOUT /ZB so the
      run actually starts (the /ZB privilege failure is proven separately in C1).
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path

import pytest

from audit_lab import labconf as L
from audit_lab.labconf import build_config, run as _run, write_file

pytestmark = pytest.mark.timeout(600)


def _flags_without_zb(cmd: list[str]) -> list[str]:
    return [a for a in cmd if a.upper() != "/ZB"]


def test_w1b2_pure_size_only_blind_spot(sandbox):
    import os as _os

    from core.cloud_sync import run_cloud_sync
    from core.cloud_verify import verify_cloud_integrity
    from core.integrity import audit_cloud
    from audit_lab.labconf import write_local_rclone_config

    src = sandbox / "source" / "FY26-26"
    write_file(src / "Clients" / "ledger.txt", b"OPENING BALANCE 100000\n" * 20)

    rc_path = write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc_path)
    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    cwd = _os.getcwd()
    _os.chdir(bucket_root)
    try:
        r = run_cloud_sync(source=str(src), bucket="lab-bucket", fy_prefix="FY26-26",
                           gcs_key_path=str(sandbox / "lab-key.json"), project_number="000000000000",
                           storage_class="STANDARD", bwlimit="1000M", retries=1, transfers=2,
                           checkers=2, buffer_size="16M", timeout=120, max_duration_seconds=0)
        assert r["status"] in ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"), r

        # silently corrupt the CLOUD copy: same size, same mtime, different bytes
        cloud_copy = bucket_root / "lab-bucket" / "FY26-26" / "Clients" / "ledger.txt"
        assert cloud_copy.exists()
        st = cloud_copy.stat()
        write_file(cloud_copy, b"OPENING BALANCE 999999\n" * 20)
        _os.utime(cloud_copy, (st.st_atime, st.st_mtime))

        daily = verify_cloud_integrity(source=str(src), bucket="lab-bucket",
                                       fy_prefix="FY26-26", config_path=rc_path, timeout=120)
        weekly = audit_cloud(str(src), "lab-bucket", "FY26-26", rc_path, timeout=120)

        L.record("B2.size_only_blind",
                 "a same-size (+ same-mtime) corrupted cloud copy is caught by the daily gate",
                 f"DAILY  --size-only : verified={daily['verified']} exit={daily['exit_code']} "
                 f"termination={daily['termination']} differences={daily['differences']} reason={daily['reason']}\n"
                 f"WEEKLY  hash       : status={weekly['status']} mismatches={weekly['mismatches']} "
                 f"capability={weekly['capability']} detail={weekly['detail'][:220]}",
                 "BLIND SPOT CONFIRMED — daily gate VERIFIED, weekly audit FAILED"
                 if daily["verified"] and weekly["status"] != "VERIFIED" else "both agreed")
        print(f"[B2] daily verified={daily['verified']} reason={daily['reason']} | weekly={weekly['status']} "
              f"mismatches={weekly['mismatches']}")
        assert daily["verified"] is True
        assert weekly["status"] == "VERIFICATION_FAILED"
    finally:
        _os.chdir(cwd)
        for (mod, attr), orig in saved.items():
            setattr(mod, attr, orig)


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


def test_w1d2_kill_lan_exit0_without_zb(sandbox):
    from core.lan_sync import decide_lan_result, run_lan_sync

    src = sandbox / "source" / "FY26-26"
    for i in range(4000):
        write_file(src / "Clients" / f"f_{i:05d}.bin", b"Z" * 4096)
    dest = sandbox / "nas" / "FY26-26"
    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")

    from core.lan_sync import build_robocopy_command
    cmd = _flags_without_zb(build_robocopy_command(str(src), str(dest), cfg.lan))
    print(f"[D2] starting: {' '.join(cmd[:4])} ... (no /ZB)")

    box: dict = {}
    err: dict = {}

    def _worker():
        try:
            box["res"] = run_lan_sync(str(src), str(dest), cfg.lan)
        except Exception as e:  # pragma: no cover
            err["e"] = repr(e)

    # run the real wrapper but force the command without /ZB
    import core.lan_sync as ls
    orig_build = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: _flags_without_zb(orig_build(s, d, c))
    try:
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        pid, deadline = None, time.time() + 30
        while time.time() < deadline and pid is None:
            pid = _find_pid("Robocopy", str(sandbox))
            time.sleep(0.05)
        if pid is None:
            pytest.fail("could not find the robocopy process to kill")
        time.sleep(0.4)
        killed = _terminate_exit_0(pid)
        t.join(timeout=300)
    finally:
        ls.build_robocopy_command = orig_build

    res = box.get("res", {})
    copied = len([p for p in dest.rglob("*") if p.is_file()])
    L.record("D2.kill_exit0.lan",
             "robocopy killed mid-run with exit code 0 is reported as success (F-08 forge)",
             f"robocopy_pid={pid} NtTerminateProcess(p,0)={killed} | status={res.get('status')} "
             f"exit_code={res.get('exit_code')} termination={res.get('termination')} "
             f"log_complete={res.get('log_complete')} log_retained={res.get('log_retained')} | "
             f"dest files={copied}/4000 | reason={(res.get('error') or '')[:180]}",
             "FAIL-CLOSED OK — kill detected as " + str(res.get('status'))
             if res.get("status") in ("LAN_SUSPECT", "LAN_FAILED")
             else f"FAIL-CLOSED BROKEN — status={res.get('status')} exit={res.get('exit_code')}")
    print(f"[D2] status={res.get('status')} exit={res.get('exit_code')} copied={copied}/4000 killed={killed}")
    # A8: with /ZB stripped the kill really lands mid-transfer. The contract:
    # an exit code of 0 with a TRUNCATED log must never read LAN_COMPLETE.
    assert killed is True, "could not kill the robocopy process - experiment is vacuous"
    assert copied < 4000, (
        f"kill landed after the whole copy finished ({copied}/4000) - this no "
        "longer proves the fail-closed path")
    assert res.get("status") == "LAN_SUSPECT", res
    assert res.get("termination") == "abnormal", res
    assert res.get("log_complete") is False, res
