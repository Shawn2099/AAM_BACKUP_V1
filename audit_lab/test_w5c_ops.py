"""WAVE 5c — the failure modes that actually hit this deployment.

c1. the canary is deleted              -> does the FULL pipeline fail AND alert?
c2. a port is listening but isn't SMB  -> what does WoL/preflight report?
c3. the source disappears mid-run      (USB drive / remap / unmount)
c4. a file is locked by a user          (accountants with Excel open)
"""
from __future__ import annotations

import os
import socket
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


def _seed(src: Path, n: int = 300) -> None:
    for i in range(n):
        L.write_file(src / "Clients" / f"f_{i:04d}.txt", b"x" * 256)


# ── c1: canary deleted ─────────────────────────────────────────────────────────
def test_w5c_missing_canary_blocks_then_one_approved_run(sandbox, monkeypatch):
    """End-to-end approval cycle with the REAL robocopy and DB (M2 2.3, plan §6.2):
    missing mount marker -> LAN_BLOCKED (G5), nothing written, ONE approval
    request + ONE attention email; a scheduled rerun stays blocked; the
    approved rerun creates the marker and mirrors; the approval is used up."""
    import core.lan_sync as ls
    import flow
    from core import approvals as ap
    from core import guards
    from core.manifest import ManifestDB
    from core.time_utils import now_iso

    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    src = sandbox / "source" / "FY26-26"
    _seed(src)
    dest = sandbox / "nas" / "FY26-26"
    dest.mkdir(parents=True, exist_ok=True)          # NO canary on purpose

    cfg = L.build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    orig = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig(s, d, c))
    emails = []
    monkeypatch.setattr(flow, "notify", lambda config, event, db=None: emails.append(event))

    def run(run_id, approval_id=None):
        try:
            flow._run_lan_pipeline(cfg, run_id, now_iso(), time.monotonic(),
                                   approval_id=approval_id)
            return None
        except Exception as e:  # noqa: BLE001 - asserted below
            return e

    def files():
        return sorted(p.name for p in dest.rglob("*") if p.is_file())

    try:
        # 1. blocked: nothing written, one request, one ACTION REQUIRED email
        exc1 = run("w5c-1")
        db = ManifestDB.from_config(cfg)
        try:
            row1 = db.last_run("lan")
            [req] = db.open_approvals()
        finally:
            db.close()
        assert isinstance(exc1, guards.GuardBlocked), exc1
        assert row1["status"] == "LAN_BLOCKED"
        assert [g["guard"] for g in req["guards"]] == ["G5_CANARY"]
        assert req["phrase"] == "INITIALISE DESTINATION"
        assert files() == [], "nothing may be written to an unconfirmed destination"
        assert [e.kind for e in emails] == ["ACTION_REQUIRED"]   # one email, no FAILURE alert

        # 2. a scheduled rerun (no approval_id) stays blocked
        assert isinstance(run("w5c-2"), guards.GuardBlocked) and files() == []

        # 3. approve once (as the dashboard does), rerun with the approval
        db = ManifestDB.from_config(cfg)
        try:
            req = db.open_approvals()[-1]
            approved_at = now_iso()
            assert db.approve_request(req["approval_id"], approved_by="lab (api-key)",
                                      approved_at=approved_at,
                                      expires_at=ap.expires_at(approved_at, 12))
        finally:
            db.close()
        exc3 = run("w5c-3", approval_id=req["approval_id"])
        db = ManifestDB.from_config(cfg)
        try:
            row3 = db.last_run("lan")
            used = db.get_approval(req["approval_id"])
        finally:
            db.close()
        assert exc3 is None, exc3
        assert row3["status"] == "LAN_COMPLETE"
        assert used["status"] == ap.USED and used["used_by_run_id"] == "w5c-3"
        assert (dest / ".AAM_TARGET_MOUNTED").is_file()
        assert len(files()) == len([p for p in src.rglob("*") if p.is_file()]) + 1

        # 4. the same approval can never cover another run
        (dest / ".AAM_TARGET_MOUNTED").unlink()
        exc4 = run("w5c-4", approval_id=req["approval_id"])
        assert isinstance(exc4, guards.GuardBlocked) and "already used" in str(exc4)
    finally:
        ls.build_robocopy_command = orig

    L.record("W5c.approval_cycle",
             "missing marker -> BLOCKED; scheduled rerun stays blocked; ONE approved rerun "
             "creates the marker and mirrors; the approval cannot be reused",
             f"rows: {row1['status']} -> {row3['status']} | emails={len(emails)} "
             f"| approval {used['status']}",
             "OK - human-in-the-loop cycle proven end to end with the real robocopy")


