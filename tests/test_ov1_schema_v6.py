"""OV-1.1 - database schema v6 (plan 5.4, D-7): fresh DB with `fy` columns, v5 files refused."""

import sqlite3

import pytest

from core import manifest
from core.manifest import ManifestDB, ManifestSchemaError


def _cols(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return {r[1]: {"notnull": r[3], "dflt": r[4]} for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    d._get_conn()
    yield d
    d.close()


def test_schema_version_is_6():
    assert manifest.SCHEMA_VERSION == 6


def test_fresh_db_is_stamped_6(db):
    assert db._get_conn().execute("PRAGMA user_version").fetchone()[0] == 6


def test_fy_columns_exist(db):
    p = db.db_path
    assert "fy" in _cols(p, "file_entries")
    assert "fy" in _cols(p, "run_history")
    assert "fy" in _cols(p, "integrity_audits")
    assert "fy" in _cols(p, "notifications")
    assert "fy" in _cols(p, "approvals")


def test_run_history_fy_not_null_notifications_fy_nullable(db):
    assert _cols(db.db_path, "run_history")["fy"]["notnull"] == 1
    assert _cols(db.db_path, "integrity_audits")["fy"]["notnull"] == 1
    assert _cols(db.db_path, "notifications")["fy"]["notnull"] == 0


def test_file_entries_unique_per_fy_and_path(db):
    conn = db._get_conn()
    ins = ("INSERT INTO file_entries (fy, relative_path, created_at, updated_at) "
           "VALUES (?, ?, 't', 't')")
    conn.execute(ins, ("FY26-27", "a/b.txt"))
    conn.execute(ins, ("FY27-28", "a/b.txt"))          # same path, other FY: allowed
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("FY26-27", "A/B.TXT"))      # same FY, NOCASE duplicate: refused
    conn.rollback()


def test_run_history_requires_fy(db):
    conn = db._get_conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO run_history (run_id, mode, started_at, status) "
                     "VALUES ('r1', 'cloud', 't', 'RUNNING')")
    conn.rollback()


def test_overlap_activations_table(db):
    cols = _cols(db.db_path, "overlap_activations")
    for c in ("fy", "activated_at", "source_path", "created_by_app", "clock_state"):
        assert c in cols
    conn = db._get_conn()
    conn.execute("INSERT INTO overlap_activations (fy, activated_at, source_path, created_by_app, clock_state) "
                 "VALUES ('FY27-28', 't', 'D:\\\\FY27-28', 1, 'OK')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO overlap_activations (fy, activated_at, source_path, created_by_app, "
                     "clock_state) VALUES ('FY27-28', 't2', 'x', 0, 'OK')")
    conn.rollback()


def test_activations_never_purged(db):
    conn = db._get_conn()
    conn.execute("INSERT INTO overlap_activations (fy, activated_at, source_path, created_by_app, clock_state) "
                 "VALUES ('FY27-28', '2000-01-01T00:00:00+00:00', 'D:\\\\FY27-28', 1, 'OK')")
    conn.commit()
    db.purge_old_runs(retention_days=1)
    assert conn.execute("SELECT COUNT(*) FROM overlap_activations").fetchone()[0] == 1


def _make_v5_file(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE file_entries (id INTEGER PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE run_history (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, mode TEXT,
            started_at TEXT, status TEXT);
        PRAGMA user_version = 5;
    """)
    conn.commit()
    conn.close()


def test_v5_file_is_refused_with_rename_message(tmp_path):
    p = tmp_path / "old.db"
    _make_v5_file(p)
    d = ManifestDB(p)
    with pytest.raises(ManifestSchemaError) as exc:
        d._get_conn()
    msg = str(exc.value).lower()
    assert "rename" in msg and "start fresh" in msg


def test_refused_v5_file_is_left_untouched(tmp_path):
    p = tmp_path / "old.db"
    _make_v5_file(p)
    with pytest.raises(ManifestSchemaError):
        ManifestDB(p)._get_conn()
    conn = sqlite3.connect(p)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "fy" not in {r[1] for r in conn.execute("PRAGMA table_info(file_entries)")}
    finally:
        conn.close()


def test_empty_existing_file_is_treated_as_fresh(tmp_path):
    p = tmp_path / "empty.db"
    sqlite3.connect(p).close()
    d = ManifestDB(p)
    assert d._get_conn().execute("PRAGMA user_version").fetchone()[0] == 6
    d.close()
