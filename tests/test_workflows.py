"""Logical workflow tests — end-to-end flow verification.

Tests the actual business logic flows, not just individual functions.
Verifies that components connect correctly and data flows through the system.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pendulum
import pytest

from core.manifest import ManifestDB
from core.backup_repository import record_sync_results, record_run_history
from core.lan_manifest import diff_snapshots, snapshot_to_dict, walk_lan_destination
from core.fy_router import get_fy_prefix
from core.health import pre_backup_health, HealthError
from models.config import load_config, AppConfig, CONFIG_PATH


# ═══════════════════════════════════════════════════════════════
# Workflow 1: Config → Validation → AppConfig
# ═══════════════════════════════════════════════════════════════

class TestConfigWorkflow:
    def test_config_path_constant_is_string(self):
        assert isinstance(CONFIG_PATH, str)
        assert CONFIG_PATH.endswith(".yaml")

    def test_app_config_has_all_sections(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("""
firm_name: "Test Firm"
paths:
  source_drive: "/tmp/src"
  lan_destination: "\\\\\\\\server\\\\share"
  database_path: "/tmp/test.db"
  gcs_key_path: "/tmp/key.json"
cloud:
  enabled: true
  bucket: "test-bucket"
lan:
  enabled: false
wol:
  enabled: false
  mac_address: "AA:BB:CC:DD:EE:FF"
dashboard:
  auth_enabled: false
""")
        cfg = load_config(str(cfg_path))
        assert cfg.firm_name == "Test Firm"
        assert cfg.paths.source_drive == "/tmp/src"
        assert cfg.cloud.enabled is True
        assert cfg.cloud.bucket == "test-bucket"
        assert cfg.lan.enabled is False

    def test_config_cross_validation_at_least_one_enabled(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("""
paths:
  source_drive: "/tmp/src"
  lan_destination: "\\\\server\\share"
  database_path: "/tmp/test.db"
  gcs_key_path: "/tmp/key.json"
cloud:
  enabled: false
lan:
  enabled: false