# ── c2: a listening port that is not SMB ───────────────────────────────────────
def test_w5c_smb_false_positive(sandbox, monkeypatch):
    """WoL reports the NAS online because *some* port answers. What next?"""
    import core.lan_sync as ls
    import flow
    from core.wol import _smb_port_open, ensure_server_online

    src = sandbox / "source" / "FY26-26"
    _seed(src, 50)
    dest = L.make_canary(sandbox / "nas" / "FY26-26")

    # a decoy TCP listener (e.g. a hung NAS service answering on 445 but not serving SMB)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    decoy_port = srv.getsockname()[1]

    open_real = _smb_port_open("127.0.0.1", port=445)
    open_decoy = _smb_port_open("127.0.0.1", port=decoy_port)

    # a NAS that answers the TCP handshake but is not actually SMB: robocopy will
    # then fail at the canary/access stage -> prove the preflight backstops it
    cfg = L.build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    cfg.wol.enabled = True
    cfg.wol.server_ip = "127.0.0.1"
    cfg.wol.mac_address = "AA:BB:CC:DD:EE:FF"
    cfg.wol.wake_timeout_seconds = 5
    cfg.wol.ping_interval_seconds = 1
    cfg.wol.stability_wait_seconds = 0

    online = ensure_server_online(cfg)     # TCP probe against the real 445 (closed here)
    orig = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig(s, d, c))
    exc = None
    try:
        flow._run_lan_pipeline(cfg, "w5c-smb", "2026-09-07T01:00:00+05:30", time.monotonic())
    except Exception as e:
        exc = e
    finally:
        ls.build_robocopy_command = orig
        srv.close()

    L.record("W5c.smb_false_positive",
             "a TCP handshake on 445 is treated as 'NAS online' by the WoL probe",
             f"_smb_port_open(445)={open_real} | _smb_port_open(decoy port {decoy_port})={open_decoy} | "
             f"ensure_server_online()={online} | pipeline raised={type(exc).__name__}: "
             f"{str(exc)[:100]}",
             "DESIGN NOTE — the WoL/SMB probe is a bare TCP connect, so a hung NAS that accepts "
             "connections reads as 'online'; the canary + robocopy access errors are the real backstop"
             if open_decoy is True else "probe behaved")
    print(f"[W5c-smb] decoy_open={open_decoy} ensure_online={online} raised={type(exc).__name__}")
    # Tier C: a DESIGN NOTE, so assert only the observation that makes it true -
    # a bare TCP handshake on ANY port reads as "NAS online" (L-5). The pipeline
    # outcome is recorded, not asserted: on this box the loopback SMB share really
    # is serving, so the backup correctly succeeds and the probe is never the last
    # line of defence.
    assert open_decoy is True, "the decoy listener was not reachable - the false positive does not apply"
    assert open_real is True, "the real 445 probe did not answer - the rig is not representative"
    L.record("W5c.smb_probe_backstop",
              "what actually stands behind the WoL probe when it is wrong: on this "
              "rig the share is real, so the pipeline succeeds; the canary + robocopy "
              "access errors remain the documented backstop (L-5)",
              f"_smb_port_open(445)={open_real} | _smb_port_open(decoy {decoy_port})={open_decoy} | "
              f"ensure_server_online()={online} | pipeline raised={type(exc).__name__ if exc else None} | "
              f"files at destination={len([p for p in dest.rglob('*') if p.is_file()])}",
              "DESIGN NOTE (L-5) - the probe is a bare TCP connect; the canary and "
              "robocopy's own access errors are what actually protect the destination")


