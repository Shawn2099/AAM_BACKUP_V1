"""Tests for cloud_preflight — mock subprocess calls."""

from unittest.mock import patch, MagicMock
import subprocess

from core.cloud_preflight import run_cloud_dry_run


def _mock_result(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestRunCloudDryRun:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight._write_temp_config", return_value="/tmp/rclone.conf")
    def test_exit_0_returns_ok_matched(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        result = run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        assert result["ok"] is True
        assert result["matched"] is True
        assert result["exit_code"] == 0
        assert result["error"] is None

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight._write_temp_config", return_value="/tmp/rclone.conf")
    def test_exit_1_returns_ok_not_matched(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(1)
        result = run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        assert result["ok"] is True
        assert result["matched"] is False
        assert result["exit_code"] == 1

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight._write_temp_config", return_value="/tmp/rclone.conf")
    def test_exit_2_returns_not_ok(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(2, stderr="auth failed")
        result = run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        assert result["ok"] is False
        assert result["exit_code"] == 2
        assert "auth failed" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight._write_temp_config", return_value="/tmp/rclone.conf")
    def test_timeout_returns_error(self, mock_cfg, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        result = run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        assert result["ok"] is False
        assert "Timeout" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight._write_temp_config", return_value="/tmp/rclone.conf")
    def test_rclone_not_found(self, mock_cfg, mock_run):
        mock_run.side_effect = FileNotFoundError
        result = run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        assert result["ok"] is False
        assert "rclone not found" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight._write_temp_config", return_value="/tmp/rclone.conf")
    def test_cleans_up_temp_config(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path:
            mock_path.return_value.unlink = MagicMock()
            run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
