"""Deep-dive edge case tests for cloud_sync module.

Tests cover:
- All rclone exit codes 0-10 with official meanings
- Boundary conditions and invalid inputs
- Command building with special characters, paths, unicode
- All required flags present
- Integration with subprocess orchestration
"""

import os
import subprocess
import tempfile
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from core.cloud_sync import build_rclone_sync_command, classify_rclone_exit, run_cloud_sync


@contextmanager
def _mock_temp_config(*args, **kwargs):
    yield "/tmp/rclone_test.conf"


# ---------------------------------------------------------------------------
# classify_rclone_exit — comprehensive edge cases
# ---------------------------------------------------------------------------

class TestClassifyRcloneExitComprehensive:
    """Test all rclone exit codes per official documentation."""

    # --- Valid codes 0-10 ---

    def test_0_success(self):
        """Exit 0: Success — all files synced."""
        assert classify_rclone_exit(0) == "CLOUD_COMPLETE"

    def test_1_error_not_categorised(self):
        """Exit 1: Error not otherwise categorised."""
        assert classify_rclone_exit(1) == "CLOUD_FAILED"

    def test_2_syntax_error(self):
        """Exit 2: Syntax or usage error."""
        assert classify_rclone_exit(2) == "CLOUD_FAILED"

    def test_3_dir_not_found(self):
        """Exit 3: Directory not found."""
        assert classify_rclone_exit(3) == "CLOUD_FAILED"

    def test_4_file_not_found(self):
        """Exit 4: File not found — transient, retryable."""
        assert classify_rclone_exit(4) == "CLOUD_PARTIAL"

    def test_5_temporary_error(self):
        """Exit 5: Temporary error — retry might fix."""
        assert classify_rclone_exit(5) == "CLOUD_PARTIAL"

    def test_6_noretry_error(self):
        """Exit 6: Less serious errors (like 461 from dropbox) — NoRetry."""
        assert classify_rclone_exit(6) == "CLOUD_FAILED"

    def test_7_fatal_error(self):
        """Exit 7: Fatal error — account suspended, retries won't help."""
        assert classify_rclone_exit(7) == "CLOUD_FAILED"

    def test_8_transfer_exceeded(self):
        """Exit 8: Transfer exceeded — --max-transfer reached."""
        assert classify_rclone_exit(8) == "CLOUD_FAILED"

    def test_9_no_files_transferred(self):
        """Exit 9: No files transferred (requires --error-on-no-transfer)."""
        assert classify_rclone_exit(9) == "CLOUD_PARTIAL"

    def test_10_duration_exceeded(self):
        """Exit 10: Duration exceeded — --max-duration reached."""
        assert classify_rclone_exit(10) == "CLOUD_PARTIAL"

    # --- Edge cases: invalid/out-of-range codes ---

    def test_negative_code(self):
        """Negative codes default to CLOUD_FAILED."""
        assert classify_rclone_exit(-1) == "CLOUD_FAILED"

    def test_large_code_255(self):
        """Exit 255 (common process error) defaults to CLOUD_FAILED."""
        assert classify_rclone_exit(255) == "CLOUD_FAILED"

    def test_large_code_999(self):
        """Exit 999 defaults to CLOUD_FAILED."""
        assert classify_rclone_exit(999) == "CLOUD_FAILED"

    def test_11_plus(self):
        """Codes 11-12 default to CLOUD_FAILED."""
        assert classify_rclone_exit(11) == "CLOUD_FAILED"
        assert classify_rclone_exit(12) == "CLOUD_FAILED"

    # --- Verify no duplicate mappings ---

    def test_only_zero_is_complete(self):
        """Only exit 0 maps to CLOUD_COMPLETE."""
        for code in range(11):
            result = classify_rclone_exit(code)
            if code == 0:
                assert result == "CLOUD_COMPLETE", f"Exit {code} should be COMPLETE"
            else:
                assert result != "CLOUD_COMPLETE", f"Exit {code} should NOT be COMPLETE"

    def test_noretry_codes_are_failed(self):
        """Exit code 6 (NoRetry) must be CLOUD_FAILED."""
        assert classify_rclone_exit(6) == "CLOUD_FAILED"

    def test_retryable_codes_are_partial(self):
        """Exit codes 4,5 (retryable) must be CLOUD_PARTIAL."""
        assert classify_rclone_exit(4) == "CLOUD_PARTIAL"
        assert classify_rclone_exit(5) == "CLOUD_PARTIAL"

    def test_limit_codes_are_partial(self):
        """Exit codes 9,10 (limits/status) must be CLOUD_PARTIAL."""
        assert classify_rclone_exit(9) == "CLOUD_PARTIAL"
        assert classify_rclone_exit(10) == "CLOUD_PARTIAL"


# ---------------------------------------------------------------------------
# build_rclone_sync_command — comprehensive flag coverage
# ---------------------------------------------------------------------------

