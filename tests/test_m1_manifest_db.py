"""M1 / plan §5.4 + WP15 — ManifestDB connection robustness."""

import sqlite3
import threading

from core.manifest import SCHEMA_VERSION, ManifestDB


def _legacy_db(path):
    """A pre-versioning DB: run_history without UNIQUE(run_id) or
    extended_metrics, holding duplicate run_ids."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE run_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, mode TEXT NOT NULL, started_at TEXT NOT NULL,
            ended_at TEXT, status TEXT NOT NULL, exit_code INTEGER,
            files_copied INTEGER DEFAULT 0, bytes_copied INTEGER DEFAULT 0,
            files_failed INTEGER DEFAULT 0, duration_seconds REAL,
            error_message TEXT
        );
        INSERT INTO run_history (run_id, mode, started_at, status) VALUES
            ('r1', 'lan', '2026-01-01T00:00:00+05:30', 'LAN_COMPLETE'),
            ('r1', 'lan', '2026-01-01T00:00:00+05:30', 'LAN_FAILED'),
            ('r2', 'cloud', '2026-01-02T00:00:00+05:30', 'CLOUD_COMPLETE');
        """
    )
    con.commit()
    con.close()


def test_legacy_db_is_migrated_once_with_unique_run_id(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    db = ManifestDB(path)
    try:
        rows = db.get_recent_runs(10)
        assert sorted(r["run_id"] for r in rows) == ["r1", "r2"]
        # first row of the duplicate pair kept
        assert next(r for r in rows if r["run_id"] == "r1")["status"] == "LAN_COMPLETE"
        # upsert on run_id now works (requires the unique index)
        db.insert_run({"run_id": "r1", "mode": "lan",
                       "started_at": "2026-01-01T00:00:00+05:30", "status": "LAN_PARTIAL",
                       "extended_metrics": "{}"})
        assert len(db.get_recent_runs(10)) == 2
        conn = db._get_conn()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        db.close()


def test_fresh_db_is_wal_and_stamped(tmp_path):
    db = ManifestDB(tmp_path / "fresh.db")
    try:
        conn = db._get_conn()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        db.close()


def test_opening_does_not_write_run_history(tmp_path):
    """The legacy dedup used to run a DELETE on every open (a write on the
    dashboard's read path). Opening an up-to-date DB must not modify it:
    PRAGMA data_version on an observer connection changes iff another
    connection committed a write."""
    path = tmp_path / "m.db"
    db = ManifestDB(path)
    db.insert_run({"run_id": "a", "mode": "lan",
                   "started_at": "2026-01-01T00:00:00+05:30", "status": "LAN_COMPLETE"})
    db.close()
    observer = sqlite3.connect(path)
    try:
        v_before = observer.execute("PRAGMA data_version").fetchone()[0]
        db2 = ManifestDB(path)
        db2.get_recent_runs(5)
        db2.close()
        v_after = observer.execute("PRAGMA data_version").fetchone()[0]
        assert v_after == v_before
        # sanity: a real write IS detected by this observer
        db3 = ManifestDB(path)
        db3.insert_run({"run_id": "b", "mode": "lan",
                        "started_at": "2026-01-02T00:00:00+05:30", "status": "LAN_COMPLETE"})
        db3.close()
        assert observer.execute("PRAGMA data_version").fetchone()[0] != v_after
    finally:
        observer.close()


def test_concurrent_first_open_of_fresh_db_loses_no_rows(tmp_path):
    path = tmp_path / "race.db"
    errors: list[str] = []
    per_thread, threads = 20, 6

    def writer(tag: str):
        db = ManifestDB(path, busy_timeout_ms=15000)
        try:
            for i in range(per_thread):
                db.insert_run({"run_id": f"{tag}-{i}", "mode": "cloud",
                               "started_at": "2026-01-01T00:00:00+05:30",
                               "status": "CLOUD_COMPLETE"})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{tag}: {e!r}")
        finally:
            db.close()

    ts = [threading.Thread(target=writer, args=(f"T{n}",)) for n in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    db = ManifestDB(path)
    try:
        assert len(db.get_recent_runs(1000)) == per_thread * threads
    finally:
        db.close()


def test_from_config_uses_every_maintenance_setting(tmp_path):
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.paths.database_path = str(tmp_path / "c.db")
    cfg.maintenance.sqlite_busy_timeout_ms = 4321
    cfg.maintenance.sqlite_vacuum_freelist_threshold = 777
    cfg.maintenance.sqlite_synchronous = "full"
    db = ManifestDB.from_config(cfg)
    try:
        assert db.busy_timeout_ms == 4321
        assert db.vacuum_freelist_threshold == 777
        assert db.synchronous == "full"
        conn = db._get_conn()
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    finally:
        db.close()
