"""Tests for cloud_preflight — mock subprocess calls."""

from contextlib import contextmanager
from unittest.mock import patch, MagicMock
import subprocess

from core.cloud_preflight import run_cloud_dry_run


def _mock_result(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


@contextmanager
def _mock_cfg(*args, **kwargs):
    yield "/tmp/rclone.conf"


class TestRunCloudDryRun:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_exit_0_returns_ok_matched(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        result = run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        assert result["ok"] is True
        assert result["matched"] is True
        assert result["exit_code"] == 0
        assert result["error"] is None

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_exit_1_returns_ok_not_matched(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(1)
        result = run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        assert result["ok"] is True
        assert result["matched"] is False
        assert result["exit_code"] == 1

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_exit_2_returns_not_ok(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(2, stderr="auth failed")
        result = run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        assert result["ok"] is False
        assert result["exit_code"] == 2
        assert "auth failed" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_timeout_returns_error(self, mock_cfg, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        result = run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        assert result["ok"] is False
        assert "Timeout" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_rclone_not_found(self, mock_cfg, mock_run):
        mock_run.side_effect = FileNotFoundError
        result = run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        assert result["ok"] is False
        assert "rclone not found" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_cleans_up_temp_config(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        result = run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        assert result["ok"] is True
