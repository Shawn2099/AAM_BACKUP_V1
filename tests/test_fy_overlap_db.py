"""A4 - database filters, final-copy lookup, approvals, overlap record and schedule record for the FY overlap
(docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, sections 2.4 and 9).

No schema change: the rows of a next-FY pass carry the FY in their id (`-FY27-28`). Rows without a suffix are the
configured FY's pass (`fy=None`), so every existing call keeps its answer while no suffixed rows exist.
"""

import json
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from core import approvals as ap
from core import statuses
from core.manifest import SCHEMA_VERSION, ManifestDB

REPO = Path(__file__).resolve().parents[1]
BASE_COMMIT = "9461ac2"   # the version on the trial box (plan header)

CLOSING = "2027-04-01T00:00:00+05:30"   # closing moment of FY26-27 (plan section 4)


def _run(run_id, mode, started, status, *, fy=None, guards=None, fault=None):
    metrics = {}
    if fy:
        metrics["fy"] = fy
    if guards is not None:
        metrics["guards"] = guards
    if fault:
        metrics["fault_code"] = fault
    return {"run_id": run_id, "mode": mode, "started_at": started, "status": status,
            "extended_metrics": json.dumps(metrics) if metrics else None}


def _audit(audit_id, mode, started, status):
    return {"audit_id": audit_id, "mode": mode, "scope": "full", "started_at": started,
            "status": status, "created_at": started}


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


# --- fy= filters on the "last" lookups ---------------------------------------------------------------

class TestFyFilters:
    def _seed(self, db):
        db.insert_run(_run("a-cloud", "cloud", "2026-12-01T10:00:00+05:30", "CLOUD_COMPLETE"))
        db.insert_run(_run("b-cloud-FY27-28", "cloud", "2026-12-02T10:00:00+05:30", "CLOUD_FAILED"))
        db.insert_run(_run("c-cloud-FY27-28", "cloud", "2026-12-03T10:00:00+05:30", "CLOUD_COMPLETE"))

    def test_last_run_none_sees_only_the_configured_pass(self, db):
        self._seed(db)
        assert db.last_run("cloud")["run_id"] == "a-cloud"
        assert db.last_run("cloud", fy=None)["run_id"] == "a-cloud"

    def test_last_run_value_sees_only_that_fy(self, db):
        self._seed(db)
        assert db.last_run("cloud", fy="FY27-28")["run_id"] == "c-cloud-FY27-28"
        assert db.last_run("cloud", fy="FY28-29") is None

    def test_last_run_without_mode_respects_fy(self, db):
        self._seed(db)
        assert db.last_run(fy="FY27-28")["run_id"] == "c-cloud-FY27-28"
        assert db.last_run()["run_id"] == "a-cloud"   # fy omitted = the configured pass (plan D-5)

    def test_last_successful_run_by_fy(self, db):
        self._seed(db)
        assert db.last_successful_run("cloud")["run_id"] == "a-cloud"
        assert db.last_successful_run("cloud", fy="FY27-28")["run_id"] == "c-cloud-FY27-28"
        assert db.last_successful_run("cloud", fy="FY26-27") is None

    def test_previous_run_status_by_fy(self, db):
        self._seed(db)
        assert db.previous_run_status("cloud", "x-new") == "CLOUD_COMPLETE"
        assert db.previous_run_status("cloud", "x-new", fy="FY27-28") == "CLOUD_COMPLETE"
        assert db.previous_run_status("cloud", "c-cloud-FY27-28", fy="FY27-28") == "CLOUD_FAILED"
        assert db.previous_run_status("cloud", "a-cloud", fy="FY26-27") is None

    def test_previous_run_fault_code_by_fy(self, db):
        db.insert_run(_run("a-cloud", "cloud", "2026-12-01T10:00:00+05:30", "CLOUD_FAILED", fault="AAM-CLD-01"))
        db.insert_run(_run("b-cloud-FY27-28", "cloud", "2026-12-02T10:00:00+05:30", "CLOUD_FAILED",
                           fault="AAM-CLD-09"))
        assert db.previous_run_fault_code("cloud", "x-new") == "AAM-CLD-01"
        assert db.previous_run_fault_code("cloud", "x-new", fy="FY27-28") == "AAM-CLD-09"

    def test_leg_success_after_by_fy(self, db):
        self._seed(db)
        assert db.leg_success_after("cloud", "2026-12-02T00:00:00+05:30") is False
        assert db.leg_success_after("cloud", "2026-12-02T00:00:00+05:30", fy="FY27-28") is True
        assert db.leg_success_after("cloud", "2026-12-03T12:00:00+05:30", fy="FY27-28") is False

    def test_first_partial_after_by_fy(self, db):
        db.insert_run(_run("a-cloud", "cloud", "2026-12-01T10:00:00+05:30", "CLOUD_PARTIAL"))
        db.insert_run(_run("b-cloud-FY27-28", "cloud", "2026-12-02T10:00:00+05:30", "CLOUD_PARTIAL"))
        assert db.first_partial_after("cloud", None) == "2026-12-01T10:00:00+05:30"
        assert db.first_partial_after("cloud", None, fy="FY27-28") == "2026-12-02T10:00:00+05:30"
        assert db.first_partial_after("cloud", None, fy="FY28-29") is None

    def test_latest_audit_by_fy(self, db):
        db.record_audit(_audit("u-cloud-full", "cloud", "2026-12-01T10:00:00+05:30", "VERIFIED"))
        db.record_audit(_audit("u-cloud-full-FY27-28", "cloud", "2026-12-02T10:00:00+05:30",
                               "VERIFICATION_FAILED"))
        assert db.latest_audit("cloud")["audit_id"] == "u-cloud-full"
        assert db.latest_audit("cloud", fy="FY27-28")["status"] == "VERIFICATION_FAILED"
        assert db.latest_audit("cloud", fy="FY28-29") is None

    def test_guard_baseline_sees_both_passes_of_its_fy(self, db):
        guards = {"fy": "FY27-28", "source_files": 42}
        db.insert_run(_run("z-cloud-FY27-28", "cloud", "2026-12-02T10:00:00+05:30", "CLOUD_COMPLETE",
                           fy="FY27-28", guards=guards))
        assert db.guard_baseline("cloud", "FY27-28") == guards
        assert db.guard_baseline("cloud", "FY26-27") is None


