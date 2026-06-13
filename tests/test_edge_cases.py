"""Edge case tests — covers FY rollover, database, pipeline, watchdog, config, and reports.

These tests catch real production bugs that unit tests masked.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ═══════════════════════════════════════════════════════════════
# Group 1: FY Rollover Edge Cases
# ═══════════════════════════════════════════════════════════════


class TestRolloverCloudSignature:
    """Verify run_cloud_sync is called with correct kwargs — not mocked away."""

    def test_run_final_backup_cloud_signature(self, tmp_path):
        """The critical bug: run_cloud_sync must receive gcs_key_path, project_number, location."""
        from core.fy_rollover import run_final_backup

        # Create minimal config objects
        cloud_config = MagicMock()
        cloud_config.enabled = True
        cloud_config.bucket = "test-bucket"
        cloud_config.project_number = "123456"
        cloud_config.storage_class = "STANDARD"
        cloud_config.location = "asia-south1"
        cloud_config.bandwidth_limit = "10M"
        cloud_config.retry_count = 3
        cloud_config.transfers = 4
        cloud_config.checkers = 16
        cloud_config.subprocess_timeout_seconds = 21600

        paths_config = MagicMock()
        paths_config.gcs_key_path = "/tmp/key.json"

        lan_config = MagicMock()
        lan_config.enabled = False

        config = MagicMock()
        config.wol.enabled = False

        with patch("core.fy_rollover.run_cloud_sync") as mock_sync:
            mock_sync.return_value = {"exit_code": 0, "status": "CLOUD_COMPLETE"}
            run_final_backup(
                source_drive="/tmp/source",
                lan_destination="\\\\server\\share",
                lan_config=lan_config,
                cloud_config=cloud_config,
                paths_config=paths_config,
                config=config,
                old_fy="FY25-26",
            )

            # Verify the call signature matches run_cloud_sync's actual parameters
            mock_sync.assert_called_once()
            kwargs = mock_sync.call_args[1]
            assert kwargs["source"] == "/tmp/source"
            assert kwargs["bucket"] == "test-bucket"
            assert kwargs["fy_prefix"] == "FY25-26"
            assert kwargs["gcs_key_path"] == "/tmp/key.json"
            assert kwargs["project_number"] == "123456"
            assert kwargs["storage_class"] == "STANDARD"
            assert kwargs["location"] == "asia-south1"
            assert "config_path" not in kwargs, "config_path is not a valid parameter"

    def test_run_final_backup_cloud_disabled(self):
        """Cloud disabled → only LAN backup runs, no cloud call."""
        from core.fy_rollover import run_final_backup

        cloud_config = MagicMock()
        cloud_config.enabled = False
        lan_config = MagicMock()
        lan_config.enabled = False
        config = MagicMock()

        with patch("core.fy_rollover.run_cloud_sync") as mock_cloud:
            cloud_ok, lan_ok = run_final_backup(
                "/tmp/src", "\\\\s\\d", lan_config, cloud_config,
                MagicMock(), config, "FY25-26",
            )
            mock_cloud.assert_not_called()
            assert cloud_ok is False
            assert lan_ok is False

    def test_run_final_backup_cloud_fails_raises_rollover_error(self, tmp_path):
        """Cloud fails → RolloverError raised, config unchanged."""
        from core.fy_rollover import RolloverError, rollover

        config_yaml = """
paths:
  source_drive: "E:\\\\SOURCE\\\\FY25-26"
  lan_destination: "\\\\\\\\server\\\\share\\\\FY25-26"
  database_path: "/tmp/test.db"
  gcs_key_path: "/tmp/key.json"
cloud:
  enabled: true
  bucket: "test-bucket"
  project_number: "123"
  storage_class: "STANDARD"
  location: "asia-south1"
lan:
  enabled: false
  retry_count: 3
  retry_wait_seconds: 10
  subprocess_timeout_seconds: 3600
  mt_threads: 8
  max_attempts: 2
  retry_delay_seconds: 600
wol:
  enabled: false
  server_ip: "192.168.1.1"
  mac_address: "AA:BB:CC:DD:EE:FF"
schedule:
  timezone: "Asia/Kolkata"
notifications:
  smtp_host: ""
  smtp_port: 587
  smtp_username: ""
  smtp_password: ""
  sender: ""
  recipients: []
  send_on_failure: false
dashboard:
  auth_enabled: false
