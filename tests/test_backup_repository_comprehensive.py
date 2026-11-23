"""Comprehensive tests for core/backup_repository.py — all functions."""

from unittest.mock import MagicMock, patch

import pytest

from core.backup_repository import record_run_history, record_sync_results


class TestRecordRunHistory:
    def test_success(self, tmp_db):
        """Successful record → True, and the row is queryable."""
        result = record_run_history(
            db=tmp_db,
            run_id="run-success-1",
            mode="cloud",
            fy="FY26-27",
            status="CLOUD_COMPLETE",
            started_at="2026-01-01T00:00:00",
            ended_at="2026-01-01T01:00:00",
            exit_code=0,
            duration_seconds=3600,
            files_copied=100,
            bytes_copied=10000,
        )
        assert result is True
        assert tmp_db.last_run("cloud", fy="FY26-27")["run_id"] == "run-success-1"

    def test_with_error_message(self, tmp_db):
        """Record with error message."""
        result = record_run_history(
            db=tmp_db,
            run_id="run-failed-1",
            mode="lan",
            fy="FY26-27",
            status="LAN_FAILED",
            started_at="2026-01-01T00:00:00",
            ended_at="2026-01-01T00:30:00",
            exit_code=1,
            duration_seconds=1800,
            files_copied=0,
            bytes_copied=0,
            error_message="Network timeout",
        )
        assert result is True
        assert tmp_db.last_run("lan", fy="FY26-27")["error_message"] == "Network timeout"

    def test_with_extended_metrics(self, tmp_db):
        """Record with extended metrics JSON."""
        result = record_run_history(
            db=tmp_db,
            run_id="run-metrics-1",
            mode="cloud",
            fy="FY26-27",
            status="CLOUD_COMPLETE",
            started_at="2026-01-01T00:00:00",
            ended_at="2026-01-01T01:00:00",
            exit_code=0,
            duration_seconds=3600,
            extended_metrics='{"diff_partial": true}',
        )
        assert result is True
        assert tmp_db.last_run("cloud", fy="FY26-27")["extended_metrics"] == '{"diff_partial": true}'


class TestRecordSyncResults:
    def test_rclone_shaped_entries(self, tmp_db):
        """rclone-shaped entries are upserted."""
        entries = [
            {"Path": "file1.txt", "Size": 100, "ModTime": "2026-01-01T00:00:00Z"},
            {"Path": "file2.txt", "Size": 200, "ModTime": "2026-01-01T01:00:00Z"},
        ]
        record_sync_results(tmp_db, "cloud", entries, fy="FY26-27")
        count = tmp_db.file_count("cloud_status", fy="FY26-27")
        assert count == 2
        assert tmp_db.get_entry("file1.txt", fy="FY26-27") is not None
        assert tmp_db.get_entry("file2.txt", fy="FY26-27") is not None

    def test_walk_shaped_entries(self, tmp_db):
        """walk-shaped entries are upserted."""
        entries = [
            {"path": "file1.txt", "size": 100, "mtime": 1700000000.0},
            {"path": "file2.txt", "size": 200, "mtime": 1700000001.0},
        ]
        record_sync_results(tmp_db, "lan", entries, fy="FY26-27")
        count = tmp_db.file_count("lan_status", fy="FY26-27")
        assert count == 2
        assert tmp_db.get_entry("file1.txt", fy="FY26-27") is not None
        assert tmp_db.get_entry("file2.txt", fy="FY26-27") is not None

    def test_cross_mode_delete(self, tmp_db):
        """Both modes track the same path independently — recording one mode
        does not disturb the other (cross-mode removal is driven by the
        `removed=[...]` argument, not by recording)."""
        entries = [
            {"Path": "file1.txt", "Size": 100, "ModTime": "2026-01-01T00:00:00Z"},
        ]
        record_sync_results(tmp_db, "cloud", entries, fy="FY26-27")
        # Now record for LAN — the cloud entry must be untouched
        record_sync_results(tmp_db, "lan", entries, fy="FY26-27")
        # Both should have the entry
        assert tmp_db.file_count("cloud_status", fy="FY26-27") == 1
        assert tmp_db.file_count("lan_status", fy="FY26-27") == 1