# --- final copy of the old FY (plan section 4) -------------------------------------------------------

class TestFinalCopySince:
    def test_status_set_is_the_plan_set(self):
        assert statuses.FINAL_COPY_STATUSES == {
            "CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE", "LAN_COMPLETE"}

    def test_counts_only_verified_final_copies_on_or_after_the_closing_moment(self, db):
        g = {"fy": "FY26-27"}
        db.insert_run(_run("old-cloud", "cloud", "2027-03-31T18:00:00+05:30", "CLOUD_COMPLETE", guards=g))
        db.insert_run(_run("at-cloud", "cloud", CLOSING, "CLOUD_NO_CHANGES_COMPLETE", guards=g))
        db.insert_run(_run("part-cloud", "cloud", "2027-04-02T01:00:00+05:30", "CLOUD_PARTIAL", guards=g))
        db.insert_run(_run("verr-cloud", "cloud", "2027-04-02T02:00:00+05:30", "CLOUD_VERIFY_FAILED", guards=g))
        db.insert_run(_run("leg-cloud", "cloud", "2027-04-02T03:00:00+05:30", "SUCCESS", guards=g))
        got = db.final_copy_since("cloud", "FY26-27", CLOSING)
        assert got["run_id"] == "at-cloud"

    def test_nothing_before_the_closing_moment(self, db):
        g = {"fy": "FY26-27"}
        db.insert_run(_run("old-cloud", "cloud", "2027-03-31T18:00:00+05:30", "CLOUD_COMPLETE", guards=g))
        assert db.final_copy_since("cloud", "FY26-27", CLOSING) is None

    def test_newest_qualifying_copy_wins_compared_as_instants(self, db):
        g = {"fy": "FY26-27"}
        db.insert_run(_run("early", "cloud", "2027-04-01T09:00:00+05:30", "CLOUD_COMPLETE", guards=g))
        db.insert_run(_run("late", "cloud", "2027-04-01T12:00:00+05:30", "CLOUD_COMPLETE", guards=g))
        assert db.final_copy_since("cloud", "FY26-27", CLOSING)["run_id"] == "late"

    def test_leg_and_fy_must_match(self, db):
        g = {"fy": "FY26-27"}
        db.insert_run(_run("lan-only", "lan", "2027-04-02T10:00:00+05:30", "LAN_COMPLETE", guards=g))
        db.insert_run(_run("next-fy", "cloud", "2027-04-02T11:00:00+05:30", "CLOUD_COMPLETE",
                           guards={"fy": "FY27-28"}))
        assert db.final_copy_since("cloud", "FY26-27", CLOSING) is None
        assert db.final_copy_since("lan", "FY26-27", CLOSING)["run_id"] == "lan-only"


# --- approvals per FY ------------------------------------------------------------------------------

def _open(db, fy, run_id, keep_fy=None):
    kwargs = {"keep_fy": keep_fy} if keep_fy is not None else {}
    return db.open_approval_request(leg="cloud", blocked_run_id=run_id, fy=fy, guards=[], phrase="APPROVE",
                                    created_at="2026-12-01T10:00:00+05:30", **kwargs)


