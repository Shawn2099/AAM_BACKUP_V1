"""Comprehensive tests for core/shutdown.py — remote shutdown command, return structure, error handling."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.shutdown import shutdown_server


# ═══════════════════════════════════════════════════════════════
# 1. Success paths
# ═══════════════════════════════════════════════════════════════

class TestShutdownSuccess:
    """Returncode 0 → shutdown_initiated=True."""

    @patch("core.shutdown.subprocess.run")
    def test_returncode_zero(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = shutdown_server("192.168.1.100")
        assert result["shutdown_initiated"] is True
        assert result["server_ip"] == "192.168.1.100"
        assert result["error"] is None

    @patch("core.shutdown.subprocess.run")
    def test_stderr_empty_on_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = shutdown_server("10.0.0.5")
        assert result["error"] is None

    @patch("core.shutdown.subprocess.run")
    def test_various_ips_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        for ip in ["10.0.0.1", "192.168.1.100", "172.16.0.50"]:
            result = shutdown_server(ip)
            assert result["server_ip"] == ip
            assert result["shutdown_initiated"] is True


# ═══════════════════════════════════════════════════════════════
# 2. Failure paths
# ═══════════════════════════════════════════════════════════════

class TestShutdownFailures:
    """Non-zero returncode → shutdown_initiated=False with error."""

    @patch("core.shutdown.subprocess.run")
    def test_nonzero_returncode_with_stderr(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="access denied")
        result = shutdown_server("192.168.1.100")
        assert result["shutdown_initiated"] is False
        assert result["error"] == "access denied"

    @patch("core.shutdown.subprocess.run")
    def test_nonzero_returncode_empty_stderr(self, mock_run):
        mock_run.return_value = MagicMock(returncode=5, stderr="")
        result = shutdown_server("192.168.1.100")
        assert result["shutdown_initiated"] is False
        assert result["error"] == "exit code 5"

    @patch("core.shutdown.subprocess.run")
    def test_nonzero_returncode_whitespace_stderr(self, mock_run):
        mock_run.return_value = MagicMock(returncode=5, stderr="  \n  ")
        result = shutdown_server("192.168.1.100")
        assert result["shutdown_initiated"] is False
        assert result["error"] == "exit code 5"

    @patch("core.shutdown.subprocess.run", side_effect=FileNotFoundError)
    def test_filenotfound(self, mock_run):
        result = shutdown_server("192.168.1.100")
        assert result["shutdown_initiated"] is False
        assert result["error"] == "shutdown.exe not found"

    @patch("core.shutdown.subprocess.run", side_effect=subprocess.TimeoutExpired("shutdown", 30))
    def test_timeout_expired(self, mock_run):
        result = shutdown_server("192.168.1.100")
        assert result["shutdown_initiated"] is False
        assert result["error"] == "timeout"

    @patch("core.shutdown.subprocess.run", side_effect=OSError("network unreachable"))
    def test_oserror(self, mock_run):
        result = shutdown_server("192.168.1.100")
        assert result["shutdown_initiated"] is False
        assert result["error"] == "network unreachable"


# ═══════════════════════════════════════════════════════════════
# 3. Command construction
# ═══════════════════════════════════════════════════════════════

class TestShutdownCommand:
    """Verify the shutdown command is constructed correctly."""

    @patch("core.shutdown.subprocess.run")
    def test_command_format(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        shutdown_server("10.0.0.1")
        cmd = mock_run.call_args[0][0]
        expected = ["shutdown", "/s", "/m", "\\\\10.0.0.1", "/t", "300", "/f"]
        assert cmd == expected

    @patch("core.shutdown.subprocess.run")
    def test_server_ip_in_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        shutdown_server("192.168.10.50")
        cmd = mock_run.call_args[0][0]
        m_index = cmd.index("/m")
        assert cmd[m_index + 1] == "\\\\192.168.10.50"

    @patch("core.shutdown.subprocess.run")
    def test_uses_capture_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        shutdown_server("10.0.0.1")
        assert mock_run.call_args[1]["capture_output"] is True

    @patch("core.shutdown.subprocess.run")
    def test_uses_text_mode(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        shutdown_server("10.0.0.1")
        assert mock_run.call_args[1]["text"] is True

    @patch("core.shutdown.subprocess.run")
    def test_timeout_30_seconds(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        shutdown_server("10.0.0.1")
        assert mock_run.call_args[1]["timeout"] == 30

    @patch("core.shutdown.subprocess.run")
    def test_five_minute_delay(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        shutdown_server("10.0.0.1")
        cmd = mock_run.call_args[0][0]
        t_index = cmd.index("/t")
        assert cmd[t_index + 1] == "300"


# ═══════════════════════════════════════════════════════════════
# 4. Return structure validation
# ═══════════════════════════════════════════════════════════════

class TestShutdownReturnStructure:
    """All paths must return dict with shutdown_initiated, server_ip, error."""

    @patch("core.shutdown.subprocess.run")
    def test_success_structure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = shutdown_server("192.168.1.100")
        assert set(result.keys()) == {"shutdown_initiated", "server_ip", "error"}

    @patch("core.shutdown.subprocess.run")
    def test_failure_structure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="err")
        result = shutdown_server("192.168.1.100")
        assert set(result.keys()) == {"shutdown_initiated", "server_ip", "error"}

    @patch("core.shutdown.subprocess.run", side_effect=FileNotFoundError)
    def test_filenotfound_structure(self, mock_run):
        result = shutdown_server("192.168.1.100")
        assert set(result.keys()) == {"shutdown_initiated", "server_ip", "error"}

    @patch("core.shutdown.subprocess.run", side_effect=subprocess.TimeoutExpired("shutdown", 30))
    def test_timeout_structure(self, mock_run):
        result = shutdown_server("192.168.1.100")
        assert set(result.keys()) == {"shutdown_initiated", "server_ip", "error"}

    @patch("core.shutdown.subprocess.run", side_effect=OSError("err"))
    def test_oserror_structure(self, mock_run):
        result = shutdown_server("192.168.1.100")
        assert set(result.keys()) == {"shutdown_initiated", "server_ip", "error"}

    @patch("core.shutdown.subprocess.run")
    def test_ip_preserved_on_all_paths(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        r1 = shutdown_server("192.168.1.200")
        assert r1["server_ip"] == "192.168.1.200"

    @patch("core.shutdown.subprocess.run", side_effect=FileNotFoundError)
    def test_ip_preserved_on_failure(self, mock_run):
        result = shutdown_server("192.168.1.200")
        assert result["server_ip"] == "192.168.1.200"


# ═══════════════════════════════════════════════════════════════
# 5. Edge cases
# ═══════════════════════════════════════════════════════════════

class TestShutdownEdgeCases:
    """Edge cases and boundary conditions."""

    @patch("core.shutdown.subprocess.run")
    def test_never_raises(self, mock_run):
        """shutdown_server never raises — always returns dict."""
        mock_run.side_effect = OSError("anything")
        result = shutdown_server("10.0.0.1")
        assert isinstance(result, dict)

    @patch("core.shutdown.subprocess.run")
    def test_localhost_ip(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = shutdown_server("127.0.0.1")
        assert result["server_ip"] == "127.0.0.1"
        assert result["shutdown_initiated"] is True