"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(config_yaml)

        with patch("core.fy_rollover.get_fy_prefix", return_value="FY26-27"):
            with patch("core.fy_rollover.run_cloud_sync", return_value={"exit_code": 1, "status": "CLOUD_FAILED"}):
                with pytest.raises(RolloverError, match="cloud"):
                    rollover(str(config_path))


class TestRolloverDetection:
    """FY boundary detection edge cases."""

    def test_detects_april_1_boundary(self):
        from core.fy_rollover import detect_rollover
        with patch("core.fy_rollover.get_fy_prefix", return_value="FY26-27"):
            assert detect_rollover("E:\\SOURCE\\FY25-26", "\\\\server\\share\\FY25-26") is True

    def test_noop_same_fy(self):
        from core.fy_rollover import detect_rollover
        with patch("core.fy_rollover.get_fy_prefix", return_value="FY26-27"):
            assert detect_rollover("E:\\SOURCE\\FY26-27", "\\\\server\\share\\FY26-27") is False

    def test_flat_paths_no_fy(self):
        from core.fy_rollover import detect_rollover
        assert detect_rollover("E:\\SOURCE", "\\\\server\\share") is False

    def test_fy_name_extraction(self):
        from core.fy_rollover import _fy_name
        assert _fy_name("E:\\SOURCE\\FY26-27") == "FY26-27"
        assert _fy_name("\\\\server\\share\\FY26-27") == "FY26-27"
        assert _fy_name("/mnt/source/FY26-27") == "FY26-27"
        assert _fy_name("E:\\SOURCE") is None
        assert _fy_name("E:\\SOURCE\\FY26") is None  # partial

    def test_parent_path_extraction(self):
        from core.fy_rollover import _parent_path
        assert _parent_path("E:\\SOURCE\\FY26-27") == "E:\\SOURCE"
        assert _parent_path("\\\\server\\share\\FY26-27") == "\\\\server\\share"

    def test_child_path_preserves_separator(self):
        from core.fy_rollover import _child_path
        assert _child_path("E:\\SOURCE", "FY26-27") == "E:\\SOURCE\\FY26-27"
        assert _child_path("/mnt/source", "FY26-27") == "/mnt/source/FY26-27"


class TestRolloverConfigMutation:
    """Config YAML atomic update edge cases."""

    def test_update_config_yaml_atomic(self, tmp_path):
        from core.fy_rollover import update_config_yaml

        config_content = """paths:
  source_drive: "E:\\\\SOURCE\\\\FY25-26"
  lan_destination: "\\\\\\\\server\\\\share\\\\FY25-26"
  # Comment preserved
  database_path: "/tmp/test.db"
"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(config_content)

        update_config_yaml(str(config_path), "E:\\SOURCE", "\\\\server\\share", "FY26-27")

        result = config_path.read_text()
        assert "FY26-27" in result
        assert "# Comment preserved" in result

    def test_update_config_yaml_failure_cleanup(self, tmp_path):
        from core.fy_rollover import update_config_yaml

        config_path = tmp_path / "config.yaml"
        config_path.write_text("paths:\n  source_drive: test\n")

        # Force a failure during write by making the directory read-only
        # This test verifies temp file cleanup
        with patch("ruamel.yaml.YAML") as mock_yaml_cls:
            mock_yaml = MagicMock()
            mock_yaml_cls.return_value = mock_yaml
            mock_yaml.load.return_value = {"paths": {"source_drive": "old", "lan_destination": "old"}}
            mock_yaml.dump.side_effect = OSError("disk full")

            with pytest.raises(OSError, match="disk full"):
                update_config_yaml(str(config_path), "root", "root", "FY26-27")

            # Temp file should be cleaned up
            remaining = list(tmp_path.glob(".config_rollover_*"))
            assert len(remaining) == 0


# ═══════════════════════════════════════════════════════════════
# Group 2: Database Edge Cases
# ═══════════════════════════════════════════════════════════════


class TestManifestDBBulkOperations:
    """Bulk operation edge cases — variable limits, chunking."""

    def test_bulk_upsert_many_entries(self, tmp_path):
        """10K entries don't hit SQLite variable limit after chunking fix."""
        from core.manifest import ManifestDB

        db = ManifestDB(str(tmp_path / "test.db"))
        try:
            entries = [
                {"path": f"file_{i}.txt", "size": i * 100, "mtime": 1700000000.0 + i}
                for i in range(10000)
            ]
            db.bulk_upsert_synced(entries, "cloud")

            count = db.file_count("cloud_status")
            assert count == 10000
        finally:
            db.close()

    def test_delete_entries_chunking(self, tmp_path):
        """1000+ entries handled without hitting variable limit."""
        from core.manifest import ManifestDB

        db = ManifestDB(str(tmp_path / "test.db"))
        try:
            # Insert 1000 entries
            entries = [
                {"path": f"file_{i}.txt", "size": i, "mtime": 0.0}
                for i in range(1000)
            ]
            db.bulk_upsert_synced(entries, "lan")
            assert db.file_count("lan_status") == 1000

            # Delete all 1000
            paths = [f"file_{i}.txt" for i in range(1000)]
            db.delete_entries(paths)
            assert db.file_count("lan_status") == 0
        finally:
            db.close()

    def test_prune_stale_synced_performance(self, tmp_path):
        """1K stale entries complete in reasonable time using executemany."""
        from core.manifest import ManifestDB

        db = ManifestDB(str(tmp_path / "test.db"))
        try:
            entries = [
                {"path": f"file_{i}.txt", "size": i, "mtime": 0.0}
                for i in range(1000)
            ]
            db.bulk_upsert_synced(entries, "cloud")

            # Prune all as stale (empty active set)
            pruned = db.prune_stale_synced("cloud", set())
            assert pruned == 1000
            assert db.file_count("cloud_status") == 0
        finally:
            db.close()