class TestApprovals:
    def test_keep_fy_leaves_the_other_fys_pending_request_alone(self, db):
        old = _open(db, "FY26-27", "r1")
        new = _open(db, "FY27-28", "r2", keep_fy="FY26-27")
        assert db.get_approval(old)["status"] == ap.PENDING
        assert db.get_approval(new)["status"] == ap.PENDING

    def test_keep_fy_still_supersedes_a_pending_request_of_the_same_fy(self, db):
        first = _open(db, "FY27-28", "r1")
        _open(db, "FY27-28", "r2", keep_fy="FY26-27")
        assert db.get_approval(first)["status"] == ap.SUPERSEDED

    def test_without_keep_fy_every_pending_request_of_the_leg_is_superseded(self, db):
        old = _open(db, "FY26-27", "r1")
        _open(db, "FY27-28", "r2")
        assert db.get_approval(old)["status"] == ap.SUPERSEDED

    def test_supersede_approvals_of_fy_covers_pending_and_approved(self, db):
        approved = _open(db, "FY26-27", "r1", keep_fy="FY27-28")
        db.approve_request(approved, approved_by="owner", approved_at="2026-12-01T11:00:00+05:30",
                           expires_at="2026-12-08T11:00:00+05:30")
        pending = _open(db, "FY26-27", "r2", keep_fy="FY27-28")   # the APPROVED one is not touched by this
        other = _open(db, "FY27-28", "r3", keep_fy="FY26-27")
        assert db.supersede_approvals_of_fy("FY26-27") == 2
        assert db.get_approval(pending)["status"] == ap.SUPERSEDED
        assert db.get_approval(approved)["status"] == ap.SUPERSEDED
        assert db.get_approval(other)["status"] == ap.PENDING


# --- overlap record and schedule record (db_meta, no new table) -------------------------------------

class TestRecords:
    def test_overlap_record_round_trip(self, db):
        assert db.overlap_record("FY27-28") is None
        record = {"activated_at": "2026-12-31T06:00:00+05:30", "from_fy": "FY26-27",
                  "source_folder": "E:\\FY27-28", "clock": "CLOCK_OK", "ended_at": None}
        db.set_overlap_record("FY27-28", record)
        assert db.overlap_record("FY27-28") == record
        assert db.overlap_record("FY26-27") is None

    def test_schedule_record_carries_the_scheduled_flags(self, db):
        base = {"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *", "timezone": "Asia/Kolkata"}
        db.set_schedule_in_effect({**base, "cloud_scheduled": True, "lan_scheduled": False})
        got = db.get_schedule_in_effect()
        assert got["cloud_scheduled"] is True
        assert got["lan_scheduled"] is False

    def test_schedule_record_without_flags_means_scheduled(self, db):
        base = {"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *", "timezone": "Asia/Kolkata"}
        db.set_schedule_in_effect(base)
        got = db.get_schedule_in_effect()
        assert got["cloud_scheduled"] is True and got["lan_scheduled"] is True

    def test_old_schedule_record_reads_as_scheduled(self, db):
        old = {"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *", "timezone": "Asia/Kolkata",
               "cloud_since": None, "lan_since": None}
        db._set_meta("schedule_in_effect", json.dumps(old))
        got = db.get_schedule_in_effect()
        assert got["cloud_scheduled"] is True and got["lan_scheduled"] is True


# --- compatibility: a database written by the base-commit DDL --------------------------------------

def _base_ddl() -> str:
    text = subprocess.run(["git", "-C", str(REPO), "show", f"{BASE_COMMIT}:core/manifest.py"],
                          capture_output=True, text=True, check=True).stdout
    match = re.search(r'DDL = """(.*?)"""', text, re.S)
    assert match, "base commit has no DDL block"
    return match.group(1)


class TestBaselineDatabaseCompatibility:
    def _baseline_file(self, path):
        conn = sqlite3.connect(str(path))
        conn.executescript(_base_ddl())
        conn.execute("PRAGMA user_version = 5")
        conn.execute("INSERT INTO run_history (run_id, mode, started_at, status, extended_metrics) "
                     "VALUES ('old-cloud', 'cloud', '2026-11-01T10:00:00+05:30', 'CLOUD_COMPLETE', NULL)")
        conn.execute("INSERT INTO approvals (approval_id, leg, blocked_run_id, fy, guards, phrase, status, created_at) "
                     "VALUES ('apr1', 'cloud', 'old-cloud', 'FY26-27', '[]', 'APPROVE', 'PENDING', "
                     "'2026-11-01T10:00:00+05:30')")
        conn.execute("INSERT INTO db_meta (key, value) VALUES ('schedule_in_effect', ?)",
                     (json.dumps({"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *",
                                  "timezone": "Asia/Kolkata", "cloud_since": None, "lan_since": None}),))
        conn.commit()
        conn.close()

    def test_opens_unchanged_with_the_same_answers(self, tmp_path):
        path = tmp_path / "baseline.db"
        self._baseline_file(path)
        tables_before = sorted(r[0] for r in sqlite3.connect(str(path)).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"))

        db = ManifestDB(str(path))
        try:
            assert db.last_run("cloud")["run_id"] == "old-cloud"
            assert db.last_run("cloud", fy=None)["run_id"] == "old-cloud"
            assert db.last_successful_run("cloud", fy=None)["run_id"] == "old-cloud"
            assert [a["approval_id"] for a in db.open_approvals()] == ["apr1"]
            schedule = db.get_schedule_in_effect()
            assert schedule["cloud_scheduled"] is True and schedule["lan_scheduled"] is True
        finally:
            db.close()

        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 5 == SCHEMA_VERSION
            tables_after = sorted(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"))
        finally:
            conn.close()
        assert tables_after == tables_before
