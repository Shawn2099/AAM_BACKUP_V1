"""Tests for core/shutdown.py — shutdown_server()."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.shutdown import shutdown_server


class TestShutdownServer:
    def test_success(self):
        """Exit 0 → shutdown_initiated=True."""
        with patch("core.shutdown.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = shutdown_server("192.168.10.10")
            assert result["shutdown_initiated"] is True
            assert result["server_ip"] == "192.168.10.10"
            assert result["error"] is None

    def test_nonzero_exit(self):
        """Non-zero exit → shutdown_initiated=False with error."""
        with patch("core.shutdown.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Access denied")
            result = shutdown_server("192.168.10.10")
            assert result["shutdown_initiated"] is False
            assert "Access denied" in result["error"]

    def test_nonzero_exit_no_stderr(self):
        """Non-zero exit with no stderr → generic error message."""
        with patch("core.shutdown.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            result = shutdown_server("192.168.10.10")
            assert result["shutdown_initiated"] is False
            assert "exit code 1" in result["error"]

    def test_shutdown_exe_not_found(self):
        """FileNotFoundError → graceful failure."""
        with patch("core.shutdown.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("shutdown.exe not found")
            result = shutdown_server("192.168.10.10")
            assert result["shutdown_initiated"] is False
            assert "not found" in result["error"]

    def test_timeout(self):
        """TimeoutExpired → graceful failure."""
        with patch("core.shutdown.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="shutdown", timeout=30)
            result = shutdown_server("192.168.10.10")
            assert result["shutdown_initiated"] is False
            assert result["error"] == "timeout"

    def test_os_error(self):
        """OSError → graceful failure."""
        with patch("core.shutdown.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("Access is denied")
            result = shutdown_server("192.168.10.10")
            assert result["shutdown_initiated"] is False
            assert "Access is denied" in result["error"]

    def test_command_construction(self):
        """Command includes /s, /m, /t 300, /f."""
        with patch("core.shutdown.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            shutdown_server("10.0.0.5")
            cmd = mock_run.call_args[0][0]
            assert "shutdown" in cmd
            assert "/s" in cmd
            assert "/m" in cmd
            assert "\\\\10.0.0.5" in cmd
            assert "/t" in cmd
            assert "300" in cmd
            assert "/f" in cmd
