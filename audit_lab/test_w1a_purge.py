"""WAVE 1A — the two data-loss hypotheses.

D1: does a source containing only EMPTY SUBDIRECTORIES pass the empty-source
    guard, and does the mirror then PURGE the whole destination?
D2: does an UNREADABLE source subtree cause the mirror to purge that subtree
    at the destination (both robocopy /MIR and rclone sync)?
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from audit_lab import labconf as L
from audit_lab.labconf import build_config, write_file

pytestmark = pytest.mark.timeout(300)


# ── D1 ─────────────────────────────────────────────────────────────────────────
def test_w1a_d1_empty_subdirs_source_passes_guard_and_purges_lan(sandbox):
    """Source = only empty subdirectories (zero files at any depth).

    Hypothesis: check_source_drive() returns OK (it only tests the top level),
    and robocopy /MIR then deletes every file at the destination.
    """
    from core.health import check_source_drive
    from core.lan_sync import build_robocopy_command, run_lan_sync

    src = sandbox / "source" / "FY26-26"
    for d in ("Clients", "Clients/2026", "Registers", "Reports"):
        (src / d).mkdir(parents=True, exist_ok=True)
    assert not any(p.is_file() for p in src.rglob("*")), "source must contain zero files"

    # 1. destination holds a healthy backup
    dest = sandbox / "nas" / "FY26-26"
    for i in range(12):
        write_file(dest / "Clients" / f"ledger_{i}.txt", f"LEDGER {i}\n".encode() * 40)
    dest_files_before = [p for p in dest.rglob("*") if p.is_file()]
    assert len(dest_files_before) == 12

    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")

    # 2. THE GUARD
    ok, reason = check_source_drive(cfg.paths.source_drive)
    L.record("D1.guard", "empty-subdir source passes the fail-closed guard",
             f"check_source_drive({cfg.paths.source_drive!r}) -> ok={ok}, reason={reason!r}",
             "GUARD PASSED (fail-open)" if ok else "GUARD BLOCKED (fail-closed)")
    assert ok is True, f"guard unexpectedly blocked: {reason}"

    # 3. command builds and the real sync runs
    cmd = build_robocopy_command(str(src), str(dest), cfg.lan)
    assert "robocopy" in cmd[0].lower()

    res = run_lan_sync(str(src), str(dest), cfg.lan)
    dest_files_after = [p for p in dest.rglob("*") if p.is_file()]
    # D1 is proven in the no-/ZB control (test_w1c_controls.py::..._c2_...).
    # With the production command (/ZB), robocopy dies with exit 16 first, so
    # here we RECORD what happened instead of asserting the wipe.
    if res["exit_code"] == 16:
        L.record("D1.lan_purge",
                 "robocopy /MIR against a dirs-only source wipes the LAN destination",
                 f"robocopy exit={res['exit_code']} (SeBackupPrivilege absent -> /ZB aborts the run "
                 f"before any copy/delete, see experiment C1) | dest files before=12 after="
                 f"{len(dest_files_after)}",
                 "MASKED BY /ZB — the purge never happens because robocopy exits 16 first; "
                 "see C2 for the same scenario without /ZB (12 -> 0 files, LAN_COMPLETE)")
        pytest.skip("robocopy /ZB privilege failure masks the purge (proved separately in C2)")
    L.record(
        "D1.lan_purge",
        "mirror run against an empty (dirs-only) source wipes the destination",
        f"robocopy exit={res['exit_code']} status={res['status']} | "
        f"dest files before={len(dest_files_before)} after={len(dest_files_after)} | "
        f"counts={res.get('counts')}",
        "DATA LOSS CONFIRMED — destination purged" if not dest_files_after
        else "no loss (files survived)",
    )
    assert len(dest_files_after) == 0, "expected the destination to be purged"
    assert res["status"] == "LAN_COMPLETE"      # <- and it reports success


def test_w1a_d1_empty_subdirs_source_purges_fake_bucket(sandbox):
    """Same shape against the cloud leg: does rclone sync delete the objects?"""
    import os

    from core.cloud_sync import build_rclone_sync_command, run_cloud_sync
    from audit_lab.labconf import write_local_rclone_config

    src = sandbox / "source" / "FY26-26"
    for d in ("Clients", "Registers"):
        (src / d).mkdir(parents=True, exist_ok=True)

    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    fy = bucket_root / "lab-bucket" / "FY26-26"
    before = []
    for i in range(12):
        p = write_file(fy / "Clients" / f"ledger_{i}.txt", f"LEDGER {i}\n".encode() * 40)
        before.append(p.name)
    assert len(list(fy.rglob("*.txt"))) == 12

    rc_path = write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc_path)
    cwd = os.getcwd()
    os.chdir(bucket_root)
    try:
        cmd = build_rclone_sync_command(str(src), "lab-bucket", "FY26-26", rc_path,
                                        "STANDARD", bwlimit="1000M", retries=1,
                                        transfers=2, checkers=2, buffer_size="16M",
                                        max_duration_seconds=0)
        assert cmd[:3][2].endswith("FY26-26")

        res = run_cloud_sync(
            source=str(src), bucket="lab-bucket", fy_prefix="FY26-26",
            gcs_key_path=str(sandbox / "lab-key.json"), project_number="000000000000",
            storage_class="STANDARD", location="us-central1", bwlimit="1000M",
            retries=1, transfers=2, checkers=2, buffer_size="16M",
            timeout=120, max_duration_seconds=0,
        )
        after = [p.name for p in fy.rglob("*.txt")]
        L.record(
            "D1.cloud_purge",
            "cloud mirror against an empty (dirs-only) source deletes the bucket objects",
            f"rclone exit={res['exit_code']} status={res['status']} | "
            f"objects before={len(before)} after={len(after)}",
            "DATA LOSS CONFIRMED — objects deleted, reported " + res["status"]
            if not after else "no loss (objects survived)",
        )
        assert after == [], "expected rclone sync to delete the objects (mirror semantics)"
    finally:
        os.chdir(cwd)
        for (mod, attr), orig in saved.items():
            setattr(mod, attr, orig)


# ── D2 ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", ["acl_deny", "maxpath"])
def test_w1a_d2_unreadable_subtree_purges(mode, sandbox, tmp_path):
    """An unreadable source subtree: is it purged from the destination?"""
    from core.lan_sync import decide_lan_result, run_lan_sync
    from audit_lab.labconf import run as _run

    src = sandbox / "source" / "FY26-26"
    dest = sandbox / "nas" / "FY26-26"
    write_file(src / "A" / "a1.txt", b"A1\n" * 10)
    write_file(src / "A" / "a2.txt", b"A2\n" * 10)

    blocked = None
    if mode == "acl_deny":
        blocked = src / "B"
        for i in range(5):
            write_file(blocked / f"b{i}.txt", f"B{i}\n".encode() * 10)
        r = _run(["icacls", str(blocked), "/deny",
                  f"{__import__('os').environ.get('USERNAME')}:".rstrip(':') + "(RD)"],
                 timeout=60)
        applied = r.returncode == 0
        L.record(f"D2[{mode}].setup", "deny read on one source subtree via icacls",
                 f"icacls rc={r.returncode} out={r.stdout.strip()[:200]} err={r.stderr.strip()[:200]}",
                 "ACL deny applied" if applied else "ACL deny NOT applied (env limit)")
        if not applied:
            pytest.skip("could not apply deny ACL in this environment")
    else:
        # path longer than MAX_PATH -> subtree unreadable/unlistable
        deep = src / "LONG"
        parts = ["seg%02d" % i for i in range(24)]  # ~ 150 + 12*24 chars
        blocked = deep.joinpath(*parts)
        for i in range(5):
            write_file(blocked / f"l{i}.txt", f"L{i}\n".encode() * 10)
        L.record(f"D2[{mode}].setup", "source subtree nested past MAX_PATH",
                 f"deepest path length={len(str(blocked / 'l0.txt'))}", "subtree created")

    # destination currently mirrors everything (A + blocked subtree)
    if mode == "acl_deny":
        for i in range(5):
            write_file(dest / "B" / f"b{i}.txt", f"B{i}\n".encode() * 10)
    else:
        for i in range(5):
            write_file(dest / "LONG" / "x" / f"l{i}.txt", f"L{i}\n".encode() * 10)
    for i in range(2):
        write_file(dest / "A" / f"a{i}.txt", (b"A1\n" if i == 0 else b"A2\n") * 10)

    before = len([p for p in dest.rglob("*") if p.is_file()])

    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    res = run_lan_sync(str(src), str(dest), cfg.lan)

    blocked_dest = dest / ("B" if mode == "acl_deny" else "LONG")
    survived = [p.name for p in blocked_dest.rglob("*") if p.is_file()] if blocked_dest.exists() else []
    after = len([p for p in dest.rglob("*") if p.is_file()])
    decision = decide_lan_result(res["exit_code"], (res.get("error") or "")) if res.get("error") else None

    L.record(
        f"D2[{mode}].result",
        "unreadable source subtree is purged from the destination",
        f"robocopy exit={res['exit_code']} status={res['status']} files_failed={res.get('files_failed')} | "
        f"dest files before={before} after={after} | unreadable-subtree survivors={len(survived)} | "
        f"decision={decision['status'] if decision else None}",
        ("PURGE CONFIRMED — " + str(len(survived)) + " survivor(s) of 5, status=" + res["status"])
        if len(survived) < 5 else
        ("NO PURGE — subtree survived intact, robocopy reported status=" + res["status"]),
    )
    # A3: the D2 hypothesis is REFUTED on the LAN leg — an unreadable subtree is
    # NEVER purged from the destination, and the run never reports a clean
    # success. Assert both halves: 5/5 survivors, and no success-looking status.
    # (If a future refactor makes /MIR delete what it cannot read, this fails.)
    assert len(survived) == 5, (
        f"D2 PROTECTION BROKEN: only {len(survived)}/5 files of the unreadable "
        f"subtree survived the mirror (mode={mode}, exit={res['exit_code']})")
    assert res["status"] != "LAN_COMPLETE", (
        f"D2 PROTECTION BROKEN: unreadable subtree mirrored as {res['status']} "
        f"instead of failing loudly")
    print(f"[D2/{mode}] survivors={len(survived)} status={res['status']} exit={res['exit_code']}")
