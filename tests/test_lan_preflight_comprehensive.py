"""Comprehensive tests for core/lan_preflight.py — all functions.

Contract (see core/lan_preflight.py + AUDIT_FINDINGS.md §11.1):
  * SMB unreachability  → raises HealthError
  * canary missing on DEST (.AAM_TARGET_MOUNTED must be a regular file) → raises HealthError
  * robocopy outcome is mapped to an honest dict: ok=True iff exit code < 8,
    TimeoutExpired/FileNotFoundError/OSError → {"ok": False, "exit_code": -1, ...}
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.health import HealthError
from core.lan_preflight import (
    _extract_unc_host,
    _is_smb_reachable,
    run_lan_dry_run,
)


def _dest_with_canary(tmp_path):
    """Local (non-UNC) dest carrying a valid canary — passes the gate."""
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / ".AAM_TARGET_MOUNTED").write_text("canary")
    return dest


class TestExtractUncHost:
    def test_unc_path(self):
        assert _extract_unc_host(r"\\10.10.151.53\share") == "10.10.151.53"

    def test_forward_slash_unc(self):
        assert _extract_unc_host("//nas/share") == "nas"

    def test_local_drive(self):
        assert _extract_unc_host("D:\\backup") is None

    def test_relative_path(self):
        assert _extract_unc_host("relative/path") is None

    def test_bare_backslashes(self):
        assert _extract_unc_host("\\\\") is None


def unittest_mock_cm():
    from unittest.mock import MagicMock
    cm = MagicMock()
    cm.__enter__ = lambda self: self
    cm.__exit__ = lambda self, *a: False
    return cm


class TestIsSmbReachable:
    def test_reachable(self):
        with patch("core.lan_preflight.socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = lambda s: s
            mock_conn.return_value.__exit__ = lambda s, *a: False
            assert _is_smb_reachable("10.0.0.1") is True

    def test_unreachable(self):
        # attempts / wait come from config (lan.preflight_smb_attempts,
        # lan.preflight_smb_wait_seconds); no real sleeping in the test.
        with patch("core.lan_preflight.socket.create_connection", side_effect=OSError) as conn, \
             patch("core.lan_preflight.time.sleep") as slept:
            assert _is_smb_reachable("10.0.0.1", timeout=0.1, attempts=5, wait_s=7) is False
        assert conn.call_count == 5
        assert [c.args[0] for c in slept.call_args_list] == [7, 7, 7, 7]

    def test_reachable_after_slow_boot(self):
        with patch("core.lan_preflight.socket.create_connection",
                   side_effect=[OSError, OSError, OSError, unittest_mock_cm()]), \
             patch("core.lan_preflight.time.sleep"):
            assert _is_smb_reachable("10.0.0.1", timeout=1, attempts=8, wait_s=15) is True


class TestRunLanDryRun:
    def test_success(self, tmp_path):
        """Robocopy exit 1 (files copied) → ok=True."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "file.txt").write_text("data")
        dest = _dest_with_canary(tmp_path)

        with patch("core.lan_preflight.resolve_binary", return_value="robocopy"), \
             patch("core.lan_preflight.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="ok", stderr="")

            result = run_lan_dry_run(source=str(source), dest=str(dest))
            assert result["ok"] is True
            assert result["exit_code"] == 1
            assert result["error"] is None

    def test_smb_unreachable_raises(self, tmp_path):
        """Unreachable UNC host → HealthError, not a dict."""
        dest = _dest_with_canary(tmp_path)
        with patch("core.lan_preflight._is_smb_reachable", return_value=False):
            with pytest.raises(HealthError, match="Cannot reach LAN host"):
                run_lan_dry_run(source=str(tmp_path), dest=r"\\10.10.151.53\share\FY26-27")

    def test_missing_canary_is_reported(self, tmp_path):
        """Missing canary on dest → reported; guard G5 blocks the run (M2 2.3)."""
        dest = tmp_path / "dest"
        dest.mkdir()
        with patch("core.lan_preflight.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            assert run_lan_dry_run(source=str(tmp_path), dest=str(dest))["canary_present"] is False

    def test_canary_directory_is_not_a_file(self, tmp_path):
        """A directory named like the canary is not a regular file → refused."""
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / ".AAM_TARGET_MOUNTED").mkdir()
        with patch("core.lan_preflight.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            assert run_lan_dry_run(source=str(tmp_path), dest=str(dest))["canary_present"] is False

    def test_timeout(self, tmp_path):
        """Timeout → ok=False, exit_code=-1, error mentions timeout."""
        source = tmp_path / "source"
        source.mkdir()
        dest = _dest_with_canary(tmp_path)

        with patch("core.lan_preflight.resolve_binary", return_value="robocopy"), \
             patch("core.lan_preflight.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="robocopy", timeout=60)

            result = run_lan_dry_run(source=str(source), dest=str(dest), timeout=60)
            assert result["ok"] is False
            assert result["exit_code"] == -1
            assert "Timeout after 60s" in result["error"]

    def test_fatal_error(self, tmp_path):
        """Fatal robocopy error (exit 16) → ok=False."""
        source = tmp_path / "source"
        source.mkdir()
        dest = _dest_with_canary(tmp_path)

        with patch("core.lan_preflight.resolve_binary", return_value="robocopy"), \
             patch("core.lan_preflight.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=16, stdout="fatal output", stderr="")

            result = run_lan_dry_run(source=str(source), dest=str(dest))
            assert result["ok"] is False
            assert result["exit_code"] == 16
            assert "exit 16" in result["error"]

    def test_binary_not_found(self, tmp_path):
        """robocopy.exe missing → ok=False with a clear error."""
        source = tmp_path / "source"
        source.mkdir()
        dest = _dest_with_canary(tmp_path)

        with patch("core.lan_preflight.resolve_binary", return_value=None), \
             patch("core.lan_preflight.subprocess.run", side_effect=FileNotFoundError):
            result = run_lan_dry_run(source=str(source), dest=str(dest))
            assert result["ok"] is False
            assert result["error"] == "robocopy.exe not found"

    def test_os_error(self, tmp_path):
        """Generic OSError → ok=False carrying the message."""
        source = tmp_path / "source"
        source.mkdir()
        dest = _dest_with_canary(tmp_path)

        with patch("core.lan_preflight.resolve_binary", return_value="robocopy"), \
             patch("core.lan_preflight.subprocess.run", side_effect=OSError("boom")):
            result = run_lan_dry_run(source=str(source), dest=str(dest))
            assert result["ok"] is False
            assert result["error"] == "boom"

    def test_dry_run_flags(self, tmp_path):
        """The command is /L /MIR /XJ and excludes the canary from the listing."""
        source = tmp_path / "source"
        source.mkdir()
        dest = _dest_with_canary(tmp_path)

        with patch("core.lan_preflight.resolve_binary", return_value="robocopy"), \
             patch("core.lan_preflight.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            run_lan_dry_run(source=str(source), dest=str(dest))
            cmd = mock_run.call_args[0][0]
            assert "/L" in cmd and "/MIR" in cmd and "/XJ" in cmd
            assert ".AAM_TARGET_MOUNTED" in cmd  # /XF excluded from the listing


class TestCanaryGate:
    """The canary gate lives inside run_lan_dry_run (no standalone helper exists)."""

    def test_canary_present_proceeds(self, tmp_path):
        """Canary file exists on dest → the dry run proceeds to robocopy."""
        dest = _dest_with_canary(tmp_path)
        with patch("core.lan_preflight.resolve_binary", return_value="robocopy"), \
             patch("core.lan_preflight.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            result = run_lan_dry_run(source=str(tmp_path), dest=str(dest))
            assert result["ok"] is True
            assert mock_run.called

    def test_canary_missing_only_lists(self, tmp_path):
        """Canary missing → only the read-only /L listing runs (verified to
        write nothing, 2026-10-15) and the marker is reported for G5."""
        dest = tmp_path / "dest"
        dest.mkdir()
        with patch("core.lan_preflight.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            res = run_lan_dry_run(source=str(tmp_path), dest=str(dest))
        assert res["canary_present"] is False
        assert all("/L" in c.args[0] for c in mock_run.call_args_list)

    def test_nonexistent_dest_is_reported(self, tmp_path):
        """Nonexistent dest → no marker (G5 blocks); /L does not create it
        (verified on robocopy 10.0, 2026-10-15)."""
        with patch("core.lan_preflight.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            res = run_lan_dry_run(source=str(tmp_path), dest=str(tmp_path / "nonexistent"))
        assert res["canary_present"] is False
        assert all("/L" in c.args[0] for c in mock_run.call_args_list)
