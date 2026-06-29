"""Comprehensive tests for cloud_verify — full coverage of every public function and path."""

import subprocess
from unittest.mock import MagicMock, patch

from core.cloud_verify import _build_error_message, verify_cloud_integrity

# ── Helpers ────────────────────────────────────────────────────────────────

def _mock_result(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# ── verify_cloud_integrity — every exit code path ────────────────────────

class TestVerifyExitCodeMapping:
    @patch("core.cloud_verify.subprocess.run")
    def test_exit_0_verified(self, mock_run):
        mock_run.return_value = _mock_result(0)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is True
        assert result["exit_code"] == 0
        assert result["error"] is None

    @patch("core.cloud_verify.subprocess.run")
    def test_exit_1_mismatch(self, mock_run):
        mock_run.return_value = _mock_result(1, stderr="files differ")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == 1
        assert "Integrity mismatch" in result["error"]

    @patch("core.cloud_verify.subprocess.run")
    def test_exit_2_rclone_error(self, mock_run):
        mock_run.return_value = _mock_result(2, stderr="auth failure")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == 2
        assert "exit code 2" in result["error"]

    @patch("core.cloud_verify.subprocess.run")
    def test_exit_3_rclone_error(self, mock_run):
        mock_run.return_value = _mock_result(3, stderr="not found")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == 3
        assert "exit code 3" in result["error"]

    @patch("core.cloud_verify.subprocess.run")
    def test_exit_10_rclone_error(self, mock_run):
        mock_run.return_value = _mock_result(10, stderr="limit exceeded")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == 10
        assert "exit code 10" in result["error"]


class TestVerifyResolveBinary:
    @patch("core.cloud_verify.resolve_binary", return_value="/usr/local/bin/rclone")
    @patch("core.cloud_verify.subprocess.run")
    def test_resolved_binary_used(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(0)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/rclone"

    @patch("core.cloud_verify.resolve_binary", return_value=None)
    @patch("core.cloud_verify.subprocess.run")
    def test_fallback_to_rclone_string(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(0)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "rclone"

    @patch("core.cloud_verify.resolve_binary", return_value="/custom/rclone")
    @patch("core.cloud_verify.subprocess.run")
    def test_resolved_path_in_check_command(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/custom/rclone"
        assert cmd[1] == "check"


class TestVerifyCommandFlags:
    @patch("core.cloud_verify.subprocess.run")
    def test_modify_window_2s(self, mock_run):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert "--modify-window" in cmd
        idx = cmd.index("--modify-window")
        assert cmd[idx + 1] == "2s"

    @patch("core.cloud_verify.subprocess.run")
    def test_one_way_flag(self, mock_run):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert "--one-way" in cmd

    @patch("core.cloud_verify.subprocess.run")
    def test_fast_list_flag(self, mock_run):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert "--fast-list" in cmd

    @patch("core.cloud_verify.subprocess.run")
    def test_size_only_flag(self, mock_run):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert "--size-only" in cmd

    @patch("core.cloud_verify.subprocess.run")
    def test_checkers_4(self, mock_run):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--checkers")
        assert cmd[idx + 1] == "4"

    @patch("core.cloud_verify.subprocess.run")
    def test_config_flag(self, mock_run):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/my/config.conf")
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--config")
        assert cmd[idx + 1] == "/my/config.conf"

    @patch("core.cloud_verify.subprocess.run")
    def test_gcs_no_check_bucket(self, mock_run):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert "--gcs-no-check-bucket" in cmd

    @patch("core.cloud_verify.subprocess.run")
    def test_dest_includes_bucket_and_prefix(self, mock_run):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "my-bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert any("my-bucket/FY26-27" in arg for arg in cmd)

    @patch("core.cloud_verify.subprocess.run")
    def test_no_transfers_flag(self, mock_run):
        """--transfers is a no-op on rclone check — must not be present."""
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert "--transfers" not in cmd

    @patch("core.cloud_verify.subprocess.run")
    def test_no_check_first_flag(self, mock_run):
        """--check-first is a no-op on rclone check — must not be present."""
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert "--check-first" not in cmd


class TestVerifyTempConfig:
    @patch("core.cloud_verify.subprocess.run")
    def test_temp_config_used(self, mock_run):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/tmp/test.conf")
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--config")
        assert cmd[idx + 1] == "/tmp/test.conf"


class TestVerifyTimeoutPassthrough:
    @patch("core.cloud_verify.subprocess.run")
    def test_default_timeout_14400(self, mock_run):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 14400

    @patch("core.cloud_verify.subprocess.run")
    def test_custom_timeout(self, mock_run):
        mock_run.return_value = _mock_result(0)
        verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg", timeout=3600)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 3600


class TestVerifyExceptions:
    @patch("core.cloud_verify.subprocess.run")
    def test_timeout_expired(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=14400)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == -1
        assert "Timeout after 14400s" in result["error"]

    @patch("core.cloud_verify.subprocess.run")
    def test_file_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("rclone missing")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == -1
        assert "rclone not found" in result["error"]

    @patch("core.cloud_verify.subprocess.run")
    def test_os_error(self, mock_run):
        mock_run.side_effect = OSError("Permission denied")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == -1
        assert "Permission denied" in result["error"]


class TestVerifyReturnStructure:
    @patch("core.cloud_verify.subprocess.run")
    def test_return_keys_on_success(self, mock_run):
        mock_run.return_value = _mock_result(0)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert set(result.keys()) == {"verified", "exit_code", "error"}

    @patch("core.cloud_verify.subprocess.run")
    def test_return_keys_on_failure(self, mock_run):
        mock_run.return_value = _mock_result(1, stderr="mismatch")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert set(result.keys()) == {"verified", "exit_code", "error"}

    @patch("core.cloud_verify.subprocess.run")
    def test_return_keys_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=600)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert set(result.keys()) == {"verified", "exit_code", "error"}


class TestVerifyStderrHandling:
    @patch("core.cloud_verify.subprocess.run")
    def test_stderr_present_on_mismatch(self, mock_run):
        mock_run.return_value = _mock_result(1, stderr="some stderr output")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False

    @patch("core.cloud_verify.subprocess.run")
    def test_empty_stderr_on_mismatch(self, mock_run):
        mock_run.return_value = _mock_result(1, stderr="")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["error"] == "Integrity mismatch — source and GCS file counts or sizes differ"

    @patch("core.cloud_verify.subprocess.run")
    def test_none_stderr_on_error(self, mock_run):
        mock_run.return_value = _mock_result(2, stderr=None)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False


# ── _build_error_message — unit tests ────────────────────────────────────

class TestBuildErrorMessage:
    def test_exit_0_returns_none(self):
        assert _build_error_message(0) is None

    def test_exit_1_returns_mismatch_message(self):
        msg = _build_error_message(1)
        assert "Integrity mismatch" in msg

    def test_exit_2_returns_rclone_error(self):
        msg = _build_error_message(2)
        assert "exit code 2" in msg

    def test_exit_5_returns_rclone_error(self):
        msg = _build_error_message(5)
        assert "exit code 5" in msg

    def test_exit_99_returns_rclone_error(self):
        msg = _build_error_message(99)
        assert "exit code 99" in msg
