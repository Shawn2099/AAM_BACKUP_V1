"""Critical-6 — schema migration must be loud, atomic, and retry transient locks.

Old behavior: ALTER TABLE failure was swallowed with logger.error and the
connection was still returned/cached — every subsequent insert_run failed
(missing column) and record_run_history silently dropped ALL run history.
"""

import sqlite3
import threading
import time

import pytest

from core.manifest import SCHEMA_VERSION, ManifestDB, ManifestSchemaError


def _columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[1]
            for row in conn.execute("PRAGMA table_info(run_history)").fetchall()
        }
    finally:
        conn.close()


def _user_version(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _legacy_db(tmp_path):
    """Create a database with the PRE-extended_metrics run_history schema."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE run_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id           TEXT NOT NULL UNIQUE,
            mode             TEXT NOT NULL,
            started_at       TEXT NOT NULL,
            ended_at         TEXT,
            status           TEXT NOT NULL,
            exit_code        INTEGER,
            files_copied     INTEGER DEFAULT 0,
            bytes_copied     INTEGER DEFAULT 0,
            files_failed     INTEGER DEFAULT 0,
            duration_seconds REAL,
            error_message    TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return path


def _open(db):
    """Force the lazy connection open (constructor performs no I/O)."""
    db.file_count()
    return db


class TestFreshDatabase:
    def test_fresh_db_stamped_and_usable(self, tmp_path):
        db = _open(ManifestDB(str(tmp_path / "fresh.db")))
        try:
            assert _user_version(str(tmp_path / "fresh.db")) == SCHEMA_VERSION
            # The column exists from DDL — insert_run must work end-to-end.
            db.insert_run({
                "run_id": "r1",
                "mode": "cloud",
                "started_at": "2026-08-24T00:00:00",
                "status": "CLOUD_COMPLETE",
                "extended_metrics": '{"k": 1}',
            })
        finally:
            db.close()

    def test_reopen_is_cheap_and_idempotent(self, tmp_path):
        path = str(tmp_path / "fresh.db")
        ManifestDB(path).close()
        # Reopen: version fast-path means no migration work; just succeeds.
        db = _open(ManifestDB(str(path)))
        try:
            assert _user_version(path) == SCHEMA_VERSION
        finally:
            db.close()


class TestLegacyMigration:
    def test_legacy_db_migrated_and_stamped(self, tmp_path):
        path = _legacy_db(tmp_path)
        assert "extended_metrics" not in _columns(str(path))

        db = _open(ManifestDB(str(path)))
        try:
            assert "extended_metrics" in _columns(str(path))
            assert _user_version(str(path)) == SCHEMA_VERSION
            db.insert_run({
                "run_id": "r1",
                "mode": "lan",
                "started_at": "2026-08-24T00:00:00",
                "status": "LAN_COMPLETE",
                "extended_metrics": '{"files": 3}',
            })
        finally:
            db.close()

    def test_legacy_db_with_column_but_no_stamp(self, tmp_path):
        """Column present (old hand-migrated DB) but user_version=0 ->
        no second ALTER, just stamping."""
        path = _legacy_db(tmp_path)
        raw = sqlite3.connect(path)
        raw.execute("ALTER TABLE run_history ADD COLUMN extended_metrics TEXT")
        raw.commit()
        raw.close()

        db = _open(ManifestDB(str(path)))
        try:
            assert _user_version(str(path)) == SCHEMA_VERSION
        finally:
            db.close()


class TestContentionSemantics:
    def test_transient_lock_recovered(self, tmp_path):
        """A lock released within the busy-timeout window must NOT fail
        startup: DDL + migration both wait it out, then apply cleanly."""
        path = _legacy_db(tmp_path)
        blocker = sqlite3.connect(path, check_same_thread=False)
        blocker.execute("BEGIN EXCLUSIVE")
        timer = threading.Timer(0.4, lambda: (blocker.rollback(), blocker.close()))
        timer.start()
        try:
            # generous busy_timeout so sqlite itself waits out the 0.4 s hold
            db = _open(ManifestDB(str(path), busy_timeout_ms=5000))
            try:
                assert "extended_metrics" in _columns(str(path))
                assert _user_version(str(path)) == SCHEMA_VERSION
            finally:
                db.close()
        finally:
            timer.join()

    def test_permanent_lock_fails_loudly(self, tmp_path):
        """A permanently locked DB must FAIL LOUDLY — either the schema
        error or sqlite's own OperationalError. Never a silent 'working'
        connection (the old swallow made history vanish quietly)."""
        path = _legacy_db(tmp_path)
        blocker = sqlite3.connect(path)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises((ManifestSchemaError, sqlite3.OperationalError)):
                _open(ManifestDB(str(path), busy_timeout_ms=50))
        finally:
            blocker.rollback()
            blocker.close()

    def test_failed_open_does_not_cache_connection(self, tmp_path):
        """After a failed open, a fresh attempt (lock gone) must succeed —
        the broken connection was never cached."""
        path = _legacy_db(tmp_path)
        blocker = sqlite3.connect(path)
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises((ManifestSchemaError, sqlite3.OperationalError)):
            _open(ManifestDB(str(path), busy_timeout_ms=50))
        blocker.rollback()
        blocker.close()

        db = _open(ManifestDB(str(path)))
        try:
            assert _user_version(str(path)) == SCHEMA_VERSION
        finally:
            db.close()


class TestMigrationAtomicity:
    def test_no_partial_state_on_interrupted_migration(self, tmp_path):
        """Simulate a crash between BEGIN and COMMIT: neither the column nor
        the version stamp may be present."""
        path = _legacy_db(tmp_path)
        raw = sqlite3.connect(path)
        raw.execute("BEGIN IMMEDIATE")
        raw.execute("ALTER TABLE run_history ADD COLUMN extended_metrics TEXT")
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        raw.rollback()  # simulated crash
        raw.close()

        assert "extended_metrics" not in _columns(str(path))
        assert _user_version(str(path)) == 0

        # And the real migration then applies cleanly on top.
        db = _open(ManifestDB(str(path)))
        try:
            assert _user_version(str(path)) == SCHEMA_VERSION
        finally:
            db.close()
