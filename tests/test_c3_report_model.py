"""Plan C3/C4: ReportModel (P3 resolved vs full history, P4 backup-only totals + per-leg data,
P5 enabled legs only) and the renderers built from it (P1 empty period still reports)."""
import pendulum
import pytest

from core import report
from core.manifest import ManifestDB
from core.report_period import ReportPeriod
from core.time_utils import IST

WEEK = ReportPeriod.previous_week(pendulum.datetime(2026, 10, 19, 8, 0, tz=IST))   # 12-18 Oct


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _run(db, rid, started, mode="cloud", status="CLOUD_COMPLETE", files=0, size=0):
    db.insert_run({"run_id": rid, "mode": mode, "started_at": started, "ended_at": started,
                   "status": status, "files_copied": files, "bytes_copied": size})


def test_p3_failure_fixed_after_the_period_reads_resolved(db):
    _run(db, "bad", "2026-10-18T23:00:00+05:30", status="CLOUD_FAILED")      # last night of the period
    _run(db, "fix", "2026-10-19T23:00:00+05:30")                              # fixed the next day
    m = report.build_report_model(db, WEEK, legs=("cloud",))
    problem = m["summaries"][0]["problems"][0]
    assert problem["resolved"] is True
    assert "action needed" not in m["headline"]


def test_p3_failure_never_fixed_stays_open(db):
    _run(db, "ok", "2026-10-13T23:00:00+05:30")
    _run(db, "bad", "2026-10-18T23:00:00+05:30", status="CLOUD_FAILED")
    m = report.build_report_model(db, WEEK, legs=("cloud",))
    assert m["summaries"][0]["problems"][0]["resolved"] is False
    assert m["headline"].startswith("action needed") and m["act"] is True


def test_p4_rollover_rows_are_not_backups_and_data_is_per_leg(db):
    _run(db, "c", "2026-10-14T23:00:00+05:30", files=100, size=1000)
    _run(db, "l", "2026-10-14T23:30:00+05:30", mode="lan", status="LAN_COMPLETE", files=40, size=400)
    _run(db, "r", "2026-10-15T00:30:00+05:30", mode="rollover", status="ROLLOVER_COMPLETE", files=9)
    m = report.build_report_model(db, WEEK, legs=("cloud", "lan"))
    assert m["totals"]["total"] == 2
    assert {r["run_id"] for r in m["runs"]} == {"c", "l"}
    assert m["per_leg"]["cloud"] == {"files": 100, "bytes": 1000}
    assert m["per_leg"]["lan"] == {"files": 40, "bytes": 400}


def test_p5_disabled_leg_has_no_audit_row(db):
    _run(db, "c", "2026-10-14T23:00:00+05:30")
    m = report.build_report_model(db, WEEK, legs=("cloud",))
    assert [a["leg"] for a in m["audits"]] == ["cloud"]
    html = report.render_report_html(m, "Firm", is_email=False)
    assert "LAN Integrity" not in html and "CLOUD Integrity" in html


def test_p1_empty_period_still_produces_an_email_that_says_action_needed(db):
    email = report.build_summary_email(db, "Firm", 7, "Weekly", legs=("cloud",), report_period=WEEK)
    assert email is not None
    subject, body, attachments = email
    assert "action needed" in subject and "no backup ran" in subject
    assert "12-18 Oct 2026" in subject
    assert "No backup ran" in body
    assert attachments and attachments[0]["content"].startswith(b"Started At")   # header-only CSV


def test_p1_download_of_an_empty_period_says_no_backups_in_period(db):
    html = report.generate_report_html(db, "Firm", 7, "Weekly", legs=("cloud",), report_period=WEEK)
    assert "No backups in 12-18 Oct 2026" in html


def test_report_period_bounds_the_runs(db):
    _run(db, "before", "2026-10-11T23:59:00+05:30")
    _run(db, "in", "2026-10-12T00:01:00+05:30")
    _run(db, "after", "2026-10-19T00:00:00+05:30")
    m = report.build_report_model(db, WEEK, legs=("cloud",))
    assert [r["run_id"] for r in m["runs"]] == ["in"]


def test_csv_rows_equal_the_model_runs(db):
    _run(db, "a", "2026-10-13T23:00:00+05:30", files=5)
    _run(db, "b", "2026-10-14T23:00:00+05:30", mode="lan", status="LAN_COMPLETE", files=6)
    _run(db, "r", "2026-10-15T00:30:00+05:30", mode="rollover", status="ROLLOVER_COMPLETE")
    _, _, att = report.build_summary_email(db, "Firm", 7, "Weekly", legs=("cloud", "lan"),
                                           report_period=WEEK)
    lines = att[0]["content"].decode().strip().splitlines()
    assert len(lines) == 1 + 2                       # header + the two backups, no rollover row
