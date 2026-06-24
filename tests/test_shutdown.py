"""Tests for shutdown — mock subprocess calls."""

from unittest.mock import patch, MagicMock
import subprocess

from core.shutdown import shutdown_server


def _mock_result(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestShutdownServer:
    @patch("core.shutdown.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = _mock_result(0)
        result = shutdown_server("192.168.10.10")
        assert result["shutdown_initiated"] is True
        assert result["server_ip"] == "192.168.10.10"
        assert result["error"] is None

    @patch("core.shutdown.subprocess.run")
    def test_failure_returns_error(self, mock_run):
        mock_run.return_value = _mock_result(1, stderr="Access denied")
        result = shutdown_server("192.168.10.10")
        assert result["shutdown_initiated"] is False
        assert "Access denied" in result["error"]

    @patch("core.shutdown.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="shutdown", timeout=30)
        result = shutdown_server("192.168.10.10")
        assert result["shutdown_initiated"] is False
        assert "timeout" in result["error"]

    @patch("core.shutdown.subprocess.run")
    def test_shutdown_exe_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        result = shutdown_server("192.168.10.10")
        assert result["shutdown_initiated"] is False
        assert "not found" in result["error"]
