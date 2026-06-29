"""Comprehensive tests for core/manifest.py — ManifestDB with real SQLite."""

from __future__ import annotations

import pytest

from core.manifest import ManifestDB


@pytest.fixture(autouse=True, scope="session")
def prefect_harness():
    """Override session-scoped fixture from conftest to avoid Prefect server startup."""
    yield


@pytest.fixture
def db(tmp_path):
    """Create a fresh ManifestDB for each test."""
    db_path = tmp_path / "test_manifest.db"
    manifest = ManifestDB(db_path)
    yield manifest
    manifest.close()


# ── Pragmas and initialization ───────────────────────────────────────────────


class TestInitialization:
    def test_wal_mode_enabled(self, db):
        conn = db._get_conn()
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    def test_synchronous_normal(self, db):
        conn = db._get_conn()
        row = conn.execute("PRAGMA synchronous").fetchone()
        assert row[0] == 1  # NORMAL = 1

    def test_foreign_keys_on(self, db):
        conn = db._get_conn()
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1

    def test_creates_parent_dirs(self, tmp_path):
        nested = tmp_path / "sub" / "dir" / "test.db"
        manifest = ManifestDB(nested)
        conn = manifest._get_conn()
        row = conn.execute("SELECT 1").fetchone()
        assert row[0] == 1
        manifest.close()

    def test_schema_version_inserted(self, db):
        conn = db._get_conn()
        row = conn.execute("SELECT value FROM db_meta WHERE key = 'schema_version'").fetchone()
        assert row is not None
        assert row[0] == "1"


# ── close ────────────────────────────────────────────────────────────────────


class TestClose:
    def test_close_sets_conn_none(self, db):
        db._get_conn()
        db.close()
        assert db._conn is None

    def test_close_idempotent(self, db):
        db.close()
        db.close()
        assert db._conn is None


# ── insert_run ───────────────────────────────────────────────────────────────


class TestInsertRun:
    def test_inserts_all_fields(self, db):
        data = {
            "run_id": "run-001",
            "mode": "cloud",
            "started_at": "2026-06-24T10:00:00+05:30",
            "ended_at": "2026-06-24T10:05:00+05:30",
            "status": "CLOUD_COMPLETE",
            "exit_code": 0,
            "files_copied": 100,
            "bytes_copied": 50000,
            "files_failed": 2,
            "duration_seconds": 300.5,
            "error_message": None,
            "extended_metrics": '{"rclone_stats": true}',
        }
        db.insert_run(data)
        runs = db.get_recent_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-001"
        assert runs[0]["files_copied"] == 100
        assert runs[0]["extended_metrics"] == '{"rclone_stats": true}'

    def test_inserts_partial_fields(self, db):
        data = {
            "run_id": "run-002",
            "mode": "lan",
            "started_at": "2026-06-24T10:00:00+05:30",
            "status": "LAN_COMPLETE",
        }
        db.insert_run(data)
        runs = db.get_recent_runs()
        assert len(runs) == 1
        assert runs[0]["files_copied"] == 0
        assert runs[0]["bytes_copied"] == 0

    def test_raises_on_missing_required_keys(self, db):
        with pytest.raises(KeyError, match="missing required keys"):
            db.insert_run({"run_id": "r1"})  # missing mode, started_at, status

    def test_upsert_on_duplicate_run_id(self, db):
        data1 = {
            "run_id": "run-003",
            "mode": "cloud",
            "started_at": "2026-06-24T10:00:00+05:30",
            "status": "RUNNING",
        }
        data2 = {
            "run_id": "run-003",
            "mode": "cloud",
            "started_at": "2026-06-24T10:00:00+05:30",
            "status": "CLOUD_COMPLETE",
            "files_copied": 50,
        }
        db.insert_run(data1)
        db.insert_run(data2)
        runs = db.get_recent_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == "CLOUD_COMPLETE"
        assert runs[0]["files_copied"] == 50


# ── get_recent_runs ──────────────────────────────────────────────────────────