""")
        with pytest.raises(Exception):
            load_config(str(cfg_path))


# ═══════════════════════════════════════════════════════════════
# Workflow 2: DB Lifecycle — create, upsert, mark, delete, purge
# ═══════════════════════════════════════════════════════════════

class TestDBLifecycleWorkflow:
    def test_full_cloud_sync_lifecycle(self, tmp_path):
        """Simulate: sync files → record to DB → mark synced → query status."""
        db_path = tmp_path / "test.db"
        db = ManifestDB(db_path)

        # Step 1: Bulk upsert cloud files
        entries = [
            {"path": "docs/file1.pdf", "size": 1024, "mtime": 100.0},
            {"path": "docs/file2.pdf", "size": 2048, "mtime": 200.0},
            {"path": "img/photo.jpg", "size": 512000, "mtime": 300.0},
        ]
        db.bulk_upsert_synced(entries, "cloud")

        # Step 2: Verify all files recorded
        assert db.file_count("cloud_status") == 3
        assert db.get_entry("docs/file1.pdf")["cloud_status"] == "synced"
        assert db.get_entry("docs/file1.pdf")["file_size"] == 1024

        # Step 3: Record run history
        record_run_history(
            db,
            run_id="run-001", mode="cloud",
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T01:00:00Z",
            status="CLOUD_COMPLETE", exit_code=0,
            duration_seconds=3600.0,
        )

        # Step 4: Verify run recorded
        run = db.last_run("cloud")
        assert run is not None
        assert run["status"] == "CLOUD_COMPLETE"
        assert run["run_id"] == "run-001"

        # Step 5: Simulate second sync with removed files
        new_entries = [
            {"path": "docs/file1.pdf", "size": 1024, "mtime": 100.0},
            {"path": "docs/file3.pdf", "size": 4096, "mtime": 400.0},
        ]
        record_sync_results(db, "cloud", new_entries, removed=["docs/file2.pdf", "img/photo.jpg"])

        # Step 6: Verify updated state
        assert db.file_count("cloud_status") == 2
        assert db.get_entry("docs/file2.pdf") is None  # Removed
        assert db.get_entry("docs/file3.pdf") is not None  # Added

        db.close()

    def test_full_lan_sync_lifecycle(self, tmp_path):
        """Simulate: walk destination → diff → sync → record → verify."""
        db_path = tmp_path / "test.db"
        db = ManifestDB(db_path)

        # Step 1: Simulate "before" snapshot
        before = {"old.txt": (100, 100.0), "keep.txt": (200, 200.0)}

        # Step 2: Simulate "after" snapshot (after robocopy)
        after_entries = [
            {"path": "keep.txt", "size": 200, "mtime": 200.0},
            {"path": "new.txt", "size": 300, "mtime": 300.0},
        ]
        after_dict = {e["path"]: (e["size"], e["mtime"]) for e in after_entries}

        # Step 3: Compute diff
        diff = diff_snapshots(before, after_dict)
        assert diff["removed"] == ["old.txt"]
        assert diff["added"] == ["new.txt"]
        assert diff["unchanged"] == ["keep.txt"]

        # Step 4: Record to DB
        record_sync_results(db, "lan", after_entries, removed=diff["removed"])

        # Step 5: Verify DB state
        assert db.file_count("lan_status") == 2
        assert db.get_entry("old.txt") is None
        assert db.get_entry("new.txt")["lan_status"] == "synced"

        # Step 6: Record run
        record_run_history(
            db,
            run_id="run-lan-001", mode="lan",
            started_at="2026-01-01T01:00:00Z", ended_at="2026-01-01T03:00:00Z",
            status="LAN_COMPLETE", exit_code=0,
            duration_seconds=7200.0,
        )
        assert db.last_run("lan")["status"] == "LAN_COMPLETE"

        db.close()


# ═══════════════════════════════════════════════════════════════
# Workflow 3: Retry Deduplication — ON CONFLICT behavior
# ═══════════════════════════════════════════════════════════════

class TestRetryDedupWorkflow:
    def test_retry_overwrites_same_run_id(self, tmp_path):
        """Simulate: attempt 1 fails → attempt 2 fails → attempt 3 succeeds.
        Only ONE entry in run_history (upserted each time)."""
        db_path = tmp_path / "test.db"
        db = ManifestDB(db_path)

        run_id = "stable-id-cloud"

        # Attempt 1: fails
        record_run_history(
            db, run_id=run_id, mode="cloud",
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:05:00Z",
            status="CLOUD_FAILED", exit_code=1,
            duration_seconds=300.0, error_message="Network error",
        )
        assert db.last_run("cloud")["status"] == "CLOUD_FAILED"

        # Attempt 2: fails again
        record_run_history(
            db, run_id=run_id, mode="cloud",
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:10:00Z",
            status="CLOUD_FAILED", exit_code=1,
            duration_seconds=600.0, error_message="Network error again",
        )

        # Attempt 3: succeeds
        record_run_history(
            db, run_id=run_id, mode="cloud",
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T01:00:00Z",
            status="CLOUD_COMPLETE", exit_code=0,
            duration_seconds=3600.0, error_message=None,
        )

        # Only ONE entry (upserted), with final status
        runs = db.get_recent_runs(10)
        assert len(runs) == 1
        assert runs[0]["status"] == "CLOUD_COMPLETE"
        assert runs[0]["error_message"] is None

        db.close()

    def test_different_modes_separate_entries(self, tmp_path):
        """Cloud and LAN runs should be separate entries."""
        db_path = tmp_path / "test.db"
        db = ManifestDB(db_path)

        record_run_history(
            db, run_id="flow-1-cloud", mode="cloud",
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T01:00:00Z",
            status="CLOUD_COMPLETE", exit_code=0, duration_seconds=3600.0,
        )
        record_run_history(
            db, run_id="flow-1-lan", mode="lan",
            started_at="2026-01-01T01:00:00Z", ended_at="2026-01-01T03:00:00Z",
            status="LAN_COMPLETE", exit_code=0, duration_seconds=7200.0,
        )

        runs = db.get_recent_runs(10)
        assert len(runs) == 2
        assert db.last_run("cloud")["status"] == "CLOUD_COMPLETE"
        assert db.last_run("lan")["status"] == "LAN_COMPLETE"

        db.close()


# ═══════════════════════════════════════════════════════════════
# Workflow 4: Maintenance — purge old runs, conditional VACUUM
# ═══════════════════════════════════════════════════════════════

class TestMaintenanceWorkflow:
    def test_purge_removes_old_keeps_recent(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = ManifestDB(db_path)

        # Insert old run
        db.insert_run({
            "run_id": "old-run", "mode": "cloud",
            "started_at": "2020-01-01T00:00:00Z", "status": "CLOUD_COMPLETE",
        })

        # Insert recent run
        db.insert_run({
            "run_id": "recent-run", "mode": "cloud",
            "started_at": pendulum.now("Asia/Kolkata").isoformat(), "status": "CLOUD_COMPLETE",
        })

        assert len(db.get_recent_runs(10)) == 2

        # Purge runs older than 90 days
        db.purge_old_runs(retention_days=90)

        remaining = db.get_recent_runs(10)
        assert len(remaining) == 1
        assert remaining[0]["run_id"] == "recent-run"

        db.close()


# ═══════════════════════════════════════════════════════════════
# Workflow 5: Fiscal Year Routing
# ═══════════════════════════════════════════════════════════════

class TestFYRoutingWorkflow:
    def test_fy_changes_on_april_1(self):
        """FY should rollover on April 1."""
        from datetime import date
        assert get_fy_prefix(date(2026, 3, 31)) == "FY25-26"
        assert get_fy_prefix(date(2026, 4, 1)) == "FY26-27"

    def test_fy_consistent_throughout_year(self):
        """Same FY for all months within a fiscal year."""
        from datetime import date
        # April 2026 to March 2027 should all be FY26-27
        for month in range(4, 13):
            assert get_fy_prefix(date(2026, month, 15)) == "FY26-27"
        for month in range(1, 4):
            assert get_fy_prefix(date(2027, month, 15)) == "FY26-27"


# ═══════════════════════════════════════════════════════════════
# Workflow 6: Diff Snapshots — full lifecycle
# ═══════════════════════════════════════════════════════════════

class TestDiffWorkflow:
    def test_full_diff_lifecycle(self):
        """Simulate: initial state → add files → modify → remove → verify diff."""
        # Initial state: 3 files
        before = {
            "docs/a.pdf": (100, 1.0),
            "docs/b.pdf": (200, 2.0),
            "img/c.jpg": (300, 3.0),
        }

        # After sync: added d.pdf, modified b.pdf, removed c.jpg
        after = {
            "docs/a.pdf": (100, 1.0),      # Unchanged
            "docs/b.pdf": (999, 99.0),      # Modified (size changed)
            "docs/d.pdf": (400, 4.0),       # Added
        }

        diff = diff_snapshots(before, after)

        assert diff["unchanged"] == ["docs/a.pdf"]
        assert diff["modified"] == ["docs/b.pdf"]
        assert diff["added"] == ["docs/d.pdf"]
        assert diff["removed"] == ["img/c.jpg"]

    def test_empty_to_full(self):
        """Initial sync: empty → full."""
        before = {}
        after = {"a.txt": (1, 1.0), "b.txt": (2, 2.0)}
        diff = diff_snapshots(before, after)
        assert sorted(diff["added"]) == ["a.txt", "b.txt"]
        assert diff["removed"] == []

    def test_full_to_empty(self):
        """Disaster: all files removed."""
        before = {"a.txt": (1, 1.0), "b.txt": (2, 2.0)}
        after = {}
        diff = diff_snapshots(before, after)
        assert diff["added"] == []
        assert sorted(diff["removed"]) == ["a.txt", "b.txt"]


# ═══════════════════════════════════════════════════════════════
# Workflow 7: Health Check Pipeline
# ═══════════════════════════════════════════════════════════════

class TestHealthWorkflow:
    def test_health_passes_with_valid_source(self, tmp_path):
        """Health check should pass when source drive is valid."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "test.txt").write_text("data")
        # Should not raise (clock skew check may warn but not fail)
        with patch("core.health.check_clock_skew", return_value=(True, "")):
            with patch("core.health.check_binary_exists", return_value=True):
                pre_backup_health(str(source), "cloud")

    def test_health_fails_on_missing_source(self):
        """Health check should fail when source drive doesn't exist."""
        with pytest.raises(HealthError):
            pre_backup_health("/nonexistent/path", "cloud")

    def test_health_fails_on_empty_source(self, tmp_path):
        """Health check should fail when source drive is empty."""
        source = tmp_path / "empty"
        source.mkdir()
        with pytest.raises(HealthError, match="empty"):
            pre_backup_health(str(source), "cloud")


