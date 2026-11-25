"""WAVE 1C — control experiments: isolate the effect of /ZB, and prove both
protective/ destructive behaviours with it removed.

C1: trivial, fully-readable copy WITH /ZB vs WITHOUT /ZB      (privilege check?)
C2: dirs-only (zero-file) source WITHOUT /ZB                   (does LAN purge?)
C3: partially readable source WITHOUT /ZB                     (does LAN purge the denied subtree?)
C4: single DENIED FILE (not dir) with the production command  (LAN partial file loss?)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from audit_lab import labconf as L
from audit_lab.labconf import build_config, run as _run, write_file

pytestmark = pytest.mark.timeout(300)


def flags_without_zb(cmd: list[str]) -> list[str]:
    return [a for a in cmd if a.upper() != "/ZB"]


# C1./ZB and C2.dirs_only_no_zb removed in P2 CL4 (robocopy /ZB removed; G1 covered by test_w1a_purge)


def test_w1c_c3_partial_source_rclone_preserves_denied(sandbox):
    """Is an ACL-denied subtree preserved on the LAN destination by rclone?"""
    from core.lan_sync import run_lan_mirror
    from core.run_artefacts import RunArtefacts

    src = sandbox / "source" / "FY26-26"
    write_file(src / "A" / "a1.txt", b"A1\n" * 10)
    denied = src / "B"
    for i in range(5):
        write_file(denied / f"b{i}.txt", f"B{i}\n".encode() * 10)
    applied, _ = _deny_read(denied)
    if not applied:
        pytest.skip("cannot apply deny ACL")

    dest = sandbox / "nas" / "FY26-26"
    write_file(dest / "A" / "a1.txt", b"A1\n" * 10)
    for i in range(5):
        write_file(dest / "B" / f"b{i}.txt", f"B{i}\n".encode() * 10)

    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    art = RunArtefacts.open(str(sandbox / "runtime" / "art_w1c_c3"))
    res = run_lan_mirror(cfg.lan, str(src), str(dest), str(art.dir), max_delete=10000)

    b_left = len([p for p in (dest / "B").rglob("*") if p.is_file()])
    L.record("C3.denied_subtree",
             "ACL-denied source subtree is preserved on the LAN destination",
             f"rclone exit={res['exit_code']} status={res['status']} | B survivors={b_left}/5",
             f"LAN protected: B survived ({b_left}/5), status={res['status']}"
             if b_left == 5 else f"LAN DATA LOSS (B={b_left}/5)")
    print(f"[C3] exit={res['exit_code']} status={res['status']} B_left={b_left}")
    assert b_left == 5, f"denied subtree purged from the destination: {b_left}/5 survived"
    assert res["status"] in ("LAN_PARTIAL", "LAN_FAILED"), res


def _deny_read(path: Path) -> tuple[bool, str]:
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    r = _run(["icacls", str(path), "/deny", f"{user}:(RD)"], timeout=60)
    return r.returncode == 0, r.stdout.strip()[:120]


def test_w1c_c4_denied_file_with_production_command(sandbox):
    """A single denied FILE — LAN result?"""
    from core.lan_sync import run_lan_mirror
    from core.run_artefacts import RunArtefacts

    src = sandbox / "source" / "FY26-26"
    write_file(src / "A" / "a1.txt", b"A1\n" * 10)
    secret = write_file(src / "A" / "locked.txt", b"LOCKED\n" * 10)
    applied, _ = _deny_read(secret)
    if not applied:
        pytest.skip("cannot apply deny ACL")

    dest = sandbox / "nas" / "FY26-26"
    write_file(dest / "A" / "a1.txt", b"A1\n" * 10)
    write_file(dest / "A" / "locked.txt", b"OLD COPY\n" * 10)

    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    art = RunArtefacts.open(str(sandbox / "runtime" / "art_w1c_c4"))
    res = run_lan_mirror(cfg.lan, str(src), str(dest), str(art.dir), max_delete=10000)
    left = (dest / "A" / "locked.txt").exists()
    content = (dest / "A" / "locked.txt").read_bytes()[:20] if left else b""
    L.record("C4.denied_file",
             "a single ACL-denied source file is not mirrored and not deleted (exit 8 path)",
             f"status={res['status']} exit={res['exit_code']} files_failed={res.get('files_failed')} | "
             f"dest locked.txt exists={left} content={content!r}",
             "LAN PARTIAL as designed (old copy retained, exit 8 -> LAN_PARTIAL)" if left
             else "LAN deleted the un-copyable file")
    print(f"[C4] status={res['status']} exit={res['exit_code']} dest_copy_exists={left}")
    # A5: the exit-8 path must RETAIN the previous good copy at the destination.
    # (L-1 is the stricter case where share-none locking + /MIR purges instead;
    #  that is test_w5c_locked_file. Here the ACL-denied single file is retained.)
    assert left, "the previous destination copy was deleted for an un-copyable file"
    assert content.startswith(b"OLD COPY"), content[:40]
    assert res["status"] in ("LAN_PARTIAL", "LAN_FAILED"), res
