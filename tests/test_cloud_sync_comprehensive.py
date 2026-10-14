"""Comprehensive tests for core/cloud_sync.py — all functions and scenarios.

Aligned with the production contract in core/cloud_sync.py:
  * classify_rclone_exit: 0→CLOUD_COMPLETE, 1/2/3→CLOUD_FAILED, 4/5→CLOUD_PARTIAL,
    6/7/8→CLOUD_FAILED, 9→CLOUD_NO_CHANGES_COMPLETE, 10→CLOUD_PARTIAL, else FAILED.
  * scan_rclone_log_for_errors → (has_error_signal, error_tail) TUPLE.
  * resolve_max_duration_seconds(timeout, configured) — note the argument order.
  * build_rclone_sync_command(..., storage_class, bwlimit=..., max_duration_seconds=...).
  * run_cloud_sync → {"status", "exit_code", "error", "suspect_reason"}; exit 0 with
    ERROR/CRITICAL log signals is reclassified CLOUD_SUSPECT (C-DK-001), exit 9 with
    fatal signals is reclassified CLOUD_FAILED (P1-EXIT9).
"""

import os
import subprocess
import tempfile
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from core.cloud_sync import (
    build_rclone_sync_command,
    classify_rclone_exit,
    resolve_max_duration_seconds,
    run_cloud_sync,
    scan_rclone_log_for_errors,
)

_DUMMY_STDERR_PATH = os.path.join(tempfile.gettempdir(), "test_cloud_sync_stderr.log")


@pytest.fixture(autouse=True)
def _pin_rclone_exe():
    """M7: builder now resolves the binary via core.process.resolve_binary.

    Pin it to the bare name so command-structure assertions (cmd[0] == "rclone")
    stay deterministic regardless of whether rclone is installed on the test
    machine. Dedicated resolution tests override this patch explicitly.
    """
    with patch("core.cloud_sync.resolve_binary", create=True, return_value="rclone"):
        yield


@contextmanager
def _mock_temp_config(*args, **kwargs):
    yield os.path.join(tempfile.gettempdir(), "rclone_test.conf")


