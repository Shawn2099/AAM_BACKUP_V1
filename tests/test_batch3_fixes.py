"""Regression tests for Batch-3 scale/config fixes (fix log 2026-08-18).

Covers: F7, F8, F12, F14, F15, F17, G2, G5, G8, G9, G11, F11.
"""

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core import lan_sync, lan_preflight, report, time_utils
from core.lan_sync import (
    _read_log_tail,
    cleanup_orphaned_robocopy_logs,
    count_failed_lines,
    run_lan_sync,
)
from models.config import AppConfig, CloudConfig, LanConfig, WolConfig


# ═══════════════════════════════════════════════════════════════
# F12 — files_failed extracted from robocopy log
# ═══════════════════════════════════════════════════════════════

class TestFilesFailed:
    @pytest.fixture
    def lan_cfg(self):
        return LanConfig()

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_partial_with_copy_errors_counts_failed_lines(self, mock_resolve, mock_run, tmp_path, lan_cfg):
        """Exit 9 (bit 3 = copy errors) with summary FAILED=2 -> files_failed=2.

        P1-COUNT: the count now comes from the positional Files-summary row;
        the log must carry one for an exact count (bit-3 floor applies when
        the summary is absent)."""
        log_file = tmp_path / "robocopy_sync_test.log"
        log_file.write_text(
            "      New File:            1 C:\\a\\ok.txt\n"
            "          ** FAILED: \\\\nas\\FY25-26\\big.iso\n"
            "          ** FAILED: \\\\nas\\FY25-26\\other.dat\n"
            "\n"
            "               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
            "    Files :         3         1         0         0         2         0\n"
        )
        mock_run.return_value = MagicMock(returncode=9)
        # point the (mocked) run at our prepared log
        with patch.object(lan_sync, "tempfile") as mock_tf:
            mock_tf.mkstemp.return_value = (3, str(log_file))
            result = run_lan_sync("D:\\", "\\\\nas\\FY25-26", lan_cfg)

        assert result["status"] == "LAN_PARTIAL"
        assert result["files_failed"] == 2

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_clean_run_files_failed_zero(self, mock_resolve, mock_run, lan_cfg):
        mock_run.return_value = MagicMock(returncode=1)
        result = run_lan_sync("D:\\", "\\\\nas\\FY25-26", lan_cfg)
        assert result["status"] == "LAN_COMPLETE"
        assert result["files_failed"] == 0

    @patch("core.lan_sync.subprocess.run")
    @patch("core.lan_sync.resolve_binary", return_value="robocopy")
    def test_timeout_files_failed_zero(self, mock_resolve, mock_run, lan_cfg):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="robocopy", timeout=5)
        result = run_lan_sync("D:\\", "\\\\nas\\FY25-26", lan_cfg)
        assert result["status"] == "LAN_FAILED"
        assert result["files_failed"] == 0


class TestCountFailedLines:
    def test_counts_only_robocopy_failure_lines(self):
        log = """x
          ** FAILED: \\\\a\\1.bin
    some line FAILED: but no asterisks
          ** FAILED: \\\\a\\2.bin
"""
        assert count_failed_lines(log) == 2


# ═══════════════════════════════════════════════════════════════
# F15 — log tail reads from the end (bounded memory)
# ═══════════════════════════════════════════════════════════════

class TestLogTailSeek:
    def test_large_log_tail_bounded(self, tmp_path):
        p = tmp_path / "big.log"
        payload = ("x" * 100 + "\n") * 20000  # ~2MB
        p.write_text(payload + "TAILMARK\n")
        tail = _read_log_tail(p, 100_000)
        assert len(tail) <= 100_000
        assert tail.rstrip().endswith("TAILMARK")

    def test_multibyte_boundary_clean(self, tmp_path):
        p = tmp_path / "mb.log"
        p.write_bytes(("héllo wörld " * 50000).encode("utf-8"))
        tail = _read_log_tail(p, 12345)
        assert "\ufffd" not in tail


# ═══════════════════════════════════════════════════════════════
# F14 — cron ordinal suffixes
# ═══════════════════════════════════════════════════════════════

class TestCronOrdinal:
    @pytest.mark.parametrize("dom", [
        "1", "2", "3", "4", "10", "11", "12", "13", "21", "22", "23", "31",
    ])
    def test_ordinal(self, dom):
        out = time_utils.cron_to_human(f"0 1 {dom} * *", "Asia/Kolkata")
        assert f"on day {dom} of the month" in out, out


# ═══════════════════════════════════════════════════════════════
# G5 — CSV formula injection neutralization
# ═══════════════════════════════════════════════════════════════

