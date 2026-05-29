"""Tests for backup_repository — DB write operations."""

from core.backup_repository import record_run_history, record_sync_results
from core.manifest import ManifestDB


class TestRecordSyncResults:
    def test_cloud_entries_rclone_format(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        entries = [
            {"Path": "a.txt", "Size": 100, "ModTime": 1.0},
            {"Path": "b.txt", "Size": 200, "ModTime": 2.0},
        ]
        record_sync_results(db, "cloud", entries)
        assert db.file_count("cloud_status") == 2
        assert db.get_entry("a.txt")["cloud_status"] == "synced"
        assert db.get_entry("b.txt")["file_size"] == 200
        db.close()

    def test_lan_entries_walk_format(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        entries = [
            {"path": "x.txt", "size": 50, "mtime": 10.0},
            {"path": "y.txt", "size": 60, "mtime": 20.0},
        ]
        record_sync_results(db, "lan", entries)
        assert db.file_count("lan_status") == 2
        assert db.get_entry("x.txt")["lan_status"] == "synced"
        db.close()

    def test_removes_deleted_entries(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.upsert_file_entry("old.txt", 100, 1.0, cloud_status="synced")
        record_sync_results(db, "cloud", [], removed=["old.txt"])
        assert db.get_entry("old.txt") is None
        db.close()

    def test_empty_entries_noop(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        record_sync_results(db, "cloud", [])
        assert db.file_count("cloud_status") == 0
        db.close()

    def test_none_removed_noop(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        entries = [{"path": "f.txt", "size": 10, "mtime": 1.0}]
        record_sync_results(db, "lan", entries, removed=None)
        assert db.file_count("lan_status") == 1
        db.close()


class TestRecordRunHistory:
    def test_records_run_and_checkpoints(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        record_run_history(
            db,
            run_id="test-123", mode="cloud",
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T01:00:00Z",
            status="CLOUD_COMPLETE", exit_code=0,
            duration_seconds=3600.0, error_message=None,
        )
        run = db.last_run("cloud")
        assert run is not None
        assert run["run_id"] == "test-123"
        assert run["status"] == "CLOUD_COMPLETE"
        assert run["error_message"] is None
        db.close()

    def test_records_error_message(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        record_run_history(
            db,
            run_id="err-1", mode="lan",
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:05:00Z",
            status="LAN_FAILED", exit_code=16,
            duration_seconds=300.0, error_message="Fatal error",
        )
        run = db.last_run("lan")
        assert run["error_message"] == "Fatal error"
        db.close()