class TestGetRecentRuns:
    def test_empty_when_no_runs(self, db):
        assert db.get_recent_runs() == []

    def test_returns_most_recent_first(self, db):
        for i in range(5):
            db.insert_run({
                "run_id": f"run-{i:03d}",
                "mode": "cloud",
                "started_at": f"2026-06-24T{10 + i:02d}:00:00+05:30",
                "status": "CLOUD_COMPLETE",
            })
        runs = db.get_recent_runs(limit=3)
        assert len(runs) == 3
        assert runs[0]["started_at"] > runs[1]["started_at"]

    def test_respects_limit(self, db):
        for i in range(10):
            db.insert_run({
                "run_id": f"run-{i:03d}",
                "mode": "cloud",
                "started_at": f"2026-06-24T{10:02d}:00:00+05:30",
                "status": "CLOUD_COMPLETE",
            })
        assert len(db.get_recent_runs(limit=5)) == 5


# ── last_run ─────────────────────────────────────────────────────────────────


class TestLastRun:
    def test_returns_none_when_empty(self, db):
        assert db.last_run() is None

    def test_returns_most_recent(self, db):
        db.insert_run({
            "run_id": "run-old",
            "mode": "cloud",
            "started_at": "2026-06-23T10:00:00+05:30",
            "status": "CLOUD_COMPLETE",
        })
        db.insert_run({
            "run_id": "run-new",
            "mode": "cloud",
            "started_at": "2026-06-24T10:00:00+05:30",
            "status": "CLOUD_COMPLETE",
        })
        assert db.last_run()["run_id"] == "run-new"

    def test_filters_by_mode(self, db):
        db.insert_run({
            "run_id": "run-cloud",
            "mode": "cloud",
            "started_at": "2026-06-24T11:00:00+05:30",
            "status": "CLOUD_COMPLETE",
        })
        db.insert_run({
            "run_id": "run-lan",
            "mode": "lan",
            "started_at": "2026-06-24T10:00:00+05:30",
            "status": "LAN_COMPLETE",
        })
        assert db.last_run(mode="lan")["run_id"] == "run-lan"


# ── file_count ───────────────────────────────────────────────────────────────


class TestFileCount:
    def test_zero_when_empty(self, db):
        assert db.file_count() == 0

    def test_counts_synced_entries(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0, lan_status="synced")
        db.upsert_file_entry("b.txt", 200, 2.0, lan_status="synced")
        db.upsert_file_entry("c.txt", 300, 3.0, lan_status="unknown")
        assert db.file_count("lan_status") == 2

    def test_counts_cloud_synced(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0, cloud_status="synced")
        assert db.file_count("cloud_status") == 1

    def test_raises_on_invalid_field(self, db):
        with pytest.raises(ValueError, match="status_field"):
            db.file_count("invalid_field")


# ── upsert_file_entry ───────────────────────────────────────────────────────


class TestUpsertFileEntry:
    def test_inserts_new_entry(self, db):
        db.upsert_file_entry("docs/file.txt", 1024, 1234567890.0, lan_status="synced")
        entry = db.get_entry("docs/file.txt")
        assert entry is not None
        assert entry["file_size"] == 1024
        assert entry["lan_status"] == "synced"

    def test_updates_existing_entry(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0, lan_status="unknown")
        db.upsert_file_entry("a.txt", 200, 2.0, lan_status="synced")
        entry = db.get_entry("a.txt")
        assert entry["file_size"] == 200
        assert entry["lan_status"] == "synced"

    def test_normalizes_backslash(self, db):
        db.upsert_file_entry("sub\\file.txt", 100, 1.0)
        entry = db.get_entry("sub/file.txt")
        assert entry is not None

    def test_preserves_existing_synced_timestamp(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0, lan_status="synced")
        entry_before = db.get_entry("a.txt")
        ts_before = entry_before["lan_last_synced_at"]
        # Update without changing status — timestamp should stay
        db.upsert_file_entry("a.txt", 200, 2.0, lan_status="synced")
        entry_after = db.get_entry("a.txt")
        assert entry_after["lan_last_synced_at"] == ts_before

    def test_updates_timestamp_on_status_change(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0, lan_status="unknown")
        db.upsert_file_entry("a.txt", 200, 2.0, lan_status="synced")
        entry = db.get_entry("a.txt")
        assert entry["lan_last_synced_at"] is not None

    def test_coalesces_md5_checksum(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0, md5_checksum="abc123")
        db.upsert_file_entry("a.txt", 200, 2.0, md5_checksum=None)
        entry = db.get_entry("a.txt")
        assert entry["md5_checksum"] == "abc123"


# ── get_entry ────────────────────────────────────────────────────────────────