class TestCsvInjection:
    @pytest.mark.parametrize("cell", ["=cmd", "+1+1", "-1", "@SUM(A1)", "\ttab"])
    def test_leading_chars_neutralized(self, cell):
        assert report._csv_safe(cell).startswith("'")

    def test_plain_text_untouched(self):
        assert report._csv_safe("hello = world") == "hello = world"

    def test_none_and_empty(self):
        assert report._csv_safe(None) == ""
        assert report._csv_safe("") == ""

    def test_generate_csv_neutralizes_error_column(self):
        out = report._generate_csv_data([
            {"started_at": "s", "ended_at": "e", "mode": "lan", "status": "LAN_PARTIAL",
             "files_copied": 1, "files_failed": 1, "bytes_copied": 1,
             "duration_seconds": 1, "exit_code": 9,
             "error_message": "=MALICIOUS", "extended_metrics": "{\"a\": 1}"},
        ]).decode()
        rows = out.strip().split("\n")
        assert "'=MALICIOUS" in rows[1]
        # the JSON metrics cell is csv-quoted (doubled inner quotes);
        # it must NOT gain the injection-prevention apostrophe
        last_cell = rows[1].rsplit(",", 1)[-1]
        assert last_cell.startswith('"{') and not last_cell.startswith("'")


# ═══════════════════════════════════════════════════════════════
# G8 — orphaned robocopy temp log cleanup
# ═══════════════════════════════════════════════════════════════

class TestOrphanLogCleanup:
    def test_removes_old_keeps_new(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        old = tmp_path / "robocopy_sync_old.log"
        fresh = tmp_path / "robocopy_sync_fresh.log"
        other = tmp_path / "unrelated.log"
        for f in (old, fresh, other):
            f.write_text("x")
        past = time.time() - 48 * 3600
        os.utime(old, (past, past))

        removed = cleanup_orphaned_robocopy_logs(max_age_hours=24)

        assert removed == 1
        assert not old.exists()
        assert fresh.exists()
        assert other.exists()

    def test_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "missing"))
        assert cleanup_orphaned_robocopy_logs() == 0


# ═══════════════════════════════════════════════════════════════
# G11 — canary error message is self-recovering
# ═══════════════════════════════════════════════════════════════

class TestCanaryMessage:
    def test_message_contains_recovery_command(self, tmp_path):
        dest = tmp_path / "FY25-26"
        dest.mkdir()
        with pytest.raises(lan_preflight.HealthError) as ei:
            lan_preflight.run_lan_dry_run(str(dest.parent), str(dest))
        msg = str(ei.value)
        assert ".AAM_TARGET_MOUNTED" in msg
        assert "type nul >" in msg
        assert "10_recreate_canary.bat" in msg

    def test_canary_script_exists(self):
        assert (Path(__file__).resolve().parent.parent / "deploy" / "10_recreate_canary.bat").exists()


# ═══════════════════════════════════════════════════════════════
# G2 — 4-digit FY trap rejected at config load
# ═══════════════════════════════════════════════════════════════

def _base_config(**path_overrides):
    paths = dict(
        source_drive="D:\\\\FY25-26\\\\data",
        lan_destination="\\\\\\\\nas\\\\FY25-26\\\\data",
        gcs_key_path="keys/gcs.json",
        database_path="db/m.db",
        log_directory="logs",
        backup_lock_path="db/lock",
    )
    paths.update(path_overrides)
    return dict(
        paths=paths,
        cloud=dict(enabled=True, bucket="aam-backup-test", project_number="123"),
        wol=dict(enabled=False),
        lan=dict(enabled=True),
        dashboard=dict(api_key="testkey12345"),
    )


class TestFourDigitFY:
    def test_two_digit_loads(self):
        AppConfig(**_base_config())

    @pytest.mark.parametrize("field, value", [
        ("source_drive", "D:\\\\FY2026-27\\\\data"),
        ("source_drive", "D:\\\\data\\\\FY2026-27"),
        ("lan_destination", "\\\\\\\\nas\\\\FY2026-27\\\\data"),
    ])
    def test_four_digit_rejected(self, field, value):
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as ei:
            AppConfig(**_base_config(**{field: value}))
        assert "4-digit" in str(ei.value)


# ═══════════════════════════════════════════════════════════════
# F11 — conditional validation (disabled pipelines need no secrets)
# ═══════════════════════════════════════════════════════════════