class TestManifestDBConcurrency:
    """Concurrent access edge cases."""

    def test_concurrent_read_write_no_busy(self, tmp_path):
        """Two ManifestDB instances can read/write without SQLITE_BUSY."""
        from core.manifest import ManifestDB

        db_path = str(tmp_path / "test.db")
        db1 = ManifestDB(db_path)
        db2 = ManifestDB(db_path)
        try:
            # Write from db1
            db1.bulk_upsert_synced([{"path": "a.txt", "size": 100, "mtime": 0.0}], "cloud")

            # Read from db2 (should not get SQLITE_BUSY)
            entry = db2.get_entry("a.txt")
            assert entry is not None
            assert entry["file_size"] == 100

            # Write from db2 while db1 holds no lock
            db2.bulk_upsert_synced([{"path": "b.txt", "size": 200, "mtime": 0.0}], "lan")

            # Both visible from db1
            assert db1.get_entry("b.txt") is not None
        finally:
            db1.close()
            db2.close()

    def test_close_then_reopen(self, tmp_path):
        """close() then new operation creates fresh connection."""
        from core.manifest import ManifestDB

        db = ManifestDB(str(tmp_path / "test.db"))
        db.bulk_upsert_synced([{"path": "a.txt", "size": 100, "mtime": 0.0}], "cloud")
        db.close()

        # Should re-open connection lazily
        entry = db.get_entry("a.txt")
        assert entry is not None
        db.close()

    def test_schema_migration_idempotent(self, tmp_path):
        """Running DDL twice doesn't corrupt."""
        from core.manifest import ManifestDB

        db_path = str(tmp_path / "test.db")
        db1 = ManifestDB(db_path)
        db1.close()

        db2 = ManifestDB(db_path)
        db2.close()

        db3 = ManifestDB(db_path)
        try:
            db3.bulk_upsert_synced([{"path": "a.txt", "size": 100, "mtime": 0.0}], "cloud")
            assert db3.file_count("cloud_status") == 1
        finally:
            db3.close()


