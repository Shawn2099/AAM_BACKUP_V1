"""WAVE 25 - plan P1 C12: the LAN leg on the REAL rclone (deploy\\bin\\rclone.exe).

Local disk source -> loopback admin share (`\\\\127.0.0.1\\C$`, the `unc_nas` fixture).
The NAS itself is never touched. Rows L1-L11 of docs/PLAN_P1_RCLONE_LAN_2026-11-25.md
(section "L audit lab"). Flow per night: plan (dry-run) -> sync -> arrival check.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

import pytest

from audit_lab import labconf as L
from core.lan_sync import check_lan_arrival, plan_lan_mirror, run_lan_mirror
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
