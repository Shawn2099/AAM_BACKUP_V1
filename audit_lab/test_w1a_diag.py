"""WAVE 1A-bis — why did robocopy exit 16, and what happens with a PARTIALLY
readable source (the realistic D2 case) on both legs?

D2-real: source = A (readable) + B (ACL-denied). Destination mirrors both.
Question: does each leg delete B from the destination?
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from audit_lab import labconf as L
from audit_lab.labconf import build_config, run as _run, write_file

pytestmark = pytest.mark.timeout(300)


def _deny_read(path: Path) -> tuple[bool, str]:
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    r = _run(["icacls", str(path), "/deny", f"{user}:(RD)"], timeout=60)
    return r.returncode == 0, f"icacls rc={r.returncode} user={user} out={r.stdout.strip()[:150]}"


def _can_read(path: Path) -> bool:
    try:
        list(path.iterdir())
        return True
    except PermissionError:
        return False
    except OSError:
        return False


def test_w1a_diag_robocopy_exit16_reason(sandbox):
    """Capture the actual robocopy output for the /ZB privilege abort (C-2).

    The source holds one real file: a dirs-only source no longer reaches
    robocopy at all (G1 backstop, M2 2.2 - see test_w1a_purge D1)."""
    from core.lan_sync import run_lan_sync

    src = sandbox / "source" / "FY26-26"
    for d in ("Clients", "Registers"):
        (src / d).mkdir(parents=True, exist_ok=True)
    write_file(src / "Clients" / "new_ledger.txt", b"y" * 50)
    dest = sandbox / "nas" / "FY26-26"
    write_file(dest / "Clients" / "ledger.txt", b"x" * 100)

    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    res = run_lan_sync(str(src), str(dest), cfg.lan)
    err = (res.get("error") or "")[:1200]
    survivors = len([p for p in dest.rglob("*") if p.is_file()])

    L.record("D1.robocopy_exit16", "robocopy refuses to mirror a dirs-only source (exit 16?)",
             f"status={res['status']} exit={res['exit_code']} survivors={survivors}\n--- log ---\n{err}",
             "LAN leg FAILS CLOSED (exit 16 = fatal, nothing copied/deleted)" if res["exit_code"] == 16
             and survivors else "see evidence")
    print(err)
    # A1: a /ZB privilege abort must fail CLOSED - never silently succeed, and
    # never delete a destination file it could not have mirrored. This is the
    # C-2 deployment blocker, asserted so it cannot regress into a partial purge.
    assert res["status"] in ("LAN_FAILED", "LAN_SUSPECT"), res
    assert res["exit_code"] == 16, (
        "expected the /ZB privilege abort (exit 16); got "
        f"{res['exit_code']} - if SeBackupPrivilege has since been granted, C-2 "
        "is resolved and this test should assert the successful-copy path instead")
    assert survivors == 1, f"/ZB abort deleted destination files: {survivors} left of 1"


def test_w1a_d2_partial_readability_both_legs(sandbox):
    """A (readable) + B (denied). Destiny mirrors both. Then mirror again.

    This is the realistic case: the service account loses read on one subtree.
    """
    import os as _os

    from core.cloud_sync import run_cloud_sync
    from core.lan_sync import run_lan_sync
    from audit_lab.labconf import write_local_rclone_config

    src = sandbox / "source" / "FY26-26"
    write_file(src / "A" / "a1.txt", b"A1\n" * 10)
    write_file(src / "A" / "a2.txt", b"A2\n" * 10)
    denied = src / "B"
    for i in range(5):
        write_file(denied / f"b{i}.txt", f"B{i}\n".encode() * 10)

    # make B unreadable for the current user
    applied, detail = _deny_read(denied)
    readable_after = _can_read(denied)
    L.record("D2.partial.setup", "deny-read applied to source subtree B",
             f"{detail} | iterdir(B) readable={readable_after}",
             "ACL DENY WORKS (unprivileged, own dir)" if applied and not readable_after
             else "ACL DENY INEFFECTIVE — using alternative fault")
    if not (applied and not readable_after):
        pytest.skip("cannot simulate unreadable subtree in this environment")

    # LAN destination mirrors A + B
    dest = sandbox / "nas" / "FY26-26"
    write_file(dest / "A" / "a1.txt", b"A1\n" * 10)
    write_file(dest / "A" / "a2.txt", b"A2\n" * 10)
    for i in range(5):
        write_file(dest / "B" / f"b{i}.txt", f"B{i}\n".encode() * 10)

    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    lan = run_lan_sync(str(src), str(dest), cfg.lan)
    b_survivors = len([p for p in (dest / "B").rglob("*") if p.is_file()]) if (dest / "B").exists() else 0
    a_files = len([p for p in (dest / "A").rglob("*") if p.is_file()])
    L.record("D2.partial.lan",
             "robocopy /MIR against a partially readable source purges the unreadable subtree",
             f"status={lan['status']} exit={lan['exit_code']} files_failed={lan.get('files_failed')} | "
             f"A survivors={a_files}/2  B survivors={b_survivors}/5 | counts={lan.get('counts')}",
             f"LAN: {'PURGED B' if b_survivors == 0 else f'B survived ({b_survivors}/5)'} with status={lan['status']}")

    # ── cloud leg: seed the fake bucket with A + B, then sync with B denied ──
    rc_path = write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc_path)
    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    cwd = _os.getcwd()
    _os.chdir(bucket_root)
    try:
        fy = bucket_root / "lab-bucket" / "FY26-26"
        write_file(fy / "A" / "a1.txt", b"A1\n" * 10)
        write_file(fy / "A" / "a2.txt", b"A2\n" * 10)
        for i in range(5):
            write_file(fy / "B" / f"b{i}.txt", f"B{i}\n".encode() * 10)

        cloud = run_cloud_sync(
            source=str(src), bucket="lab-bucket", fy_prefix="FY26-26",
            gcs_key_path=str(sandbox / "lab-key.json"), project_number="000000000000",
            storage_class="STANDARD", location="us-central1", bwlimit="1000M",
            retries=1, transfers=2, checkers=2, buffer_size="16M",
            timeout=120, max_duration_seconds=0,
        )
        b_left = len([p for p in (fy / "B").rglob("*") if p.is_file()]) if (fy / "B").exists() else 0
        a_left = len([p for p in (fy / "A").rglob("*") if p.is_file()])
        L.record("D2.partial.cloud",
                 "rclone sync against a partially readable source deletes the unreadable objects",
                 f"status={cloud['status']} exit={cloud['exit_code']} error={(cloud.get('error') or '')[:300]} | "
                 f"A left={a_left}/2  B left={b_left}/5",
                 f"CLOUD: {'DELETED B objects' if b_left == 0 else f'B survived ({b_left}/5)'} "
                 f"and reported status={cloud['status']}")
        print(f"[D2] LAN: B survivors {b_survivors}/5 status={lan['status']} | "
              f"CLOUD: B left {b_left}/5 status={cloud['status']} exit={cloud['exit_code']}")
        # A2: the D2 hypothesis is REFUTED on BOTH legs - an unreadable subtree is
        # never purged from the destination, and neither leg reports a clean
        # success while it cannot read the source. Assert all four halves.
        assert b_survivors == 5, f"LAN purged the unreadable subtree: {b_survivors}/5 survived"
        assert a_files == 2, f"readable subtree lost files: {a_files}/2"
        assert lan["status"] != "LAN_COMPLETE", (
            f"LAN reported {lan['status']} while a subtree was unreadable")
        assert b_left == 5, f"cloud purged the unreadable subtree: {b_left}/5 survived"
        assert a_left == 2, f"cloud lost readable files: {a_left}/2"
        assert cloud["status"] != "CLOUD_COMPLETE", (
            f"cloud reported {cloud['status']} while a subtree was unreadable")
    finally:
        _os.chdir(cwd)
        for (mod, attr), orig in saved.items():
            setattr(mod, attr, orig)
