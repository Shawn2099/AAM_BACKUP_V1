"""Tests for manifest.py — ON CONFLICT dedup, mode validation, edge cases."""

import pytest

from core.manifest import ManifestDB


class TestInsertRunUpsert:
    def test_insert_creates_new_run(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.insert_run({
            "run_id": "r1", "mode": "cloud",
            "started_at": "2026-01-01T00:00:00Z", "status": "CLOUD_COMPLETE",
        })
        run = db.last_run("cloud")
        assert run["run_id"] == "r1"
        assert run["status"] == "CLOUD_COMPLETE"
        db.close()

    def test_upsert_updates_on_conflict(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.insert_run({
            "run_id": "r1", "mode": "cloud",
            "started_at": "2026-01-01T00:00:00Z", "status": "CLOUD_FAILED",
            "error_message": "attempt 1 failed",
        })
        db.insert_run({
            "run_id": "r1", "mode": "cloud",
            "started_at": "2026-01-01T00:00:00Z", "status": "CLOUD_COMPLETE",
            "ended_at": "2026-01-01T01:00:00Z", "error_message": None,
        })
        runs = db.get_recent_runs(10)
        assert len(runs) == 1  # Only one entry, not two
        assert runs[0]["status"] == "CLOUD_COMPLETE"
        assert runs[0]["error_message"] is None
        db.close()

    def test_upsert_preserves_started_at(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.insert_run({
            "run_id": "r1", "mode": "cloud",
            "started_at": "2026-01-01T00:00:00Z", "status": "CLOUD_FAILED",
        })
        db.insert_run({
            "run_id": "r1", "mode": "cloud",
            "started_at": "2026-01-01T00:00:00Z", "status": "CLOUD_COMPLETE",
            "ended_at": "2026-01-01T01:00:00Z",
        })
        run = db.last_run("cloud")
        assert run["started_at"] == "2026-01-01T00:00:00Z"
        db.close()

    def test_different_run_ids_are_separate(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.insert_run({
            "run_id": "r1", "mode": "cloud",
            "started_at": "2026-01-01T00:00:00Z", "status": "CLOUD_COMPLETE",
        })
        db.insert_run({
            "run_id": "r2", "mode": "cloud",
            "started_at": "2026-01-02T00:00:00Z", "status": "CLOUD_COMPLETE",
        })
        runs = db.get_recent_runs(10)
        assert len(runs) == 2
        db.close()


class TestBulkUpsertSyncedModeValidation:
    def test_invalid_mode_raises(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        with pytest.raises(ValueError, match="mode must be"):
            db.bulk_upsert_synced([{"path": "f.txt", "size": 1, "mtime": 1.0}], "invalid")
        db.close()

    def test_cloud_mode_works(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.bulk_upsert_synced([{"path": "f.txt", "size": 1, "mtime": 1.0}], "cloud")
        assert db.file_count("cloud_status") == 1
        db.close()

    def test_lan_mode_works(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.bulk_upsert_synced([{"path": "f.txt", "size": 1, "mtime": 1.0}], "lan")
        assert db.file_count("lan_status") == 1
        db.close()


class TestManifestEdgeCases:
    def test_delete_nonexistent_paths_noop(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.delete_entries(["nonexistent.txt"])  # Should not raise
        db.close()

    def test_mark_synced_empty_paths_noop(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.mark_lan_synced([])
        db.mark_cloud_synced([])
        db.close()

    def test_file_count_invalid_field_raises(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        with pytest.raises(ValueError, match="status_field must be"):
            db.file_count("invalid_status")
        db.close()

    def test_get_entry_nonexistent_returns_none(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        assert db.get_entry("nonexistent.txt") is None
        db.close()

    def test_concurrent_upserts_safe(self, temp_db_path):
        """Multiple upserts in sequence should not corrupt data."""
        db = ManifestDB(temp_db_path)
        for i in range(100):
            db.upsert_file_entry(f"file_{i}.txt", i, float(i), cloud_status="synced")
        assert db.file_count("cloud_status") == 100
        db.close()
