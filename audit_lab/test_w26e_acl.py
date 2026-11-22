"""WAVE 26e - release data card L7 (docs/TEST_DESIGN_RELEASE_2026-11-22.md section B; plan
docs/PLAN_E2E_SIMULATION_2026-11-23.md section 1c).

L7: an ACL-denied subfolder with `lan.use_backup_mode=false` (no SeBackupPrivilege, the lab account).
    `icacls <src>\\Audit /deny %USERNAME%:(OI)(CI)R` is set before both legs run. Expect: LAN PARTIAL/FAILED,
    cloud not COMPLETE, the files outside Audit copied. Cleanup in finally:
    `icacls <src>\\Audit /remove:d %USERNAME%`.

Runs through the PRODUCTION flow (flow.backup(mode="all")) on the WAVE 6 rig, real robocopy and real rclone.

Run (lab rules, CLAUDE.md "Commands" and "Environment gotchas"):
    PREFECT_HOME=<empty dir> .venv\\Scripts\\python.exe -m pytest audit_lab/test_w26e_acl.py -q -s -p no:cacheprovider -o addopts=""
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from audit_lab import labconf as L
from audit_lab.test_w6_flow import _db_rows, rig  # noqa: F401 - the production-wired rig fixture

pytestmark = pytest.mark.timeout(1200)

LAN_NOT_OK = ("LAN_PARTIAL", "LAN_FAILED")                    # card L7
COMPLETE = ("LAN_COMPLETE", "LAN_NO_CHANGES_COMPLETE", "CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_w26e_L7_acl_denied_subfolder_without_backup_mode(rig):
    flow = rig["flow"]
    src, nas = rig["src"], rig["nas"]
    assert rig["cfg"].lan.use_backup_mode is False, "card L7 needs lan.use_backup_mode=false"
    user = os.environ["USERNAME"]
    audit = src / "Audit"
    for i in range(5):
        L.write_file(audit / f"audit_{i}.txt", b"AUDIT FOLDER " * 200)
    outside = sorted((src / "Clients").glob("*.txt"))
    assert outside, "the rig must provide files outside Audit"

    denied = L.run(["icacls", str(audit), "/deny", f"{user}:(OI)(CI)R"])
    cleanup = None
    try:
        assert denied.returncode == 0, f"icacls deny failed: {denied.stdout}{denied.stderr}"
        # precondition: the deny really blocks the lab account from reading the subfolder
        with pytest.raises(PermissionError):
            (audit / "audit_0.txt").read_bytes()

        try:
            flow.backup(config_path=str(rig["cfg_path"]), mode="all")
        except Exception:  # noqa: BLE001 - a non-COMPLETE run raises by design
            pass
    finally:
        cleanup = L.run(["icacls", str(audit), "/remove:d", user])
    assert cleanup.returncode == 0, f"icacls cleanup failed: {cleanup.stdout}{cleanup.stderr}"

    rows, _lan, _cloud = _db_rows(rig["cfg_path"])
    latest = {}
    for r in rows:                      # newest first: keep the first row per leg
        latest.setdefault(r["mode"], r["status"])
    assert "lan" in latest, "the LAN leg wrote no run_history row"
    assert latest["lan"] in LAN_NOT_OK, latest
    assert latest.get("cloud") not in COMPLETE, latest

    # files outside Audit are copied to the NAS, byte for byte
    missing, different = [], []
    for p in outside:
        twin = nas / "Clients" / p.name
        if not twin.is_file():
            missing.append(p.name)
        elif _sha256(twin) != _sha256(p):
            different.append(p.name)
    assert not missing and not different, f"outside-Audit files: missing={missing[:4]} different={different[:4]}"

    L.record("W26e.L7_acl_denied_subfolder",
             "an ACL-denied subfolder with use_backup_mode=false makes the LAN leg PARTIAL/FAILED, the cloud "
             "leg not COMPLETE, and the files outside the denied folder are still copied",
             f"lan={latest['lan']} | cloud={latest.get('cloud')} | outside files={len(outside)} "
             f"copied={len(outside) - len(missing) - len(different)} | icacls cleanup rc={cleanup.returncode}",
             "OK — LAN not COMPLETE, cloud not COMPLETE, outside-Audit files copied")