class TestBuildRcloneSyncCommandFlags:
    """Verify all required flags are present and correctly valued."""

    def _build_default(self, **kwargs):
        defaults = dict(
            source="D:\\data",
            bucket="my-bucket",
            fy_prefix="FY26-27",
            config_path="/tmp/rclone.conf",
            storage_class="COLDLINE",
        )
        defaults.update(kwargs)
        return build_rclone_sync_command(**defaults)

    # --- Structure ---

    def test_is_list_of_strings(self):
        """Command should be a list of strings."""
        cmd = self._build_default()
        assert isinstance(cmd, list)
        assert all(isinstance(x, str) for x in cmd)

    def test_starts_with_rclone_sync(self):
        cmd = self._build_default()
        assert cmd[0] == "rclone"
        assert cmd[1] == "sync"

    def test_source_and_dest_in_correct_positions(self):
        cmd = self._build_default()
        assert cmd[2] == "D:\\data"
        assert cmd[3] == "aam_gcs:my-bucket/FY26-27"

    # --- Required flags present ---

    @pytest.mark.parametrize("flag", [
        "--config",
        "--fast-list",
        "--gcs-no-check-bucket",
        "--gcs-storage-class",
        "--error-on-no-transfer",
        "--modify-window",
        "--bwlimit",
        "--transfers",
        "--checkers",
        "--retries",
        "--retries-sleep",
        "--track-renames",
        "--max-delete",
        "--use-json-log",
        "--log-level",
        "--stats",
    ])
    def test_required_flag_present(self, flag):
        """Every required flag must be present in the command."""
        cmd = self._build_default()
        assert flag in cmd, f"Missing flag: {flag}"

    # --- Flag values ---

    @pytest.mark.parametrize("flag,expected", [
        ("--config", "/tmp/rclone.conf"),
        ("--gcs-storage-class", "COLDLINE"),
        ("--modify-window", "1s"),
        ("--bwlimit", "10M"),
        ("--transfers", "2"),
        ("--checkers", "4"),
        ("--retries", "3"),
        ("--retries-sleep", "30s"),
        ("--log-level", "INFO"),
        ("--stats", "60s"),
        ("--max-delete", "45"),
    ])
    def test_flag_value(self, flag, expected):
        """Flag values should match expected defaults."""
        cmd = self._build_default()
        idx = cmd.index(flag)
        assert cmd[idx + 1] == expected

    # --- Custom parameters ---

    def test_custom_storage_class(self):
        cmd = self._build_default(storage_class="ARCHIVE")
        idx = cmd.index("--gcs-storage-class")
        assert cmd[idx + 1] == "ARCHIVE"

    def test_custom_bwlimit(self):
        cmd = self._build_default(bwlimit="50M")
        idx = cmd.index("--bwlimit")
        assert cmd[idx + 1] == "50M"

    def test_custom_transfers(self):
        cmd = self._build_default(transfers=8)
        idx = cmd.index("--transfers")
        assert cmd[idx + 1] == "8"

    def test_custom_checkers(self):
        cmd = self._build_default(checkers=32)
        idx = cmd.index("--checkers")
        assert cmd[idx + 1] == "32"

    def test_custom_retries(self):
        cmd = self._build_default(retries=5)
        idx = cmd.index("--retries")
        assert cmd[idx + 1] == "5"

    # --- Dest path construction ---

    def test_bucket_with_fy_prefix(self):
        cmd = self._build_default(bucket="my-bucket", fy_prefix="FY26-27")
        assert "aam_gcs:my-bucket/FY26-27" in cmd

    def test_simple_bucket_and_prefix(self):
        cmd = self._build_default(bucket="backup", fy_prefix="FY")
        assert "aam_gcs:backup/FY" in cmd

    # --- Flag count (no duplicates) ---

    def test_no_duplicate_flags(self):
        """Each flag should appear exactly once."""
        cmd = self._build_default()
        flags = [x for x in cmd if x.startswith("--")]
        assert len(flags) == len(set(flags)), f"Duplicate flags found"

    # --- Ransomware kill-switch ---

    def test_max_delete_default_is_45(self):
        """Default ransomware kill-switch should be 45%."""
        cmd = self._build_default()
        assert "--max-delete" in cmd
        idx = cmd.index("--max-delete")
        assert cmd[idx + 1] == "45"

    def test_max_delete_custom_value(self):
        """Custom max_delete_percent is injected correctly."""
        cmd = self._build_default(max_delete_percent=20)
        idx = cmd.index("--max-delete")
        assert cmd[idx + 1] == "20"


# ---------------------------------------------------------------------------
# build_rclone_sync_command — special characters and paths
# ---------------------------------------------------------------------------

