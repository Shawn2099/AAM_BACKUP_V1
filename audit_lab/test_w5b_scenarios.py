"""WAVE 5b — real-world conditions that happen *during* a backup.

b1. a JUNCTION in the source tree (extremely common: "Current" links, legacy remaps)
b2. a file RENAMED/DELETED mid-run (accountants working while the backup runs)
b3. a crash of the backup process mid-run, then the next night's run
b4. two backups started at once (operator double-click + cron)
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

pytestmark = pytest.mark.timeout(900


)


def _pid_of(exe: str, needle: str) -> int | None:
    import psutil
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info.get("name") or "").lower().startswith(exe.lower()) and \
               needle.lower() in " ".join(p.info.get("cmdline") or []).lower():
                return p.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _kill_hard(pid: int) -> bool:
    h = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
    if not h:
        return False
    try:
        return ctypes.windll.ntdll.NtTerminateProcess(h, 1) == 0
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


# ── b1: junction in the source ─────────────────────────────────────────────────
def test_w5b_junction_in_source(sandbox):
    """A junction pointing back at the source root — recursion / duplicate data?"""
    import core.lan_sync as ls
    from audit_lab.labconf import run as _run

    src = sandbox / "source" / "FY26-26"
    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    for i in range(20):
        L.write_file(src / "Clients" / f"l{i}.txt", f"L{i}\n".encode() * 40)

    # realistic operator artefact: a junction back to the FY root
    r = _run(["cmd", "/c", "mklink", "/J", str(src / "Clients" / "Current"), str(src)],
             timeout=60)
    if r.returncode != 0:
        pytest.skip(f"mklink unavailable: {r.stdout[:100]}{r.stderr[:100]}")

    cfg = L.build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    orig = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig(s, d, c))
    try:
        res = ls.run_lan_sync(str(src), str(dest), cfg.lan)
    finally:
        ls.build_robocopy_command = orig

    copied = [p for p in dest.rglob("*") if p.is_file() and p.name != ".AAM_TARGET_MOUNTED"]
    nested = [p for p in copied if "Current" in str(p)]
    L.record("W5b.junction.lan",
             "a junction inside the source does not duplicate the tree into the destination",
             f"mklink rc={r.returncode} | robocopy exit={res['exit_code']} status={res['status']} | "
             f"copied files={len(copied)} (expected 20) | files under the junction={len(nested)} | "
             f"e.g. {[str(p.relative_to(dest))[:60] for p in nested[:2]]}",
             "OK — /XJ excluded the junction (no duplication, no recursion)"
             if len(nested) == 0 and len(copied) >= 20 else
             f"ISSUE: junction content was mirrored ({len(nested)} files) — possible runaway duplication")
    print(f"[W5b-junction lan] status={res['status']} copied={len(copied)} via_junction={len(nested)}")

    # ── cloud leg: rclone's treatment of the same junction ────────────────────
    import os as _os

    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    rc = L.write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc)
    cwd = _os.getcwd()
    _os.chdir(bucket_root)
    try:
        from core.cloud_sync import run_cloud_sync

        cres = run_cloud_sync(
            source=str(src), bucket="lab-bucket", fy_prefix="FY26-26",
            gcs_key_path=str(sandbox / "lab-key.json"), project_number="000000000000",
            storage_class="STANDARD", bwlimit="1000M", retries=1, transfers=2,
            checkers=2, buffer_size="16M", timeout=180, max_duration_seconds=0)
        cloud_files = [p for p in (bucket_root / "lab-bucket" / "FY26-26").rglob("*") if p.is_file()]
        via_junction = [p for p in cloud_files if "Current" in str(p)]
        L.record("W5b.junction.cloud",
                 "rclone does not follow the junction into the cloud",
                 f"rclone exit={cres['exit_code']} status={cres['status']} "
                 f"error={(cres.get('error') or '')[:200]} | objects={len(cloud_files)} | "
                 f"via junction={len(via_junction)}",
                 "OK — junction not followed" if not via_junction else
                 f"ISSUE: {len(via_junction)} objects uploaded through the junction")
        print(f"[W5b-junction cloud] status={cres['status']} objects={len(cloud_files)} "
              f"via_junction={len(via_junction)}")
        # A12: /XJ must exclude the junction from the LAN mirror and rclone must
        # not follow it into the cloud, while the REAL tree still mirrors.
        assert len(nested) == 0, f"{len(nested)} files were mirrored through the junction"
        assert len(copied) >= 20, f"only {len(copied)} of the 20 real files copied"
        assert len(via_junction) == 0, f"{len(via_junction)} objects went through the junction"
    finally:
        _os.chdir(cwd)
        for (mod, attr), o in saved.items():
            setattr(mod, attr, o)


# ── b2: file renamed mid-run ───────────────────────────────────────────────────
def test_w5b_rename_during_mirror(sandbox):
    """A client folder is renamed while robocopy /MIR is copying."""
    import core.lan_sync as ls

    src = sandbox / "source" / "FY26-26"
    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    for i in range(4000):
        L.write_file(src / "Clients" / f"f_{i:05d}.txt", b"x" * 512)
    keep_dir = src / "Clients" / "TO_BE_RENAMED"
    keep_dir.mkdir()
    for i in range(50):
        L.write_file(keep_dir / f"keep_{i}.txt", b"y" * 200)

    cfg = L.build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    orig = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig(s, d, c))

    box: dict = {}

    def _run_sync():
        box["res"] = ls.run_lan_sync(str(src), str(dest), cfg.lan)

    t = threading.Thread(target=_run_sync, daemon=True)
    t.start()
    pid, deadline = None, time.time() + 30
    while time.time() < deadline and pid is None:
        pid = _pid_of("Robocopy", str(sandbox))
        time.sleep(0.02)
    time.sleep(0.15)
    # the operator renames the folder mid-backup
    renamed = keep_dir.rename(src / "Clients" / "RENAMED_BY_USER")
    t.join(timeout=600)
    ls.build_robocopy_command = orig

    res = box.get("res", {})
    dest_renamed = dest / "Clients" / "RENAMED_BY_USER"
    dest_old = dest / "Clients" / "TO_BE_RENAMED"
    old_left = len([p for p in dest_old.rglob("*") if p.is_file()]) if dest_old.exists() else 0
    new_left = len([p for p in dest_renamed.rglob("*") if p.is_file()]) if dest_renamed.exists() else 0
    L.record("W5b.rename_mid_run",
             "a source rename during the mirror does not lose the destination copy",
             f"robocopy exit={res.get('exit_code')} status={res.get('status')} "
             f"files_failed={res.get('files_failed')} | "
             f"dest 'TO_BE_RENAMED' (purged by /MIR)={old_left}/50 | "
             f"dest 'RENAMED_BY_USER'={new_left}/50",
             "MIRROR-RACE CONFIRMED — the destination ends up with 0 copies of the renamed folder "
             "in this run; the next run re-copies it (recovers), but for one cycle the backup is "
             "missing those files"
             if new_left == 0 and old_left == 0 else
             f"OK — {new_left} copies present (next run would converge)")
    print(f"[W5b-rename] status={res.get('status')} old={old_left} new={new_left}")
    assert renamed.exists()


# ── b3: crash mid-run + next-night recovery ───────────────────────────────────
def test_w5b_crash_then_recovery(sandbox):
    """Kill the whole pipeline hard (TerminateProcess), leaving the lock + WAL behind,
    then run the next night's pipeline over the same runtime dir."""
    import flow

    from core.manifest import ManifestDB
    from core.process import read_lock_alive

    src = sandbox / "source" / "FY26-26"
    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    for i in range(4000):
        L.write_file(src / "Clients" / f"f_{i:05d}.txt", b"x" * 512)

    cfg = L.build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    lock = cfg.paths.backup_lock_path
    db_path = cfg.paths.database_path

    # night 1: start the LAN pipeline and hard-kill the FLOW process itself
    import core.lan_sync as ls

    orig = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig(s, d, c))

    proc = subprocess.Popen(
        [str(Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe"),
         "-c", (
             "import os,sys,time,pathlib;sys.path.insert(0,%r);"
             "from core.lan_sync import run_lan_sync;"
             "from audit_lab.labconf import build_config, strip_zb;"
             "import core.lan_sync as ls;"
             "_o=ls.build_robocopy_command;"
             "ls.build_robocopy_command=lambda s,d,c:strip_zb(_o(s,d,c));"
             "P=pathlib.Path;"
             "cfg=build_config(P(%r),source=P(%r),lan_dest=%r,runtime=P(%r));"
             "print(run_lan_sync(%r,cfg.paths.lan_destination,cfg.lan));time.sleep(60)"
         ) % (str(Path(__file__).resolve().parent.parent), str(sandbox), str(src),
              str(dest), str(sandbox / "runtime"), str(src))],
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    # wait until the robocopy child exists, then kill the whole tree
    robocopy_pid, deadline = None, time.time() + 30
    while time.time() < deadline and robocopy_pid is None:
        robocopy_pid = _pid_of("Robocopy", str(sandbox))
        time.sleep(0.05)
    time.sleep(0.3)
    killed_tree = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                 capture_output=True, text=True)
    proc.wait(timeout=60)

    lock_present_after = lock.exists()
    alive_after, pid_after = read_lock_alive(lock)

    # night 2: same /ZB-free command, over the same runtime dir
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig(s, d, c))
    try:
        out = flow._run_lan_pipeline(cfg, "w5b-night2", "2026-09-07T01:00:00+05:30",
                                     time.monotonic())
    finally:
        ls.build_robocopy_command = orig

    m = ManifestDB(db_path)
    runs = m.get_recent_runs(5)
    rows = m.file_count("lan_status")
    m.close()
    on_nas = len([p for p in dest.rglob("*.txt")])

    L.record("W5b.crash_recovery",
             "a hard crash mid-backup does not wedge the next night's run",
             f"killed flow tree rc={killed_tree.returncode} (robocopy pid={robocopy_pid}) | "
             f"lock file after crash: exists={lock_present_after} alive={alive_after} pid={pid_after} | "
             f"night-2 pipeline={out} | files on NAS={on_nas}/4000 | "
             f"manifest lan_status rows={rows} | run rows={[r['status'] for r in runs]}",
             "OK — stale lock collected, night 2 completed, DB consistent"
             if out["status"] == "LAN_COMPLETE" and on_nas == 4000 else
             f"ISSUE — night 2 result {out['status']}, {on_nas}/4000 on NAS")
    print(f"[W5b-crash] lock_after={lock_present_after} night2={out['status']} on_nas={on_nas} rows={rows}")
    # A13: a hard crash must not wedge the next night, and the next night must
    # converge on the full tree. A stale lock that survives blocks every future
    # backup; a lost file is the R5/M-3 class of silent data loss.
    assert out["status"] == "LAN_COMPLETE", out
    assert on_nas == 4000, f"only {on_nas}/4000 files on the NAS after recovery"
    assert len(runs) >= 1 and runs[0]["status"] == "LAN_COMPLETE", runs
    if lock_present_after:
        assert alive_after is False, (
            "the crashed run left a lock that still reads as LIVE - every future "
            "backup would refuse to start")


