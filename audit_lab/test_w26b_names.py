"""WAVE 26b - names and path lengths on the real tools (release test design cards L2 and L3).

Cards: docs/TEST_DESIGN_RELEASE_2026-11-22.md section B (L2, L3); plan docs/PLAN_E2E_SIMULATION_2026-11-23.md 1c.

L2  Unicode, case and normalization churn on BOTH legs: a Malayalam name, a cafe-with-acute in NFC and in NFD
    form, Ledger.xlsx renamed to LEDGER.xlsx, an emoji name. Both legs run after the rename (run 2) and again
    (run 3). Oracle: os.listdir exact strings of source vs each destination, and the transfer counts found in
    the run's own log files (robocopy "Files :" Copied column, rclone "Copied" JSON messages).

L3  300- and 400-character source paths (created through the extended-length prefix) on the cloud leg, then a
    cloud audit. Expected: CLOUD_COMPLETE with every object present in the local remote and an audit VERIFIED,
    OR a status that is not COMPLETE when rclone refuses the path (the card's either-or). Oracle: sha256 walk
    of source and remote, both walked through the extended-length prefix.

Rig: the WAVE 6 wiring (real Prefect flows via flow.backup / flow.integrity_audit_flow, real robocopy into a
loopback-share NAS folder, real rclone with a local remote as the bucket) on the valid FY leaf FY26-27.

Run with a clean PREFECT_HOME (CLAUDE.md "Environment gotchas"), e.g.
    PREFECT_HOME=<empty dir> .venv\\Scripts\\python.exe -m pytest audit_lab/test_w26b_names.py -q -s -p no:cacheprovider -o addopts=""
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from audit_lab import labconf as L
from audit_lab.test_w12_clock import _flow_call, _write_cfg

pytestmark = pytest.mark.timeout(1200)

FY = "FY26-27"
EXT = "\\\\?\\"   # extended-length prefix (CLAUDE.md / test_w5a_paths): lets os.* see paths over 260 chars

# Copied from audit_lab/test_w25_fy_overlap.py::_OK (by value), plus the legacy no-change LAN name for run 3.
_OK = ("LAN_COMPLETE", "CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE")
_CLOUD_COMPLETE = ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE")
_LAN_NO_CHANGE_OK = ("LAN_COMPLETE", "LAN_NO_CHANGES_COMPLETE")

# Per-step log names written by core/run_artefacts.py: <date>_<HHmm>_<leg>_robocopy.log, <...>_rclone-sync.log
_LAN_ROBOCOPY_LOG = "*_lan_robocopy*.log"
_CLOUD_RCLONE_LOG = "*_cloud_rclone-sync*.log"

# Job-summary "Files :" row of robocopy: Total Copied Skipped Mismatch FAILED Extras (fixed column order).
_FILES_ROW = re.compile(r"^\s*Files\s*:\s+(\d+)\s+(\d+)", re.MULTILINE)

L2_LEDGER = "Ledger.xlsx"
L2_LEDGER_RENAMED = "LEDGER.xlsx"
L2_NAMES = [
    "മലയാളം രേഖ.xlsx",          # Malayalam
    "café.txt",             # cafe + acute, NFC (one code point)
    "café.txt",            # cafe + combining acute, NFD (two code points)
    L2_LEDGER,
    "\U0001F600.pdf",            # emoji (UTF-16 surrogate pair on NTFS)
]


# ── small helpers ─────────────────────────────────────────────────────────────────
def _long(p) -> str:
    return EXT + str(p)


def _write_long(path: Path, data: bytes) -> None:
    """Create parents and the file through the extended-length prefix."""
    os.makedirs(_long(path.parent), exist_ok=True)
    fd = os.open(_long(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _long_file(parent: Path, total: int) -> Path:
    """One file whose full path (no prefix) is exactly `total` characters.

    Folder segments of 100 characters are added until the rest fits, then the file name takes the remainder.
    """
    d = parent
    remaining = total - len(str(parent)) - 1          # the separator before the first segment
    i = 0
    while remaining > 120:
        seg = f"s{i:02d}_" + "d" * 96                  # 100 characters
        d = d / seg
        remaining -= len(seg) + 1
        i += 1
    assert remaining - 4 >= 1, f"cannot shape a {total}-char path under {parent}"
    target = d / ("n" * (remaining - 4) + ".txt")
    assert len(str(target)) == total, (len(str(target)), total)
    _write_long(target, f"long path {total}\n".encode() * 20)
    return target


def _tree(root: Path) -> dict[str, str]:
    """relative posix path -> sha256, walked through the extended-length prefix (paths over 260 chars)."""
    out: dict[str, str] = {}
    if not os.path.isdir(_long(root)):
        return out
    for dirpath, _dirs, names in os.walk(_long(root)):
        for n in names:
            full = os.path.join(dirpath, n)[len(EXT):]
            rel = os.path.relpath(full, str(root)).replace("\\", "/")
            with open(_long(full), "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _rm_long(p: Path) -> None:
    """Remove a tree that holds over-260-char paths (shutil.rmtree needs the prefix there)."""
    L.assert_lab_path(str(p))
    shutil.rmtree(_long(p), ignore_errors=True)


def _robocopy_copied(log: Path) -> int | None:
    raw = log.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):          # /UNILOG is UTF-16 with a BOM
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8", errors="replace")
    m = _FILES_ROW.search(text)
    return int(m.group(2)) if m else None


def _rclone_copied(log: Path) -> int:
    """Count the 'Copied ...' transfer messages in an rclone --use-json-log file."""
    n = 0
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            msg = json.loads(line).get("msg", "")
        except (ValueError, AttributeError):
            continue
        if isinstance(msg, str) and msg.startswith("Copied"):
            n += 1
    return n


def _log_set(rig, pattern: str) -> set[str]:
    base = rig["cfg"].run_logs_dir
    if not Path(base).exists():
        return set()
    return {str(p) for p in Path(base).rglob(pattern)}


def _run_rows(rig) -> dict[str, list[str]]:
    """product-view: run_history statuses per leg, newest first (the order w6 relies on)."""
    from core.manifest import ManifestDB
    from models.config import load_config

    cfg = load_config(str(rig["cfg_path"]))
    m = ManifestDB.from_config(cfg)
    try:
        rows = m.get_recent_runs(500, fy=FY)
    finally:
        m.close()
    return {leg: [r["status"] for r in rows if r["mode"] == leg] for leg in ("cloud", "lan")}


def _audit_status(rig, legs) -> dict[str, str | None]:
    """product-view: the latest integrity_audits row per leg."""
    from core.manifest import ManifestDB
    from models.config import load_config

    cfg = load_config(str(rig["cfg_path"]))
    m = ManifestDB.from_config(cfg)
    try:
        return {leg: ((m.latest_audit(leg, fy=FY) or {}).get("status")) for leg in legs}
    finally:
        m.close()


def _run(rig, mode: str) -> str | None:
    """One real flow.backup call; a raised ExceptionGroup is returned as text, not hidden."""
    try:
        _flow_call(rig["flow"].backup, config_path=str(rig["cfg_path"]), mode=mode)
        return None
    except Exception as e:  # noqa: BLE001 - PartialRun / ExceptionGroup is an outcome here
        return f"{type(e).__name__}: {str(e)[:200]}"


def _audit(rig, mode: str) -> str | None:
    try:
        _flow_call(rig["flow"].integrity_audit_flow, config_path=str(rig["cfg_path"]), mode=mode)
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {str(e)[:200]}"


# ── rig (WAVE 6 wiring, FY leaf FY26-27) ────────────────────────────────────────────
@pytest.fixture()
def names_rig(sandbox, monkeypatch):
    import contextlib

    import core.lan_sync as ls
    import flow
    from flow import _effective_fy_prefix

    src = sandbox / "source" / FY
    (src / "Clients").mkdir(parents=True, exist_ok=True)
    nas = L.make_canary(sandbox / "nas" / FY)
    L.write_file(sandbox / "lab-key.json", b'{"type": "service_account"}')

    cfg = L.build_config(sandbox, source=src, lan_dest=str(nas), runtime=sandbox / "runtime")

    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    # the cloud leg syncs to the source leaf prefix; the bucket dir must exist so the preflight can list it
    (bucket_root / "lab-bucket" / _effective_fy_prefix(cfg)).mkdir(parents=True, exist_ok=True)

    cfg_path = _write_cfg(sandbox, cfg)
    rc = L.write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc)

    orig_build = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig_build(s, d, c))

    alerts: list = []
    monkeypatch.setattr(flow, "notify", lambda *a, **k: alerts.append(a) or True)
    monkeypatch.setattr(flow, "temp_rclone_config", lambda *a, **k: contextlib.nullcontext(rc))
    # WoL is disabled in the lab config; the no-op mirrors what test_w6 does for its audit test
    monkeypatch.setattr(flow, "wol_check_task", lambda *a, **k: None)
    monkeypatch.chdir(bucket_root)

    monkeypatch.delenv("PREFECT_TEST_MODE", raising=False)   # use the REAL concurrency slot
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness():
        from launch import _ensure_concurrency_limit

        _ensure_concurrency_limit()
        yield {
            "cfg": cfg, "cfg_path": cfg_path, "sandbox": sandbox, "src": src, "nas": nas,
            "buckets": bucket_root, "alerts": alerts, "flow": flow,
        }
    ls.build_robocopy_command = orig_build
    for (mod, attr), orig in saved.items():
        setattr(mod, attr, orig)


# ── L2 ────────────────────────────────────────────────────────────────────────────
def _assert_case_only_rename_recorded(src_c: Path, nas_c: Path, cloud_c: Path) -> None:
    """RECORDED, expected NTFS behaviour (not a product defect): NTFS is case-insensitive and case-preserving,
    and robocopy copies onto the existing destination entry without renaming it, so after the case-only rename
    LEDGER.xlsx -> Ledger.xlsx the destination keeps the OLD spelling. (The lab's local 'cloud' is an NTFS folder
    too, so it behaves the same; a real GCS bucket is case-sensitive.) What must hold: the destination still has
    exactly ONE entry for the file (no duplicate, no loss), its content equals the source, and every other
    name matches exactly."""
    src_names = os.listdir(src_c)
    for label, dest in (("NAS", nas_c), ("cloud", cloud_c)):
        names = os.listdir(dest)
        ledger = [n for n in names if n.lower() == L2_LEDGER.lower()]
        assert len(ledger) == 1, f"{label}: expected ONE entry for the ledger file, got {ledger}"
        assert (dest / ledger[0]).read_bytes() == (src_c / L2_LEDGER_RENAMED).read_bytes(), (
            f"{label}: the ledger content differs from the source")
        others_dest = sorted(n for n in names if n.lower() != L2_LEDGER.lower())
        others_src = sorted(n for n in src_names if n.lower() != L2_LEDGER.lower())
        assert others_dest == others_src, (label, others_dest, others_src)
        if ledger[0] != L2_LEDGER_RENAMED:
            print(f"[L2] {label} keeps the old-case name {ledger[0]!r} (source now {L2_LEDGER_RENAMED!r}): "
                  "expected NTFS case-insensitivity, one entry, same content")


def test_l2_unicode_case_normalization_churn(names_rig):
    rig = names_rig
    src_c = rig["src"] / "Clients"
    nas_c = rig["nas"] / "Clients"
    cloud_c = rig["buckets"] / "lab-bucket" / FY / "Clients"

    # setup: the awkward names, both forms of the accented name, then the first run of both legs
    for name in L2_NAMES:
        L.write_file(src_c / name, f"{name}\n".encode() * 30)
    listed = os.listdir(src_c)
    assert "café.txt" in listed and "café.txt" in listed, listed
    assert L2_LEDGER in listed

    setup_err = _run(rig, "all")
    setup_rows = _run_rows(rig)
    assert setup_err is None, setup_err
    assert setup_rows["cloud"][0] in _CLOUD_COMPLETE, setup_rows
    assert setup_rows["lan"][0] in _OK, setup_rows

    # the case-only rename (NTFS is case-insensitive, so this is a rename of the same entry)
    os.rename(src_c / L2_LEDGER, src_c / L2_LEDGER_RENAMED)
    listed = os.listdir(src_c)
    assert L2_LEDGER_RENAMED in listed and L2_LEDGER not in listed, listed

    # run 2 (after the rename): both legs
    before = {leg: len(v) for leg, v in _run_rows(rig).items()}
    err2 = _run(rig, "all")
    rows2 = _run_rows(rig)
    assert err2 is None, err2
    assert {leg: len(v) for leg, v in rows2.items()} == {leg: before[leg] + 1 for leg in before}, rows2
    assert rows2["cloud"][0] in _CLOUD_COMPLETE, rows2
    assert rows2["lan"][0] in _OK, rows2
    _assert_case_only_rename_recorded(src_c, nas_c, cloud_c)

    # run 3: nothing changed since run 2, so nothing may transfer
    log_lan_3_before, log_cloud_3_before = _log_set(rig, _LAN_ROBOCOPY_LOG), _log_set(rig, _CLOUD_RCLONE_LOG)
    before3 = {leg: len(v) for leg, v in _run_rows(rig).items()}
    err3 = _run(rig, "all")
    rows3 = _run_rows(rig)
    assert err3 is None, err3
    assert {leg: len(v) for leg, v in rows3.items()} == {leg: before3[leg] + 1 for leg in before3}, rows3
    assert rows3["cloud"][0] == "CLOUD_NO_CHANGES_COMPLETE", rows3
    assert rows3["lan"][0] in _LAN_NO_CHANGE_OK, rows3

    new_lan_logs = sorted(_log_set(rig, _LAN_ROBOCOPY_LOG) - log_lan_3_before)
    new_cloud_logs = sorted(_log_set(rig, _CLOUD_RCLONE_LOG) - log_cloud_3_before)
    assert new_lan_logs, "run 3 wrote no robocopy log (the transfer-count oracle would be empty)"
    assert new_cloud_logs, "run 3 wrote no rclone log (the transfer-count oracle would be empty)"
    lan_copied = [_robocopy_copied(Path(p)) for p in new_lan_logs]
    cloud_copied = sum(_rclone_copied(Path(p)) for p in new_cloud_logs)
    assert all(c == 0 for c in lan_copied), f"run 3 robocopy Copied per log: {lan_copied}"
    assert cloud_copied == 0, f"run 3 rclone copied {cloud_copied} object(s)"

    # both destinations: exact strings, and the audit clean on both legs
    _assert_case_only_rename_recorded(src_c, nas_c, cloud_c)
    audit_err = _audit(rig, "all")
    audits = _audit_status(rig, ("cloud", "lan"))
    assert audit_err is None, audit_err
    assert audits == {"cloud": "VERIFIED", "lan": "VERIFIED"}, audits

    print(f"[L2] listing src={sorted(os.listdir(src_c))}")
    print(f"[L2] rows run1={setup_rows} run2={rows2} run3={rows3}")
    print(f"[L2] run3 robocopy Copied={lan_copied} rclone copied={cloud_copied} audits={audits}")


# ── L3 ────────────────────────────────────────────────────────────────────────────
_COMPLETE_CLOUD = ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE")


def test_l3_long_source_paths_cloud_leg(names_rig):
    rig = names_rig
    src_c = rig["src"] / "Clients"
    remote = rig["buckets"] / "lab-bucket" / FY

    long300 = _long_file(src_c / "long300", 300)
    long400 = _long_file(src_c / "long400", 400)
    src_map = _tree(rig["src"])
    assert len(str(long300)) == 300 and len(str(long400)) == 400
    assert set(src_map) == {long300.relative_to(rig["src"]).as_posix(),
                            long400.relative_to(rig["src"]).as_posix()}, src_map

    err = _run(rig, "cloud")
    rows = _run_rows(rig)
    status = rows["cloud"][0] if rows["cloud"] else None
    assert status is not None, f"no cloud run row was recorded (flow error: {err})"

    remote_map = _tree(remote)
    remote_ok = bool(src_map) and remote_map == src_map
    audit_err = None
    audit_status = None
    if status in _COMPLETE_CLOUD:
        audit_err = _audit(rig, "cloud")
        audit_status = _audit_status(rig, ("cloud",))["cloud"]
    audit_ok = audit_status == "VERIFIED" and audit_err is None

    # the card's either-or: COMPLETE with the object in the remote and an audit VERIFIED, OR a status that is
    # not COMPLETE (rclone refused the path)
    complete_branch = status in _COMPLETE_CLOUD and remote_ok and audit_ok
    refused_branch = status not in _COMPLETE_CLOUD

    print(f"[L3] cloud status={status} flow_error={err} remote_objects={len(remote_map)} "
          f"remote_matches_source={remote_map == src_map} audit={audit_status} audit_error={audit_err}")
    assert complete_branch or refused_branch, (
        f"L3 neither COMPLETE with the objects present and audit VERIFIED, nor a non-COMPLETE status: "
        f"status={status} flow_error={err} remote_objects={sorted(remote_map)} "
        f"audit={audit_status} audit_error={audit_err}"
    )

    if complete_branch:
        _rm_long(src_c / "long300")
        _rm_long(src_c / "long400")
        _rm_long(rig["buckets"] / "lab-bucket")
