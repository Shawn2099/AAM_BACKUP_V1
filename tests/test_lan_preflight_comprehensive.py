"""Comprehensive tests for core/lan_preflight.py — robocopy /L dry-run, canary check."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.lan_preflight import HealthError, run_lan_dry_run

# ═══════════════════════════════════════════════════════════════
# 1. Preflight check: ok=True
# ═══════════════════════════════════════════════════════════════

class TestPreflightSuccess:
    """Source exists + NAS accessible + canary exists → ok=True."""

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_all_conditions_met(self, mock_path_cls, mock_resolve, mock_run, tmp_path):
        # Canary file exists
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")

        assert result["ok"] is True
        assert result["exit_code"] == 0
        assert result["error"] is None

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_exit_code_7_still_ok(self, mock_path_cls, mock_resolve, mock_run, tmp_path):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)

        mock_run.return_value = MagicMock(returncode=7, stdout="", stderr="")

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")

        assert result["ok"] is True
        assert result["exit_code"] == 7


# ═══════════════════════════════════════════════════════════════
# 2. Preflight check: canary missing → HealthError
# ═══════════════════════════════════════════════════════════════

class TestPreflightCanary:
    """Canary file missing → raises HealthError."""

    @patch("core.lan_preflight.Path")
    def test_canary_missing_raises_health_error(self, mock_path_cls):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = False
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)

        with pytest.raises(HealthError, match="Canary file"):
            run_lan_dry_run("D:\\", "\\\\NAS\\share")


# ═══════════════════════════════════════════════════════════════
# 3. Preflight check: NAS not accessible → ok=False
# ═══════════════════════════════════════════════════════════════

class TestPreflightNASNotAccessible:
    """Robocopy exit code 8+ → ok=False."""

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_exit_code_8(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)

        mock_run.return_value = MagicMock(returncode=8, stdout="error output", stderr="")

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")

        assert result["ok"] is False
        assert result["exit_code"] == 8

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_exit_code_16(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)

        mock_run.return_value = MagicMock(returncode=16, stdout="fatal", stderr="")

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")

        assert result["ok"] is False
        assert result["exit_code"] == 16

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_error_output_captured(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)

        mock_run.return_value = MagicMock(returncode=8, stdout="err1", stderr="err2")

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")

        assert "err1" in result["error"]


# ═══════════════════════════════════════════════════════════════
# 4. OSError handling
# ═══════════════════════════════════════════════════════════════

class TestPreflightOSError:
    """OSError from subprocess → ok=False."""

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_oserror(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)

        mock_run.side_effect = OSError("network error")

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")

        assert result["ok"] is False
        assert "network error" in result["error"]


# ═══════════════════════════════════════════════════════════════
# 5. Empty stdout/stderr → "no output" fallback
# ═══════════════════════════════════════════════════════════════

class TestPreflightEmptyOutput:
    """Empty stdout/stderr → 'no output' fallback."""

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_empty_stdout_stderr(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)

        mock_run.return_value = MagicMock(returncode=10, stdout="", stderr="")

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")

        assert "no output" in result["error"]

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_none_stdout_stderr(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)

        mock_run.return_value = MagicMock(returncode=10, stdout=None, stderr=None)

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")

        assert "no output" in result["error"]


# ═══════════════════════════════════════════════════════════════
# 6. Return structure validation
# ═══════════════════════════════════════════════════════════════

class TestPreflightReturnStructure:
    """All paths must return dict with ok, exit_code, error."""

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_success_structure(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")
        assert set(result.keys()) == {"ok", "exit_code", "error"}

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_failure_structure(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)
        mock_run.return_value = MagicMock(returncode=10, stdout="", stderr="")

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")
        assert set(result.keys()) == {"ok", "exit_code", "error"}

    @patch("core.lan_preflight.subprocess.run", side_effect=OSError("err"))
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_oserror_structure(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")
        assert set(result.keys()) == {"ok", "exit_code", "error"}

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_timeout_structure(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)
        mock_run.side_effect = subprocess.TimeoutExpired("robocopy", 300)

        result = run_lan_dry_run("D:\\", "\\\\NAS\\share")
        assert set(result.keys()) == {"ok", "exit_code", "error"}
        assert result["ok"] is False


# ═══════════════════════════════════════════════════════════════
# 7. Command flags
# ═══════════════════════════════════════════════════════════════

class TestPreflightCommand:
    """Verify dry-run command flags."""

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_uses_list_only_mode(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_lan_dry_run("D:\\", "\\\\NAS\\share")

        cmd = mock_run.call_args[0][0]
        assert "/L" in cmd
        assert "/MIR" in cmd
        assert "/XJ" in cmd

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_excludes_aam_target(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_lan_dry_run("D:\\", "\\\\NAS\\share")

        cmd = mock_run.call_args[0][0]
        assert ".AAM_TARGET_MOUNTED" in cmd

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_uses_capture_output(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_lan_dry_run("D:\\", "\\\\NAS\\share")

        assert mock_run.call_args[1]["capture_output"] is True

    @patch("core.lan_preflight.subprocess.run")
    @patch("core.lan_preflight.resolve_binary", return_value="robocopy")
    @patch("core.lan_preflight.Path")
    def test_uses_text_mode(self, mock_path_cls, mock_resolve, mock_run):
        mock_canary = MagicMock()
        mock_canary.exists.return_value = True
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_canary)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_lan_dry_run("D:\\", "\\\\NAS\\share")

        assert mock_run.call_args[1]["text"] is True
