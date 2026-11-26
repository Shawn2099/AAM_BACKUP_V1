"""AF-11: cloud sync, dry-run, and verify share NTFS_SYSTEM_EXCLUDES with LAN."""
from __future__ import annotations

from core import cloud_sync, cloud_verify, lan_sync
from test_support.fake_tools import fake_run_tool


def test_af11_ntfs_system_excludes_shared():
    assert "System Volume Information/**" in lan_sync.NTFS_SYSTEM_EXCLUDES
    assert "$RECYCLE.BIN/**" in lan_sync.NTFS_SYSTEM_EXCLUDES
    for pat in lan_sync.NTFS_SYSTEM_EXCLUDES:
        assert pat in lan_sync.LAN_EXCLUDES


def test_af11_cloud_sync_command_includes_ntfs_excludes(tmp_path):
    src = tmp_path / "FY26-27"
    src.mkdir()
    (src / "a.txt").write_text("x", encoding="utf-8")
    cmd = cloud_sync.build_rclone_sync_command(
        str(src), "test-bucket", "FY26-27", "/tmp/rclone.conf", "STANDARD",
    )
    excludes = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--exclude"]
    for pat in lan_sync.NTFS_SYSTEM_EXCLUDES:
        assert pat in excludes


def test_af11_cloud_measure_planned_deletes_includes_ntfs_excludes(tmp_path):
    with fake_run_tool("core.cloud_sync", exit_code=0, log="") as calls:
        cloud_sync.measure_planned_deletes(str(tmp_path), "aam_gcs:b/FY26-27", config_path="/c")
    argv = calls[0]["argv"]
    excludes = [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]
    for pat in lan_sync.NTFS_SYSTEM_EXCLUDES:
        assert pat in excludes


def test_af11_cloud_verify_includes_ntfs_excludes(tmp_path):
    with fake_run_tool("core.cloud_verify", exit_code=0, stderr="0 differences found") as calls:
        cloud_verify.verify_cloud_integrity(
            str(tmp_path), "test-bucket", "FY26-27", "/tmp/rclone.conf",
            timeout=60, checkers=2, modify_window_seconds=2,
        )
    argv = calls[0]["argv"]
    excludes = [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]
    for pat in lan_sync.NTFS_SYSTEM_EXCLUDES:
        assert pat in excludes


def test_af11_cloud_verify_and_diff_includes_ntfs_excludes(tmp_path):
    with fake_run_tool("core.cloud_verify", exit_code=0, stderr="0 differences found") as calls:
        cloud_verify.verify_cloud_and_diff(
            str(tmp_path), "test-bucket", "FY26-27", "/tmp/rclone.conf",
            timeout=60, checkers=2, modify_window_seconds=2,
        )
    argv = calls[0]["argv"]
    excludes = [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]
    for pat in lan_sync.NTFS_SYSTEM_EXCLUDES:
        assert pat in excludes
