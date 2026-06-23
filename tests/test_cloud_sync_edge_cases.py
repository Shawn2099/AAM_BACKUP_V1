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
    """Test every exit code and boundary."""

    @pytest.mark.parametrize("code,status", [
        (0, "CLOUD_COMPLETE"),
        (1, "CLOUD_FAILED"),
        (2, "CLOUD_FAILED"),
        (3, "CLOUD_FAILED"),
        (4, "CLOUD_PARTIAL"),
        (5, "CLOUD_PARTIAL"),
        (6, "CLOUD_FAILED"),
        (7, "CLOUD_FAILED"),
        (8, "CLOUD_FAILED"),
        (9, "CLOUD_NO_CHANGES_COMPLETE"),
        (10, "CLOUD_PARTIAL"),
    ])
    def test_exit_code_classification(self, code, status):
        assert classify_rclone_exit(code) == status

    def test_negative_code(self):
        assert classify_rclone_exit(-1) == "CLOUD_FAILED"

    def test_large_code_255(self):
        assert classify_rclone_exit(255) == "CLOUD_FAILED"

    def test_large_code_999(self):
        assert classify_rclone_exit(999) == "CLOUD_FAILED"

    def test_11_plus(self):
        assert classify_rclone_exit(11) == "CLOUD_FAILED"

    def test_only_zero_is_complete(self):
        """Only exit 0 means files were synced."""
        for code in range(1, 11):
            assert classify_rclone_exit(code) != "CLOUD_COMPLETE"

    def test_noretry_codes_are_failed(self):
        """Exit codes 1,2,3,6,7,8 are CLOUD_FAILED (no point retrying)."""
        for code in [1, 2, 3, 6, 7, 8]:
            assert classify_rclone_exit(code) == "CLOUD_FAILED"

    def test_retryable_codes_are_partial(self):
        """Exit codes 4,5 are CLOUD_PARTIAL (retryable)."""
        for code in [4, 5]:
            assert classify_rclone_exit(code) == "CLOUD_PARTIAL"

    def test_limit_codes_are_partial(self):
        """Exit code 10 (duration limit) is CLOUD_PARTIAL."""
        assert classify_rclone_exit(10) == "CLOUD_PARTIAL"


# ---------------------------------------------------------------------------
# build_rclone_sync_command — required flags
# ---------------------------------------------------------------------------


class TestBuildRcloneSyncCommandFlags:
    """Verify all required flags are present with correct values."""

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

    def test_is_list_of_strings(self):
        cmd = self._build_default()
        assert isinstance(cmd, list)
        assert all(isinstance(x, str) for x in cmd)

    def test_starts_with_rclone_sync(self):
        cmd = self._build_default()
        assert cmd[:2] == ["rclone", "sync"]

    def test_source_and_dest_in_correct_positions(self):
        cmd = self._build_default()
        assert cmd[2] == "D:\\data"
        assert cmd[3] == "aam_gcs:my-bucket/FY26-27"

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
        "--check-first",
        "--buffer-size",
        "--use-json-log",
        "--log-level",
        "--stats",
    ])
    def test_required_flag_present(self, flag):
        cmd = self._build_default()
        assert flag in cmd, f"{flag} missing from command"

    @pytest.mark.parametrize("flag,expected", [
        ("--config", "/tmp/rclone.conf"),
        ("--gcs-storage-class", "COLDLINE"),
        ("--modify-window", "2s"),
        ("--bwlimit", "10M"),
        ("--transfers", "2"),
        ("--checkers", "4"),
        ("--retries", "3"),
        ("--retries-sleep", "30s"),
        ("--log-level", "INFO"),
        ("--stats", "60s"),
        ("--buffer-size", "64M"),
    ])
    def test_flag_value(self, flag, expected):
        cmd = self._build_default()
        idx = cmd.index(flag)
        assert cmd[idx + 1] == expected

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
        cmd = self._build_default(checkers=16)
        idx = cmd.index("--checkers")
        assert cmd[idx + 1] == "16"

    def test_custom_retries(self):
        cmd = self._build_default(retries=5)
        idx = cmd.index("--retries")
        assert cmd[idx + 1] == "5"

    def test_bucket_with_fy_prefix(self):
        cmd = self._build_default(bucket="prod-bucket", fy_prefix="FY26-27")
        assert "aam_gcs:prod-bucket/FY26-27" in cmd

    def test_simple_bucket_and_prefix(self):
        cmd = self._build_default(bucket="b", fy_prefix="FY")
        assert "aam_gcs:b/FY" in cmd

    def test_no_duplicate_flags(self):
        cmd = self._build_default()
        flags = [x for x in cmd if x.startswith("--")]
        assert len(flags) == len(set(flags)), f"Duplicate flags found"

    def test_no_max_delete(self):
        """--max-delete removed — GCS versioning covers this."""
        cmd = self._build_default()
        assert "--max-delete" not in cmd

    def test_no_track_renames(self):
        """--track-renames removed — hash cost > bandwidth savings."""
        cmd = self._build_default()
        assert "--track-renames" not in cmd

    def test_custom_buffer_size(self):
        cmd = self._build_default(buffer_size="128M")
        idx = cmd.index("--buffer-size")
        assert cmd[idx + 1] == "128M"


# ---------------------------------------------------------------------------
# build_rclone_sync_command — edge cases with paths
# ---------------------------------------------------------------------------


class TestBuildRcloneSyncCommandEdgeCases:
    """Test command building with special characters and paths."""

    def test_source_with_spaces(self):
        cmd = build_rclone_sync_command(
            source="D:\\My Documents\\backup data",
            bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "D:\\My Documents\\backup data" in cmd

    def test_config_path_with_spaces(self):
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
            source="", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "" in cmd

    def test_backward_slashes_preserved(self):
        cmd = build_rclone_sync_command(
            source="D:\\folder\\subfolder",
            bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "D:\\folder\\subfolder" in cmd

    def test_forward_slashes_preserved(self):
        cmd = build_rclone_sync_command(
            source="/home/user/data",
            bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "/home/user/data" in cmd


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
        (9, "CLOUD_NO_CHANGES_COMPLETE"),
        (10, "CLOUD_PARTIAL"),
    ])
    def test_exit_code_classification(self, exit_code, expected_status):
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
        mock_run.return_value = MagicMock(returncode=1)
        mock_path.return_value.read_text.return_value = "x" * 150000
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert len(result["error"]) == 100000

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_unreadable(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.return_value = MagicMock(returncode=1)
        mock_path.return_value.read_text.side_effect = OSError("permission denied")
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert "stderr unreadable" in result["error"]

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_timeout_expired(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
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
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert isinstance(result["status"], str)
        assert result["status"] == "CLOUD_FAILED"
