"""WAVE 25 - plan P1 C12: the LAN leg on the REAL rclone (deploy\\bin\\rclone.exe).

Local disk source -> loopback admin share (`\\\\127.0.0.1\\C$`, the `unc_nas` fixture).
The NAS itself is never touched. Rows L1-L11 of docs/PLAN_P1_RCLONE_LAN_2026-11-25.md
(section "L audit lab"). Flow per night: plan (dry-run) -> sync -> arrival check.
"""
from __future__ import annotations

import ctypes
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from audit_lab import labconf as L
from core.integrity import audit_lan
from core.lan_sync import (
    check_lan_arrival,
    check_lan_transfers_hash,
    plan_lan_mirror,
    run_lan_mirror,
    transferred_files,
)
from core.permissions import PROBE_PREFIX
from models.config import LanConfig

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="rclone LAN lab is Windows-only")


# ── helpers ────────────────────────────────────────────────────────────────────
def _cfg(**kw) -> LanConfig:
    """Lab LAN config: fast retries, production defaults otherwise (track_renames on)."""
    base = {"retry_count": 1, "retry_wait_seconds": 1}
    base.update(kw)
    return LanConfig(**base)


def _artefacts(sandbox: Path, name: str) -> str:
    d = sandbox / "runs" / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _plan(cfg, src: Path, dest: str, sandbox: Path, name: str) -> dict:
    return plan_lan_mirror(cfg, str(src), dest, _artefacts(sandbox, name))


def _sync(cfg, src: Path, dest: str, sandbox: Path, name: str, max_delete: int = 100_000) -> dict:
    return run_lan_mirror(cfg, str(src), dest, _artefacts(sandbox, name), max_delete=max_delete)


def _check(cfg, src: Path, dest: str, sandbox: Path, name: str, changed_after: float) -> dict:
    return check_lan_arrival(cfg, str(src), dest, _artefacts(sandbox, name),
                             changed_after_epoch=changed_after)


def _long(p: Path | str) -> str:
    """Extended-length form so Python can create/read paths over 260 characters."""
    return "\\\\?\\" + os.path.abspath(str(p))


def _read_combined(plan: dict) -> str:
    return Path(plan["combined_path"]).read_text(encoding="utf-8", errors="replace")


def _seed_ledgers(src: Path, n: int) -> None:
    for i in range(n):
        L.write_file(src / "Clients" / f"ledger_{i:04d}.txt", f"LEDGER {i}\n".encode() * 20)


def _verdict(exp_id: str, text: str, ok: bool) -> None:
    L.record(exp_id, text, text, "PASS" if ok else "FAIL")


# ── Windows file lock (share mode 0, the Excel-style deny-all open) ────────────
_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _open_share_none(path: Path):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    k32.CreateFileW.restype = ctypes.c_void_p
    handle = k32.CreateFileW(str(path), _GENERIC_READ, 0, None, _OPEN_EXISTING,
                             _FILE_ATTRIBUTE_NORMAL, None)
    if not handle or handle == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _close_handle(handle) -> None:
    ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(handle))


# ── L1 ─────────────────────────────────────────────────────────────────────────
def test_L1_fresh_mirror_unicode_and_empty_folder(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 200)
    unicode_names = ["Ünïcode café.txt", "Résumé_ñ.txt", "日本語 ledger.txt"]
    for n in unicode_names:
        L.write_file(src / "Clients" / n, f"content of {n}\n".encode("utf-8") * 30)
    (src / "emptydir").mkdir(parents=True, exist_ok=True)
    cfg = _cfg()

    plan = _plan(cfg, src, unc, sandbox, "L1-plan")
    assert plan["ok"] is True, plan
    assert plan["planned_copies"] == 203, plan

    t0 = time.time()
    res = _sync(cfg, src, unc, sandbox, "L1-sync")
    assert res["status"] == "LAN_COMPLETE", res
    assert (local_dest / "emptydir").is_dir(), "empty source folder was not created at the NAS"

    arrival = _check(cfg, src, unc, sandbox, "L1-check", changed_after=t0)
    assert arrival["verified"] is True, arrival
    assert arrival["matching"] == 203, arrival

    for rel in [f"Clients/ledger_{i:04d}.txt" for i in range(200)] + [f"Clients/{n}" for n in unicode_names]:
        assert L.md5(src / rel) == L.md5(local_dest / rel), f"md5 differs: {rel}"
    _verdict("L1", "fresh mirror: 200 files + unicode names + empty folder COMPLETE, arrival verified, md5 equal",
             True)


