"""Comprehensive tests for core/backup_repository.py — DB write operations."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.manifest import ManifestDB
from core.backup_repository import record_run_history, record_sync_results


@pytest.fixture(autouse=True, scope="session")
def prefect_harness():
    """Override session-scoped fixture from conftest to avoid Prefect server startup."""
    yield


@pytest.fixture
def db(tmp_path):
    """Create a fresh ManifestDB for each test."""
    db_path = tmp_path / "test_backup_repo.db"
    manifest = ManifestDB(db_path)
    yield manifest
    manifest.close()


# ── record_sync_results ──────────────────────────────────────────────────────


class TestRecordSyncResults:
    def test_records_rclone_format_entries(self, db):
        entries = [
            {"Path": "file1.txt", "Size": 100, "ModTime": 1234567890.0},
            {"Path": "dir/file2.txt", "Size": 200, "ModTime": 1234567891.0},
        ]
        record_sync_results(db, "cloud", entries)
        assert db.file_count("cloud_status") == 2
        entry = db.get_entry("file1.txt")
        assert entry["file_size"] == 100

    def test_records_walk_format_entries(self, db):
        entries = [
            {"path": "file_a.txt", "size": 300, "mtime": 1234567892.0},
            {"path": "sub/file_b.txt", "size": 400, "mtime": 1234567893.0},
        ]
        record_sync_results(db, "lan", entries)
        assert db.file_count("lan_status") == 2
        entry = db.get_entry("file_a.txt")
        assert entry["file_size"] == 300

    def test_records_mixed_format_entries(self, db):
        entries = [
            {"Path": "rclone_file.txt", "Size": 100, "ModTime": 1.0},
            {"path": "walk_file.txt", "size": 200, "mtime": 2.0},
        ]
        record_sync_results(db, "cloud", entries)
        assert db.file_count("cloud_status") == 2

    def test_handles_empty_entries(self, db):
        record_sync_results(db, "cloud", [])
        assert db.file_count("cloud_status") == 0

    def test_handles_none_entries(self, db):
        record_sync_results(db, "cloud", None)
        assert db.file_count("cloud_status") == 0

    def test_prunes_stale_entries(self, db):
        # Pre-populate with an entry that's marked synced
        db.upsert_file_entry("old_file.txt", 100, 1.0, cloud_status="synced")
        # Record new entries that don't include old_file.txt
        entries = [{"Path": "new_file.txt", "Size": 200, "ModTime": 2.0}]
        record_sync_results(db, "cloud", entries)
        # old_file.txt should be pruned (both statuses null → deleted)
        assert db.get_entry("old_file.txt") is None
        assert db.get_entry("new_file.txt") is not None

    def test_deletes_removed_entries(self, db):
        db.upsert_file_entry("delete_me.txt", 100, 1.0)
        entries = [{"Path": "keep.txt", "Size": 200, "ModTime": 2.0}]
        record_sync_results(db, "cloud", entries, removed=["delete_me.txt"])
        assert db.get_entry("delete_me.txt") is None
        assert db.get_entry("keep.txt") is not None

    def test_handles_empty_removed_list(self, db):
        entries = [{"Path": "a.txt", "Size": 100, "ModTime": 1.0}]
        record_sync_results(db, "cloud", entries, removed=[])
        assert db.file_count("cloud_status") == 1

    def test_handles_none_removed(self, db):
        entries = [{"Path": "a.txt", "Size": 100, "ModTime": 1.0}]
        record_sync_results(db, "cloud", entries, removed=None)
        assert db.file_count("cloud_status") == 1

    def test_normalizes_backslash_in_entries(self, db):
        entries = [{"Path": "sub\\file.txt", "Size": 100, "ModTime": 1.0}]
        record_sync_results(db, "lan", entries)
        assert db.get_entry("sub/file.txt") is not None

    def test_defaults_missing_fields(self, db):
        entries = [{"Path": "minimal.txt"}]
        record_sync_results(db, "cloud", entries)
        entry = db.get_entry("minimal.txt")
        assert entry["file_size"] == 0
        assert entry["mtime"] == 0


# ── record_run_history ───────────────────────────────────────────────────────


class TestRecordRunHistory:
    def test_records_run_successfully(self, db):
        result = record_run_history(
            db,
            run_id="run-001",
            mode="cloud",
            started_at="2026-06-24T10:00:00+05:30",
            ended_at="2026-06-24T10:05:00+05:30",
            status="CLOUD_COMPLETE",
            exit_code=0,
            duration_seconds=300.0,
            files_copied=100,
            bytes_copied=50000,
        )
        assert result is True
        runs = db.get_recent_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-001"

    def test_returns_false_on_db_error(self, db):
        with patch.object(db, "insert_run", side_effect=Exception("db locked")):
            result = record_run_history(
                db,
                run_id="run-002",
                mode="lan",
                started_at="2026-06-24T10:00:00+05:30",
                ended_at="2026-06-24T10:05:00+05:30",
                status="LAN_COMPLETE",
                exit_code=0,
                duration_seconds=300.0,
            )
        assert result is False

    def test_calls_wal_checkpoint(self, db):
        with patch.object(db, "wal_checkpoint") as mock_ckpt:
            record_run_history(
                db,
                run_id="run-003",
                mode="cloud",
                started_at="2026-06-24T10:00:00+05:30",
                ended_at="2026-06-24T10:05:00+05:30",
                status="CLOUD_COMPLETE",
                exit_code=0,
                duration_seconds=300.0,
            )
            mock_ckpt.assert_called_once()

    def test_includes_extended_metrics(self, db):
        record_run_history(
            db,
            run_id="run-004",
            mode="cloud",
            started_at="2026-06-24T10:00:00+05:30",
            ended_at="2026-06-24T10:05:00+05:30",
            status="CLOUD_COMPLETE",
            exit_code=0,
            duration_seconds=300.0,
            extended_metrics='{"key": "value"}',
        )
        runs = db.get_recent_runs()
        assert runs[0]["extended_metrics"] == '{"key": "value"}'

    def test_includes_error_message(self, db):
        record_run_history(
            db,
            run_id="run-005",
            mode="cloud",
            started_at="2026-06-24T10:00:00+05:30",
            ended_at="2026-06-24T10:05:00+05:30",
            status="FAILED",
            exit_code=1,
            duration_seconds=300.0,
            error_message="something went wrong",
        )
        runs = db.get_recent_runs()
        assert runs[0]["error_message"] == "something went wrong"
        assert runs[0]["exit_code"] == 1

    def test_defaults_optional_fields(self, db):
        record_run_history(
            db,
            run_id="run-006",
            mode="lan",
            started_at="2026-06-24T10:00:00+05:30",
            ended_at="2026-06-24T10:05:00+05:30",
            status="LAN_COMPLETE",
            exit_code=0,
            duration_seconds=300.0,
        )
        runs = db.get_recent_runs()
        assert runs[0]["files_copied"] == 0
        assert runs[0]["bytes_copied"] == 0
        assert runs[0]["error_message"] is None
        assert runs[0]["extended_metrics"] is None
