"""Tests for cloud_sync — rclone command building, exit classification, and orchestration."""

import os
import subprocess
import tempfile
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from core.cloud_sync import build_rclone_sync_command, classify_rclone_exit, run_cloud_sync


@contextmanager
def _mock_temp_config(*args, **kwargs):
    yield "/tmp/rclone_test.conf"


def _with_real_stderr_log():
    """Create a real temp file so os.close(fd) and Path.unlink() work."""
    fd, path = tempfile.mkstemp(suffix=".log", prefix="cloud_sync_test_")
    os.close(fd)
    return path


class TestClassifyRcloneExit:
    def test_zero_is_complete(self):
        assert classify_rclone_exit(0) == "CLOUD_COMPLETE"

    def test_nine_is_partial(self):
        """Exit 9 = 'no files transferred' with --error-on-no-transfer."""
        assert classify_rclone_exit(9) == "CLOUD_PARTIAL"

    def test_one_is_failed(self):
        assert classify_rclone_exit(1) == "CLOUD_FAILED"

    def test_two_is_failed(self):
        assert classify_rclone_exit(2) == "CLOUD_FAILED"

    def test_three_is_failed(self):
        assert classify_rclone_exit(3) == "CLOUD_FAILED"

    def test_four_is_partial(self):
        assert classify_rclone_exit(4) == "CLOUD_PARTIAL"

    def test_five_is_partial(self):
        assert classify_rclone_exit(5) == "CLOUD_PARTIAL"

    def test_six_is_failed(self):
        """Exit 6 = 'NoRetry errors' — retries won't help."""
        assert classify_rclone_exit(6) == "CLOUD_FAILED"

    def test_seven_is_failed(self):
        assert classify_rclone_exit(7) == "CLOUD_FAILED"

    def test_eight_is_failed(self):
        assert classify_rclone_exit(8) == "CLOUD_FAILED"

    def test_ten_is_partial(self):
        assert classify_rclone_exit(10) == "CLOUD_PARTIAL"

    def test_unknown_code_defaults_to_failed(self):
        assert classify_rclone_exit(99) == "CLOUD_FAILED"

    def test_negative_defaults_to_failed(self):
        assert classify_rclone_exit(-1) == "CLOUD_FAILED"


class TestBuildRcloneSyncCommand:
    def test_basic_structure(self):
        cmd = build_rclone_sync_command(
            source="D:\\data",
            bucket="my-bucket",
            fy_prefix="FY26-27",
            config_path="/tmp/rclone.conf",
            storage_class="COLDLINE",
        )
        assert cmd[0] == "rclone"
        assert cmd[1] == "sync"
        assert "D:\\data" in cmd
        assert "aam_gcs:my-bucket/FY26-27" in cmd

    def test_config_path_included(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/myconfig.conf",
            storage_class="COLDLINE",
        )
        assert "--config" in cmd
        cfg_idx = cmd.index("--config")
        assert cmd[cfg_idx + 1] == "/tmp/myconfig.conf"

    def test_custom_transfers(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE", transfers=8,
        )
        assert "--transfers" in cmd
        t_idx = cmd.index("--transfers")
        assert cmd[t_idx + 1] == "8"

    def test_custom_checkers(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE", checkers=32,
        )
        assert "--checkers" in cmd
        c_idx = cmd.index("--checkers")
        assert cmd[c_idx + 1] == "32"

    def test_custom_storage_class(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="ARCHIVE",
        )
        sc_idx = cmd.index("--gcs-storage-class")
        assert cmd[sc_idx + 1] == "ARCHIVE"

    def test_custom_bandwidth(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE", bwlimit="50M",
        )
        assert "--bwlimit" in cmd
        b_idx = cmd.index("--bwlimit")
        assert cmd[b_idx + 1] == "50M"

    def test_retry_count(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE", retries=5,
        )
        r_idx = cmd.index("--retries")
        assert cmd[r_idx + 1] == "5"

    def test_gcs_no_check_bucket_present(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf",
            storage_class="COLDLINE",
        )
        assert "--gcs-no-check-bucket" in cmd

    def test_fast_list_present(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf",
            storage_class="COLDLINE",
        )
        assert "--fast-list" in cmd


class TestRunCloudSync:
    """Unit tests for the run_cloud_sync subprocess orchestration."""

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_success_exit_0(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = MagicMock(returncode=0)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_COMPLETE"
        assert result["exit_code"] == 0
        assert result["error"] is None

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_no_files_to_transfer_exit_9(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = MagicMock(returncode=9)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_PARTIAL"
        assert result["exit_code"] == 9

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_partial_exit_4(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = MagicMock(returncode=4)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_PARTIAL"
        assert result["exit_code"] == 4

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_fatal_exit_7_with_stderr(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.return_value = MagicMock(returncode=7)
        mock_path.return_value.read_text.return_value = "auth failed: bad credentials"
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_FAILED"
        assert result["exit_code"] == 7
        assert "auth failed" in result["error"]

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_truncation(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
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

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_log_cleaned_up_on_success(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_path_instance = mock_path.return_value
        mock_run.return_value = MagicMock(returncode=0)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_COMPLETE"
        mock_path_instance.unlink.assert_called_once()

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_log_cleaned_up_on_timeout(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_path_instance = mock_path.return_value
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_FAILED"
        mock_path_instance.unlink.assert_called_once()
