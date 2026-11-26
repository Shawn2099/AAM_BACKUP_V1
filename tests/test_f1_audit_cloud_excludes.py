"""F1: audit_cloud passes NTFS_SYSTEM_EXCLUDES just like cloud_sync and cloud_verify."""
from __future__ import annotations

from core import integrity
from core.rclone_errors import NTFS_SYSTEM_EXCLUDES
from test_support.fake_tools import fake_run_tool


def test_f1_audit_cloud_includes_ntfs_excludes(tmp_path):
    with fake_run_tool("core.integrity", exit_code=0, stderr="0 differences found") as calls:
        res = integrity.audit_cloud(
            str(tmp_path), "test-bucket", "FY26-27", "/tmp/rclone.conf",
            timeout=60,
        )
    assert res["status"] == "VERIFIED"
    argv = calls[0]["argv"]
    excludes = [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]
    for pat in NTFS_SYSTEM_EXCLUDES:
        assert pat in excludes