class TestGetEntry:
    def test_returns_none_when_not_exists(self, db):
        assert db.get_entry("no/such/file.txt") is None

    def test_returns_dict_when_exists(self, db):
        db.upsert_file_entry("x.txt", 50, 99.0)
        entry = db.get_entry("x.txt")
        assert isinstance(entry, dict)
        assert entry["relative_path"] == "x.txt"

    def test_normalizes_backslash_on_get(self, db):
        db.upsert_file_entry("a/b.txt", 10, 1.0)
        entry = db.get_entry("a\\b.txt")
        assert entry is not None


# ── update_checksums ─────────────────────────────────────────────────────────


class TestUpdateChecksums:
    def test_updates_checksums(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0)
        db.upsert_file_entry("b.txt", 200, 2.0)
        db.update_checksums({"a.txt": "hash_a", "b.txt": "hash_b"})
        assert db.get_entry("a.txt")["md5_checksum"] == "hash_a"
        assert db.get_entry("b.txt")["md5_checksum"] == "hash_b"

    def test_noop_on_empty_dict(self, db):
        db.update_checksums({})

    def test_normalizes_backslash(self, db):
        db.upsert_file_entry("sub\\file.txt", 100, 1.0)
        db.update_checksums({"sub\\file.txt": "hash1"})
        assert db.get_entry("sub/file.txt")["md5_checksum"] == "hash1"


# ── purge_old_runs ───────────────────────────────────────────────────────────


class TestPurgeOldRuns:
    def test_removes_old_runs(self, db):
        db.insert_run({
            "run_id": "run-old",
            "mode": "cloud",
            "started_at": "2025-01-01T10:00:00+05:30",
            "status": "CLOUD_COMPLETE",
        })
        db.insert_run({
            "run_id": "run-new",
            "mode": "cloud",
            "started_at": "2026-06-24T10:00:00+05:30",
            "status": "CLOUD_COMPLETE",
        })
        db.purge_old_runs(retention_days=30)
        runs = db.get_recent_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-new"

    def test_keeps_all_when_within_retention(self, db):
        db.insert_run({
            "run_id": "run-1",
            "mode": "cloud",
            "started_at": "2026-06-24T10:00:00+05:30",
            "status": "CLOUD_COMPLETE",
        })
        db.purge_old_runs(retention_days=365)
        assert len(db.get_recent_runs()) == 1


# ── mark_lan_synced / mark_cloud_synced ──────────────────────────────────────


class TestMarkSynced:
    def test_mark_lan_synced(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0)
        db.upsert_file_entry("b.txt", 200, 2.0)
        db.mark_lan_synced(["a.txt", "b.txt"])
        assert db.get_entry("a.txt")["lan_status"] == "synced"
        assert db.get_entry("b.txt")["lan_status"] == "synced"

    def test_mark_cloud_synced(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0)
        db.mark_cloud_synced(["a.txt"])
        assert db.get_entry("a.txt")["cloud_status"] == "synced"

    def test_empty_list_noop(self, db):
        db.mark_lan_synced([])
        db.mark_cloud_synced([])

    def test_normalizes_backslash(self, db):
        db.upsert_file_entry("sub\\file.txt", 100, 1.0)
        db.mark_lan_synced(["sub\\file.txt"])
        assert db.get_entry("sub/file.txt")["lan_status"] == "synced"


# ── delete_entries ───────────────────────────────────────────────────────────


class TestDeleteEntries:
    def test_deletes_entries(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0)
        db.upsert_file_entry("b.txt", 200, 2.0)
        db.delete_entries(["a.txt"])
        assert db.get_entry("a.txt") is None
        assert db.get_entry("b.txt") is not None

    def test_empty_list_noop(self, db):
        db.delete_entries([])

    def test_normalizes_backslash(self, db):
        db.upsert_file_entry("sub\\file.txt", 100, 1.0)
        db.delete_entries(["sub\\file.txt"])
        assert db.get_entry("sub/file.txt") is None


# ── get_runs_since ───────────────────────────────────────────────────────────