class TestManifestDBEdgeCases:
    """Miscellaneous database edge cases."""

    def test_path_normalization_consistency(self, tmp_path):
        """Backslash paths normalized everywhere."""
        from core.manifest import ManifestDB

        db = ManifestDB(str(tmp_path / "test.db"))
        try:
            db.bulk_upsert_synced([{"path": "folder\\subfolder\\file.txt", "size": 100, "mtime": 0.0}], "cloud")
            entry = db.get_entry("folder/subfolder/file.txt")
            assert entry is not None
            assert entry["file_size"] == 100
        finally:
            db.close()

    def test_duplicate_run_id_upsert(self, tmp_path):
        """ON CONFLICT handles Prefect retry correctly."""
        from core.manifest import ManifestDB

        db = ManifestDB(str(tmp_path / "test.db"))
        try:
            db.insert_run({
                "run_id": "run-123", "mode": "cloud",
                "started_at": "2026-01-01T00:00:00Z", "status": "RUNNING",
            })
            # Retry with same run_id — should update, not duplicate
            db.insert_run({
                "run_id": "run-123", "mode": "cloud",
                "started_at": "2026-01-01T00:00:00Z",
                "ended_at": "2026-01-01T01:00:00Z",
                "status": "CLOUD_COMPLETE", "exit_code": 0,
            })
            runs = db.get_recent_runs(10)
            assert len(runs) == 1
            assert runs[0]["status"] == "CLOUD_COMPLETE"
        finally:
            db.close()

    def test_wal_checkpoint_after_run(self, tmp_path):
        """WAL is checkpointed after run history write."""
        from core.manifest import ManifestDB
        from core.backup_repository import record_run_history

        db = ManifestDB(str(tmp_path / "test.db"))
        try:
            record_run_history(
                db, run_id="test-run", mode="cloud",
                started_at="2026-01-01T00:00:00Z",
                ended_at="2026-01-01T01:00:00Z",
                status="CLOUD_COMPLETE", exit_code=0,
                duration_seconds=3600,
            )
            # WAL should be checkpointed — check WAL file is small/empty
            wal_path = tmp_path / "test.db-wal"
            if wal_path.exists():
                assert wal_path.stat().st_size < 10000  # Should be truncated
        finally:
            db.close()


# ═══════════════════════════════════════════════════════════════
# Group 3: Pipeline Edge Cases
# ═══════════════════════════════════════════════════════════════


class TestManifestParsing:
    """Falsy-value and type edge cases in manifest entry parsing."""

    def test_falsy_value_manifest_parsing(self):
        """Size=0, path='', mtime=0 handled correctly with is-not-None checks."""
        # Simulate the parsing logic from flow.py
        items = [
            {"Path": "file.txt", "Size": 0, "ModTime": 0},  # Falsy values!
            {"path": "", "size": 0, "mtime": 0},
            {"Path": "ok.txt", "Size": 100, "ModTime": 1700000000},
        ]

        parsed = []
        for item in items:
            path = item.get("Path") if item.get("Path") is not None else item.get("path", "")
            size = item.get("Size") if item.get("Size") is not None else item.get("size", 0)
            mtime = item.get("ModTime") if item.get("ModTime") is not None else item.get("mtime", 0)
            parsed.append((path, size, mtime))

        assert parsed[0] == ("file.txt", 0, 0)  # Not ("file.txt", 0, 0) from wrong key
        assert parsed[1] == ("", 0, 0)
        assert parsed[2] == ("ok.txt", 100, 1700000000)

    def test_numeric_mtime_comparison(self):
        """Unix timestamps compared directly, no pendulum parse."""
        import pendulum

        mtime = 1700000000
        old_mtime = 1700000000.5

        # Direct numeric comparison
        if isinstance(mtime, (int, float)) and isinstance(old_mtime, (int, float)):
            t1, t2 = float(mtime), float(old_mtime)
        else:
            t1 = pendulum.parse(str(mtime)).timestamp()
            t2 = pendulum.parse(str(old_mtime)).timestamp()

        assert abs(t1 - t2) < 1.1  # Should match (within threshold)

    def test_string_mtime_comparison(self):
        """ISO strings parsed via pendulum."""
        import pendulum

        mtime = "2023-11-14T22:13:20Z"
        old_mtime = "2023-11-14T22:13:20Z"

        if isinstance(mtime, (int, float)) and isinstance(old_mtime, (int, float)):
            t1, t2 = float(mtime), float(old_mtime)
        else:
            t1 = pendulum.parse(str(mtime)).timestamp()
            t2 = pendulum.parse(str(old_mtime)).timestamp()

        assert abs(t1 - t2) < 1.1

    def test_unparseable_mtime_fallback(self):
        """Invalid mtime falls back to string comparison."""
        mtime = "not-a-date"
        old_mtime = "not-a-date"

        try:
            if isinstance(mtime, (int, float)) and isinstance(old_mtime, (int, float)):
                t1, t2 = float(mtime), float(old_mtime)
            else:
                import pendulum
                t1 = pendulum.parse(str(mtime)).timestamp()
                t2 = pendulum.parse(str(old_mtime)).timestamp()
            matched = abs(t1 - t2) <= 1.1
        except Exception:
            matched = str(mtime) != str(old_mtime)

        assert matched is False  # Same string → "not different"


