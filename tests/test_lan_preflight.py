"""Tests for lan_preflight — mock subprocess calls."""

import subprocess
from unittest.mock import MagicMock, patch

from core.lan_preflight import run_lan_dry_run


def _mock_result(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestRunLanDryRun:
    @patch("core.lan_preflight.Path.exists", return_value=True)
    @patch("core.lan_preflight.subprocess.run")
    def test_exit_0_returns_ok(self, mock_run, mock_exists):
        mock_run.return_value = _mock_result(0)
        result = run_lan_dry_run("/src", "\\\\server\\share")
        assert result["ok"] is True
        assert result["exit_code"] == 0
        assert result["error"] is None

    @patch("core.lan_preflight.Path.exists", return_value=True)
    @patch("core.lan_preflight.subprocess.run")
    def test_exit_7_returns_ok(self, mock_run, mock_exists):
        """Exit codes 0-7 are OK (bits 0-2 only)."""
        mock_run.return_value = _mock_result(7)
        result = run_lan_dry_run("/src", "\\\\server\\share")
        assert result["ok"] is True

    @patch("core.lan_preflight.Path.exists", return_value=True)
    @patch("core.lan_preflight.subprocess.run")
    def test_exit_8_returns_not_ok(self, mock_run, mock_exists):
        """Bit 3 (8) = copy errors."""
        mock_run.return_value = _mock_result(8)
        result = run_lan_dry_run("/src", "\\\\server\\share")
        assert result["ok"] is False
        assert result["exit_code"] == 8

    @patch("core.lan_preflight.Path.exists", return_value=True)
    @patch("core.lan_preflight.subprocess.run")
    def test_exit_16_returns_not_ok(self, mock_run, mock_exists):
        """Bit 4 (16) = fatal error."""
        mock_run.return_value = _mock_result(16)
        result = run_lan_dry_run("/src", "\\\\server\\share")
        assert result["ok"] is False

    @patch("core.lan_preflight.Path.exists", return_value=True)
    @patch("core.lan_preflight.subprocess.run")
    def test_timeout(self, mock_run, mock_exists):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="robocopy", timeout=300)
        result = run_lan_dry_run("/src", "\\\\server\\share")
        assert result["ok"] is False
        assert "Timeout" in result["error"]

    @patch("core.lan_preflight.Path.exists", return_value=True)
    @patch("core.lan_preflight.subprocess.run")
    def test_robocopy_not_found(self, mock_run, mock_exists):
        mock_run.side_effect = FileNotFoundError
        result = run_lan_dry_run("/src", "\\\\server\\share")
        assert result["ok"] is False
        assert "robocopy" in result["error"]