# ═══════════════════════════════════════════════════════════════
# Workflow 8: Report Generation
# ═══════════════════════════════════════════════════════════════

class TestReportWorkflow:
    def test_summary_report_with_runs(self, tmp_path):
        """Summary report should aggregate run data correctly."""
        from core.report import send_summary_report
        from models.config import NotificationConfig

        db_path = tmp_path / "test.db"
        db = ManifestDB(db_path)

        # Insert some runs
        for i in range(5):
            db.insert_run({
                "run_id": f"run-{i}", "mode": "cloud",
                "started_at": f"2026-01-{i+1:02d}T00:00:00Z",
                "status": "CLOUD_COMPLETE", "exit_code": 0,
                "files_copied": 100 + i, "bytes_copied": 1000 * i,
                "duration_seconds": 60.0 * (i + 1),
            })

        # Report should not crash even with no SMTP config
        config = NotificationConfig()
        result = send_summary_report(db, config, "Test Firm", 30, "Weekly")
        assert result is False  # No SMTP configured

        db.close()

    def test_summary_report_empty_db(self, tmp_path):
        """Summary report should handle empty DB gracefully."""
        from core.report import send_summary_report
        from models.config import NotificationConfig

        db_path = tmp_path / "test.db"
        db = ManifestDB(db_path)
        config = NotificationConfig()
        result = send_summary_report(db, config, "Test Firm", 7, "Weekly")
        assert result is False  # No runs, no SMTP

        db.close()


