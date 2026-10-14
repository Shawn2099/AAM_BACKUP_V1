"""Production scenario tests — simulate real-world conditions.

These tests simulate the actual environments and conditions the
backup system will face in production.
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.cloud_sync import run_cloud_sync
from core.lan_sync import run_lan_sync
from core.cloud_verify import verify_cloud_integrity
from core.integrity import audit_lan, audit_cloud
from core.cloud_reporter import get_cloud_diff, get_cloud_manifest, get_cloud_size


class TestNetworkConditions:
    """Simulate various network conditions."""

    def test_cloud_sync_slow_network(self):
        """Cloud sync with slow network (timeout)."""
        with patch("core.cloud_sync.resolve_binary", return_value="rclone"), \
             patch("core.cloud_sync.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=100)
            result = run_cloud_sync(
                source="E:\\",
                bucket="bucket",
                fy_prefix="FY26-27",
                gcs_key_path="key.json",
                project_number="123",
                storage_class="STANDARD",
                timeout=100,
            )
            assert result["status"] == "CLOUD_FAILED"

    def test_lan_sync_slow_smb(self):
        """LAN sync with slow SMB (timeout)."""
        from models.config import LanConfig
        config = LanConfig(subprocess_timeout_seconds=3600)  # model minimum is 3600
        with patch("core.lan_sync.resolve_binary", return_value="robocopy"), \
             patch("core.lan_sync.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="robocopy", timeout=100)
            result = run_lan_sync("E:\\", "\\\\nas\\share", config)
            assert result["status"] == "LAN_FAILED"


class TestDiskConditions:
    """Simulate various disk conditions."""

    def test_source_drive_full(self):
        """Source drive full → health check fails."""
        from core.health import check_source_drive
        # Use an existing, populated path so the existence/empty gates pass
        # and the disk-usage (space) check is actually reached.
        existing = Path.cwd()
        with patch("core.health.shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(free=0)
            ok, reason = check_source_drive(str(existing), min_free_gb=1)
            assert ok is False
            assert "space" in reason.lower()

    def test_source_drive_disappears_mid_operation(self):
        """Source drive disappears → OSError handled."""
        from core.health import check_source_drive
        with patch("core.health.Path.exists", return_value=False):
            ok, reason = check_source_drive("E:\\")
            assert ok is False


class TestAuthenticationConditions:
    """Simulate authentication failures."""

    def test_gcs_auth_expired_key(self):
        """GCS auth with expired key → verify fails."""
        with patch("core.cloud_verify.resolve_binary", return_value="rclone"), \
             patch("core.cloud_verify.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Signature has expired",
            )
            result = verify_cloud_integrity(
                source="E:\\",
                bucket="bucket",
                fy_prefix="FY26-27",
                config_path="/tmp/config",
            )
            assert result["verified"] is False


class TestConcurrencyConditions:
    """Simulate concurrent operations."""

    def test_multiple_manifest_db_instances(self, tmp_path):
        """Multiple ManifestDB instances for same path."""
        from core.manifest import ManifestDB
        db1 = ManifestDB(str(tmp_path / "test.db"))
        db2 = ManifestDB(str(tmp_path / "test.db"))
        # Both should work without crashing (insert_run is dict-based).
        db1.insert_run({"run_id": "r1", "mode": "cloud", "status": "CLOUD_COMPLETE",
                        "started_at": "2026-01-01T00:00:00"})
        db2.insert_run({"run_id": "r2", "mode": "lan", "status": "LAN_COMPLETE",
                        "started_at": "2026-01-01T00:00:00"})
        assert db1.last_run("cloud")["status"] == "CLOUD_COMPLETE"
        assert db2.last_run("lan")["status"] == "LAN_COMPLETE"
        db1.close()
        db2.close()

    def test_lock_file_contention(self, tmp_path):
        """Two processes competing for same lock."""
        from core.process import acquire_lock
        lock_path = tmp_path / "backup.lock"
        # First acquire (Path, per the contract) → True
        result1 = acquire_lock(lock_path)
        assert result1 is True
        # Second acquire with the live lock still present → False
        result2 = acquire_lock(lock_path)
        assert result2 is False
        lock_path.unlink(missing_ok=True)


class TestDataIntegrityConditions:
    """Simulate data integrity scenarios."""

    def test_same_size_corruption(self):
        """File with same size but different content must be detected.

        Exercises the REAL audit entry point (audit_lan → _run_rclone_check)
        with a faithful rclone simulation: a --combined diff carrying the
        `*` (modified) marker plus rclone's own "1 differences found"
        completion summary. The verdict must fail closed with 1 mismatch.
        """
        import os as _os
        import contextlib
        import tempfile as _tf

        from core.integrity import audit_lan

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "file.txt").write_text("A" * 1000)

            # Faithful rclone check simulation: exit 1 (differences), the
            # combined diff marking file.txt as modified, and the summary.
            diff_fd, diff_path = _tf.mkstemp(suffix=".txt")
            with _os.fdopen(diff_fd, "w") as fh:
                fh.write("* file.txt\n")

            with patch("core.integrity.subprocess.run") as mock_run, \
                 patch("core.integrity.tempfile.mkstemp", return_value=(0, diff_path)), \
                 patch("core.integrity.resolve_binary", return_value="rclone"):
                mock_run.return_value = MagicMock(
                    returncode=1,
                    stdout="",
                    stderr="2026/01/01 00:00:00 NOTICE : 1 differences found\n",
                )
                result = audit_lan(
                    source=str(source),
                    dest=str(Path(tmp) / "dest"),
                )
                assert result is not None
                assert result["status"] == "VERIFICATION_FAILED"
                assert result["mismatches"] == 1
            # _run_rclone_check unlinks the diff file it was given; tolerate that.
            with contextlib.suppress(FileNotFoundError):
                _os.unlink(diff_path)

    def test_large_file_handling(self):
        """Large file doesn't crash hashing."""
        from core.hashing import compute_md5
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 1_000_000)  # 1MB
            f.flush()
            result = compute_md5(f.name)
            assert len(result) == 32
        os.unlink(f.name)