# ── L2 ─────────────────────────────────────────────────────────────────────────
def test_L2_unchanged_rerun_is_no_change_complete(sandbox, unc_nas):
    _, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 30)
    cfg = _cfg()
    assert _sync(cfg, src, unc, sandbox, "L2-first")["status"] == "LAN_COMPLETE"

    res = _sync(cfg, src, unc, sandbox, "L2-rerun")
    assert res["status"] == "LAN_COMPLETE", res
    assert res["reason"] == "no changes", res
    assert res["stats"]["transfers"] == 0 and res["stats"]["deletes"] == 0, res["stats"]
    _verdict("L2", "unchanged rerun COMPLETE 'no changes', transfers 0, deletes 0", True)


# ── L3 ─────────────────────────────────────────────────────────────────────────
def test_L3_locked_file_partial_then_clean_rerun(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 40)
    locked_rel = "Clients/OPEN_BY_USER.xlsx"
    L.write_file(src / locked_rel, b"OLD" * 500)
    cfg = _cfg()
    assert _sync(cfg, src, unc, sandbox, "L3-first")["status"] == "LAN_COMPLETE"

    # the client's workbook changes (size differs, so rclone must read it) and 3 sources vanish
    L.write_file(src / locked_rel, b"NEW-CONTENT" * 900)
    gone = [f"Clients/ledger_{i:04d}.txt" for i in range(3)]
    for rel in gone:
        (src / rel).unlink()

    handle = _open_share_none(src / locked_rel)
    try:
        plan = _plan(cfg, src, unc, sandbox, "L3-plan")
        assert plan["ok"] is True, plan
        assert plan["planned_deletes"] == 3, plan
        res = _sync(cfg, src, unc, sandbox, "L3-locked")
    finally:
        _close_handle(handle)

    assert res["status"] == "LAN_PARTIAL", res
    assert any("OPEN_BY_USER" in p for p in res["failed_paths"]), res["failed_paths"]
    assert res["deletes_held"] is True, res
    for rel in gone:
        assert (local_dest / rel).exists(), f"delete was not held: {rel} is gone from the NAS"
    assert (local_dest / locked_rel).read_bytes() == b"OLD" * 500, "previous copy was altered"

    res2 = _sync(cfg, src, unc, sandbox, "L3-rerun")
    assert res2["status"] == "LAN_COMPLETE", res2
    for rel in gone:
        assert not (local_dest / rel).exists(), f"deletion not applied on the clean rerun: {rel}"
    assert L.md5(src / locked_rel) == L.md5(local_dest / locked_rel)
    _verdict("L3", "locked file: PARTIAL names it, deletes held, 3 files kept; clean rerun COMPLETE and deletes applied",
             True)


# ── L4 ─────────────────────────────────────────────────────────────────────────
def test_L4_folder_rename_is_move_not_delete(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 5)
    photos = src / "photos"
    for i in range(1, 6):
        L.write_file(photos / f"p{i}.jpg", bytes([i]) * 2001)
    cfg = _cfg()
    assert _sync(cfg, src, unc, sandbox, "L4-first")["status"] == "LAN_COMPLETE"

    (src / "photos").rename(src / "photos_renamed")
    plan = _plan(cfg, src, unc, sandbox, "L4-plan")
    assert plan["ok"] is True, plan
    assert plan["moves"] == 5, plan
    assert plan["planned_deletes"] == 0, plan

    res = _sync(cfg, src, unc, sandbox, "L4-sync")
    assert res["status"] == "LAN_COMPLETE", res
    assert res["stats"]["renames"] == 5, res["stats"]
    assert res["stats"]["deletes"] == 0, res["stats"]
    assert (local_dest / "photos_renamed" / "p1.jpg").exists()
    assert not (local_dest / "photos").exists()
    _verdict("L4", "rename of 5-file folder: plan moves 5 deletes 0; sync renames 5 deletes 0", True)