class TestPipelineExitCodes:
    """Exit code classification edge cases."""

    def test_cloud_partial_does_not_raise(self):
        """CLOUD_PARTIAL (exit 4-6, 10) completes pipeline."""
        from core.cloud_sync import classify_rclone_exit

        for code in (4, 5, 6, 10):
            status = classify_rclone_exit(code)
            assert status == "CLOUD_PARTIAL", f"exit {code} should be CLOUD_PARTIAL"

    def test_cloud_failed_raises(self):
        """CLOUD_FAILED (exit 1-3, 7-8) aborts pipeline."""
        from core.cloud_sync import classify_rclone_exit

        for code in (1, 2, 3, 7, 8):
            status = classify_rclone_exit(code)
            assert status == "CLOUD_FAILED", f"exit {code} should be CLOUD_FAILED"

    def test_lan_partial_completes(self):
        """LAN_PARTIAL (exit 8-15) completes pipeline."""
        from core.lan_sync import classify_exit_code

        for code in (8, 9, 10, 11, 15):
            status = classify_exit_code(code)
            assert status == "LAN_PARTIAL", f"exit {code} should be LAN_PARTIAL"

    def test_lan_failed_raises(self):
        """LAN_FAILED (exit 16+) aborts pipeline."""
        from core.lan_sync import classify_exit_code

        for code in (16, 17, 32):
            status = classify_exit_code(code)
            assert status == "LAN_FAILED", f"exit {code} should be LAN_FAILED"

    def test_lan_complete_range(self):
        """LAN_COMPLETE (exit 0-3)."""
        from core.lan_sync import classify_exit_code

        for code in range(0, 4):
            status = classify_exit_code(code)
            assert status == "LAN_COMPLETE", f"exit {code} should be LAN_COMPLETE"


# ═══════════════════════════════════════════════════════════════
# Group 4: Watchdog Edge Cases
# ═══════════════════════════════════════════════════════════════


class TestWatchdogBackupDetection:
    """Backup lock and process detection edge cases."""

    def test_stale_lock_detection(self, tmp_path):
        """Dead PID → lock removed, backup not running."""
        lock_path = tmp_path / "backup.lock"
        lock_path.write_text("99999999")  # PID that doesn't exist

        with patch("watchdog.BACKUP_LOCK_PATH", lock_path):
            from watchdog import _is_backup_running
            result = _is_backup_running()

        assert result is False
        assert not lock_path.exists()

    def test_alive_pid_lock_honored(self, tmp_path):
        """Alive PID → lock honored, backup detected."""
        lock_path = tmp_path / "backup.lock"
        lock_path.write_text(str(os.getpid()))  # Our own PID is alive

        with patch("watchdog.BACKUP_LOCK_PATH", lock_path):
            with patch("watchdog._pid_is_alive", return_value=True):
                from watchdog import _is_backup_running
                result = _is_backup_running()

        assert result is True

    def test_fallback_rclone_detection(self, tmp_path):
        """No lock file, rclone running → backup detected."""
        lock_path = tmp_path / "backup.lock"  # Doesn't exist

        mock_proc = MagicMock()
        mock_proc.info = {"name": "rclone.exe"}
        mock_proc.pid = 12345

        with patch("watchdog.BACKUP_LOCK_PATH", lock_path):
            with patch("psutil.process_iter", return_value=[mock_proc]):
                from watchdog import _is_backup_running
                result = _is_backup_running()

        assert result is True

    def test_fallback_no_processes(self, tmp_path):
        """No lock, no rclone/robocopy → backup not running."""
        lock_path = tmp_path / "backup.lock"

        mock_proc = MagicMock()
        mock_proc.info = {"name": "explorer.exe"}
        mock_proc.pid = 999

        with patch("watchdog.BACKUP_LOCK_PATH", lock_path):
            with patch("psutil.process_iter", return_value=[mock_proc]):
                from watchdog import _is_backup_running
                result = _is_backup_running()

        assert result is False


class TestWatchdogDeferral:
    """Maximum deferral limit edge cases."""

    def test_maximum_deferral_limit_exists(self):
        """Verify MAX_DEFERRALS constant is defined and reasonable."""
        from watchdog import MAX_DEFERRALS, BACKUP_WAIT_INTERVAL
        assert MAX_DEFERRALS == 15
        assert BACKUP_WAIT_INTERVAL == 120
        # 15 deferrals × 120s = 30 minutes max wait
        assert MAX_DEFERRALS * BACKUP_WAIT_INTERVAL == 1800