# ── c3: source disappears mid-run ──────────────────────────────────────────────
def test_w5c_source_disappears_mid_run(sandbox, monkeypatch):
    """The source folder is renamed (drive remap/unmount) while robocopy mirrors."""
    import core.lan_sync as ls
    import flow

    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    src = sandbox / "source" / "FY26-26"
    _seed(src, 3000)
    dest = L.make_canary(sandbox / "nas" / "FY26-26")

    cfg = L.build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    orig = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig(s, d, c))

    box: dict = {}

    def _run():
        try:
            box["res"] = ls.run_lan_sync(str(src), str(dest), cfg.lan)
        except Exception as e:
            box["err"] = repr(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    pid, deadline = None, time.time() + 30
    while time.time() < deadline and pid is None:
        pid = _pid_of("Robocopy", str(sandbox))
        time.sleep(0.02)
    time.sleep(0.2)
    vanished = None
    try:                                    # simulate the source becoming unavailable
        vanished = src.rename(sandbox / "source" / "GONE")
    except OSError as e:
        box["rename_err"] = repr(e)
    t.join(timeout=300)
    ls.build_robocopy_command = orig
    if vanished:
        vanished.rename(src)                # restore for inspection

    res = box.get("res", {})
    on_dest = len([p for p in dest.rglob("*.txt")])
    L.record("W5c.source_disappears",
             "the source vanishing mid-run is detected, not silently mirrored as empty",
             f"robocopy exit={res.get('exit_code')} status={res.get('status')} "
             f"error={(res.get('error') or '')[:200]} | destination files after={on_dest}/3000 | "
             f"rename_err={box.get('rename_err')}",
             "OK — the destination kept the previous copies and the reported "
             "status is consistent with how far the mirror got"
             if on_dest > 0 and (res.get("status") != "LAN_COMPLETE" or on_dest == 800)
             else f"ISSUE — status={res.get('status')} on_dest={on_dest}")
    print(f"[W5c-src] status={res.get('status')} exit={res.get('exit_code')} on_dest={on_dest}")
    # A15 - L-5 / SOURCE-LOSS SIGNATURE (asserted, not sanitised). Two things are
    # timing-dependent by construction: the destination file count (it depends on
    # how far the mirror got before the rename landed, and whether the rename
    # itself succeeded at all) and the reported status (LAN_PARTIAL when the
    # fail-closed detection fires, LAN_COMPLETE when the mirror finished before
    # the rename - observed BOTH ways on identical production code, 2026-10-12).
    # The CONTRACT asserted here is only that the destination is never wiped to
    # zero; the status itself is recorded as evidence above, not asserted.
    assert on_dest > 0, (
        f"the source vanished and the destination was purged to {on_dest} files - "
        "that is C-1-class data loss, not the L-5 silent-success signature")


# ── c4: a file locked by a user ────────────────────────────────────────────────
def test_w5c_locked_file(sandbox, monkeypatch):
    """A client's XLSX is open r/w while robocopy mirrors (the classic case)."""
    import core.lan_sync as ls

    src = sandbox / "source" / "FY26-26"
    _seed(src, 200)
    locked = L.write_file(src / "Clients" / "OPEN_BY_USER.xlsx", b"OLD" * 500)
    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    L.write_file(dest / "Clients" / "OPEN_BY_USER.xlsx", b"PREVIOUS BACKUP" * 500)

    # hold the source file open with deny-write, like Excel does
    import msvcrt

    fh = open(locked, "rb")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        pass                        # byte-range locking may not block; share mode is what matters
    cfg = L.build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")
    orig = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig(s, d, c))
    try:
        res = ls.run_lan_sync(str(src), str(dest), cfg.lan)
    finally:
        ls.build_robocopy_command = orig
        fh.close()

    dest_copy = dest / "Clients" / "OPEN_BY_USER.xlsx"
    kept = dest_copy.exists()
    content = dest_copy.read_bytes()[:7] if kept else b""
    L.record("W5c.locked_file",
             "a file locked by a user is not mirrored but not deleted either",
             f"status={res['status']} exit={res['exit_code']} files_failed={res.get('files_failed')} | "
             f"dest copy present={kept} content={content!r} | "
             f"dest files={len([p for p in dest.rglob('*.txt')])}/200",
             "OK — previous copy retained, run reported as a failure that retries next night"
             if kept and content == b"PREVIOUS" else
             "ISSUE — locked file was deleted or replaced at the destination")
    print(f"[W5c-locked] status={res['status']} dest_kept={kept} content={content!r}")
    # A17 - L-1 SIGNATURE (asserted, not sanitised): a file the OS will not let
    # robocopy read was PURGED from the destination instead of retaining the
    # previous good copy (exit 11 -> LAN_PARTIAL). The Excel-held-open case: the
    # firm's last good copy of that workbook is gone for one cycle, and the run
    # only reports LAN_PARTIAL. When L-1 is fixed, `kept` becomes True.
    assert res["status"] in ("LAN_PARTIAL", "LAN_FAILED"), res
    assert kept is False, (
        "L-1 appears FIXED: the locked file's previous destination copy was "
        "retained - update this assertion to expect it")
    assert len([p for p in dest.rglob("*.txt")]) == 200, (
        "the locked file's neighbours were affected - more than L-1's single-file loss")