# ── L5 ─────────────────────────────────────────────────────────────────────────
def test_L5_three_deletes_plan_matches_sync(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 20)
    cfg = _cfg()
    assert _sync(cfg, src, unc, sandbox, "L5-first")["status"] == "LAN_COMPLETE"

    gone = [f"Clients/ledger_{i:04d}.txt" for i in (5, 6, 7)]
    for rel in gone:
        (src / rel).unlink()
    plan = _plan(cfg, src, unc, sandbox, "L5-plan")
    assert plan["ok"] is True, plan
    assert plan["planned_deletes"] == 3, plan

    res = _sync(cfg, src, unc, sandbox, "L5-sync")
    assert res["status"] == "LAN_COMPLETE", res
    assert res["stats"]["deletes"] == 3, res["stats"]
    for rel in gone:
        assert not (local_dest / rel).exists(), rel
    _verdict("L5", "3 deletes: plan 3, sync deletes 3 and removes them at the NAS", True)


# ── L6 ─────────────────────────────────────────────────────────────────────────
def test_L6_path_longer_than_260_copied_and_verified(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 3)
    segments = [f"deep_folder_{k}_" + "x" * 45 for k in range(4)]
    rel = Path(*segments) / "deep_ledger_over_260.txt"
    deep_src = src / rel
    assert len(str(deep_src)) > 260, len(str(deep_src))
    os.makedirs(_long(deep_src.parent), exist_ok=True)
    payload = b"LONG PATH CONTENT\n" * 50
    with open(_long(deep_src), "wb") as fh:
        fh.write(payload)
    cfg = _cfg()

    plan = _plan(cfg, src, unc, sandbox, "L6-plan")
    assert plan["ok"] is True, plan
    t0 = time.time()
    res = _sync(cfg, src, unc, sandbox, "L6-sync")
    assert res["status"] == "LAN_COMPLETE", res

    with open(_long(local_dest / rel), "rb") as fh:
        assert fh.read() == payload, "long-path copy content differs"
    arrival = _check(cfg, src, unc, sandbox, "L6-check", changed_after=t0)
    assert arrival["verified"] is True, arrival
    _verdict("L6", f"path of {len(str(deep_src))} chars copied, content equal, arrival verified", True)


# ── L7 ─────────────────────────────────────────────────────────────────────────
def test_L7_max_delete_stops_at_one(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 10)
    cfg = _cfg()
    assert _sync(cfg, src, unc, sandbox, "L7-first")["status"] == "LAN_COMPLETE"

    gone = [f"Clients/ledger_{i:04d}.txt" for i in (0, 1, 2)]
    for rel in gone:
        (src / rel).unlink()
    plan = _plan(cfg, src, unc, sandbox, "L7-plan")
    assert plan["planned_deletes"] == 3, plan

    res = _sync(cfg, src, unc, sandbox, "L7-capped", max_delete=1)
    assert res["exit_code"] == 7, res
    assert res["max_delete_reached"] is True, res
    assert res["status"] == "LAN_FAILED", res
    deleted = sum(1 for rel in gone if not (local_dest / rel).exists())
    assert deleted <= 1, f"{deleted} files deleted with --max-delete 1"
    _verdict("L7", f"--max-delete 1 with 3 pending: exit 7, max_delete_reached, {deleted} deleted", True)