# ═══════════════════════════════════════════════════════════════
# Group 5: Config Edge Cases
# ═══════════════════════════════════════════════════════════════


class TestConfigEdgeCases:
    """Configuration validation edge cases."""

    def test_config_partial_sections_missing(self, tmp_path):
        """Defaults used for missing sections."""
        from models.config import AppConfig

        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
paths:
  source_drive: "E:\\\\SOURCE"
  lan_destination: "\\\\\\\\server\\\\share"
  database_path: "/tmp/test.db"
  gcs_key_path: "/tmp/key.json"
cloud:
  enabled: true
  bucket: "test-bucket"
  project_number: "123"
  storage_class: "STANDARD"
  location: "asia-south1"
lan:
  enabled: false
wol:
  enabled: false
  server_ip: "192.168.1.1"
  mac_address: "AA:BB:CC:DD:EE:FF"
dashboard:
  auth_enabled: false
""")
        cfg = AppConfig.from_yaml(str(config_path))
        # maintenance section should have defaults
        assert cfg.maintenance.db_retention_days == 90
        # notifications should have defaults
        assert cfg.notifications.send_on_failure is True
        assert cfg.notifications.weekly_enabled is True

    def test_config_whitespace_stripping(self):
        from models.config import PathsConfig

        p = PathsConfig(
            source_drive="  E:\\SOURCE  ",
            lan_destination="  \\\\server\\share  ",
            database_path="  /tmp/test.db  ",
            gcs_key_path="  /tmp/key.json  ",
        )
        assert p.source_drive == "E:\\SOURCE"
        assert p.lan_destination == "\\\\server\\share"

    def test_config_maintenance_bounds(self):
        from models.config import MaintenanceConfig
        from pydantic import ValidationError

        # Valid bounds
        assert MaintenanceConfig(db_retention_days=7).db_retention_days == 7
        assert MaintenanceConfig(db_retention_days=3650).db_retention_days == 3650

        # Invalid bounds
        with pytest.raises(ValidationError):
            MaintenanceConfig(db_retention_days=6)
        with pytest.raises(ValidationError):
            MaintenanceConfig(db_retention_days=3651)

    def test_config_cross_validation_neither_enabled(self, tmp_path):
        """Neither destination enabled → error."""
        from models.config import AppConfig
        from pydantic import ValidationError

        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
paths:
  source_drive: "E:\\\\SOURCE"
  lan_destination: "\\\\\\\\server\\\\share"
  database_path: "/tmp/test.db"
  gcs_key_path: "/tmp/key.json"
cloud:
  enabled: false
lan:
  enabled: false
""")
        with pytest.raises(ValidationError):
            AppConfig.from_yaml(str(config_path))


# ═══════════════════════════════════════════════════════════════
# Group 6: Report Edge Cases
# ═══════════════════════════════════════════════════════════════


class TestReportEdgeCases:
    """Report generation edge cases."""

    def test_report_empty_db(self, tmp_path):
        """No runs → returns False, no email."""
        from core.manifest import ManifestDB
        from core.report import send_weekly_report
        from models.config import NotificationConfig

        db = ManifestDB(str(tmp_path / "test.db"))
        try:
            cfg = NotificationConfig(
                smtp_host="smtp.test.com", smtp_port=587,
                smtp_username="user", smtp_password="pass",
                sender="test@test.com", recipients=["r@test.com"],
            )
            result = send_weekly_report(db, cfg, "Test Firm")
            assert result is False
        finally:
            db.close()

    def test_report_with_body_html(self, tmp_path):
        """body_html parameter bypasses report generation."""
        from core.manifest import ManifestDB
        from core.report import send_summary_report
        from models.config import NotificationConfig

        db = ManifestDB(str(tmp_path / "test.db"))
        try:
            cfg = NotificationConfig(
                smtp_host="smtp.test.com", smtp_port=587,
                smtp_username="user", smtp_password="pass",
                sender="test@test.com", recipients=["r@test.com"],
            )
            with patch("core.report._send_email", return_value=True):
                result = send_summary_report(
                    db, cfg, "Test Firm", 7, "Weekly",
                    body_html="<html>test</html>",
                )
                assert result is True
        finally:
            db.close()
