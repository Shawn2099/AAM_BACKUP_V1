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


def test_w1c_c1_zb_privilege_is_a_hard_failure(sandbox):
    """A bog-standard copy WITH /ZB fails if the account lacks SeBackupPrivilege."""
    from core.lan_sync import build_robocopy_command

    src = sandbox / "source" / "FY26-26"
    write_file(src / "a.txt", b"hello\n" * 10)
    dest = sandbox / "nas" / "FY26-26"

    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")

    with_zb = build_robocopy_command(str(src), str(dest), cfg.lan)
    without_zb = flags_without_zb(with_zb)

    r_zb = _run([*with_zb, "/LOG:{}".format(sandbox / "runtime" / "with_zb.log")], timeout=120)
    r_no = _run([*without_zb, "/LOG:{}".format(sandbox / "runtime" / "no_zb.log")], timeout=120)

    L.record("C1./ZB", "robocopy /ZB works for a normal service account",
             f"WITH /ZB    : exit={r_zb.returncode} err={r_zb.stderr.strip()[:160]!r} | "
             f"copied file exists={ (dest / 'a.txt').exists() if False else 'see below' }\n"
             f"WITHOUT /ZB : exit={r_no.returncode} err={r_no.stderr.strip()[:120]!r}",
             "CRITICAL: /ZB is a HARD FAILURE (exit 16, privilege absent) — "
             "every LAN backup would fail before copying anything" if r_zb.returncode == 16
             else "/ZB worked in this account (privilege present)")
    print(f"[C1] with_zb exit={r_zb.returncode}  without_zb exit={r_no.returncode}")
    assert r_no.returncode in (0, 1), r_no.stdout


def test_w1c_c2_dirs_only_source_without_zb_purges(sandbox):
    """With /ZB removed, does an all-empty-dirs source wipe the LAN destination?"""
    from core.lan_sync import build_robocopy_command

    src = sandbox / "source" / "FY26-26"
    for d in ("Clients", "Clients/2026", "Registers"):
        (src / d).mkdir(parents=True, exist_ok=True)
    dest = sandbox / "nas" / "FY26-26"
    for i in range(12):
        write_file(dest / "Clients" / f"ledger_{i}.txt", f"L{i}\n".encode() * 40)
    before = 12

    cfg = build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    cmd = flags_without_zb(build_robocopy_command(str(src), str(dest), cfg.lan))
    cmd.append(f"/LOG:{sandbox / 'runtime' / 'c2.log'}")
    r = _run(cmd, timeout=120)

    after = len([p for p in dest.rglob("*") if p.is_file()])
    L.record("C2.dirs_only_no_zb",
             "all-empty-dirs source with /ZB removed still wipes the LAN destination",
             f"robocopy exit={r.returncode} | dest files before={before} after={after}",
             "LAN DATA LOSS CONFIRMED (purge with no files in source)" if after == 0
             else "LAN leg protected (files survived)")
    print(f"[C2] exit={r.returncode} before={before} after={after}")
    assert after < before, "expected at least some deletion to observe the behaviour"


def test_w1c_c3_partial_source_without_zb_purges(sandbox):
    """With /ZB removed, is an ACL-denied subtree purged from the LAN destination?"""
    from core.lan_sync import build_robocopy_command

    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
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
    cmd = flags_without_zb(build_robocopy_command(str(src), str(dest), cfg.lan))
    cmd.append(f"/LOG:{sandbox / 'runtime' / 'c3.log'}")
    from core.lan_sync import decide_lan_result
    r = _run(cmd, timeout=120)

    b_left = len([p for p in (dest / "B").rglob("*") if p.is_file()])
    log_text = (sandbox / "runtime" / "c3.log").read_text(encoding="utf-8", errors="replace")[-100_000:]
    dec = decide_lan_result(r.returncode, log_text)
    L.record("C3.denied_subtree_no_zb",
             "ACL-denied source subtree is purged from the LAN destination when /ZB is absent",
             f"robocopy exit={r.returncode} decision={dec['status']} | B survivors={b_left}/5",
             "LAN DATA LOSS CONFIRMED (denied subtree purged, status=" + dec["status"] + ")"
             if b_left == 0 else f"LAN protected: B survived ({b_left}/5), status={dec['status']}")
    print(f"[C3] exit={r.returncode} decision={dec['status']} B_left={b_left}")
    # A4: the denied subtree must survive (robocopy refuses to delete what it
    # cannot read) AND the verdict must not be a clean success.
    assert b_left == 5, f"denied subtree purged from the destination: {b_left}/5 survived"
    assert dec["status"] in ("LAN_PARTIAL", "LAN_FAILED"), dec


def _deny_read(path: Path) -> tuple[bool, str]:
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    r = _run(["icacls", str(path), "/deny", f"{user}:(RD)"], timeout=60)
    return r.returncode == 0, r.stdout.strip()[:120]


def test_w1c_c4_denied_file_with_production_command(sandbox):
    """A single denied FILE (production command, /ZB intact) — LAN result?"""
    from core.lan_sync import run_lan_sync

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
    res = run_lan_sync(str(src), str(dest), cfg.lan)
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
