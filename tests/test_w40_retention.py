"""W40 (owner decision 2026-11-23): logs and history are kept at least two years. App logs, run logs and service
logs default to 730 days (app logs up to 1825); the run history stays at 730 (35..3650). Nothing younger than the
configured days is ever deleted, and the mail audit log, approvals, safety checks and config changes are never
purged."""
from __future__ import annotations

import os
import time

import pytest
from pydantic import ValidationError

from core import logging as app_logging
from core.manifest import ManifestDB
from core.run_artefacts import purge_older_than
from models.config import MaintenanceConfig, RunLogsConfig, model_default

DAY = 86400


def _age(path, days: float) -> None:
    t = time.time() - days * DAY
    os.utime(path, (t, t))


def test_defaults_are_two_years():
    assert model_default(MaintenanceConfig, "log_retention_days") == 730
    assert model_default(RunLogsConfig, "retention_days") == 730
    assert model_default(RunLogsConfig, "service_log_retention_days") == 730
    assert model_default(MaintenanceConfig, "db_retention_days") == 730


def test_bounds():
    MaintenanceConfig(log_retention_days=1)
    MaintenanceConfig(log_retention_days=1825)
    for bad in (0, 1826):
        with pytest.raises(ValidationError):
            MaintenanceConfig(log_retention_days=bad)
    MaintenanceConfig(db_retention_days=35)
    MaintenanceConfig(db_retention_days=3650)
    for bad in (34, 3651):
        with pytest.raises(ValidationError):
            MaintenanceConfig(db_retention_days=bad)


def test_loguru_gets_the_configured_days(tmp_path, monkeypatch):
    seen = []
    real_add = app_logging.logger.add

    def spy(sink, *a, **kw):
        seen.append(kw.get("retention"))
        return real_add(sink, *a, **kw)
    monkeypatch.setattr(app_logging.logger, "add", spy)
    app_logging.configure(tmp_path / "logs")
    assert "730 days" in seen


def test_run_log_day_folders_400_days_kept_800_removed(tmp_path):
    keep, drop = tmp_path / "2025-10-20", tmp_path / "2024-09-20"
    for folder, days in ((keep, 400), (drop, 800)):
        (folder / "run").mkdir(parents=True)
        (folder / "run" / "x.log").write_text("log")
        for p in (folder / "run" / "x.log", folder / "run", folder):
            _age(p, days)
    assert purge_older_than(tmp_path, 730) == 1
    assert keep.exists() and not drop.exists()


def test_service_logs_400_days_kept_800_removed(tmp_path):
    keep, drop = tmp_path / "agent_svc-1.log", tmp_path / "agent_svc-2.log"
    for f, days in ((keep, 400), (drop, 800)):
        f.write_text("x")
        _age(f, days)
    assert purge_older_than(tmp_path, 730, glob="agent_svc-*", files=True) == 1
    assert keep.exists() and not drop.exists()


def test_a_day_folder_with_a_young_file_inside_is_kept(tmp_path):
    """The folder's own mtime is old, but a file in it (compressed or appended later) is young."""
    day = tmp_path / "2024-01-02"
    (day / "run").mkdir(parents=True)
    young = day / "run" / "late.log.gz"
    young.write_text("x")
    _age(day / "run", 900)
    _age(day, 900)
    assert purge_older_than(tmp_path, 730) == 0
    assert young.exists()


def test_purge_old_runs_never_touches_the_audit_tables(tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        old = "2020-01-01T00:00:00+00:00"
        with db._lock:
            c = db._get_conn()
            c.execute("INSERT INTO notifications (event_key, kind, mode, subject, status, attempts, created_at) "
                      "VALUES ('k', 'FAILURE', 'lan', 's', 'SENT', 1, ?)", (old,))
            c.execute("INSERT INTO config_changes (changed_at, changed_by, key, old_value, new_value, risky) "
                      "VALUES (?, 'u', 'k', 'a', 'b', 0)", (old,))
            c.commit()
        db.record_audit({"audit_id": "a1", "mode": "lan", "fy": "FY19-20", "started_at": old,
                         "status": "VERIFIED"})
        db.purge_old_runs(retention_days=35)
        with db._lock:
            c = db._get_conn()
            for table in ("notifications", "config_changes", "integrity_audits"):
                assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1, table
    finally:
        db.close()