# ═══════════════════════════════════════════════════════════════
# Workflow 9: Backup Repository Integration
# ═══════════════════════════════════════════════════════════════

class TestBackupRepositoryWorkflow:
    def test_record_sync_results_normalizes_keys(self, tmp_path):
        """record_sync_results should handle both rclone and os.walk formats."""
        db_path = tmp_path / "test.db"
        db = ManifestDB(db_path)

        # Rclone format (uppercase keys)
        rclone_entries = [
            {"Path": "a.txt", "Size": 100, "ModTime": 1.0},
            {"Path": "b.txt", "Size": 200, "ModTime": 2.0},
        ]
        record_sync_results(db, "cloud", rclone_entries)
        assert db.file_count("cloud_status") == 2

        # os.walk format (lowercase keys)
        walk_entries = [
            {"path": "x.txt", "size": 50, "mtime": 10.0},
            {"path": "y.txt", "size": 60, "mtime": 20.0},
        ]
        record_sync_results(db, "lan", walk_entries)
        assert db.file_count("lan_status") == 2

        db.close()

    def test_record_sync_results_with_removals(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = ManifestDB(db_path)

        # Initial sync
        entries = [{"Path": "a.txt", "Size": 100, "ModTime": 1.0}]
        record_sync_results(db, "cloud", entries)
        assert db.get_entry("a.txt") is not None

        # Second sync: a.txt removed
        record_sync_results(db, "cloud", [], removed=["a.txt"])
        assert db.get_entry("a.txt") is None

        db.close()


# ═══════════════════════════════════════════════════════════════
# Workflow 10: Template Rendering with Real Data
# ═══════════════════════════════════════════════════════════════

class TestTemplateWorkflow:
    def test_render_with_all_data(self):
        """Template should render correctly with all dashboard data."""
        from templates.dashboard import render_dashboard

        html = render_dashboard(
            fy_prefix="FY26-27",
            flash_html='<div class="flash success">Backup started</div>',
            auth_enabled=True,
            cloud_schedule="Daily at 6:00 PM",
            lan_schedule="Daily at 1:00 AM",
        )

        assert "FY26-27" in html
        assert "/logout" in html
        assert "Backup started" in html
        assert "Daily at 6:00 PM" in html
        assert "Daily at 1:00 AM" in html

    def test_render_with_no_data(self):
        """Template should render correctly with default/empty data."""
        from templates.dashboard import render_dashboard

        html = render_dashboard()
        assert "<!DOCTYPE html>" in html
        assert "Loading data..." in html
        assert "/logout" not in html  # auth disabled by default
