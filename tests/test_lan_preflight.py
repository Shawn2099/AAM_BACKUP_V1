"""Tests for lan_preflight — REAL robocopy /L runs against the live SMB share."""

import os
import time
from pathlib import Path

import pytest

from core.health import HealthError
from core.lan_preflight import run_lan_dry_run


SMB_ROOT = Path(r"\\127.0.0.1\lan_backup\AAM_PYTEST_LAN")


@pytest.fixture
def smb_dest():
    d = SMB_ROOT / f"pf_{int(time.time() * 1000)}_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def src(tmp_path):
    s = tmp_path / "src"
    (s / "docs").mkdir(parents=True)
    (s / "a.txt").write_text("alpha")
    (s / "docs" / "n.txt").write_text("nested")
    return s


class TestRunLanDryRun:

    def test_exit_ok_when_share_ready(self, src, smb_dest):
        (smb_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        result = run_lan_dry_run(str(src), str(smb_dest))

        assert result["ok"] is True
        assert 0 <= result["exit_code"] <= 7
        assert result["error"] is None

    def test_copy_pending_still_ok(self, src, smb_dest):
        """Files waiting to copy set bit 0 (exit 1) - still an OK preflight."""
        (smb_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        result = run_lan_dry_run(str(src), str(smb_dest))

        assert result["ok"] is True
        # bit0 = copies pending. This robocopy build additionally reports the
        # /XF-excluded canary in the Extras column (bit1) even though /MIR
        # will never delete it, so accept 1 or 3 - both are OK preflights.
        assert result["exit_code"] in (1, 3)

    def test_missing_canary_raises_health_error(self, src, smb_dest):
        (smb_dest / ".AAM_TARGET_MOUNTED").unlink(missing_ok=True)

        with pytest.raises(HealthError, match="Canary"):
            run_lan_dry_run(str(src), str(smb_dest))

    def test_unverifiable_destination_refused_before_network(self, src):
        """An unroutable destination cannot prove the canary — the preflight
        must refuse locally (HealthError) instead of mirroring into the dark."""
        with pytest.raises(HealthError):
            run_lan_dry_run(str(src), r"\\203.0.113.5\noshare")

    def test_missing_source_reports_not_ok(self, smb_dest):
        (smb_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        result = run_lan_dry_run(r"Q:\gone", str(smb_dest))

        assert result["ok"] is False
        assert result["exit_code"] >= 8
        assert result["error"]

    def test_real_timeout_enforced(self, smb_dest):
        (smb_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        started = time.monotonic()
        result = run_lan_dry_run(r"\\203.0.113.5\noshare", str(smb_dest), timeout=2)
        elapsed = time.monotonic() - started

        # Canary gate passes (dest is the real share); the OS then kills the
        # blocked robocopy at the 2-second deadline.
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "Timeout after 2s" in result["error"]
        assert elapsed < 30

    def test_list_mode_moves_zero_bytes(self, src, smb_dest):
        (smb_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        run_lan_dry_run(str(src), str(smb_dest))

        assert [p.name for p in smb_dest.iterdir()] == [".AAM_TARGET_MOUNTED"]