def _mock_result(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _build(**kwargs):
    """build_rclone_sync_command with the required positional args defaulted."""
    defaults = dict(
        source="D:\\data",
        bucket="my-bucket",
        fy_prefix="FY26-27",
        config_path="/tmp/rclone.conf",
        storage_class="COLDLINE",
    )
    defaults.update(kwargs)
    return build_rclone_sync_command(**defaults)


# ── classify_rclone_exit — every exit code 0-10 + edge cases ──────────────

class TestClassifyRcloneExit:
    def test_exit_0(self):
        assert classify_rclone_exit(0) == "CLOUD_COMPLETE"

    def test_exit_1(self):
        """rclone exit 1 = syntax/usage error → CLOUD_FAILED (not COMPLETE)."""
        assert classify_rclone_exit(1) == "CLOUD_FAILED"

    def test_exit_2(self):
        """rclone exit 2 = error not otherwise categorised → CLOUD_FAILED."""
        assert classify_rclone_exit(2) == "CLOUD_FAILED"

    def test_exit_3(self):
        """rclone exit 3 = directory not found → CLOUD_FAILED."""
        assert classify_rclone_exit(3) == "CLOUD_FAILED"

    def test_exit_4_partial(self):
        """rclone exit 4 = file not found (transient) → CLOUD_PARTIAL."""
        assert classify_rclone_exit(4) == "CLOUD_PARTIAL"

    def test_exit_5_partial(self):
        """rclone exit 5 = temporary error (retryable) → CLOUD_PARTIAL.

        The status name is CLOUD_PARTIAL; the production vocabulary has no
        separate CLOUD_TRANSIENT_ERROR status.
        """
        assert classify_rclone_exit(5) == "CLOUD_PARTIAL"

    def test_exit_6(self):
        """rclone exit 6 = less serious, NoRetry errors → CLOUD_FAILED.

        There is no CLOUD_FATAL_ERROR status in the production vocabulary.
        """
        assert classify_rclone_exit(6) == "CLOUD_FAILED"

    def test_exit_7(self):
        """rclone exit 7 = fatal (auth, bucket) → CLOUD_FAILED."""
        assert classify_rclone_exit(7) == "CLOUD_FAILED"

    def test_exit_8_failed(self):
        """rclone exit 8 = transfer limit exceeded → CLOUD_FAILED."""
        assert classify_rclone_exit(8) == "CLOUD_FAILED"

    def test_exit_9_no_changes(self):
        """exit 9 = no files transferred (needs --error-on-no-transfer)."""
        assert classify_rclone_exit(9) == "CLOUD_NO_CHANGES_COMPLETE"

    def test_exit_10_partial(self):
        """rclone exit 10 = duration limit hit → CLOUD_PARTIAL."""
        assert classify_rclone_exit(10) == "CLOUD_PARTIAL"

    def test_exit_negative(self):
        assert classify_rclone_exit(-1) == "CLOUD_FAILED"

    def test_exit_99(self):
        assert classify_rclone_exit(99) == "CLOUD_FAILED"

    def test_exit_11_unknown_defaults_failed(self):
        assert classify_rclone_exit(11) == "CLOUD_FAILED"


# ── resolve_max_duration_seconds(timeout, configured) ────────────────────

class TestResolveMaxDuration:
    def test_none_configured_auto_derives(self):
        """configured=None → auto = timeout minus the 300s margin."""
        assert resolve_max_duration_seconds(18000, None) == 17700

    def test_none_configured_small_window_disables(self):
        """Timeout too small to hold the margin → cap stays off, not negative."""
        assert resolve_max_duration_seconds(120, None) is None
        assert resolve_max_duration_seconds(300, None) is None

    def test_zero_disables(self):
        assert resolve_max_duration_seconds(18000, 0) is None

    def test_negative_disables(self):
        assert resolve_max_duration_seconds(18000, -5) is None

    def test_explicit_value(self):
        assert resolve_max_duration_seconds(18000, 3600) == 3600

    def test_explicit_value_below_margin_used_as_is(self):
        """An explicit operator override is honoured even below the margin."""
        assert resolve_max_duration_seconds(18000, 60) == 60


# ── build_rclone_sync_command — flags and structure ───────────────────────

class TestBuildRcloneSyncCommand:
    def test_basic_command(self):
        cmd = _build()
        assert cmd[0] == "rclone"
        assert cmd[1] == "sync"
        assert "D:\\data" in cmd
        assert "aam_gcs:my-bucket/FY26-27" in cmd
        assert "--config" in cmd
        assert "/tmp/rclone.conf" in cmd

    def test_config_path_included(self):
        cmd = _build(config_path="/tmp/myconfig.conf")
        idx = cmd.index("--config")
        assert cmd[idx + 1] == "/tmp/myconfig.conf"

    def test_modify_window_2s(self):
        cmd = _build()
        idx = cmd.index("--modify-window")
        assert cmd[idx + 1] == "2s"

    def test_buffer_size_default(self):
        cmd = _build()
        idx = cmd.index("--buffer-size")
        assert cmd[idx + 1] == "64M"

    def test_buffer_size_custom(self):
        cmd = _build(buffer_size="128M")
        idx = cmd.index("--buffer-size")
        assert cmd[idx + 1] == "128M"

    def test_transfers_default(self):
        cmd = _build()
        idx = cmd.index("--transfers")
        assert cmd[idx + 1] == "2"

    def test_transfers_custom(self):
        cmd = _build(transfers=8)
        idx = cmd.index("--transfers")
        assert cmd[idx + 1] == "8"

    def test_checkers_default(self):
        cmd = _build()
        idx = cmd.index("--checkers")
        assert cmd[idx + 1] == "4"

    def test_checkers_custom(self):
        cmd = _build(checkers=32)
        idx = cmd.index("--checkers")
        assert cmd[idx + 1] == "32"

    def test_storage_class(self):
        cmd = _build(storage_class="ARCHIVE")
        idx = cmd.index("--gcs-storage-class")
        assert cmd[idx + 1] == "ARCHIVE"

    def test_bwlimit_default(self):
        cmd = _build()
        idx = cmd.index("--bwlimit")
        assert cmd[idx + 1] == "10M"

    def test_bwlimit_custom(self):
        """The tunable parameter is named bwlimit (not bandwidth_limit)."""
        cmd = _build(bwlimit="50M")
        idx = cmd.index("--bwlimit")
        assert cmd[idx + 1] == "50M"

    def test_retries_default(self):
        cmd = _build()
        idx = cmd.index("--retries")
        assert cmd[idx + 1] == "3"

    def test_retries_custom(self):
        cmd = _build(retries=5)
        idx = cmd.index("--retries")
        assert cmd[idx + 1] == "5"

    def test_error_on_no_transfer_present(self):
        cmd = _build()
        assert "--error-on-no-transfer" in cmd

    def test_fast_list_present(self):
        cmd = _build()
        assert "--fast-list" in cmd

    def test_gcs_no_check_bucket_present(self):
        cmd = _build()
        assert "--gcs-no-check-bucket" in cmd

    def test_check_first_present(self):
        cmd = _build()
        assert "--check-first" in cmd

    def test_use_json_log_present(self):
        cmd = _build()
        assert "--use-json-log" in cmd

    def test_log_level_info(self):
        cmd = _build()
        idx = cmd.index("--log-level")
        assert cmd[idx + 1] == "INFO"

    def test_stats_60s(self):
        cmd = _build()
        idx = cmd.index("--stats")
        assert cmd[idx + 1] == "60s"

    def test_retries_sleep_30s(self):
        cmd = _build()
        idx = cmd.index("--retries-sleep")
        assert cmd[idx + 1] == "30s"

    def test_no_max_delete(self):
        cmd = _build()
        assert "--max-delete" not in cmd

    def test_no_track_renames(self):
        cmd = _build()
        assert "--track-renames" not in cmd

    def test_max_duration_flag(self):
        cmd = _build(max_duration_seconds=3600)
        assert "--max-duration" in cmd
        idx = cmd.index("--max-duration")
        assert cmd[idx + 1] == "3600s"
        # SOFT lets the in-flight transfer finish and preserves .partial state.
        assert "--cutoff-mode" in cmd
        assert cmd[cmd.index("--cutoff-mode") + 1] == "SOFT"

    def test_no_max_duration_when_zero(self):
        cmd = _build(max_duration_seconds=0)
        assert "--max-duration" not in cmd

    def test_no_max_duration_when_none(self):
        cmd = _build(max_duration_seconds=None)
        assert "--max-duration" not in cmd


# ── scan_rclone_log_for_errors → (has_error_signal, error_tail) ───────────

class TestScanRcloneLogForErrors:
    def test_clean_log(self):
        result = scan_rclone_log_for_errors("INFO  : file copied\n")
        assert result == (False, "")

    def test_error_in_log(self):
        result = scan_rclone_log_for_errors("ERROR : failed to copy\n")
        has_error, tail = result
        assert has_error is True
        assert "failed to copy" in tail

    def test_fatal_in_log(self):
        """A FATAL line is only an error signal via the plaintext fatal markers
        (log.Fatalf paths bypass the JSON logger) — "FATAL : fatal error"
        carries no marker and is NOT flagged."""
        result = scan_rclone_log_for_errors("FATAL : fatal error\n")
        assert result == (False, "")

    def test_json_error_entry_is_flagged(self):
        """JSON-log ERROR/CRITICAL entries are the primary signal."""
        line = '{"level": "ERROR", "msg": "Failed to delete: boom"}'
        has_error, tail = scan_rclone_log_for_errors(line)
        assert has_error is True
        assert "boom" in tail

    def test_json_critical_entry_is_flagged(self):
        line = '{"level": "critical", "msg": "dead"}'
        has_error, tail = scan_rclone_log_for_errors(line)
        assert has_error is True
        assert "dead" in tail

    def test_json_info_entry_not_flagged(self):
        line = '{"level": "INFO", "msg": "There was nothing to transfer"}'
        assert scan_rclone_log_for_errors(line) == (False, "")

    def test_empty_log(self):
        result = scan_rclone_log_for_errors("")
        assert result == (False, "")

    def test_none_log(self):
        assert scan_rclone_log_for_errors(None) == (False, "")


# ── run_cloud_sync — subprocess orchestration ────────────────────────────

class TestRunCloudSyncSuccess:
    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_0_cloud_complete(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_COMPLETE"
        assert result["exit_code"] == 0
        assert result["error"] is None
        assert result["suspect_reason"] is None

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_9_no_changes(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(9)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_NO_CHANGES_COMPLETE"
        assert result["exit_code"] == 9
        assert result["error"] is None

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_4_partial(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(4)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_PARTIAL"
        assert result["exit_code"] == 4

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_5_partial(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(5)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_PARTIAL"
        assert result["exit_code"] == 5

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_10_partial(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(10)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_PARTIAL"
        assert result["exit_code"] == 10


class TestRunCloudSyncStderr:
    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
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
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
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
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_unreadable_uses_fallback(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(1)
        mock_path.return_value.read_text.side_effect = OSError("permission denied")
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert "stderr unreadable" in result["error"]

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_9_clean_log_stays_no_changes(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        """P1-EXIT9: exit 9 + clean log stream = genuine no-changes success."""
        mock_run.return_value = _mock_result(9)
        mock_path.return_value.read_text.return_value = (
            '{"level":"INFO","msg":"There was nothing to transfer"}\n'
        )
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_NO_CHANGES_COMPLETE"
        assert result["error"] is None

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_9_with_fatal_log_reclassified_failed(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        """P1-EXIT9: exit 9 + fatal signals = CLOUD_FAILED, never NO_CHANGES."""
        mock_run.return_value = _mock_result(9)
        mock_path.return_value.read_text.return_value = (
            '{"level":"ERROR","msg":"Failed to create file system for \\"aam_gcs:bucket\\": '
            'bucket doesn\'t exist"}\n'
        )
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_FAILED"
        assert result["exit_code"] == 9
        assert "reclassified" in result["error"]

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_exit_0_with_error_log_reclassified_suspect(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        """C-DK-001: exit 0 contradicted by ERROR/CRITICAL signals → CLOUD_SUSPECT."""
        mock_run.return_value = _mock_result(0)
        mock_path.return_value.read_text.return_value = (
            '{"level":"ERROR","msg":"Failed to update checksum"}\n'
        )
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_SUSPECT"
        assert result["exit_code"] == 0
        assert result["suspect_reason"] is not None
        assert "CLOUD_SUSPECT" in result["suspect_reason"]


class TestRunCloudSyncExceptions:
    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_timeout_expired(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE", timeout=300)
        assert result["status"] == "CLOUD_FAILED"
        assert result["exit_code"] == -1
        assert "Timeout after 300s" in result["error"]
        assert "resumable" in result["error"]

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_file_not_found_rclone(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.side_effect = FileNotFoundError("rclone not on PATH")
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        assert result["status"] == "CLOUD_FAILED"
        assert result["exit_code"] == -1
        assert "rclone not found" in result["error"]

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
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
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_cleaned_up_on_success(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        mock_path.return_value.unlink.assert_called_once()

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_cleaned_up_on_timeout(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        mock_path.return_value.unlink.assert_called_once()

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_cleaned_up_on_file_not_found(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.side_effect = FileNotFoundError("rclone missing")
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        mock_path.return_value.unlink.assert_called_once()

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.Path")
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_cleaned_up_on_os_error(self, mock_cfg, mock_run, mock_path, mock_mkstemp, mock_close):
        mock_run.side_effect = OSError("io error")
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        mock_path.return_value.unlink.assert_called_once()

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
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
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_return_has_status_exit_code_error(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        # suspect_reason is the C-DK-001 hardening field (None on clean runs).
        assert set(result.keys()) == {"status", "exit_code", "error", "suspect_reason"}
        assert result["suspect_reason"] is None

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_return_has_status_exit_code_error_on_failure(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(7)
        mock_path_patcher = patch("core.cloud_sync.Path")
        mock_path = mock_path_patcher.start()
        mock_path.return_value.read_text.return_value = "fatal error"
        try:
            result = run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
            assert set(result.keys()) == {"status", "exit_code", "error", "suspect_reason"}
        finally:
            mock_path_patcher.stop()


class TestRunCloudSyncTimeoutPassthrough:
    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_default_timeout_18000(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 18000

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_custom_timeout_passed(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE", timeout=7200)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 7200


class TestRunCloudSyncEnvAndSubprocess:
    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_subprocess_run_called_with_cmd(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "rclone"
        assert cmd[1] == "sync"

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stdout_devnull(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["stdout"] == subprocess.DEVNULL

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_stderr_to_file(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["stderr"] is not None

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_text_mode(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["text"] is True

    @patch("core.cloud_sync.os.close")
    @patch("core.cloud_sync.tempfile.mkstemp", return_value=(0, _DUMMY_STDERR_PATH))
    @patch("core.cloud_sync.subprocess.run")
    @patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config)
    def test_buffer_size_flows_to_command(self, mock_cfg, mock_run, mock_mkstemp, mock_close):
        mock_run.return_value = _mock_result(0)
        run_cloud_sync("/src", "bucket", "FY", "/key", "123", "COLDLINE", buffer_size="256M")
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--buffer-size")
        assert cmd[idx + 1] == "256M"


# -- M7: binary resolution -----------------------------------------------------

class TestBinaryResolution:
    """M7 regression: the sync command must honor resolve_binary() like every
    other rclone-calling module (preflight, verify, size, manifest, diff).
    Original bug: cmd[0] was hardcoded to "rclone", so a bundled-only binary
    passed preflight but failed sync with FileNotFoundError."""

    def test_bundled_binary_preferred(self):
        exe = os.path.join("deploy", "bin", "rclone.exe")
        with patch("core.cloud_sync.resolve_binary", create=True, return_value=exe):
            cmd = _build()
        assert cmd[0] == exe

    def test_falls_back_to_path_name_when_unresolved(self):
        with patch("core.cloud_sync.resolve_binary", create=True, return_value=None):
            cmd = _build()
        assert cmd[0] == "rclone"

    def test_resolution_applies_inside_run_cloud_sync(self):
        """End-to-end: run_cloud_sync must execute the RESOLVED binary."""
        exe = os.path.join("custom", "rclone")

        def _fake_cm(*args, **kwargs):
            cm = MagicMock()
            cm.__enter__.return_value = os.path.join(
                tempfile.gettempdir(), "rclone_test.conf"
            )
            cm.__exit__.return_value = False
            return cm

        with patch("core.cloud_sync.resolve_binary", create=True, return_value=exe), \
             patch("core.cloud_sync.temp_rclone_config", _fake_cm), \
             patch("core.cloud_sync.subprocess.run",
                   return_value=_mock_result(0)) as mock_run:
            run_cloud_sync(
                source="D:\\data", bucket="b", fy_prefix="FY26-27",
                gcs_key_path="k.json", project_number="1",
                storage_class="COLDLINE",
            )
        assert mock_run.call_args[0][0][0] == exe
