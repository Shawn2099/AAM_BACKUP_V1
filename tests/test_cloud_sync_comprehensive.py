"""Comprehensive tests for cloud_sync — full coverage of every public function and path."""

import os
import subprocess
from contextlib import contextmanager
from unittest.mock import MagicMock, patch, call

import pytest

from core.cloud_sync import (
    classify_rclone_exit,
    build_rclone_sync_command,
    run_cloud_sync,
)


# ── Helpers ────────────────────────────────────────────────────────────────

@contextmanager
def _mock_temp_config(*args, **kwargs):
    yield "/tmp/rclone_test.conf"


def _mock_result(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# ── classify_rclone_exit — every exit code 0-10 + edge cases ──────────────

class TestClassifyRcloneExit:
    def test_exit_0_complete(self):
        assert classify_rclone_exit(0) == "CLOUD_COMPLETE"

    def test_exit_1_failed(self):
        assert classify_rclone_exit(1) == "CLOUD_FAILED"

    def test_exit_2_failed(self):
        assert classify_rclone_exit(2) == "CLOUD_FAILED"

    def test_exit_3_failed(self):
        assert classify_rclone_exit(3) == "CLOUD_FAILED"

    def test_exit_4_partial(self):
        assert classify_rclone_exit(4) == "CLOUD_PARTIAL"

    def test_exit_5_partial(self):
        assert classify_rclone_exit(5) == "CLOUD_PARTIAL"

    def test_exit_6_failed(self):
        assert classify_rclone_exit(6) == "CLOUD_FAILED"

    def test_exit_7_failed(self):
        assert classify_rclone_exit(7) == "CLOUD_FAILED"

    def test_exit_8_failed(self):
        assert classify_rclone_exit(8) == "CLOUD_FAILED"

    def test_exit_9_no_changes_complete(self):
        assert classify_rclone_exit(9) == "CLOUD_NO_CHANGES_COMPLETE"

    def test_exit_10_partial(self):
        assert classify_rclone_exit(10) == "CLOUD_PARTIAL"

    def test_unknown_positive_code_defaults_failed(self):
        assert classify_rclone_exit(99) == "CLOUD_FAILED"

    def test_negative_code_defaults_failed(self):
        assert classify_rclone_exit(-1) == "CLOUD_FAILED"

    def test_large_code_defaults_failed(self):
        assert classify_rclone_exit(999) == "CLOUD_FAILED"


# ── build_rclone_sync_command — flags and structure ───────────────────────

class TestBuildRcloneSyncCommand:
    def test_starts_with_rclone_sync(self):
        cmd = build_rclone_sync_command(
            source="D:\\data", bucket="my-bucket", fy_prefix="FY26-27",
            config_path="/tmp/rclone.conf", storage_class="COLDLINE",
        )
        assert cmd[0] == "rclone"
        assert cmd[1] == "sync"

    def test_source_and_dest_present(self):
        cmd = build_rclone_sync_command(
            source="D:\\data", bucket="my-bucket", fy_prefix="FY26-27",
            config_path="/tmp/rclone.conf", storage_class="COLDLINE",
        )
        assert "D:\\data" in cmd
        assert "aam_gcs:my-bucket/FY26-27" in cmd

    def test_config_path_included(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/myconfig.conf", storage_class="COLDLINE",
        )
        idx = cmd.index("--config")
        assert cmd[idx + 1] == "/tmp/myconfig.conf"

    def test_modify_window_2s(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        idx = cmd.index("--modify-window")
        assert cmd[idx + 1] == "2s"

    def test_buffer_size_default_64M(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        idx = cmd.index("--buffer-size")
        assert cmd[idx + 1] == "64M"

    def test_buffer_size_custom(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
            buffer_size="128M",
        )
        idx = cmd.index("--buffer-size")
        assert cmd[idx + 1] == "128M"

    def test_transfers_default(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        idx = cmd.index("--transfers")
        assert cmd[idx + 1] == "2"

    def test_transfers_custom(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
            transfers=8,
        )
        idx = cmd.index("--transfers")
        assert cmd[idx + 1] == "8"

    def test_checkers_default(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        idx = cmd.index("--checkers")
        assert cmd[idx + 1] == "4"

    def test_checkers_custom(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
            checkers=32,
        )
        idx = cmd.index("--checkers")
        assert cmd[idx + 1] == "32"

    def test_storage_class(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="ARCHIVE",
        )
        idx = cmd.index("--gcs-storage-class")
        assert cmd[idx + 1] == "ARCHIVE"

    def test_bwlimit_default(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        idx = cmd.index("--bwlimit")
        assert cmd[idx + 1] == "10M"

    def test_bwlimit_custom(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
            bwlimit="50M",
        )
        idx = cmd.index("--bwlimit")
        assert cmd[idx + 1] == "50M"

    def test_retries_default(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        idx = cmd.index("--retries")
        assert cmd[idx + 1] == "3"

    def test_retries_custom(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
            retries=5,
        )
        idx = cmd.index("--retries")
        assert cmd[idx + 1] == "5"

    def test_error_on_no_transfer_present(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "--error-on-no-transfer" in cmd

    def test_fast_list_present(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "--fast-list" in cmd

    def test_gcs_no_check_bucket_present(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "--gcs-no-check-bucket" in cmd

    def test_check_first_present(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "--check-first" in cmd

    def test_use_json_log_present(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "--use-json-log" in cmd

    def test_log_level_info(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        idx = cmd.index("--log-level")
        assert cmd[idx + 1] == "INFO"

    def test_stats_60s(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        idx = cmd.index("--stats")
        assert cmd[idx + 1] == "60s"

    def test_retries_sleep_30s(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        idx = cmd.index("--retries-sleep")
        assert cmd[idx + 1] == "30s"

    def test_no_max_delete(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "--max-delete" not in cmd

    def test_no_track_renames(self):
        cmd = build_rclone_sync_command(
            source="D:\\", bucket="b", fy_prefix="FY",
            config_path="/tmp/c.conf", storage_class="COLDLINE",
        )
        assert "--track-renames" not in cmd


# ── run_cloud_sync — subprocess orchestration ────────────────────────────

class TestRunCloudSyncSuccess:
    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_0_cloud_complete(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_COMPLETE"
        assert result["exit_code"] == 0
        assert result["error"] is None

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_9_no_changes(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(9)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_NO_CHANGES_COMPLETE"
        assert result["exit_code"] == 9
        assert result["error"] is None

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_4_partial(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(4)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_PARTIAL"
        assert result["exit_code"] == 4

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_5_partial(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(5)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_PARTIAL"
        assert result["exit_code"] == 5

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_10_partial(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(10)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_PARTIAL"
        assert result["exit_code"] == 10


class TestRunCloudSyncStderr:
    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_nonzero_reads_stderr(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(7)
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
    def test_full_stderr_returned_no_truncation(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(1)
        large_stderr = "x" * 150000
        mock_path.return_value.read_text.return_value = large_stderr
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["error"] == large_stderr

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_unreadable_uses_fallback(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(1)
        mock_path.return_value.read_text.side_effect = OSError("permission denied")
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert "stderr unreadable" in result["error"]

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_9_no_stderr_read(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        """Exit 9 (no changes) should NOT read stderr."""
        mock_run.return_value = _mock_result(9)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["error"] is None
        mock_path.return_value.read_text.assert_not_called()


class TestRunCloudSyncExceptions:
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
    def test_file_not_found_rclone(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
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


class TestRunCloudSyncStderrCleanup:
    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_cleaned_up_on_success(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        mock_path.return_value.unlink.assert_called_once()

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_cleaned_up_on_timeout(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        mock_path.return_value.unlink.assert_called_once()

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_cleaned_up_on_file_not_found(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.side_effect = FileNotFoundError("rclone missing")
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        mock_path.return_value.unlink.assert_called_once()

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_cleaned_up_on_os_error(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.side_effect = OSError("io error")
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        mock_path.return_value.unlink.assert_called_once()

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_unlink_failure_swallowed(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        """If unlink fails in finally, it must not raise."""
        mock_run.return_value = _mock_result(0)
        mock_path.return_value.unlink.side_effect = OSError("still in use")
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_COMPLETE"


class TestRunCloudSyncReturnStructure:
    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_return_has_status_exit_code_error(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert set(result.keys()) == {"status", "exit_code", "error"}

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_return_has_status_exit_code_error_on_failure(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(7)
        mock_path_patcher = patch("core.cloud_sync.Path")
        mock_path = mock_path_patcher.start()
        mock_path.return_value.read_text.return_value = "fatal error"
        try:
            result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
            assert set(result.keys()) == {"status", "exit_code", "error"}
        finally:
            mock_path_patcher.stop()


class TestRunCloudSyncTimeoutPassthrough:
    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_default_timeout_21600(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 21600

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_custom_timeout_passed(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE", timeout=7200)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 7200


class TestRunCloudSyncEnvAndSubprocess:
    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_subprocess_run_called_with_cmd(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "rclone"
        assert cmd[1] == "sync"

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stdout_devnull(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["stdout"] == subprocess.DEVNULL

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_to_file(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["stderr"] is not None

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_text_mode(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["text"] is True

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(99, "/tmp/stderr.log"))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_buffer_size_flows_to_command(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE", buffer_size="256M")
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--buffer-size")
        assert cmd[idx + 1] == "256M"
