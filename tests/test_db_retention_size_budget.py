"""Run history is bounded by SIZE (maintenance.db_retention_max_mb); db_retention_days is only a floor.

Owner request 2026-11-23: days-based purge lost history during the 15-month FY-overlap cycle.
Rule: delete oldest-first ONLY while the run history exceeds the budget, never rows younger than
db_retention_days, never the protected rows (running, newest per (mode, fy), last success per (mode, fy))."""

import inspect
import uuid

import pytest
from loguru import logger
from pydantic import ValidationError

from core.manifest import ManifestDB
from core.time_utils import cutoff_iso
from models.config import MaintenanceConfig, model_default

FY = "FY26-27"
PAD = "x" * 2000


def _run(started, mode="cloud", status="CLOUD_COMPLETE", fy=FY, pad=PAD):
    return {"run_id": uuid.uuid4().hex, "mode": mode, "started_at": started, "ended_at": started,
            "status": status, "fy": fy, "error_message": pad}


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "t.db"))
    yield d
    d.close()


def _count(db):
    return db._get_conn().execute("SELECT COUNT(*) FROM run_history").fetchone()[0]


def _started(db):
    return [r[0] for r in db._get_conn().execute("SELECT started_at FROM run_history ORDER BY started_at")]


def test_under_budget_nothing_is_deleted_even_if_very_old(db):
    for i in range(5):
        db.insert_run(_run(f"2019-01-0{i + 1}T00:00:00+05:30", status="CLOUD_FAILED"))
    res = db.purge_old_runs(retention_days=35, max_mb=5)
    assert _count(db) == 5 and res["deleted"] == 0 and not res["over_budget"]


def test_over_budget_deletes_oldest_first_and_keeps_protected(db):
    db.insert_run(_run("2019-01-01T00:00:00+05:30", status="CLOUD_FAILED"))   # deletable
    db.insert_run(_run("2019-01-02T00:00:00+05:30", status="CLOUD_FAILED"))   # deletable
    db.insert_run(_run("2019-01-03T00:00:00+05:30"))                          # last success: protected
    db.insert_run(_run("2019-01-04T00:00:00+05:30", status="CLOUD_FAILED"))   # newest cloud: protected
    db.insert_run(_run("2019-01-05T00:00:00+05:30", mode="lan", status="LAN_FAILED"))  # newest lan: protected
    res = db.purge_old_runs(retention_days=35, max_mb=0.005)    # 5 KB: the 3 protected rows alone exceed it
    assert _started(db) == ["2019-01-03T00:00:00+05:30", "2019-01-04T00:00:00+05:30",
                            "2019-01-05T00:00:00+05:30"]
    assert res["deleted"] == 2 and res["over_budget"] is True


def test_stops_deleting_as_soon_as_the_budget_is_met(db):
    for i in range(10):
        db.insert_run(_run(f"2019-01-{i + 1:02d}T00:00:00+05:30", status="CLOUD_FAILED"))
    res = db.purge_old_runs(retention_days=35, max_mb=0.012)      # about 5 rows of ~2.2 KB
    left = _started(db)
    assert 3 <= len(left) <= 6 and left[-1] == "2019-01-10T00:00:00+05:30"
    assert res["deleted"] == 10 - len(left) and not res["over_budget"]
    assert left[0] > "2019-01-03"      # the oldest went first


def test_rows_younger_than_the_floor_are_never_deleted(db):
    for i in range(6):
        db.insert_run(_run(cutoff_iso(2 + i), status="CLOUD_FAILED"))
    res = db.purge_old_runs(retention_days=35, max_mb=0.001)
    assert _count(db) == 6 and res["deleted"] == 0 and res["over_budget"] is True


def test_running_rows_are_protected(db):
    db.insert_run(_run("2019-01-01T00:00:00+05:30", status="CLOUD_RUNNING"))
    for i in range(3):
        db.insert_run(_run(f"2019-02-0{i + 1}T00:00:00+05:30", status="CLOUD_FAILED"))
    db.purge_old_runs(retention_days=35, max_mb=0.001)
    assert "CLOUD_RUNNING" in [r[0] for r in db._get_conn().execute("SELECT status FROM run_history")]


def test_unreachable_budget_warns_once_and_does_not_crash(db):
    msgs = []
    sink = logger.add(lambda m: msgs.append(m.record["level"].name + ":" + m.record["message"]))
    try:
        db.insert_run(_run("2019-01-01T00:00:00+05:30"))
        res = db.purge_old_runs(retention_days=35, max_mb=0.0005)
    finally:
        logger.remove(sink)
    assert res["over_budget"] is True and _count(db) == 1
    assert sum(m.startswith("WARNING:") and "budget" in m for m in msgs) == 1


def test_every_deletion_is_logged_with_counts(db):
    msgs = []
    sink = logger.add(lambda m: msgs.append(m.record["message"]))
    try:
        for i in range(6):
            db.insert_run(_run(f"2019-01-{i + 1:02d}T00:00:00+05:30", status="CLOUD_FAILED"))
        res = db.purge_old_runs(retention_days=35, max_mb=0.006)
    finally:
        logger.remove(sink)
    assert res["deleted"] > 0
    assert any(f"deleted {res['deleted']} " in m and "run history" in m for m in msgs)


def test_other_tables_are_untouched(db):
    db.insert_run(_run("2019-01-01T00:00:00+05:30", status="CLOUD_FAILED"))
    db.insert_run(_run("2019-01-02T00:00:00+05:30", status="CLOUD_FAILED"))
    conn = db._get_conn()
    tables = ("file_entries", "db_meta", "approvals", "rollover_journal", "notifications")
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    db.purge_old_runs(retention_days=35, max_mb=0.001)
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    assert before == after and before["db_meta"] > 0


def test_config_key_default_and_bounds():
    assert model_default(MaintenanceConfig, "db_retention_max_mb") == 5
    assert MaintenanceConfig(db_retention_max_mb=1).db_retention_max_mb == 1
    assert MaintenanceConfig(db_retention_max_mb=500).db_retention_max_mb == 500
    for bad in (0, -1, 501):
        with pytest.raises(ValidationError):
            MaintenanceConfig(db_retention_max_mb=bad)


def test_purge_default_follows_the_model():
    assert inspect.signature(ManifestDB.purge_old_runs).parameters["max_mb"].default == 5