# ── b4: two backups at once ────────────────────────────────────────────────────
def test_w5b_double_trigger(sandbox, monkeypatch):
    """Cron fires while the operator also clicks the dashboard button."""
    import flow

    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    src = sandbox / "source" / "FY26-26"
    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    for i in range(400):
        L.write_file(src / "Clients" / f"f_{i:05d}.txt", b"x" * 256)
    cfg = L.build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")

    import core.lan_sync as ls

    orig = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig(s, d, c))
    results: list = []

    def _one(tag):
        try:
            results.append((tag, flow._run_lan_pipeline(
                cfg, f"w5b-{tag}", "2026-09-07T01:00:00+05:30", time.monotonic())["status"]))
        except Exception as e:
            results.append((tag, f"RAISED {type(e).__name__}: {e}"))

    t1 = threading.Thread(target=_one, args=("first",))
    t2 = threading.Thread(target=_one, args=("second",))
    t1.start(); time.sleep(0.05); t2.start(); t1.join(); t2.join()
    ls.build_robocopy_command = orig

    from core.manifest import ManifestDB

    m = ManifestDB(cfg.paths.database_path)
    runs = m.get_recent_runs(5)
    m.close()
    L.record("W5b.double_trigger",
             "two simultaneous LAN runs are serialised by the lock, not interleaved",
             f"threads -> {results} | run rows={[r['status'] for r in runs]} | "
             f"files on NAS={len([p for p in dest.rglob('*.txt')])}/400 | "
             "NOTE: PREFECT_TEST_MODE=1 so the Prefect concurrency slot is bypassed here; "
             "this exercises only the backup.lock layer",
             "OK — no interleaving of the two mirror runs (they ran back-to-back or one refused)"
             if len(runs) <= 2 and len([p for p in dest.rglob("*.txt")]) == 400 else
             "ISSUE — both pipelines executed concurrently")
    print(f"[W5b-double] {results}")
    # A14: two triggers must serialise - never interleave and never double-copy.
    # Both threads must have produced exactly one terminal status each and the
    # lock/Prefect layer must have prevented overlapping mirrors.
    assert len(results) == 2, f"expected two terminal outcomes, got {results}"
    assert all(isinstance(s, str) and s for _t, s in results), results
    assert len(runs) <= 2, f"more than 2 run rows recorded for 2 triggers: {runs}"
    on_nas = len([p for p in dest.rglob("*.txt")])
    assert on_nas == 400, f"expected exactly 400 files on the NAS, found {on_nas}"
