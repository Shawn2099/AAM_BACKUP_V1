"""Tests for ManifestDB — SQLite operations."""


import pytest

from core.manifest import ManifestDB


class TestManifestDB:
    def test_create_db_and_upsert(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.upsert_file_entry("file1.txt", 1024, 1717200000.0, lan_status="synced")
        entry = db.get_entry("file1.txt")
        assert entry is not None
        assert entry["relative_path"] == "file1.txt"
        assert entry["file_size"] == 1024
        assert entry["lan_status"] == "synced"
        db.close()
    def test_pragmas_applied(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        conn = db._get_conn()
        
        # Verify WAL mode
        cursor = conn.execute("PRAGMA journal_mode;")
        assert cursor.fetchone()[0].upper() == "WAL"
        
        # Verify HDD optimized synchronous mode (NORMAL == 1)
        cursor = conn.execute("PRAGMA synchronous;")
        assert cursor.fetchone()[0] == 1
        
        # Verify Foreign Keys
        cursor = conn.execute("PRAGMA foreign_keys;")
        assert cursor.fetchone()[0] == 1
        
        db.close()

    def test_upsert_with_cloud_status(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.upsert_file_entry("file2.txt", 2048, 1717200000.0, cloud_status="synced")
        entry = db.get_entry("file2.txt")
        assert entry["cloud_status"] == "synced"
        db.close()

    def test_upsert_updates_existing(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.upsert_file_entry("dup.txt", 100, 100.0, lan_status="synced")
        db.upsert_file_entry("dup.txt", 200, 200.0, cloud_status="synced")
        entry = db.get_entry("dup.txt")
        assert entry["file_size"] == 200
        assert entry["lan_status"] == "synced"
        assert entry["cloud_status"] == "synced"
        db.close()

    def test_mark_lan_synced_bulk(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.upsert_file_entry("a.txt", 10, 1.0)
        db.upsert_file_entry("b.txt", 20, 2.0)
        db.mark_lan_synced(["a.txt", "b.txt"])
        assert db.get_entry("a.txt")["lan_status"] == "synced"
        assert db.get_entry("b.txt")["lan_status"] == "synced"
        db.close()

    def test_mark_cloud_synced_bulk(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.upsert_file_entry("c.txt", 10, 1.0)
        db.mark_cloud_synced(["c.txt"])
        assert db.get_entry("c.txt")["cloud_status"] == "synced"
        db.close()

    def test_mark_empty_list_noop(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.mark_lan_synced([])
        db.mark_cloud_synced([])
        db.close()

    def test_delete_entries(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.upsert_file_entry("del.txt", 100, 1.0)
        assert db.get_entry("del.txt") is not None
        db.delete_entries(["del.txt"])
        assert db.get_entry("del.txt") is None
        db.close()

    def test_delete_empty_list_noop(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.delete_entries([])
        db.close()

    def test_file_count(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.upsert_file_entry("lan1.txt", 10, 1.0, lan_status="synced")
        db.upsert_file_entry("lan2.txt", 20, 2.0, lan_status="synced")
        db.upsert_file_entry("cloud1.txt", 30, 3.0, cloud_status="synced")
        assert db.file_count("lan_status") == 2
        assert db.file_count("cloud_status") == 1
        db.close()

    def test_insert_run_and_get_runs(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.insert_run({
            "run_id": "run-001",
            "mode": "cloud",
            "started_at": "2026-05-27T10:00:00+00:00",
            "status": "CLOUD_COMPLETE",
            "exit_code": 0,
            "duration_seconds": 120.5,
        })
        last = db.last_run("cloud")
        assert last is not None
        assert last["run_id"] == "run-001"
        assert last["status"] == "CLOUD_COMPLETE"
        db.close()

    def test_last_run_none_for_empty_db(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        assert db.last_run("cloud") is None
        db.close()

    def test_get_recent_runs_limit(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        for i in range(15):
            db.insert_run({
                "run_id": f"run-{i:03d}",
                "mode": "cloud",
                "started_at": f"2026-05-27T10:{i:02d}:00+00:00",
                "status": "CLOUD_COMPLETE",
            })
        runs = db.get_recent_runs(10)
        assert len(runs) == 10
        runs_all = db.get_recent_runs(50)
        assert len(runs_all) == 15
        db.close()

    def test_purge_old_runs(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.insert_run({
            "run_id": "old-run",
            "mode": "cloud",
            "started_at": "2020-01-01T00:00:00+00:00",
            "status": "CLOUD_COMPLETE",
        })
        assert db.last_run("cloud") is not None
        db.purge_old_runs(retention_days=1)
        assert db.last_run("cloud") is None
        db.close()

    def test_insert_run_missing_required_keys(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        with pytest.raises(KeyError):
            db.insert_run({"mode": "cloud"})
        db.close()

    def test_wal_checkpoint(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.upsert_file_entry("w.txt", 10, 1.0)
        db.wal_checkpoint()
        db.close()

    def test_update_checksums(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.upsert_file_entry("chk.txt", 100, 1.0)
        db.update_checksums({"chk.txt": "abc123def456"})
        entry = db.get_entry("chk.txt")
        assert entry["md5_checksum"] == "abc123def456"
        db.close()

    def test_update_checksums_empty_noop(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.update_checksums({})
        db.close()

    def test_bulk_upsert_synced_cloud(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        entries = [
            {"path": "a.txt", "size": 100, "mtime": 1.0},
            {"path": "b.txt", "size": 200, "mtime": 2.0},
            {"path": "c.txt", "size": 300, "mtime": 3.0},
        ]
        db.bulk_upsert_synced(entries, "cloud")
        assert db.file_count("cloud_status") == 3
        assert db.get_entry("a.txt")["cloud_status"] == "synced"
        assert db.get_entry("b.txt")["file_size"] == 200
        db.close()

    def test_bulk_upsert_synced_lan(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        entries = [
            {"path": "x.txt", "size": 50, "mtime": 10.0},
            {"path": "y.txt", "size": 60, "mtime": 20.0},
        ]
        db.bulk_upsert_synced(entries, "lan")
        assert db.file_count("lan_status") == 2
        assert db.get_entry("x.txt")["lan_status"] == "synced"
        db.close()

    def test_bulk_upsert_synced_updates_existing(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.upsert_file_entry("old.txt", 100, 1.0, cloud_status="unknown")
        db.bulk_upsert_synced([{"path": "old.txt", "size": 999, "mtime": 99.0}], "cloud")
        entry = db.get_entry("old.txt")
        assert entry["file_size"] == 999
        assert entry["cloud_status"] == "synced"
        db.close()

    def test_bulk_upsert_synced_empty_noop(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        db.bulk_upsert_synced([], "cloud")
        assert db.file_count("cloud_status") == 0
        db.close()

    def test_bulk_upsert_synced_with_md5(self, temp_db_path):
        db = ManifestDB(temp_db_path)
        entries = [{"path": "f.txt", "size": 10, "mtime": 1.0, "md5_checksum": "abc123"}]
        db.bulk_upsert_synced(entries, "cloud")
        assert db.get_entry("f.txt")["md5_checksum"] == "abc123"
        db.close()