class TestConditionalValidation:
    def test_enabled_cloud_requires_bucket(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="cloud.bucket"):
            CloudConfig(enabled=True)

    def test_disabled_cloud_allows_empty_bucket(self):
        assert CloudConfig(enabled=False).bucket == ""

    def test_enabled_wol_requires_mac(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="wol.mac_address"):
            WolConfig(enabled=True)

    def test_disabled_wol_allows_empty_mac(self):
        assert WolConfig(enabled=False).mac_address == ""

    def test_enabled_wol_still_validates_mac_format(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            WolConfig(enabled=True, mac_address="not-a-mac")


# ═══════════════════════════════════════════════════════════════
# F7 / F8 — scale-aware timeout defaults
# ═══════════════════════════════════════════════════════════════

class TestScaleDefaults:
    def test_cloud_listing_timeouts(self):
        c = CloudConfig(enabled=False)
        assert c.cloud_size_timeout_seconds == 300
        assert c.manifest_timeout_seconds == 900
        assert c.diff_timeout_seconds == 1800

    def test_lan_dry_run_timeout(self):
        assert LanConfig().dry_run_timeout_seconds == 900


# ═══════════════════════════════════════════════════════════════
# F17 / G9 — deploy artifacts
# ═══════════════════════════════════════════════════════════════

class TestDeployArtifacts:
    def test_gcs_lifecycle_deletion_day_92(self):
        """COLDLINE minimum duration is 90 days; the version enters COLDLINE on
        day 1 of non-current-ness, so deletion at day 90 was one day short
        (early-delete penalty on every replaced version). 92 = 90 + 1 + buffer."""
        rules = json.loads(
            (Path(__file__).resolve().parent.parent / "deploy" / "gcs_lifecycle.json").read_text()
        )
        delete_rules = [r for r in rules["rule"] if r["action"]["type"] == "Delete"]
        assert any(r["condition"].get("daysSinceNoncurrentTime") == 92 for r in delete_rules)
        assert not any(r["condition"].get("daysSinceNoncurrentTime") == 90 for r in delete_rules)

    def test_setup_script_sets_active_hours(self):
        bat = (Path(__file__).resolve().parent.parent / "deploy" / "03_setup_system.bat").read_text()
        assert "ActiveHours" in bat
        assert re.search(r"/v Start /t REG_DWORD /d 12", bat)
        assert re.search(r"/v End /t REG_DWORD /d 6", bat)

    def test_readiness_checks_active_hours(self):
        ps1 = (Path(__file__).resolve().parent.parent / "deploy" / "04_check_readiness.ps1").read_text()
        assert "ActiveHours" in ps1


# ═══════════════════════════════════════════════════════════════
# G14 — post-sync walk failure must not fail the run
# ═══════════════════════════════════════════════════════════════

class TestAfterWalkTolerance:
    def test_failed_walk_returns_none(self):
        import flow as flow_mod
        cfg = MagicMock()
        with patch.object(flow_mod, "walk_lan_destination", side_effect=OSError("SMB session reset")):
            result = flow_mod.lan_snapshot_after_task.fn(cfg)
        assert result is None

    def test_successful_walk_unchanged(self):
        import flow as flow_mod
        cfg = MagicMock()
        with patch.object(flow_mod, "walk_lan_destination") as mock_walk:
            mock_walk.return_value = [{"path": "x.txt", "size": 10, "mtime": 123.0}]
            result = flow_mod.lan_snapshot_after_task.fn(cfg)
        assert result == {"x.txt": (10, 123.0)}


# ═══════════════════════════════════════════════════════════════
# F12 wiring — files_failed reaches run_history
# ═══════════════════════════════════════════════════════════════

class TestFilesFailedWiring:
    def test_record_run_history_persists_files_failed(self, tmp_path):
        from core.backup_repository import record_run_history
        from core.manifest import ManifestDB
        db_path = str(tmp_path / "m.db")
        db = ManifestDB(db_path)
        try:
            ok = record_run_history(
                db, run_id="r1", mode="lan", started_at="2026-08-01T01:00:00",
                ended_at="2026-08-01T02:00:00", status="LAN_PARTIAL",
                exit_code=9, duration_seconds=3600.0, files_failed=7,
            )
            assert ok
            row = db._conn.execute(
                "SELECT files_failed FROM run_history WHERE run_id='r1'"
            ).fetchone()
            assert row[0] == 7
        finally:
            db.close()

    def test_record_run_history_defaults_zero(self, tmp_path):
        from core.backup_repository import record_run_history
        from core.manifest import ManifestDB
        db_path = str(tmp_path / "m.db")
        db = ManifestDB(db_path)
        try:
            ok = record_run_history(
                db, run_id="r2", mode="cloud", started_at="2026-08-01T01:00:00",
                ended_at="2026-08-01T02:00:00", status="CLOUD_COMPLETE",
                exit_code=0, duration_seconds=3600.0,
            )
            assert ok
            row = db._conn.execute(
                "SELECT files_failed FROM run_history WHERE run_id='r2'"
            ).fetchone()
            assert row[0] == 0
        finally:
            db.close()