class TestRecoveryScenarios:
    """Simulate recovery from failures."""

    def test_cloud_retry_after_transient_error(self):
        """Cloud sync delegates retry to rclone (--retries), then classifies honestly.

        Contract: run_cloud_sync passes `retries` into the rclone command
        (rclone's own --retries mechanism) and does NOT invent an internal
        process-level retry loop; a persistent failure classifies as
        CLOUD_PARTIAL (exit 5).
        """
        with patch("core.cloud_sync.resolve_binary", return_value="rclone"), \
             patch("core.cloud_sync.subprocess.run") as mock_run, \
             patch("core.cloud_sync.tempfile.mkstemp", side_effect=tempfile.mkstemp):
            mock_run.return_value = MagicMock(returncode=5, stdout="", stderr="transient")
            result = run_cloud_sync(
                source="E:\\",
                bucket="bucket",
                fy_prefix="FY26-27",
                gcs_key_path="key.json",
                project_number="123",
                storage_class="STANDARD",
                retries=2,
            )
            assert result["status"] == "CLOUD_PARTIAL"
            assert result["exit_code"] == 5
            # The retry budget is carried into the rclone command itself.
            cmd = mock_run.call_args[0][0]
            assert "--retries" in cmd and cmd[cmd.index("--retries") + 1] == "2"

    def test_orphaned_log_cleanup_after_crash(self):
        """Orphaned logs are cleaned up after crash."""
        from core.lan_sync import cleanup_orphaned_robocopy_logs
        with tempfile.TemporaryDirectory() as tmp:
            old_log = Path(tmp) / "robocopy_sync_old.log"
            old_log.write_text("old")
            old_time = time.time() - 48 * 3600  # 48 hours ago
            os.utime(old_log, (old_time, old_time))
            # A fresh log must survive.
            fresh_log = Path(tmp) / "robocopy_sync_new.log"
            fresh_log.write_text("new")

            with patch("core.lan_sync.tempfile.gettempdir", return_value=tmp):
                result = cleanup_orphaned_robocopy_logs(max_age_hours=24)

            assert result == 1
            assert not old_log.exists()
            assert fresh_log.exists()


class TestFYBoundaryConditions:
    """Simulate fiscal year boundary conditions."""

    def test_fy_rollover_detection(self):
        """FY rollover is detected correctly."""
        from core.fy_rollover import detect_rollover
        with patch("core.fy_rollover.get_fy_prefix", return_value="FY26-27"):
            # Old FY in paths
            assert detect_rollover("E:\\SOURCE\\FY25-26", "\\\\nas\\share\\FY25-26") is True
            # Current FY in paths
            assert detect_rollover("E:\\SOURCE\\FY26-27", "\\\\nas\\share\\FY26-27") is False

    def test_fy_rollover_no_fy_folders(self):
        """No FY folders → rollover disabled."""
        from core.fy_rollover import detect_rollover
        with patch("core.fy_rollover.get_fy_prefix", return_value="FY26-27"):
            assert detect_rollover("E:\\SOURCE\\DATA", "\\\\nas\\share\\DATA") is False
