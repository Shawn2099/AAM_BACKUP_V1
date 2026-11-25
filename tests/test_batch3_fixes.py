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
from core.lan_sync import run_lan_mirror
from models.config import AppConfig, CloudConfig, LanConfig, WolConfig


# ═══════════════════════════════════════════════════════════════
# F12 — files_failed from the rclone LAN run
# ═══════════════════════════════════════════════════════════════

class TestFilesFailed:
    """F12 on the rclone LAN leg: files_failed comes from the rclone final stats / log."""

    FIXTURES = Path(__file__).resolve().parents[1] / "test_support" / "rclone_logs"

    def _run(self, tmp_path, spec):
        from core.run_artefacts import RunArtefacts
        from test_support.fake_tools import fake_rclone_lan
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("x")
        art = RunArtefacts(tmp_path / "rl", "r", day="2026-10-14")
        with fake_rclone_lan({"rclone-lan-sync": spec}):
            return run_lan_mirror(LanConfig(), str(src), str(tmp_path / "dst"), str(art.dir),
                                  max_delete=0)

    def test_partial_with_copy_errors_counts_failed(self, tmp_path):
        log = (self.FIXTURES / "locked_file_exit1.jsonl").read_text(encoding="utf-8")
        result = self._run(tmp_path, {"exit_code": 1, "log": log})
        assert result["status"] == "LAN_PARTIAL"
        assert result["files_failed"] >= 1

    def test_clean_run_files_failed_zero(self, tmp_path):
        log = (self.FIXTURES / "lan_clean.jsonl").read_text(encoding="utf-8")
        result = self._run(tmp_path, {"exit_code": 0, "log": log})
        assert result["status"] == "LAN_COMPLETE"
        assert result["files_failed"] == 0

    def test_timeout_files_failed_zero(self, tmp_path):
        result = self._run(tmp_path, {"exit_code": None, "timed_out": True})
        assert result["status"] == "LAN_FAILED"
        assert result["files_failed"] == 0


# ═══════════════════════════════════════════════════════════════
# F12 — files_failed extracted from robocopy log
# ═══════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════
# F15 — log tail reads from the end (bounded memory)
# ═══════════════════════════════════════════════════════════════



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



# ═══════════════════════════════════════════════════════════════
# G11 — canary error message is self-recovering
# ═══════════════════════════════════════════════════════════════

class TestCanaryMessage:
    def test_missing_marker_is_reported_and_g5_message_guides_the_operator(self, tmp_path):
        # M2 2.3: recovery is a recorded decision on the dashboard (the G5
        # approval recreates the marker), not a hand-typed command.
        from core import guards
        dest = tmp_path / "FY25-26"
        dest.mkdir()
        assert lan_preflight.check_lan_destination(str(dest)) is False
        msg = guards.g5_canary(False, True, str(dest)).reason
        assert ".AAM_TARGET_MOUNTED" in msg and "Approve only if" in msg

    def test_canary_script_exists(self):
        assert (Path(__file__).resolve().parent.parent / "deploy" / "10_recreate_canary.bat").exists()


# ═══════════════════════════════════════════════════════════════
# G2 — 4-digit FY trap rejected at config load
# ═══════════════════════════════════════════════════════════════

def _base_config(**path_overrides):
    paths = dict(
        source_drive="D:\\\\FY25-26",
        lan_destination="\\\\\\\\nas\\\\FY25-26",
        gcs_key_path="keys/gcs.json",
        database_path="db/m.db",
        log_directory="logs",
    )
    paths.update(path_overrides)
    return dict(
        paths=paths,
        cloud=dict(enabled=True, bucket="aam-backup-test", project_number="123"),
        wol=dict(enabled=False),
        lan=dict(enabled=True),
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
        with pytest.raises(ValidationError, match="Google Cloud bucket name"):
            CloudConfig(enabled=True)

    def test_disabled_cloud_allows_empty_bucket(self):
        assert CloudConfig(enabled=False).bucket == ""

    def test_enabled_wol_requires_mac(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="MAC"):
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
        """F7: listing timeouts sized for the target scale.

        v1.1.3 raised manifest/diff from 900/1800 to the 5h policy (18000)
        after the 15m/30m values proved too tight for the 1M-object bucket
        (lsjson ~2-5 min, diff ~3-8 min at scale, growing to ~2.5M objects by
        yr-5); cloud_size stays at 300 (rclone size returns in ~40-150s)."""
        c = CloudConfig(enabled=False)
        assert c.cloud_size_timeout_seconds == 300
        assert c.manifest_timeout_seconds == 18000
        assert c.diff_timeout_seconds == 18000

    def test_lan_plan_timeout(self):
        # the LAN dry-run is rclone now (plan P1 K2); old key dry_run_timeout_seconds is deprecated
        assert LanConfig().rclone_measure_timeout_seconds == 1800


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
        assert re.search(r"/v ActiveHoursStart /t REG_DWORD /d 20", bat)   # plan H2: 12 h max on Server 2016
        assert re.search(r"/v ActiveHoursEnd /t REG_DWORD /d 8", bat)

    def test_readiness_checks_active_hours(self):
        ps1 = (Path(__file__).resolve().parent.parent / "deploy" / "04_check_readiness.ps1").read_text()
        assert "ActiveHours" in ps1


# ═══════════════════════════════════════════════════════════════
# G14 — post-sync walk failure must not fail the run
# ═══════════════════════════════════════════════════════════════



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
