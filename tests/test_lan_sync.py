"""Tests for lan_sync — robocopy command building and exit code classification."""

import pytest

from core.lan_sync import _validate_required_flags, build_robocopy_command, classify_exit_code
from models.config import LanConfig


class TestClassifyExitCode:
    def test_zero_returns_complete(self):
        assert classify_exit_code(0) == "LAN_COMPLETE"

    def test_bit0_files_copied(self):
        assert classify_exit_code(1) == "LAN_COMPLETE"

    def test_bit1_extra_files(self):
        assert classify_exit_code(2) == "LAN_COMPLETE"

    def test_bit2_mismatched(self):
        assert classify_exit_code(4) == "LAN_COMPLETE"

    def test_bits_0_1_2_combined(self):
        assert classify_exit_code(7) == "LAN_COMPLETE"

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
        assert cmd[0] == "robocopy"
        assert cmd[1] == "D:\\"
        assert cmd[2] == "\\\\10.0.0.1\\share"
        assert "/MIR" in cmd
        assert "/Z" in cmd
        assert "/XJ" in cmd

    def test_mt_flag_from_config(self):
        cfg = LanConfig(mt_threads=16)
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", cfg)
        assert "/MT:16" in cmd

    def test_mt_default_is_8(self):
        cfg = LanConfig()
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", cfg)
        assert "/MT:8" in cmd

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
