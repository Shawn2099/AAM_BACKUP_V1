"""Tests for lan_preflight — mock subprocess calls."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.lan_preflight import HealthError as LanHealthError
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


class TestCanaryAccessDeniedGuard:
    """M5/S2-12: the 2026-07-10/11 production preflight crashes were a raw
    PermissionError [WinError 5] escaping Path.exists() at the canary check
    — the NAS answered ACCESS_DENIED instead of NOT_FOUND, and CPython
    3.12.3's Path.exists() only swallows {ENFILE, EMFILE, ENOENT, ENOTDIR,
    ETIMEDOUT}, so WinError 5 re-raised and bypassed the G11 self-recovering
    HealthError (the one that carries the recovery command). The guard must
    convert any OSError from the exists() probe into that HealthError."""

    @patch("core.lan_preflight.subprocess.run")
    def test_access_denied_canary_raises_healtherror(self, mock_run, capture_logs):
        import pathlib

        class _DenyPath(pathlib.Path):
            def exists(self):
                raise PermissionError(13, "Access is denied")

        with patch("core.lan_preflight.Path", _DenyPath):
            with pytest.raises(LanHealthError, match="Canary"):
                run_lan_dry_run("E:\\source", "\\\\10.0.0.1\\share\\FY26-27")

        # M5: pre-fix a raw PermissionError escaped instead of this path
        mock_run.assert_not_called()  # no robocopy against an unproven destination
        logs = capture_logs.getvalue().lower()
        assert "canary" in logs
        assert "recovery" in logs  # the G11 self-recovery message is intact

    @patch("core.lan_preflight.subprocess.run")
    def test_missing_canary_still_healtherror(self, mock_run, capture_logs):
        """Regression guard: the plain missing-canary branch is unchanged."""
        import pathlib

        class _AbsentPath(pathlib.Path):
            def exists(self):
                return False

        with patch("core.lan_preflight.Path", _AbsentPath):
            with pytest.raises(LanHealthError, match="Canary"):
                run_lan_dry_run("E:\\source", "\\\\10.0.0.1\\share\\FY26-27")
        mock_run.assert_not_called()