class TestBuildRcloneSyncCommandEdgeCases:
    """Test command building with special characters and paths."""

    def test_source_with_spaces(self):
        """Windows paths with spaces should be preserved."""
        cmd = build_rclone_sync_command(
            source="D:\\My Documents\\backup data",
            bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "D:\\My Documents\\backup data" in cmd

    def test_config_path_with_spaces(self):
        """Config path with spaces should be preserved."""
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="C:\\Users\\John Doe\\rclone.conf",
            storage_class="COLDLINE",
        )
        idx = cmd.index("--config")
        assert cmd[idx + 1] == "C:\\Users\\John Doe\\rclone.conf"

    def test_empty_strings_preserved(self):
        """Empty strings should be preserved (caller's responsibility)."""
        cmd = build_rclone_sync_command(
            source="", bucket="", fy_prefix="",
            config_path="", storage_class="",
        )
        assert cmd[2] == ""
        assert cmd[3] == "aam_gcs:/"

    def test_backward_slashes_preserved(self):
        """Windows backslashes should not be altered."""
        cmd = build_rclone_sync_command(
            source="D:\\backup\\FY26-27",
            bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "D:\\backup\\FY26-27" in cmd

    def test_forward_slashes_preserved(self):
        """Unix forward slashes should be preserved."""
        cmd = build_rclone_sync_command(
            source="/mnt/backup/data",
            bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "/mnt/backup/data" in cmd


# ---------------------------------------------------------------------------
# run_cloud_sync — comprehensive integration tests
# ---------------------------------------------------------------------------

class TestRunCloudSyncComprehensive:
    """Integration tests for subprocess orchestration."""

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_success_returns_complete(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        """Exit 0 → CLOUD_COMPLETE."""
        mock_run.return_value = MagicMock(returncode=0)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_COMPLETE"
        assert result["exit_code"] == 0
        assert result["error"] is None

    @pytest.mark.parametrize("exit_code,expected_status", [
        (1, "CLOUD_FAILED"),
        (2, "CLOUD_FAILED"),
        (3, "CLOUD_FAILED"),
        (4, "CLOUD_PARTIAL"),
        (5, "CLOUD_PARTIAL"),
        (6, "CLOUD_FAILED"),
        (7, "CLOUD_FAILED"),
        (8, "CLOUD_FAILED"),
        (9, "CLOUD_PARTIAL"),
        (10, "CLOUD_PARTIAL"),
    ])
    def test_exit_code_classification(self, exit_code, expected_status):
        """Each exit code should produce the correct status."""
        with patch("core.cloud_sync.os.close"), \
             patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log")), \
             patch("core.cloud_sync.subprocess.run") as mock_run, \
             patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config):
            mock_run.return_value = MagicMock(returncode=exit_code)
            result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
            assert result["status"] == expected_status
            assert result["exit_code"] == exit_code

    # --- Error scenarios ---

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_truncation(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        """Stderr > 2000 chars should be truncated."""
        mock_run.return_value = MagicMock(returncode=1)
        mock_path.return_value.read_text.return_value = "x" * 3000
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert len(result["error"]) == 2000

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_unreadable(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        """Stderr read failure → graceful error message."""
        mock_run.return_value = MagicMock(returncode=1)
        mock_path.return_value.read_text.side_effect = OSError("permission denied")
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert "stderr unreadable" in result["error"]

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_timeout_expired(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        """Timeout → CLOUD_FAILED with timeout message."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE", timeout=300)
        assert result["status"] == "CLOUD_FAILED"
        assert result["exit_code"] == -1
        assert "Timeout after 300s" in result["error"]

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_rclone_not_found(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        """rclone not found → CLOUD_FAILED."""
        mock_run.side_effect = FileNotFoundError("rclone not on PATH")
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_FAILED"
        assert result["exit_code"] == -1
        assert "rclone not found" in result["error"]

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_os_error(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        """OS error → CLOUD_FAILED."""
        mock_run.side_effect = OSError("disk full")
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_FAILED"
        assert result["exit_code"] == -1
        assert result["error"] == "disk full"

    # --- Cleanup verification ---

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_cleanup_on_success(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        """Stderr temp file should be deleted on success."""
        mock_path_instance = mock_path.return_value
        mock_run.return_value = MagicMock(returncode=0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        mock_path_instance.unlink.assert_called_once()

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_cleanup_on_failure(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        """Stderr temp file should be deleted on failure."""
        mock_path_instance = mock_path.return_value
        mock_run.return_value = MagicMock(returncode=1)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        mock_path_instance.unlink.assert_called_once()

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_cleanup_on_timeout(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        """Stderr temp file should be deleted on timeout."""
        mock_path_instance = mock_path.return_value
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        mock_path_instance.unlink.assert_called_once()

    # --- Return structure ---

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_return_structure_has_required_keys(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        """Return should have status, exit_code, error keys."""
        mock_run.return_value = MagicMock(returncode=0)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert "status" in result
        assert "exit_code" in result
        assert "error" in result

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_timeout_returns_string_status(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        """Timeout should return string status, not None."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert isinstance(result["status"], str)
        assert result["status"] == "CLOUD_FAILED"
