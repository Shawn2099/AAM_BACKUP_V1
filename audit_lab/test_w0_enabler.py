"""WAVE 0 — enabler: prove the REAL cloud pipeline runs against a local rclone backend.

If this file passes, every later cloud experiment exercises production code
(run_cloud_sync / verify_cloud_integrity / get_cloud_manifest / get_cloud_diff /
audit_cloud) with real subprocesses, real logs, real exit-code classification —
with zero network and zero GCS cost.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from audit_lab import labconf as L  # noqa: F401  (registers fixtures)

pytestmark = pytest.mark.timeout(180)


def test_w0_cloud_pipeline_runs_on_local_backend(sandbox, monkeypatch):
    from core.cloud_reporter import get_cloud_diff, get_cloud_manifest, get_cloud_size
    from core.cloud_sync import run_cloud_sync
    from core.cloud_verify import verify_cloud_integrity
    from core.integrity import audit_cloud
    from audit_lab.labconf import build_config, write_file, write_local_rclone_config

    src = sandbox / "source" / "FY26-27"
    write_file(src / "Clients" / "ledger_lab.txt", b"AAM ledger content\n" * 100)
    write_file(src / "Registers" / "cash_lab.csv", b"date,amount\n2026-04-01,100\n")

    cfg = build_config(sandbox, source=src, lan_dest=str(sandbox / "nas" / "FY26-27"),
                       runtime=sandbox / "runtime")

    rc_path = write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc_path)
    try:
        # rclone resolves "aam_gcs:<bucket>/<prefix>" relative to CWD because the
        # local backend has no root option -> run from inside the lab sandbox.
        bucket_root = sandbox / "buckets"
        bucket_root.mkdir(parents=True, exist_ok=True)
        cwd = os.getcwd()
        os.chdir(bucket_root)
        try:
            res = run_cloud_sync(
                source=str(src), bucket="lab-bucket", fy_prefix="FY26-27",
                gcs_key_path=str(sandbox / "lab-key.json"),
                project_number="000000000000", storage_class="STANDARD",
                bwlimit="1000M", retries=1, transfers=2, checkers=2,
                buffer_size="16M", timeout=120, max_duration_seconds=0,
            )
            assert res["status"] == "CLOUD_COMPLETE", res
            assert res["exit_code"] == 0, res

            landed = sorted(p.name for p in (bucket_root / "lab-bucket" / "FY26-27").rglob("*")
                            if p.is_file())
            assert landed == ["cash_lab.csv", "ledger_lab.txt"], landed

            # verify (size-only, exactly like the daily gate)
            v = verify_cloud_integrity(source=str(src), bucket="lab-bucket",
                                       fy_prefix="FY26-27", config_path=rc_path,
                                       timeout=120)
            assert v["verified"] is True, v
            assert v["termination"] == "normal"

            manifest = get_cloud_manifest("lab-bucket", "FY26-27", rc_path, timeout=60)
            assert sorted(f["Path"] for f in manifest) == [
                "Clients/ledger_lab.txt", "Registers/cash_lab.csv"], manifest

            size = get_cloud_size("lab-bucket", "FY26-27", rc_path, timeout=30)
            assert size["count"] == 2, size

            diff = get_cloud_diff(str(src), "lab-bucket", "FY26-27", rc_path, timeout=60)
            assert diff["added"] == [] and diff["removed"] == [] and diff["modified"] == [], diff
            assert len(diff["unchanged"]) == 2, diff

            # weekly read-only audit against the same local backend
            audit = audit_cloud(str(src), "lab-bucket", "FY26-27", rc_path, timeout=120)
            assert audit["status"] == "VERIFIED", audit

            # idempotent re-run -> exit 9 -> CLOUD_NO_CHANGES_COMPLETE
            res2 = run_cloud_sync(
                source=str(src), bucket="lab-bucket", fy_prefix="FY26-27",
                gcs_key_path=str(sandbox / "lab-key.json"),
                project_number="000000000000", storage_class="STANDARD",
                bwlimit="1000M", retries=1, transfers=2, checkers=2,
                buffer_size="16M", timeout=120, max_duration_seconds=0,
            )
            assert res2["status"] == "CLOUD_NO_CHANGES_COMPLETE", res2
            assert res2["exit_code"] == 9, res2
        finally:
            os.chdir(cwd)
    finally:
        L.patch_cloud_modules_to_local.__wrapped__ if False else None
        for (mod, attr), orig in saved.items():
            setattr(mod, attr, orig)
