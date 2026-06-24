"""Tests for lan_sync — robocopy command building, exit classification, and orchestration."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.lan_sync import _validate_required_flags, _read_log_tail, build_robocopy_command, classify_exit_code, run_lan_sync
from models.config import LanConfig


class TestClassifyExitCode:
    def test_zero_returns_complete(self):
        assert classify_exit_code(0) == "LAN_COMPLETE"

    def test_bit0_files_copied(self):
        assert classify_exit_code(1) == "LAN_COMPLETE"

    def test_bit1_extra_files(self):
        assert classify_exit_code(2) == "LAN_COMPLETE"

    def test_bit2_mismatched(self):
        assert classify_exit_code(4) == "LAN_PARTIAL"

    def test_bits_0_1_2_combined(self):
        assert classify_exit_code(7) == "LAN_PARTIAL"

    def test_bit3_copy_errors_returns_partial(self):
        assert classify_exit_code(8) == "LAN_PARTIAL"

    def test_bit3_with_bit0_returns_partial(self):
        assert classify_exit_code(9) == "LAN_PARTIAL"

    def test_bit4_fatal_error_returns_failed(self):
        assert classify_exit_code(16) == "LAN_FAILED"

    def test_bit4_combined_with_others(self):
        assert classify_exit_code(24) == "LAN_FAILED"  # 16 + 8

    def test_negative_code_returns_failed(self):
        assert classify_exit_code(-1) == "LAN_FAILED"


class TestValidateRequiredFlags:
    def test_nc_flag_raises(self):
        with pytest.raises(ValueError, match="/NC"):
            _validate_required_flags(["/MIR", "/NC"])

    def test_nc_lowercase_raises(self):
        with pytest.raises(ValueError, match="/NC"):
            _validate_required_flags(["/nc"])

    def test_nc_dash_raises(self):
        with pytest.raises(ValueError, match="/NC"):
            _validate_required_flags(["-NC"])

    def test_valid_flags_pass(self):
        _validate_required_flags(["/MIR", "/Z", "/XJ"])


class TestBuildRobocopyCommand:
    def test_basic_command_structure(self):
        cfg = LanConfig(retry_count=3, retry_wait_seconds=10, mt_threads=8)
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.1\\share", cfg)
        assert "robocopy" in cmd[0].lower()
        assert cmd[1] == "D:\\"
        assert cmd[2] == "\\\\10.0.0.1\\share"
        assert "/MIR" in cmd
        assert "/Z" in cmd
        assert "/XJ" in cmd

    def test_mt_flag_from_config(self):
        cfg = LanConfig(mt_threads=16)
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", cfg)
        assert "/MT:16" in cmd

    def test_mt_default_is_4(self):
        """Default /MT is 4 — matches 4 hardware threads on the target HDD server."""
        cfg = LanConfig()
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", cfg)
        assert "/MT:4" in cmd

    def test_retry_count_included(self):
        cfg = LanConfig(retry_count=5)
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", cfg)
        assert "/R:5" in cmd

    def test_retry_wait_included(self):
        cfg = LanConfig(retry_wait_seconds=30)
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", cfg)
        assert "/W:30" in cmd

    def test_no_nc_flag_present(self):
        cfg = LanConfig()
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", cfg)
        assert "/NC" not in [f.upper() for f in cmd]

    def test_system_volume_information_excluded(self):
        cfg = LanConfig()
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", cfg)
        xd_idx = cmd.index("/XD")
        assert cmd[xd_idx + 1] == "System Volume Information"


class TestRunLanSync:
    """Unit tests for the run_lan_sync subprocess orchestration."""

    @patch("core.lan_sync.os.close")
    @patch("core.lan_sync.tempfile.mkstemp", return_value=(99, "/tmp/robocopy_test.log"))
    @patch("core.lan_sync.Path")
    @patch("core.lan_sync.subprocess.run")
    def test_success_exit_0(self, mock_run, mock_path, mock_mkstemp, mock_close):
        cfg = LanConfig(subprocess_timeout_seconds=3600)
        mock_run.return_value = MagicMock(returncode=0)
        mock_path.return_value.exists.return_value = True
        result = run_lan_sync("/src", "\\\\server\\share", cfg)
        assert result["status"] == "LAN_COMPLETE"
        assert result["exit_code"] == 0
        assert result["error"] is None
        assert result["anomaly_details"] is None

    @patch("core.lan_sync.os.close")
    @patch("core.lan_sync.tempfile.mkstemp", return_value=(99, "/tmp/robocopy_test.log"))
    @patch("core.lan_sync.Path")
    @patch("core.lan_sync.subprocess.run")
    def test_exit_1_files_copied(self, mock_run, mock_path, mock_mkstemp, mock_close):
        cfg = LanConfig()
        mock_run.return_value = MagicMock(returncode=1)
        mock_path.return_value.exists.return_value = True
        result = run_lan_sync("/src", "\\\\server\\share", cfg)
        assert result["status"] == "LAN_COMPLETE"

    @patch("core.lan_sync.os.close")
    @patch("core.lan_sync.tempfile.mkstemp", return_value=(99, "/tmp/robocopy_test.log"))
    @patch("core.lan_sync.Path")
    @patch("core.lan_sync.subprocess.run")
    def test_exit_4_anomalies_no_error_field(self, mock_run, mock_path, mock_mkstemp, mock_close):
        """Code 4 (mismatches) — sync completed. error must be None (no alert).
        anomaly_details must be populated so operators can investigate."""
        cfg = LanConfig()
        mock_run.return_value = MagicMock(returncode=4)
        mock_path.return_value.exists.return_value = True
        mock_path.return_value.read_text.return_value = "Mismatch: file.bak size differs"
        result = run_lan_sync("/src", "\\\\server\\share", cfg)
        assert result["status"] == "LAN_PARTIAL"
        assert result["error"] is None, "code 4 must not trigger alerts"
        assert result["anomaly_details"] is not None, "anomaly context must be captured"
        assert "Mismatch" in result["anomaly_details"]

    @patch("core.lan_sync.os.close")
    @patch("core.lan_sync.tempfile.mkstemp", return_value=(99, "/tmp/robocopy_test.log"))
    @patch("core.lan_sync.Path")
    @patch("core.lan_sync.subprocess.run")
    def test_exit_8_copy_errors_has_error_field(self, mock_run, mock_path, mock_mkstemp, mock_close):
        """Code 8 (copy errors) — sync failed. error must contain log for triage.
        anomaly_details must be None (error field already carries the context)."""
        cfg = LanConfig()
        mock_run.return_value = MagicMock(returncode=8)
        mock_path.return_value.exists.return_value = True
        mock_path.return_value.read_text.return_value = "ERROR: File in use"
        result = run_lan_sync("/src", "\\\\server\\share", cfg)
        assert result["status"] == "LAN_PARTIAL"
        assert "File in use" in result["error"]
        assert result["anomaly_details"] is None, "real errors must not populate anomaly_details"

    @patch("core.lan_sync.os.close")
    @patch("core.lan_sync.tempfile.mkstemp", return_value=(99, "/tmp/robocopy_test.log"))
    @patch("core.lan_sync.Path")
    @patch("core.lan_sync.subprocess.run")
    def test_fatal_with_log_tail(self, mock_run, mock_path, mock_mkstemp, mock_close):
        cfg = LanConfig()
        mock_run.return_value = MagicMock(returncode=16)
        mock_path.return_value.exists.return_value = True
        mock_path.return_value.read_text.return_value = "ERROR: Access denied (0x00000005)"
        result = run_lan_sync("/src", "\\\\server\\share", cfg)
        assert result["status"] == "LAN_FAILED"
        assert "Access denied" in result["error"]

    @patch("core.lan_sync.os.close")
    @patch("core.lan_sync.tempfile.mkstemp", return_value=(99, "/tmp/robocopy_test.log"))
    @patch("core.lan_sync.Path")
    @patch("core.lan_sync.subprocess.run")
    def test_log_tail_truncation(self, mock_run, mock_path, mock_mkstemp, mock_close):
        cfg = LanConfig()
        mock_run.return_value = MagicMock(returncode=16)
        mock_path.return_value.exists.return_value = True
        long_log = "x" * 150000
        mock_path.return_value.read_text.return_value = long_log
        result = run_lan_sync("/src", "\\\\server\\share", cfg)
        assert len(result["error"]) == 100000
        assert result["anomaly_details"] is None

    @patch("core.lan_sync.os.close")
    @patch("core.lan_sync.tempfile.mkstemp", return_value=(99, "/tmp/robocopy_test.log"))
    @patch("core.lan_sync.Path")
    @patch("core.lan_sync.subprocess.run")
    def test_log_unreadable(self, mock_run, mock_path, mock_mkstemp, mock_close):
        cfg = LanConfig()
        mock_run.return_value = MagicMock(returncode=16)
        mock_path.return_value.exists.return_value = True
        mock_path.return_value.read_text.side_effect = OSError("bad file")
        result = run_lan_sync("/src", "\\\\server\\share", cfg)
        assert "log unreadable" in result["error"]

    @patch("core.lan_sync.os.close")
    @patch("core.lan_sync.tempfile.mkstemp", return_value=(99, "/tmp/robocopy_test.log"))
    @patch("core.lan_sync.Path")
    @patch("core.lan_sync.subprocess.run")
    def test_timeout_expired(self, mock_run, mock_path, mock_mkstemp, mock_close):
        cfg = LanConfig(subprocess_timeout_seconds=3600)
        mock_path.return_value.exists.return_value = True
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="robocopy", timeout=3600)
        result = run_lan_sync("/src", "\\\\server\\share", cfg)
        assert result["status"] == "LAN_FAILED"
        assert result["exit_code"] == -1
        assert "Timeout after 3600s" in result["error"]
        assert result["anomaly_details"] is None

    @patch("core.lan_sync.os.close")
    @patch("core.lan_sync.tempfile.mkstemp", return_value=(99, "/tmp/robocopy_test.log"))
    @patch("core.lan_sync.Path")
    @patch("core.lan_sync.subprocess.run")
    def test_robocopy_not_found(self, mock_run, mock_path, mock_mkstemp, mock_close):
        cfg = LanConfig()
        mock_path.return_value.exists.return_value = True
        mock_run.side_effect = FileNotFoundError("robocopy.exe missing")
        result = run_lan_sync("/src", "\\\\server\\share", cfg)
        assert result["status"] == "LAN_FAILED"
        assert "robocopy.exe not found" in result["error"]

    @patch("core.lan_sync.os.close")
    @patch("core.lan_sync.tempfile.mkstemp", return_value=(99, "/tmp/robocopy_test.log"))
    @patch("core.lan_sync.Path")
    @patch("core.lan_sync.subprocess.run")
    def test_os_error(self, mock_run, mock_path, mock_mkstemp, mock_close):
        cfg = LanConfig()
        mock_path.return_value.exists.return_value = True
        mock_run.side_effect = OSError("network unreachable")
        result = run_lan_sync("/src", "\\\\server\\share", cfg)
        assert result["status"] == "LAN_FAILED"
        assert result["error"] == "network unreachable"

    @patch("core.lan_sync.os.close")
    @patch("core.lan_sync.tempfile.mkstemp", return_value=(99, "/tmp/robocopy_test.log"))
    @patch("core.lan_sync.Path")
    @patch("core.lan_sync.subprocess.run")
    def test_log_cleaned_up_on_success(self, mock_run, mock_path, mock_mkstemp, mock_close):
        cfg = LanConfig()
        mock_run.return_value = MagicMock(returncode=0)
        mock_path.return_value.exists.return_value = True
        result = run_lan_sync("/src", "\\\\server\\share", cfg)
        assert result["status"] == "LAN_COMPLETE"
        mock_path.return_value.unlink.assert_called_once()


class TestReadLogTail:
    """Unit tests for the _read_log_tail helper — tested independently of run_lan_sync."""

    def test_short_log_returned_in_full(self, tmp_path):
        log = tmp_path / "robocopy.log"
        log.write_text("short log", encoding="utf-8")
        assert _read_log_tail(log, 100) == "short log"

    def test_long_log_truncated_to_max_bytes(self, tmp_path):
        log = tmp_path / "robocopy.log"
        log.write_text("x" * 200, encoding="utf-8")
        tail = _read_log_tail(log, 100)
        assert len(tail) == 100
        assert tail == "x" * 100

    def test_missing_file_returns_fallback_message(self, tmp_path):
        missing = tmp_path / "does_not_exist.log"
        result = _read_log_tail(missing, 1000)
        assert "log unreadable" in result

    def test_anomaly_tail_limited_to_100kb(self, tmp_path):
        """Anomaly log tail must be capped at _ANOMALY_LOG_TAIL (100000 bytes),
        matching the error log tail — full context preserved for forensics."""
        from core.lan_sync import _ANOMALY_LOG_TAIL
        log = tmp_path / "robocopy.log"
        log.write_text("a" * 200_000, encoding="utf-8")
        tail = _read_log_tail(log, _ANOMALY_LOG_TAIL)
        assert len(tail) == _ANOMALY_LOG_TAIL

    def test_error_tail_limited_to_100kb(self, tmp_path):
        """Error log tail must be capped at _ERROR_LOG_TAIL (100000 bytes)."""
        from core.lan_sync import _ERROR_LOG_TAIL
        log = tmp_path / "robocopy.log"
        log.write_text("e" * 200_000, encoding="utf-8")
        tail = _read_log_tail(log, _ERROR_LOG_TAIL)
        assert len(tail) == _ERROR_LOG_TAIL
