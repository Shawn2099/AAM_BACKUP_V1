"""WAVE 1A — the two data-loss hypotheses.

D1: does a source containing only EMPTY SUBDIRECTORIES pass the empty-source
    guard, and does the mirror then PURGE the whole destination?
    (Confirmed pre-M2 as finding C-1; closed in M2 2.2 by guard G1 - these
    tests now assert the destination stays byte-identical.)
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
# C-1 was CONFIRMED here before M2 (12 -> 0 files, reported LAN_COMPLETE, with
# /ZB removed). Closed in M2 2.2: guard G1 + the command-builder backstop.
# These are now regression tests: the destination must stay byte-identical.

def _tree_bytes(root):
    return sorted((str(p.relative_to(root)), p.read_bytes())
                  for p in root.rglob("*") if p.is_file())


def test_w1a_d1_empty_subdirs_source_is_blocked_lan(sandbox, monkeypatch):
    """Source = only empty subdirectories. The REAL LAN pipeline (real robocopy
    /L, real destination walk, real DB) must stop as LAN_BLOCKED by G1 before
    any transfer, with the destination byte-identical."""
    import time

    import flow
    from core import guards
    from core.lan_sync import build_robocopy_command
    from core.manifest import ManifestDB
    from core.time_utils import now_iso

    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    src = sandbox / "source" / "FY26-26"
    for d in ("Clients", "Clients/2026", "Registers", "Reports"):
        (src / d).mkdir(parents=True, exist_ok=True)
    assert not any(p.is_file() for p in src.rglob("*")), "source must contain zero files"

    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    for i in range(12):
        write_file(dest / "Clients" / f"ledger_{i}.txt", f"LEDGER {i}\n".encode() * 40)
    before = _tree_bytes(dest)

    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime",
                       lan_enabled=True, cloud_enabled=False)

    with pytest.raises(guards.GuardBlocked) as ei:
        flow._run_lan_pipeline(cfg, "w1a-d1-lan", now_iso(), time.monotonic())
    blocked = [r["guard"] for r in ei.value.record["results"] if r["verdict"] == guards.BLOCK]

    # backstop: even a direct caller cannot build the mirror command
    with pytest.raises(ValueError, match="contains no files"):
        build_robocopy_command(str(src), str(dest), cfg.lan)

    db = ManifestDB.from_config(cfg)
    try:
        row = db.last_run("lan")
    finally:
        db.close()
    after = _tree_bytes(dest)
    L.record("D1.lan_purge",
             "dirs-only source: LAN pipeline blocks before any transfer (C-1 closed)",
             f"blocked guards={blocked} | row={row['status'] if row else None} | "
             f"dest files before={len(before)} after={len(after)}",
             "LAN leg protected (byte-identical)" if after == before else "DATA CHANGED")
    assert "G1_SOURCE_HAS_FILES" in blocked
    assert row and row["status"] == "LAN_BLOCKED"
    assert after == before, "destination must be byte-identical"


def test_w1a_d1_empty_subdirs_source_is_refused_cloud(sandbox):
    """Same shape against the cloud leg: rclone sync must never start."""
    import os

    from core.cloud_sync import build_rclone_sync_command, run_cloud_sync
    from audit_lab.labconf import write_local_rclone_config

    src = sandbox / "source" / "FY26-26"
    for d in ("Clients", "Registers"):
        (src / d).mkdir(parents=True, exist_ok=True)

    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    fy = bucket_root / "lab-bucket" / "FY26-26"
    for i in range(12):
        write_file(fy / "Clients" / f"ledger_{i}.txt", f"LEDGER {i}\n".encode() * 40)
    before = _tree_bytes(fy)

    rc_path = write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc_path)
    cwd = os.getcwd()
    os.chdir(bucket_root)
    try:
        with pytest.raises(ValueError, match="contains no files"):
            build_rclone_sync_command(str(src), "lab-bucket", "FY26-26", rc_path,
                                      "STANDARD", bwlimit="1000M", retries=1,
                                      transfers=2, checkers=2, buffer_size="16M",
                                      max_duration_seconds=0)
        with pytest.raises(ValueError, match="contains no files"):
            run_cloud_sync(
                source=str(src), bucket="lab-bucket", fy_prefix="FY26-26",
                gcs_key_path=str(sandbox / "lab-key.json"), project_number="000000000000",
                storage_class="STANDARD", location="us-central1", bwlimit="1000M",
                retries=1, transfers=2, checkers=2, buffer_size="16M",
                timeout=120, max_duration_seconds=0,
            )
        after = _tree_bytes(fy)
        L.record("D1.cloud_purge",
                 "dirs-only source: rclone sync is never started (C-1 closed)",
                 f"objects before={len(before)} after={len(after)}",
                 "cloud leg protected (byte-identical)" if after == before else "DATA CHANGED")
        assert after == before, "bucket objects must be byte-identical"
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