# ── L8 ─────────────────────────────────────────────────────────────────────────
def test_L8_dry_run_facts(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    sizes = {"new_a.bin": 100, "new_b.bin": 2000, "new_c.bin": 33}
    for name, size in sizes.items():
        L.write_file(src / "Clients" / name, b"z" * size)
    (src / "emptydir").mkdir(parents=True, exist_ok=True)
    L.write_file(src / ".AAM_TARGET_MOUNTED", b"m" * 500)
    L.write_file(local_dest / "extra_on_nas.txt", b"only on the NAS")
    cfg = _cfg()

    plan = _plan(cfg, src, unc, sandbox, "L8-plan")
    assert plan["ok"] is True, plan
    assert plan["planned_bytes"] == sum(sizes.values()) == 2133, plan
    assert plan["planned_copies"] == 3, plan
    combined = _read_combined(plan)
    lines = [ln for ln in combined.splitlines() if ln.strip()]
    assert not any("emptydir" in ln for ln in lines), "folder appears in --combined"
    assert not any(".AAM_TARGET_MOUNTED" in ln for ln in lines), "marker appears in --combined"
    assert any(ln.startswith("- ") and "extra_on_nas.txt" in ln for ln in lines), combined
    assert plan["planned_deletes"] == 1, plan
    _verdict("L8", "dry-run: planned bytes = sum of new sizes (2133), no folder or marker lines, NAS-only file shows '-'",
             True)


# ── L9 ─────────────────────────────────────────────────────────────────────────
def test_L9_marker_and_probe_never_copied_or_deleted(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 5)
    probe_name = PROBE_PREFIX + "ab12cd"
    L.write_file(src / ".AAM_TARGET_MOUNTED", b"source-marker")
    L.write_file(src / probe_name, b"source-probe")
    L.write_file(local_dest / ".AAM_TARGET_MOUNTED", b"nas-marker")
    L.write_file(local_dest / probe_name, b"nas-probe")
    cfg = _cfg()

    t0 = time.time()
    plan = _plan(cfg, src, unc, sandbox, "L9-plan")
    assert plan["ok"] is True, plan
    combined = _read_combined(plan)
    assert ".AAM_TARGET_MOUNTED" not in combined and PROBE_PREFIX not in combined, combined

    res = _sync(cfg, src, unc, sandbox, "L9-sync")
    assert res["status"] == "LAN_COMPLETE", res
    assert (local_dest / ".AAM_TARGET_MOUNTED").read_bytes() == b"nas-marker", "NAS marker was touched"
    assert (local_dest / probe_name).read_bytes() == b"nas-probe", "NAS probe was touched"
    arrival = _check(cfg, src, unc, sandbox, "L9-check", changed_after=t0)
    assert arrival["verified"] is True, arrival
    _verdict("L9", "marker and probe: never in the plan, never copied, NAS copies untouched", True)


# ── L10 ────────────────────────────────────────────────────────────────────────
def test_L10_nas_file_removed_behind_rclone_is_unverified(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 5)
    L.write_file(src / "Clients" / "sub" / "deep" / "c.txt", b"c" * 50)
    cfg = _cfg()
    # the source must not count as "changed during the backup": its mtime/ctime has to be older
    # than the arrival window (lan.modify_window_seconds, default 2 s) before t0
    time.sleep(3.5)

    t0 = time.time()
    assert _sync(cfg, src, unc, sandbox, "L10-sync")["status"] == "LAN_COMPLETE"
    (local_dest / "Clients" / "sub" / "deep" / "c.txt").unlink()

    arrival = _check(cfg, src, unc, sandbox, "L10-check", changed_after=t0)
    assert arrival["verified"] is False, arrival
    assert arrival["missing"] == 1, arrival
    assert arrival["reason"], arrival
    _verdict("L10", "file deleted on the NAS behind rclone: arrival not verified, missing 1", True)


# ── L11 ────────────────────────────────────────────────────────────────────────
def test_L11_empty_folder_removed_from_source_is_removed_at_nas(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 5)
    (src / "gone_empty").mkdir(parents=True, exist_ok=True)
    cfg = _cfg()

    assert _sync(cfg, src, unc, sandbox, "L11-first")["status"] == "LAN_COMPLETE"
    assert (local_dest / "gone_empty").is_dir()

    (src / "gone_empty").rmdir()
    res = _sync(cfg, src, unc, sandbox, "L11-sync")
    assert res["status"] == "LAN_COMPLETE", res
    assert not (local_dest / "gone_empty").exists(), "empty folder removed at the source still exists at the NAS"
    _verdict("L11", "empty folder removed from source is removed at the NAS", True)


# ── L12 ────────────────────────────────────────────────────────────────────────
def test_l12_read_only_destination_file_partial_then_clean_rerun(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 10)
    L.write_file(src / "ro.txt", b"OLD-RO" * 100)
    cfg = _cfg()
    assert _sync(cfg, src, unc, sandbox, "L12-first")["status"] == "LAN_COMPLETE"

    nas_ro = local_dest / "ro.txt"
    gone = "Clients/ledger_0000.txt"
    try:
        # the NAS copy becomes read-only (attrib +R) and the source changes and loses one file
        os.chmod(nas_ro, stat.S_IREAD)
        L.write_file(src / "ro.txt", b"NEW-RO-CONTENT" * 300)
        (src / gone).unlink()

        res = _sync(cfg, src, unc, sandbox, "L12-partial")
        assert res["status"] == "LAN_PARTIAL", res
        assert res["exit_code"] == 1, res
        assert res["failed_paths"] == ["ro.txt"], res["failed_paths"]
        assert res["deletes_held"] is True, res
        assert "ro.txt" in (res["reason"] or ""), res["reason"]
        assert (local_dest / gone).exists(), "delete was not held while the read-only copy failed"
        assert nas_ro.read_bytes() == b"OLD-RO" * 100, "read-only NAS copy was altered"
    finally:
        os.chmod(nas_ro, stat.S_IWRITE)  # scratch copy only, so the sandbox can be cleaned up

    res2 = _sync(cfg, src, unc, sandbox, "L12-rerun")
    assert res2["status"] == "LAN_COMPLETE", res2
    assert not (local_dest / gone).exists(), "held delete not applied on the clean rerun"
    assert L.md5(src / "ro.txt") == L.md5(local_dest / "ro.txt"), "ro.txt not updated on the rerun"
    _verdict("L12", "read-only NAS file: PARTIAL names ro.txt, delete held; clean rerun COMPLETE, delete applied",
             True)


# ── L13 ────────────────────────────────────────────────────────────────────────
def _tonight_files(src: Path) -> list[str]:
    """Three files that are NEW on the NAS tonight; written before the sync start (t0)."""
    rels = ["Clients/new_0.bin", " lead space.txt", "#hash_note.txt"]
    for i, rel in enumerate(rels):
        L.write_file(src / rel, bytes([65 + i]) * 700, mtime=time.time() - 120)
    return rels


def test_l13_same_size_corruption_caught_by_hash_not_size(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 5)
    cfg = _cfg()
    assert _sync(cfg, src, unc, sandbox, "L13-first")["status"] == "LAN_COMPLETE"

    rels = _tonight_files(src)
    time.sleep(1.5)  # source ctime (creation) is strictly before t0
    t0 = time.time()
    res = _sync(cfg, src, unc, sandbox, "L13-sync")
    assert res["status"] == "LAN_COMPLETE", res
    copied = transferred_files([Path(res["log_path"]).read_text(encoding="utf-8", errors="replace")])[0]
    assert sorted(copied) == sorted(rels), copied

    # same-size, different bytes on the NAS copy of one file copied tonight
    bad = "Clients/new_0.bin"
    L.write_file(local_dest / bad, b"Z" * 700)
    assert (local_dest / bad).stat().st_size == (src / bad).stat().st_size == 700

    arrival = _check(cfg, src, unc, sandbox, "L13-size", changed_after=t0)
    assert arrival["verified"] is True, f"size-only check should not see same-size corruption: {arrival}"

    hashed = check_lan_transfers_hash(cfg, str(src), unc, _artefacts(sandbox, "L13-hash"),
                                      paths=copied, changed_after_epoch=t0)
    assert hashed["verified"] is False, hashed
    assert hashed["differ"] == 1, hashed
    assert bad in hashed["unverified_paths"], hashed["unverified_paths"]
    _verdict("L13", "same-size corruption: size-only verified, hash check NOT verified, the file is listed",
             True)


# ── L13b ───────────────────────────────────────────────────────────────────────
def test_l13b_clean_night_hash_verified(sandbox, unc_nas):
    _, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 5)
    cfg = _cfg()
    assert _sync(cfg, src, unc, sandbox, "L13b-first")["status"] == "LAN_COMPLETE"

    rels = _tonight_files(src)
    time.sleep(1.5)
    t0 = time.time()
    res = _sync(cfg, src, unc, sandbox, "L13b-sync")
    assert res["status"] == "LAN_COMPLETE", res
    copied = transferred_files([Path(res["log_path"]).read_text(encoding="utf-8", errors="replace")])[0]
    assert sorted(copied) == sorted(rels), copied

    hashed = check_lan_transfers_hash(cfg, str(src), unc, _artefacts(sandbox, "L13b-hash"),
                                      paths=copied, changed_after_epoch=t0)
    assert hashed["verified"] is True, hashed
    assert hashed["matching"] == 3, hashed
    _verdict("L13b", "clean night: hash check verified, matching 3 (leading-space and '#' names passed raw)", True)


