"""Comprehensive tests for core/manifest.py — all public methods."""

import uuid

import pytest

from core.manifest import ManifestDB

FY = "FY26-27"


def _run(mode="cloud", status="CLOUD_COMPLETE", started="2026-01-01T00:00:00", ended="2026-01-01T01:00:00", **extra):
    """Build an insert_run payload with the required keys (run_id, mode, started_at, status)."""
    data = {
        "run_id": uuid.uuid4().hex,
        "mode": mode,
        "started_at": started,
        "ended_at": ended,
        "status": status,
        "fy": FY,
    }
    data.update(extra)
    return data


@pytest.fixture
def db(tmp_path):
    database = ManifestDB(str(tmp_path / "test.db"))
    yield database
    database.close()


class TestManifestDBOperations:
    def test_insert_and_get_run(self, db):
        db.insert_run(_run(
            mode="cloud",
            files_copied=100,
            bytes_copied=10000,
            duration_seconds=3600,
        ))
        run = db.last_run("cloud", fy=FY)
        assert run is not None
        assert run["status"] == "CLOUD_COMPLETE"
        assert run["files_copied"] == 100

    def test_insert_run_rejects_missing_keys(self, db):
        """insert_run is dict-based: missing required keys raise KeyError."""
        with pytest.raises(KeyError):
            db.insert_run({"mode": "cloud"})

    def test_get_recent_runs(self, db):
        for i in range(5):
            db.insert_run(_run(
                mode="lan",
                status="LAN_COMPLETE",
                started=f"2026-01-0{i+1}T00:00:00",
                ended=f"2026-01-0{i+1}T01:00:00",
            ))
        runs = db.get_recent_runs(3, fy=FY)
        assert len(runs) == 3

    def test_get_runs_since_filters_mode(self, db):
        from core.time_utils import now_iso
        today = now_iso()
        db.insert_run(_run(mode="cloud", started=today))
        db.insert_run(_run(mode="lan", status="LAN_COMPLETE", started=today))
        cloud = db.get_runs_since(30, mode="cloud", fy=FY)
        assert all(r["mode"] == "cloud" for r in cloud)
        assert len(cloud) == 1
        assert len(db.get_runs_since(30, fy=FY)) == 2
        # A run older than the cutoff window is excluded.
        db.insert_run(_run(mode="cloud", started="2020-01-01T00:00:00"))
        assert len(db.get_runs_since(30, mode="cloud", fy=FY)) == 1

    def test_last_successful_run(self, db):
        db.insert_run(_run(mode="cloud", status="CLOUD_COMPLETE", started="2026-01-01T00:00:00"))
        db.insert_run(_run(mode="cloud", status="CLOUD_FAILED", started="2026-01-02T00:00:00"))
        run = db.last_successful_run("cloud", fy=FY)
        assert run is not None
        assert run["status"] == "CLOUD_COMPLETE"

    def test_last_successful_run_glob_not_like(self, db):
        """GLOB '_' is literal: INCOMPLETE must NOT match *_COMPLETE."""
        db.insert_run(_run(mode="cloud", status="INCOMPLETE"))
        assert db.last_successful_run("cloud", fy=FY) is None

    def test_bulk_upsert_synced(self, db):
        entries = [
            {"path": f"file_{i}.txt", "size": i * 100, "mtime": 1700000000.0 + i}
            for i in range(10)
        ]
        db.bulk_upsert_synced(entries, mode="cloud", fy=FY)
        count = db.file_count("cloud_status", fy=FY)
        assert count == 10

    def test_get_cloud_synced_entries(self, db):
        entries = [
            {"path": f"file_{i}.txt", "size": i * 100, "mtime": 1700000000.0 + i}
            for i in range(5)
        ]
        db.bulk_upsert_synced(entries, mode="cloud", fy=FY)
        result = db.get_cloud_synced_entries(fy=FY)
        assert len(result) == 5

    def test_get_lan_synced_paths(self, db):
        entries = [
            {"path": f"lan_file_{i}.txt", "size": i * 100, "mtime": 1700000000.0 + i}
            for i in range(5)
        ]
        db.bulk_upsert_synced(entries, mode="lan", fy=FY)
        result = db.get_synced_paths("lan", fy=FY)
        assert len(result) == 5

    def test_upsert_and_get_entry(self, db):
        db.upsert_file_entry("a.txt", 10, 1700000000.0, fy=FY, lan_status="synced")
        entry = db.get_entry("a.txt", fy=FY)
        assert entry is not None
        assert entry["lan_status"] == "synced"
        assert entry["file_size"] == 10

    def test_delete_entries(self, db):
        entries = [{"path": f"f{i}.txt", "size": 1, "mtime": 1700000000.0} for i in range(3)]
        db.bulk_upsert_synced(entries, mode="cloud", fy=FY)
        db.delete_entries(["f0.txt", "f1.txt"], fy=FY)  # returns None; verify by effect
        assert db.file_count("cloud_status", fy=FY) == 1
        assert db.get_entry("f0.txt", fy=FY) is None

    def test_prune_stale_synced(self, db):
        entries = [{"path": f"f{i}.txt", "size": 1, "mtime": 1700000000.0} for i in range(3)]
        db.bulk_upsert_synced(entries, mode="cloud", fy=FY)
        pruned = db.prune_stale_synced("cloud", {"f1.txt"}, fy=FY)
        assert pruned == 2
        assert db.get_synced_paths("cloud", fy=FY) == ["f1.txt"]

    def test_file_count_empty(self, db):
        assert db.file_count("cloud_status", fy=FY) == 0

    def test_purge_old_runs(self, db):
        """Old runs are purged."""
        db.insert_run(_run(
            mode="cloud",
            status="CLOUD_COMPLETE",
            started="2020-01-01T00:00:00",
            ended="2020-01-01T01:00:00",
        ))
        db.purge_old_runs(retention_days=30)
        runs = db.get_recent_runs(10, fy=FY)
        # Old run should be purged
        assert len(runs) == 0

    def test_wal_checkpoint(self, db):
        """WAL checkpoint doesn't crash."""
        db.insert_run(_run(
            mode="cloud",
            status="CLOUD_COMPLETE",
            started="2026-01-01T00:00:00",
            ended="2026-01-01T01:00:00",
        ))
        db.wal_checkpoint()

    def test_audit_operations(self, db):
        """Audit insert and retrieve (record_audit is dict-based)."""
        db.record_audit({
            "audit_id": uuid.uuid4().hex,
            "fy": FY,
            "mode": "cloud",
            "started_at": "2026-01-01T00:00:00",
            "ended_at": "2026-01-01T01:00:00",
            "status": "VERIFIED",
            "files_checked": 100,
            "mismatches": 0,
        })
        audit = db.latest_audit("cloud", fy=FY)
        assert audit is not None
        assert audit["status"] == "VERIFIED"

    def test_record_audit_missing_keys(self, db):
        with pytest.raises(KeyError):
            db.record_audit({"mode": "cloud"})

    def test_record_audit_rejects_invalid_status(self, db):
        """status must be VERIFIED or VERIFICATION_FAILED."""
        with pytest.raises(ValueError):
            db.record_audit({
                "audit_id": uuid.uuid4().hex,
                "fy": FY,
                "mode": "cloud",
                "started_at": "2026-01-01T00:00:00",
                "status": "BOGUS",
            })

    def test_record_audit_upserts_same_id(self, db):
        audit_id = uuid.uuid4().hex
        base = {
            "audit_id": audit_id,
            "fy": FY,
            "mode": "cloud",
            "started_at": "2026-01-01T00:00:00",
            "status": "VERIFIED",
        }
        db.record_audit(dict(base))
        db.record_audit({**base, "status": "VERIFICATION_FAILED", "mismatches": 7})
        audits = db.latest_audit("cloud", fy=FY)
        assert audits["status"] == "VERIFICATION_FAILED"
        assert audits["mismatches"] == 7

    def test_latest_audit_none_when_absent(self, db):
        assert db.latest_audit("cloud", fy=FY) is None

    def test_mode_status_operations(self, db):
        """Synced-path upsert and retrieval."""
        entries = [{"path": "test.txt", "size": 5, "mtime": 1700000000.0}]
        db.bulk_upsert_synced(entries, mode="cloud", fy=FY)
        assert db.get_synced_paths("cloud", fy=FY) == ["test.txt"]

    def test_clear_mode_status(self, db):
        """Clear mode status."""
        entries = [{"path": "test.txt", "size": 5, "mtime": 1700000000.0}]
        db.bulk_upsert_synced(entries, mode="cloud", fy=FY)
        db.clear_mode_status("cloud", ["test.txt"], fy=FY)
        count = db.file_count("cloud_status", fy=FY)
        assert count == 0
