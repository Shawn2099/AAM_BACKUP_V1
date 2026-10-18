"""Plan SCH + A1: the schema a NEW database is created with (D-16: no upgrade path) and the
FY rollover step journal repository.

Journal = one row per (from_fy, to_fy) with one row per step; every step is idempotent and
the daily check resumes at the first step that is not done / skipped.
"""

import sqlite3

import pytest

from core import rollover_journal as rj
from core.manifest import SCHEMA_VERSION, ManifestDB


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _columns(path, table):
    with sqlite3.connect(path) as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def test_fresh_database_has_every_new_table_and_column(tmp_path):
    path = tmp_path / "m.db"
    fresh = ManifestDB(str(path))
    fresh.rollover_incomplete()          # the connection (and the schema) is created lazily
    fresh.close()
    assert SCHEMA_VERSION == 4
    assert {"from_fy", "to_fy", "created_at"} <= _columns(path, "rollover_journal")
    assert {"journal_id", "step", "state", "attempts", "last_error", "started_at",
            "finished_at", "run_id"} <= _columns(path, "rollover_journal_steps")
    assert "attachments_rung" in _columns(path, "notifications")
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_notification_records_the_attachment_rung(db):
    db.record_notification({"event_key": "k", "kind": "FAILURE", "mode": "lan", "subject": "s",
                            "status": "SENT", "attempts": 1, "created_at": "2026-10-18T10:00:00",
                            "attachments_rung": "report_only"})
    assert db.last_notification("k")["attachments_rung"] == "report_only"


def test_open_creates_all_steps_pending_in_order_and_is_idempotent(db):
    j = db.rollover_open("FY26-27", "FY27-28", "2026-04-01T06:00:00+05:30")
    assert list(j["steps"]) == list(rj.STEPS)
    assert all(s["state"] == rj.PENDING and s["attempts"] == 0 for s in j["steps"].values())
    again = db.rollover_open("FY26-27", "FY27-28", "2026-04-02T06:00:00+05:30")
    assert again["created_at"] == j["created_at"]
    assert db.rollover_get("FY26-27", "FY27-28")["steps"] == j["steps"]


def test_step_transitions_record_attempts_errors_and_times(db):
    db.rollover_open("FY26-27", "FY27-28", "t0")
    db.rollover_step_start("FY26-27", "FY27-28", rj.NAS_FOLDER, "t1", run_id="r1")
    db.rollover_step_failed("FY26-27", "FY27-28", rj.NAS_FOLDER, "t2", error="NAS off")
    s = db.rollover_get("FY26-27", "FY27-28")["steps"][rj.NAS_FOLDER]
    assert (s["state"], s["attempts"], s["last_error"], s["run_id"]) == (
        rj.FAILED, 1, "NAS off", "r1")
    assert s["started_at"] == "t1" and s["finished_at"] == "t2"
    db.rollover_step_start("FY26-27", "FY27-28", rj.NAS_FOLDER, "t3", run_id="r2")
    db.rollover_step_done("FY26-27", "FY27-28", rj.NAS_FOLDER, "t4")
    s = db.rollover_get("FY26-27", "FY27-28")["steps"][rj.NAS_FOLDER]
    assert (s["state"], s["attempts"], s["finished_at"], s["run_id"]) == (rj.DONE, 2, "t4", "r2")
    assert s["last_error"] is None          # a later success clears the old error


def test_skipped_counts_as_finished(db):
    db.rollover_open("FY26-27", "FY27-28", "t0")
    db.rollover_step_skipped("FY26-27", "FY27-28", rj.ARCHIVE, "t1", reason="cloud disabled")
    s = db.rollover_get("FY26-27", "FY27-28")["steps"][rj.ARCHIVE]
    assert s["state"] == rj.SKIPPED and s["last_error"] == "cloud disabled"
    assert rj.is_finished(s)


def test_next_step_is_the_first_not_finished_one():
    steps = {name: {"state": rj.DONE} for name in rj.STEPS}
    assert rj.next_step({"steps": steps}) is None and rj.is_complete({"steps": steps})
    steps[rj.CONFIG_SWITCH] = {"state": rj.FAILED}
    steps[rj.ARCHIVE] = {"state": rj.PENDING}
    assert rj.next_step({"steps": steps}) == rj.CONFIG_SWITCH
    steps[rj.CONFIG_SWITCH] = {"state": rj.SKIPPED}
    assert rj.next_step({"steps": steps}) == rj.ARCHIVE
    assert not rj.is_complete({"steps": steps})


def test_open_journal_lookup_returns_the_incomplete_one_only(db):
    assert db.rollover_incomplete() is None
    db.rollover_open("FY25-26", "FY26-27", "t0")
    for step in rj.STEPS:
        db.rollover_step_done("FY25-26", "FY26-27", step, "t1")
    assert db.rollover_incomplete() is None
    db.rollover_open("FY26-27", "FY27-28", "t2")
    inc = db.rollover_incomplete()
    assert (inc["from_fy"], inc["to_fy"]) == ("FY26-27", "FY27-28")


def test_unknown_step_or_state_is_refused(db):
    db.rollover_open("FY26-27", "FY27-28", "t0")
    with pytest.raises(ValueError):
        db.rollover_step_done("FY26-27", "FY27-28", "no_such_step", "t1")
    with pytest.raises(ValueError):
        db.rollover_step_done("FY26-27", "FY27-99", rj.ARCHIVE, "t1")   # no such journal


def test_journal_survives_reopening_the_database(tmp_path):
    path = str(tmp_path / "m.db")
    d = ManifestDB(path)
    d.rollover_open("FY26-27", "FY27-28", "t0")
    d.rollover_step_done("FY26-27", "FY27-28", rj.FINAL_BACKUPS, "t1")
    d.close()
    d2 = ManifestDB(path)
    try:
        j = d2.rollover_get("FY26-27", "FY27-28")
        assert j["steps"][rj.FINAL_BACKUPS]["state"] == rj.DONE
        assert rj.next_step(j) == rj.SOURCE_FOLDER
    finally:
        d2.close()