# ── L14 ────────────────────────────────────────────────────────────────────────
def test_l14_weekly_audit_ignores_mirror_excludes(sandbox, unc_nas):
    local_dest, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 4)
    L.write_file(src / "$RECYCLE.BIN" / "r.txt", b"recycled" * 10)
    L.write_file(src / "System Volume Information" / "s.txt", b"restore" * 10)
    L.write_file(src / (PROBE_PREFIX + "ab12cd"), b"probe")
    L.write_file(src / ".AAM_TARGET_MOUNTED", b"marker")
    cfg = _cfg()

    res = _sync(cfg, src, unc, sandbox, "L14-sync")
    assert res["status"] == "LAN_COMPLETE", res
    assert not (local_dest / "$RECYCLE.BIN").exists(), "excluded folder was copied to the NAS"

    full = audit_lan(str(src), unc)
    assert full["status"] == "VERIFIED", full
    assert full["missing"] == [] and full["extra"] == [] and full["modified"] == [], full

    sharded = audit_lan(str(src), unc, scope_prefixes=["Clients"])
    assert sharded["status"] == "VERIFIED", sharded
    _verdict("L14", "weekly audit (full and sharded) VERIFIED: recycle bin, SVI, probe and marker not reported",
             True)


# ── RF4-L ──────────────────────────────────────────────────────────────────────
def test_rf4_l_plan_includes_planned_move_bytes(sandbox, unc_nas):
    _, unc = unc_nas
    src = sandbox / "source" / "FY26-27"
    _seed_ledgers(src, 4)
    cfg = _cfg()

    res = _sync(cfg, src, unc, sandbox, "RF4L-seed")
    assert res["status"] == "LAN_COMPLETE", res

    target = next(p for p in (src / "Clients").rglob("*") if p.is_file())
    size = target.stat().st_size
    target.rename(target.with_name(f"renamed_rf4_{target.name}"))

    art = sandbox / "runs" / "RF4L-plan"
    art.mkdir(parents=True, exist_ok=True)
    plan = plan_lan_mirror(cfg, str(src), unc, str(art))
    assert plan["ok"] is True, plan
    assert plan["moves"] >= 1, plan
    assert plan["planned_move_bytes"] == size, plan
    _verdict("RF4-L", f"plan_lan_mirror reported moves={plan['moves']} planned_move_bytes={plan['planned_move_bytes']}",
             True)
