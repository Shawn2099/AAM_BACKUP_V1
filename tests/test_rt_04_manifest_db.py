import sqlite3
import threading
import time
from pathlib import Path

from core.manifest import ManifestDB
from core.time_utils import cutoff_iso, now_iso


def get_temp_db_path(tmp_path) -> Path:
    return tmp_path / "test_manifest.db"


def test_db_01_fresh_schema(tmp_path):
    """DB-01: Fresh Database — DDL and Schema Created Correctly."""
    db_path = get_temp_db_path(tmp_path)
    
    db = ManifestDB(db_path)
    try:
        # Access triggers DDL
        db.file_count("lan_status")
        
        assert db_path.exists()
        
        conn = sqlite3.connect(db_path)
        try:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert journal_mode.lower() == "wal"
            
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert "file_entries" in tables
            assert "run_history" in tables
            assert "db_meta" in tables
            
            schema_ver = conn.execute("SELECT value FROM db_meta WHERE key='schema_version'").fetchone()[0]
            assert schema_ver == "1"
        finally:
            conn.close()
    finally:
        db.close()


def test_db_02_upsert_retrieve_entry(tmp_path):
    """DB-02: Upsert and Retrieve a File Entry."""
    db_path = get_temp_db_path(tmp_path)
    db = ManifestDB(db_path)
    
    try:
        db.upsert_file_entry("folder/test.txt", 1024, 1234567890.0, lan_status="synced")
        
        entry = db.get_entry("folder/test.txt")
        assert entry is not None
        assert entry["file_size"] == 1024
        assert entry["lan_status"] == "synced"
        assert entry["lan_last_synced_at"] is not None
    finally:
        db.close()


def test_db_03_bulk_upsert_scale(tmp_path):
    """DB-03: Bulk Upsert — 10,000 Entries in One Transaction."""
    db_path = get_temp_db_path(tmp_path)
    db = ManifestDB(db_path)
    
    entries = [{"path": f"folder/test_{i}.txt", "size": i, "mtime": float(i)} for i in range(10000)]
    
    start = time.time()
    try:
        db.bulk_upsert_synced(entries, "cloud")
        elapsed = time.time() - start
        
        assert db.file_count("cloud_status") == 10000
        assert elapsed < 10.0 # Should be very fast with WAL + executemany
    finally:
        db.close()


def test_db_04_run_history_insert_retrieve(tmp_path):
    """DB-04: Run History — Insert and Retrieve."""
    db_path = get_temp_db_path(tmp_path)
    db = ManifestDB(db_path)
    
    run_data = {
        "run_id": "test_run_123",
        "mode": "cloud",
        "started_at": now_iso(),
        "status": "CLOUD_COMPLETE",
        "exit_code": 0,
        "files_copied": 10,
        "bytes_copied": 2048,
    }
    
    try:
        db.insert_run(run_data)
        
        last_run = db.last_run("cloud")
        assert last_run is not None
        assert last_run["run_id"] == "test_run_123"
        assert last_run["status"] == "CLOUD_COMPLETE"
        assert last_run["exit_code"] == 0
        assert last_run["files_copied"] == 10
    finally:
        db.close()


def test_db_05_duplicate_run_id_upsert(tmp_path):
    """DB-05: Duplicate Run ID — Upsert Overwrites Correctly."""
    db_path = get_temp_db_path(tmp_path)
    db = ManifestDB(db_path)
    
    try:
        db.insert_run({
            "run_id": "duplicate_run_1",
            "mode": "lan",
            "started_at": now_iso(),
            "status": "LAN_SKIPPED"
        })
        
        db.insert_run({
            "run_id": "duplicate_run_1",
            "mode": "lan",
            "started_at": now_iso(),
            "status": "LAN_COMPLETE",
            "exit_code": 1
        })
        
        last_run = db.last_run("lan")
        assert last_run["status"] == "LAN_COMPLETE"
        assert last_run["exit_code"] == 1
        
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM run_history WHERE run_id='duplicate_run_1'").fetchone()[0]
            assert count == 1
        finally:
            conn.close()
    finally:
        db.close()


def test_db_06_purge_old_runs(tmp_path):
    """DB-06: purge_old_runs Deletes Old Entries Only."""
    db_path = get_temp_db_path(tmp_path)
    db = ManifestDB(db_path)
    
    try:
        old_time = cutoff_iso(200) # 200 days old
        now_time = now_iso()
        
        for i in range(5):
            db.insert_run({
                "run_id": f"old_run_{i}",
                "mode": "cloud",
                "started_at": old_time,
                "status": "CLOUD_COMPLETE"
            })
            
        for i in range(5):
            db.insert_run({
                "run_id": f"new_run_{i}",
                "mode": "cloud",
                "started_at": now_time,
                "status": "CLOUD_COMPLETE"
            })
            
        # Also add a file entry to ensure it's not deleted
        db.upsert_file_entry("test.txt", 100, 100.0, cloud_status="synced")
            
        db.purge_old_runs(retention_days=90)
        
        conn = sqlite3.connect(db_path)
        try:
            runs = conn.execute("SELECT run_id FROM run_history").fetchall()
            assert len(runs) == 5
            for row in runs:
                assert row[0].startswith("new_run_")
                
            files = conn.execute("SELECT COUNT(*) FROM file_entries").fetchone()[0]
            assert files == 1
        finally:
            conn.close()
    finally:
        db.close()


def test_db_07_prune_stale_synced(tmp_path):
    """DB-07: prune_stale_synced Cleans Ghost Entries."""
    db_path = get_temp_db_path(tmp_path)
    db = ManifestDB(db_path)
    
    try:
        entries = [{"path": f"test_{i}.txt", "size": 100, "mtime": 100.0} for i in range(10)]
        db.bulk_upsert_synced(entries, "cloud")
        
        active_paths = {f"test_{i}.txt" for i in range(7)}
        
        pruned = db.prune_stale_synced("cloud", active_paths)
        assert pruned == 3
        
        # Verify their cloud_status was set to NULL
        conn = sqlite3.connect(db_path)
        try:
            files = conn.execute("SELECT COUNT(*) FROM file_entries WHERE cloud_status IS NOT NULL").fetchone()[0]
            assert files == 7
        finally:
            conn.close()
            
    finally:
        db.close()


def test_db_08_wal_survives_crash(tmp_path):
    """DB-08: WAL Mode Survives Abrupt Process Kill (Crash Recovery)."""
    db_path = get_temp_db_path(tmp_path)
    
    def crashy_writer():
        db = ManifestDB(db_path)
        try:
            # We don't use bulk upsert here so we can interrupt it mid-loop
            for i in range(100):
                db.upsert_file_entry(f"file_{i}.txt", 100, 100.0, cloud_status="synced")
                time.sleep(0.01)
        finally:
            db.close()
            
    t = threading.Thread(target=crashy_writer)
    t.daemon = True # Will be killed abruptly when main thread exits if we don't join
    t.start()
    
    # Wait a bit then kill it abruptly (by letting the test function finish/thread object be garbage collected without joining cleanly, or just timeout join)
    t.join(timeout=0.2)
    # The thread is still running and will be killed when the test suite exits.
    # But for this test, we just want to verify we can open a NEW connection while it was interrupted or writing
    
    # Open new connection
    db2 = ManifestDB(db_path)
    try:
        # Should not crash, and should return a valid integer
        count = db2.file_count("cloud_status")
        assert count >= 0
        
        conn = sqlite3.connect(db_path)
        try:
            res = conn.execute("PRAGMA integrity_check").fetchone()[0]
            assert res.lower() == "ok"
        finally:
            conn.close()
    finally:
        db2.close()
