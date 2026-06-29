"""Comprehensive tests for core/lan_sync.py — robocopy /MIR wrapper, exit code classification, log tails."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.lan_sync import (
    _read_log_tail,
    _validate_required_flags,
    build_robocopy_command,
    classify_exit_code,
    run_lan_sync,
)
from models.config import LanConfig


def _make_lan_config(**kwargs):
    defaults = dict(
        enabled=True,
        retry_count=3,
        retry_wait_seconds=10,
        subprocess_timeout_seconds=14400,
        shutdown_after_backup=True,
        max_attempts=2,
        retry_delay_seconds=600,
        mt_threads=8,
    )
    defaults.update(kwargs)
    return LanConfig(**defaults)


# ═══════════════════════════════════════════════════════════════
# 1. classify_exit_code
# ═══════════════════════════════════════════════════════════════

class TestClassifyExitCode:
    """Official Microsoft bitmask rules."""

    @pytest.mark.parametrize("code,expected", [
        (0, "LAN_COMPLETE"),
        (1, "LAN_COMPLETE"),
        (2, "LAN_COMPLETE"),
        (3, "LAN_COMPLETE"),
    ])
    def test_codes_0_to_3_complete(self, code, expected):
        assert classify_exit_code(code) == expected

    @pytest.mark.parametrize("code,expected", [
        (4, "LAN_PARTIAL"),
        (5, "LAN_PARTIAL"),
        (6, "LAN_PARTIAL"),
        (7, "LAN_PARTIAL"),
    ])
    def test_codes_4_to_7_partial(self, code, expected):
        assert classify_exit_code(code) == expected

    @pytest.mark.parametrize("code,expected", [
        (8, "LAN_PARTIAL"),
        (9, "LAN_PARTIAL"),
        (10, "LAN_PARTIAL"),
        (15, "LAN_PARTIAL"),
    ])
    def test_codes_8_to_15_partial(self, code, expected):
        assert classify_exit_code(code) == expected

    def test_code_16_failed(self):
        assert classify_exit_code(16) == "LAN_FAILED"

    def test_code_32_failed(self):
        assert classify_exit_code(32) == "LAN_FAILED"

    def test_negative_code_failed(self):
        assert classify_exit_code(-1) == "LAN_FAILED"

    def test_code_17_failed(self):
        """16+ → bit 4 set → LAN_FAILED."""
        assert classify_exit_code(17) == "LAN_FAILED"

    def test_bit_3_and_bit_0(self):
        """9 = 8+1 → bit 3 set → LAN_PARTIAL."""
        assert classify_exit_code(9) == "LAN_PARTIAL"


# ═══════════════════════════════════════════════════════════════
# 2. build_robocopy_command
# ═══════════════════════════════════════════════════════════════

class TestBuildRobocopyCommand:
    """Build robocopy /MIR command with production flags."""

    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_all_flags_present(self, mock_resolve):
        cfg = _make_lan_config(mt_threads=8, retry_count=5, retry_wait_seconds=15)
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert "/MIR" in cmd
        assert "/Z" in cmd
        assert "/XJ" in cmd
        assert "/MT:8" in cmd
        assert "/R:5" in cmd
        assert "/W:15" in cmd
        assert "/NP" in cmd
        assert "/NDL" in cmd
        assert "/NJH" in cmd
        assert "/NJS" in cmd
        assert "/TS" in cmd
        assert "/FP" in cmd
        assert "/V" in cmd
        assert "/ZB" in cmd

    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_source_and_dest_in_command(self, mock_resolve):
        cfg = _make_lan_config()
        cmd = build_robocopy_command("E:\\SOURCE", "\\\\NAS\\Backups", cfg)

        assert cmd[1] == "E:\\SOURCE"
        assert cmd[2] == "\\\\NAS\\Backups"

    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_excludes_system_volume_info(self, mock_resolve):
        cfg = _make_lan_config()
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert "System Volume Information" in cmd

    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_excludes_recycle_bin(self, mock_resolve):
        cfg = _make_lan_config()
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert "$RECYCLE.BIN" in cmd

    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_excludes_aam_target_mounted(self, mock_resolve):
        cfg = _make_lan_config()
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert ".AAM_TARGET_MOUNTED" in cmd

    @patch("core.lan_sync.resolve_binary", return_value="/usr/bin/robocopy")
    def test_uses_resolved_binary(self, mock_resolve):
        cfg = _make_lan_config()
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert cmd[0] == "/usr/bin/robocopy"

    @patch("core.lan_sync.resolve_binary", return_value=None)
    def test_fallback_to_robocopy(self, mock_resolve):
        cfg = _make_lan_config()
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert cmd[0] == "robocopy"

    def test_nc_flag_forbidden(self):
        with pytest.raises(ValueError, match="/NC"):
            _validate_required_flags(["/MIR", "/NC"])


# ═══════════════════════════════════════════════════════════════
# 3. _read_log_tail
# ═══════════════════════════════════════════════════════════════

class TestReadLogTail:
    """Read tail of robocopy log file."""

    def test_short_log(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("line1\nline2\nline3")
        result = _read_log_tail(log, 10000)
        assert result == "line1\nline2\nline3"

    def test_long_log_returns_tail(self, tmp_path):
        log = tmp_path / "test.log"
        content = "x" * 200_000
        log.write_text(content)
        result = _read_log_tail(log, 100_000)
        assert len(result) == 100_000

    def test_unreadable_file(self, tmp_path):
        log = tmp_path / "nonexistent.log"
        result = _read_log_tail(log, 10000)
        assert "robocopy log unreadable" in result


# ═══════════════════════════════════════════════════════════════
# 4. run_lan_sync
# ═══════════════════════════════════════════════════════════════

class TestRunLanSync:
    """Execute robocopy /MIR mirror sync."""

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_exit_0_lan_complete(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _make_lan_config()

        result = run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert result["status"] == "LAN_COMPLETE"
        assert result["exit_code"] == 0
        assert result["error"] is None
        assert result["anomaly_details"] is None

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_exit_1_lan_complete(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        cfg = _make_lan_config()

        result = run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert result["status"] == "LAN_COMPLETE"
        assert result["exit_code"] == 1

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_exit_2_lan_complete(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=2)
        cfg = _make_lan_config()

        result = run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert result["status"] == "LAN_COMPLETE"
        assert result["exit_code"] == 2

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_exit_3_lan_complete(self, mock_resolve, mock_run):
        """3 = bits 0+1 → files copied + extras."""
        mock_run.return_value = MagicMock(returncode=3)
        cfg = _make_lan_config()

        result = run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert result["status"] == "LAN_COMPLETE"

    @patch("core.lan_sync._read_log_tail", return_value="error log tail")
    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_exit_8_lan_partial_with_error(self, mock_resolve, mock_run, mock_tail):
        mock_run.return_value = MagicMock(returncode=8)
        cfg = _make_lan_config()

        result = run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert result["status"] == "LAN_PARTIAL"
        assert result["error"] == "error log tail"

    @patch("core.lan_sync._read_log_tail", return_value="anomaly log")
    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_exit_4_lan_partial_with_anomaly(self, mock_resolve, mock_run, mock_tail):
        mock_run.return_value = MagicMock(returncode=4)
        cfg = _make_lan_config()

        result = run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert result["status"] == "LAN_PARTIAL"
        assert result["anomaly_details"] == "anomaly log"
        assert result["error"] is None

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_exit_16_lan_failed(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=16)
        cfg = _make_lan_config()

        result = run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert result["status"] == "LAN_FAILED"

    @patch("core.lan_sync.subprocess.run", side_effect=subprocess.TimeoutExpired("robocopy", 14400))
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_timeout(self, mock_resolve, mock_run):
        cfg = _make_lan_config(subprocess_timeout_seconds=14400)

        result = run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert result["status"] == "LAN_FAILED"
        assert result["exit_code"] == -1
        assert "Timeout" in result["error"]

    @patch("core.lan_sync.subprocess.run", side_effect=FileNotFoundError)
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_filenotfound(self, mock_resolve, mock_run):
        cfg = _make_lan_config()

        result = run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert result["status"] == "LAN_FAILED"
        assert result["exit_code"] == -1
        assert "not found" in result["error"].lower()

    @patch("core.lan_sync.subprocess.run", side_effect=OSError("network error"))
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_oserror(self, mock_resolve, mock_run):
        cfg = _make_lan_config()

        result = run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert result["status"] == "LAN_FAILED"
        assert result["exit_code"] == -1
        assert "network error" in result["error"]

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_return_structure_keys(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _make_lan_config()

        result = run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert set(result.keys()) == {"status", "exit_code", "error", "anomaly_details"}

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_creates_temp_log_file(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _make_lan_config()

        run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        # Verify subprocess was called with /LOG: flag
        cmd = mock_run.call_args[0][0]
        log_args = [a for a in cmd if a.startswith("/LOG:")]
        assert len(log_args) == 1

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_stdout_stderr_are_devnull(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _make_lan_config()

        run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert mock_run.call_args[1]["stdout"] == subprocess.DEVNULL
        assert mock_run.call_args[1]["stderr"] == subprocess.DEVNULL

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_uses_subprocess_timeout(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _make_lan_config(subprocess_timeout_seconds=7200)

        run_lan_sync("D:\\", "\\\\10.0.0.5\\share", cfg)

        assert mock_run.call_args[1]["timeout"] == 7200
