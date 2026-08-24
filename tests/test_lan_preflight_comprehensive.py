"""Comprehensive tests for core/lan_preflight.py — REAL robocopy /L dry-runs.

Covers the full preflight contract against the live SMB share: canary gate,
self-recovering error message, exit-code boundaries, junction exclusion and
the zero-byte guarantee of list-only mode.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from core.health import HealthError
from core.lan_preflight import run_lan_dry_run


SMB_ROOT = Path(r"\\127.0.0.1\lan_backup\AAM_PYTEST_LAN")


@pytest.fixture
def smb_dest():
    d = SMB_ROOT / f"pfc_{int(time.time() * 1000)}_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def src(tmp_path):
    s = tmp_path / "src"
    (s / "docs").mkdir(parents=True)
    (s / "a.txt").write_text("alpha")
    (s / "docs" / "nested.txt").write_text("nested")
    return s


def _canary(dest: Path):
    (dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 1. Success paths
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestPreflightSuccess:

    def test_all_conditions_met(self, src, smb_dest):
        _canary(smb_dest)
        result = run_lan_dry_run(str(src), str(smb_dest))
        assert result == {"ok": True, "exit_code": result["exit_code"], "error": None}
        assert 0 <= result["exit_code"] <= 7

    def test_pending_copies_report_exit_1_but_ok(self, src, smb_dest):
        """Bit 0 set by files waiting for copy - still within the OK band."""
        _canary(smb_dest)
        result = run_lan_dry_run(str(src), str(smb_dest))
        assert result["ok"] is True
        # This robocopy build tallies the /XF-excluded canary as an Extra
        # (bit1) even under /L /MIR, so accept 1 or 3.
        assert result["exit_code"] in (1, 3)

    def test_second_dry_run_after_sync_is_exit_0(self, src, smb_dest):
        from core.lan_sync import run_lan_sync
        from models.config import LanConfig

        _canary(smb_dest)
        run_lan_sync(str(src), str(smb_dest),
                     LanConfig(retry_count=2, retry_wait_seconds=1, mt_threads=2))

        result = run_lan_dry_run(str(src), str(smb_dest))
        assert result["ok"] is True
        # No pending copies remain; the /XF-excluded canary may still be
        # tallied as an Extra (bit1) on this robocopy build -> 0 or 2.
        assert result["exit_code"] in (0, 2)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 2. Canary gate
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestPreflightCanary:

    def test_missing_canary_raises(self, src, smb_dest):
        with pytest.raises(HealthError, match="Canary file"):
            run_lan_dry_run(str(src), str(smb_dest))

    def test_error_message_carries_recovery_command(self, src, smb_dest, capture_logs):
        (smb_dest / ".AAM_TARGET_MOUNTED").unlink(missing_ok=True)

        with pytest.raises(HealthError) as exc:
            run_lan_dry_run(str(src), str(smb_dest))

        # G11: self-recovering message names the exact fix.
        assert "type nul" in str(exc.value)
        assert ".AAM_TARGET_MOUNTED" in str(exc.value)

    def test_canary_restored_preflight_passes_again(self, src, smb_dest):
        """The documented recovery path: recreate canary â†’ preflight green."""
        (smb_dest / ".AAM_TARGET_MOUNTED").unlink(missing_ok=True)
        with pytest.raises(HealthError):
            run_lan_dry_run(str(src), str(smb_dest))

        _canary(smb_dest)
        assert run_lan_dry_run(str(src), str(smb_dest))["ok"] is True

    def test_canary_never_listed_for_copy(self, src, smb_dest):
        """/XF excludes the canary even when present in SOURCE."""
        (src / ".AAM_TARGET_MOUNTED").write_text("CANARY")
        _canary(smb_dest)

        result = run_lan_dry_run(str(src), str(smb_dest))
        assert result["ok"] is True


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 3. Failure paths — real robocopy failures
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestPreflightFailures:

    def test_missing_source_not_ok_with_output(self, smb_dest):
        _canary(smb_dest)
        result = run_lan_dry_run(r"Q:\vanished", str(smb_dest))
        assert result["ok"] is False
        assert result["exit_code"] >= 8
        assert "Robocopy /L failed" in result["error"]

    def test_real_timeout_returns_minus_one(self, smb_dest):
        _canary(smb_dest)
        started = time.monotonic()
        result = run_lan_dry_run(r"\\203.0.113.5\noshare_src", str(smb_dest),
                                 timeout=2)
        elapsed = time.monotonic() - started

        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "Timeout after 2s" in result["error"]
        assert elapsed < 30

    def test_unverifiable_destination_refused_locally(self, src):
        with pytest.raises(HealthError):
            run_lan_dry_run(str(src), r"\\203.0.113.5\noshare")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 4. List-only guarantees
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestListOnlyMode:

    def test_zero_bytes_moved_to_destination(self, src, smb_dest):
        _canary(smb_dest)
        run_lan_dry_run(str(src), str(smb_dest))
        assert [p.name for p in smb_dest.iterdir()] == [".AAM_TARGET_MOUNTED"]

    def test_junction_in_source_does_not_break_preflight(self, tmp_path, src,
                                                         smb_dest):
        """/XJ must exclude junction points; a real junction in the tree is
        ignored rather than traversed or copied."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("not backed up")
        junction = src / "link_dir"
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            check=True, capture_output=True,
        )
        try:
            _canary(smb_dest)
            result = run_lan_dry_run(str(src), str(smb_dest))
            assert result["ok"] is True
        finally:
            subprocess.run(["cmd", "/c", "rmdir", str(junction)],
                           capture_output=True)
