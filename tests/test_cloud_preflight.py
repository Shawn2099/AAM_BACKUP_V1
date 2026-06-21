"""Tests for cloud_preflight — two-probe preflight (source drive + GCS auth).

Test matrix:
    Probe A (source drive):
        - source path does not exist → ok=False, no rclone called
        - source path exists but iterdir raises OSError → ok=False
        - source path is empty (StopIteration) → continues to Probe B
        - source path is accessible → continues to Probe B

    Probe B (rclone lsjson GCS probe):
        - exit 0 → ok=True
        - exit non-zero → ok=False with exit_code preserved
        - TimeoutExpired → ok=False, "Timeout" in error
        - FileNotFoundError → ok=False, "rclone not found" in error
        - OSError → ok=False

    Command shape assertions:
        - uses "lsjson" subcommand (not "check")
        - includes "--max-depth" "0"
        - does NOT include "--gcs-no-check-bucket"
        - does NOT include source path in rclone args

    Integration:
        - temp config is always cleaned up (context manager respected)
"""

from contextlib import contextmanager
from unittest.mock import patch, MagicMock, call
import subprocess

import pytest

from core.cloud_preflight import run_cloud_dry_run


# ── Helpers ────────────────────────────────────────────────────────────────

def _mock_result(returncode=0, stdout="[]", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


@contextmanager
def _mock_cfg(*args, **kwargs):
    yield "/tmp/rclone_test.conf"


# Shared call args for a standard call
_CALL_ARGS = ("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")


def _make_accessible_path_mock():
    """Returns a mock Path instance that looks like an accessible, non-empty dir."""
    mock_path_inst = MagicMock()
    mock_path_inst.exists.return_value = True
    mock_path_inst.iterdir.return_value = iter([MagicMock()])  # one entry
    return mock_path_inst


# ── Probe A: Source drive checks ───────────────────────────────────────────

class TestProbeA:
    """Source drive reachability — pure Python, no rclone involved."""

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_source_not_exists_returns_error_no_rclone(self, mock_cfg, mock_run):
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = False
            mock_path_cls.return_value = mock_path_inst

            result = run_cloud_dry_run(*_CALL_ARGS)

        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "not accessible" in result["error"]
        mock_run.assert_not_called()

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_source_iterdir_oserror_returns_error(self, mock_cfg, mock_run):
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = True
            mock_path_inst.iterdir.side_effect = OSError("Permission denied")
            mock_path_cls.return_value = mock_path_inst

            result = run_cloud_dry_run(*_CALL_ARGS)

        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "read error" in result["error"]
        mock_run.assert_not_called()

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_empty_source_proceeds_to_probe_b(self, mock_cfg, mock_run):
        """StopIteration (empty drive) is valid — sync will handle it."""
        mock_run.return_value = _mock_result(0)

        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = True
            mock_path_inst.iterdir.return_value = iter([])  # empty
            mock_path_cls.return_value = mock_path_inst

            result = run_cloud_dry_run(*_CALL_ARGS)

        assert result["ok"] is True
        mock_run.assert_called_once()

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_accessible_source_proceeds_to_probe_b(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)

        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)

        assert result["ok"] is True
        mock_run.assert_called_once()


# ── Probe B: GCS rclone probe ─────────────────────────────────────────────

class TestProbeB:
    """GCS auth/bucket probe via rclone lsjson."""

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_exit_0_returns_ok(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is True
        assert result["exit_code"] == 0
        assert result["error"] is None

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_exit_nonzero_returns_error(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(5, stderr="AccessDenied")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == 5
        assert "AccessDenied" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_exit_3_bucket_not_found_returns_error(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(3, stderr="bucket not found")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == 3
        assert "bucket not found" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_timeout_returns_error(self, mock_cfg, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=30)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "Timeout" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_rclone_not_found_returns_error(self, mock_cfg, mock_run):
        mock_run.side_effect = FileNotFoundError
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "rclone not found" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_oserror_launching_rclone_returns_error(self, mock_cfg, mock_run):
        mock_run.side_effect = OSError("exec format error")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert result["error"] is not None

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_stderr_empty_on_failure(self, mock_cfg, mock_run):
        """Non-zero exit with no stderr should still return a structured error."""
        mock_run.return_value = _mock_result(5, stderr="")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["error"] is not None
        assert "no stderr" in result["error"]


# ── Command shape assertions ───────────────────────────────────────────────

class TestCommandShape:
    """Verify the exact rclone command built by the preflight."""

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_uses_lsjson_not_check(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        assert "lsjson" in cmd
        assert "check" not in cmd

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_max_depth_0_present(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        assert "--max-depth" in cmd
        depth_idx = cmd.index("--max-depth")
        assert cmd[depth_idx + 1] == "0"

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_no_gcs_no_check_bucket_flag(self, mock_cfg, mock_run):
        """Bucket existence MUST be validated — this flag must be absent."""
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        assert "--gcs-no-check-bucket" not in cmd

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_source_path_not_in_rclone_cmd(self, mock_cfg, mock_run):
        """Source drive must not be passed to rclone — no HDD scan."""
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        cmd = mock_run.call_args[0][0]
        assert "/src" not in cmd

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_dest_includes_bucket_and_fy_prefix(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run("/src", "my-bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        cmd = mock_run.call_args[0][0]
        assert any("my-bucket/FY26-27" in arg for arg in cmd)

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_config_flag_present(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        assert "--config" in cmd
        cfg_idx = cmd.index("--config")
        assert cmd[cfg_idx + 1] == "/tmp/rclone_test.conf"

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_no_one_way_flag(self, mock_cfg, mock_run):
        """--one-way was part of rclone check, must not appear in lsjson cmd."""
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        assert "--one-way" not in cmd


# ── Return dict contract ───────────────────────────────────────────────────

class TestReturnContract:
    """Verify the returned dict always has the expected keys."""

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_success_has_required_keys(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert "ok" in result
        assert "exit_code" in result
        assert "error" in result

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_no_matched_key_in_return(self, mock_cfg, mock_run):
        """'matched' is deprecated — must not be in return dict."""
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert "matched" not in result

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_failure_has_required_keys(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(5, stderr="auth error")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert "ok" in result
        assert "exit_code" in result
        assert "error" in result

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_custom_timeout_passed_to_subprocess(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS, timeout=45)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 45


# ── Config cleanup ─────────────────────────────────────────────────────────

class TestConfigCleanup:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_temp_config_context_manager_called(self, mock_cfg, mock_run):
        """Verify temp_rclone_config context manager is entered (and thus exited)."""
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        mock_cfg.assert_called_once()

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_config_cleaned_up_on_rclone_failure(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(5, stderr="fail")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        mock_cfg.assert_called_once()

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_config_cleaned_up_on_timeout(self, mock_cfg, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=30)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        mock_cfg.assert_called_once()