class TestGetRunsSince:
    def test_filters_by_days(self, db):
        db.insert_run({
            "run_id": "run-old",
            "mode": "cloud",
            "started_at": "2025-01-01T10:00:00+05:30",
            "status": "CLOUD_COMPLETE",
        })
        db.insert_run({
            "run_id": "run-new",
            "mode": "cloud",
            "started_at": "2026-06-24T10:00:00+05:30",
            "status": "CLOUD_COMPLETE",
        })
        runs = db.get_runs_since(days=1)
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-new"

    def test_filters_by_mode(self, db):
        db.insert_run({
            "run_id": "run-cloud",
            "mode": "cloud",
            "started_at": "2026-06-24T10:00:00+05:30",
            "status": "CLOUD_COMPLETE",
        })
        db.insert_run({
            "run_id": "run-lan",
            "mode": "lan",
            "started_at": "2026-06-24T10:00:00+05:30",
            "status": "LAN_COMPLETE",
        })
        runs = db.get_runs_since(days=30, mode="cloud")
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-cloud"


# ── bulk_upsert_synced ──────────────────────────────────────────────────────


class TestBulkUpsertSynced:
    def test_bulk_inserts(self, db):
        entries = [
            {"path": f"file_{i}.txt", "size": i * 100, "mtime": float(i)}
            for i in range(5)
        ]
        db.bulk_upsert_synced(entries, "lan")
        assert db.file_count("lan_status") == 5

    def test_empty_list_noop(self, db):
        db.bulk_upsert_synced([], "lan")

    def test_invalid_mode_raises(self, db):
        with pytest.raises(ValueError, match="mode must be"):
            db.bulk_upsert_synced([{"path": "a.txt", "size": 10, "mtime": 1.0}], "invalid")

    def test_chunking_many_entries(self, db):
        entries = [{"path": f"f{i}.txt", "size": 10, "mtime": 1.0} for i in range(250)]
        db.bulk_upsert_synced(entries, "cloud")
        assert db.file_count("cloud_status") == 250

    def test_normalizes_backslash(self, db):
        db.bulk_upsert_synced([{"path": "sub\\file.txt", "size": 10, "mtime": 1.0}], "lan")
        assert db.get_entry("sub/file.txt") is not None


# ── prune_stale_synced ──────────────────────────────────────────────────────


class TestPruneStaleSynced:
    def test_prunes_stale_entries(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0, lan_status="synced")
        db.upsert_file_entry("b.txt", 200, 2.0, lan_status="synced")
        pruned = db.prune_stale_synced("lan", {"a.txt"})
        assert pruned == 1
        assert db.get_entry("a.txt")["lan_status"] == "synced"
        # b.txt has both statuses NULL after pruning → fully deleted
        assert db.get_entry("b.txt") is None

    def test_returns_zero_when_no_stale(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0, lan_status="synced")
        pruned = db.prune_stale_synced("lan", {"a.txt"})
        assert pruned == 0

    def test_deletes_fully_null_entries(self, db):
        db.upsert_file_entry("orphan.txt", 100, 1.0, lan_status="synced")
        db.prune_stale_synced("lan", set())
        # Both statuses NULL → entry deleted
        entry = db.get_entry("orphan.txt")
        assert entry is None

    def test_invalid_mode_raises(self, db):
        with pytest.raises(ValueError, match="mode must be"):
            db.prune_stale_synced("invalid", set())


# ── get_synced_paths ─────────────────────────────────────────────────────────


class TestGetSyncedPaths:
    def test_returns_synced_paths(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0, lan_status="synced")
        db.upsert_file_entry("b.txt", 200, 2.0, cloud_status="synced")
        assert db.get_synced_paths("lan") == ["a.txt"]
        assert db.get_synced_paths("cloud") == ["b.txt"]

    def test_invalid_mode_raises(self, db):
        with pytest.raises(ValueError, match="mode must be"):
            db.get_synced_paths("invalid")


# ── get_cloud_synced_entries ─────────────────────────────────────────────────


class TestGetCloudSyncedEntries:
    def test_returns_dict_of_synced(self, db):
        db.upsert_file_entry("a.txt", 100, 1.0, cloud_status="synced")
        db.upsert_file_entry("b.txt", 200, 2.0, cloud_status="unknown")
        result = db.get_cloud_synced_entries()
        assert "a.txt" in result
        assert result["a.txt"] == (100, 1.0)
        assert "b.txt" not in result


# ── wal_checkpoint ───────────────────────────────────────────────────────────


class TestWalCheckpoint:
    def test_does_not_raise(self, db):
        db.insert_run({
            "run_id": "r1",
            "mode": "cloud",
            "started_at": "2026-06-24T10:00:00+05:30",
            "status": "CLOUD_COMPLETE",
        })
        db.wal_checkpoint()
