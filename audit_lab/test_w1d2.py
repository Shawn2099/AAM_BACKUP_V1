"""WAVE 1B-bis / D-bis — corrected experiments.

* B2: cloud copy has same size + same mtime but DIFFERENT content (pure silent
  corruption at the destination) -> daily gate vs weekly audit.
* D2: kill robocopy mid-run with exit code 0, using a command WITHOUT /ZB so the
      run actually starts (the /ZB privilege failure is proven separately in C1).
"""
from __future__ import annotations

import pytest

from audit_lab import labconf as L
from audit_lab.labconf import write_file

pytestmark = pytest.mark.timeout(600)


def test_w1b2_pure_size_only_blind_spot(sandbox):
    import os as _os

    from core.cloud_sync import run_cloud_sync
    from core.cloud_verify import verify_cloud_integrity
    from core.integrity import audit_cloud
    from audit_lab.labconf import write_local_rclone_config

    src = sandbox / "source" / "FY26-26"
    write_file(src / "Clients" / "ledger.txt", b"OPENING BALANCE 100000\n" * 20)

    rc_path = write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc_path)
    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    cwd = _os.getcwd()
    _os.chdir(bucket_root)
    try:
        r = run_cloud_sync(source=str(src), bucket="lab-bucket", fy_prefix="FY26-26",
                           gcs_key_path=str(sandbox / "lab-key.json"), project_number="000000000000",
                           storage_class="STANDARD", bwlimit="1000M", retries=1, transfers=2,
                           checkers=2, buffer_size="16M", timeout=120, max_duration_seconds=0)
        assert r["status"] in ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"), r

        # silently corrupt the CLOUD copy: same size, same mtime, different bytes
        cloud_copy = bucket_root / "lab-bucket" / "FY26-26" / "Clients" / "ledger.txt"
        assert cloud_copy.exists()
        st = cloud_copy.stat()
        write_file(cloud_copy, b"OPENING BALANCE 999999\n" * 20)
        _os.utime(cloud_copy, (st.st_atime, st.st_mtime))

        daily = verify_cloud_integrity(source=str(src), bucket="lab-bucket",
                                       fy_prefix="FY26-26", config_path=rc_path, timeout=120, checkers=4, modify_window_seconds=2)
        weekly = audit_cloud(str(src), "lab-bucket", "FY26-26", rc_path, timeout=120)

        L.record("B2.size_only_blind",
                 "a same-size (+ same-mtime) corrupted cloud copy is caught by the daily gate",
                 f"DAILY  --size-only : verified={daily['verified']} exit={daily['exit_code']} "
                 f"termination={daily['termination']} differences={daily['differences']} reason={daily['reason']}\n"
                 f"WEEKLY  hash       : status={weekly['status']} mismatches={weekly['mismatches']} "
                 f"capability={weekly['capability']} detail={weekly['detail'][:220]}",
                 "BLIND SPOT CONFIRMED — daily gate VERIFIED, weekly audit FAILED"
                 if daily["verified"] and weekly["status"] != "VERIFIED" else "both agreed")
        print(f"[B2] daily verified={daily['verified']} reason={daily['reason']} | weekly={weekly['status']} "
              f"mismatches={weekly['mismatches']}")
        assert daily["verified"] is True
        assert weekly["status"] == "VERIFICATION_FAILED"
    finally:
        _os.chdir(cwd)
        for (mod, attr), orig in saved.items():
            setattr(mod, attr, orig)
