"""Reports in plain words (docs/EMAIL_CONTENT_SPEC.md §7.13, audit 2026-10-18
item 5) and the financial-year INFO email (§7.10, owner D5).

A per-leg summary comes first (runs OK, last good copy, problems with their
fault code and whether they were resolved, safety check, open decisions), every
run of the period is listed (not "last 10"), the monthly report covers the
previous calendar month, and the subject says the outcome."""

import json

import pendulum
import pytest

from core import report as rep
from core.manifest import ManifestDB
from core.time_utils import IST

NOW = pendulum.datetime(2026, 10, 19, 8, 0, tz=IST)          # Monday 08:00 weekly report


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _run(db, rid, leg, when, status, code=None):
    db.insert_run({"run_id": rid, "mode": leg, "started_at": when.isoformat(),
                   "ended_at": when.add(minutes=5).isoformat(), "status": status,
                   "files_copied": 3,
                   "extended_metrics": json.dumps({"fault_code": code} if code else {})})


def _week(db, lan_fail_day=None):
    for d in range(7):
        day = NOW.subtract(days=7 - d)
        _run(db, f"c{d}", "cloud", day.replace(hour=2), "CLOUD_NO_CHANGES_COMPLETE")
        bad = d == lan_fail_day
        _run(db, f"l{d}", "lan", day.replace(hour=21), "LAN_FAILED" if bad else "LAN_COMPLETE",
             "AAM-NAS-01" if bad else None)


def test_weekly_summary_first_in_plain_words(db):
    _week(db, lan_fail_day=2)
    html = rep.generate_report_html(db, "AAM", 7, "Weekly", legs=("cloud", "lan"), now=NOW)
    top = html.split("Recent Backups", 1)[0]
    assert "Online backup (Google Cloud)" in top and "7 of 7 runs OK" in top
    assert "Office NAS backup" in top and "6 of 7 runs OK" in top
    assert "NAS not reachable (AAM-NAS-01)" in top and "resolved" in top
    assert "Last good copy" in top
    assert html.count("<tr><td>") >= 14                       # every run, not the last 10


def test_subject_says_the_outcome(db):
    _week(db)
    subject, _, _ = rep.build_summary_email(db, "AAM", 7, "Weekly", legs=("cloud", "lan"), now=NOW)
    assert subject == "[AAM Backup] Weekly backup report 12-18 Oct 2026 - all backups OK"


def test_unresolved_problem_and_open_decision_need_action(db):
    _week(db, lan_fail_day=6)                          # last LAN run failed: not resolved
    db.open_approval_request(leg="lan", blocked_run_id="x", fy="FY26-27", guards=[],
                             phrase="P", created_at=NOW.isoformat())
    subject, body, _ = rep.build_summary_email(db, "AAM", 7, "Weekly", legs=("cloud", "lan"),
                                               now=NOW)
    assert subject.startswith("[AAM Backup] Weekly backup report 12-18 Oct 2026 - action needed")
    assert "not resolved" in body
    assert "1 backup waiting for your decision" in body


def test_an_enabled_leg_that_never_ran_is_shown(db):
    for d in range(7):
        _run(db, f"c{d}", "cloud", NOW.subtract(days=7 - d).replace(hour=2), "CLOUD_COMPLETE")
    subject, body, _ = rep.build_summary_email(db, "AAM", 7, "Weekly", legs=("cloud", "lan"),
                                               now=NOW)
    assert "0 of 0 runs OK" in body and "no backup ran" in body.lower()
    assert "action needed" in subject


def test_monthly_is_the_previous_calendar_month(db):
    first = pendulum.datetime(2026, 10, 1, 8, 30, tz=IST)
    _run(db, "aug", "lan", pendulum.datetime(2026, 8, 31, 21, tz=IST), "LAN_COMPLETE")
    _run(db, "sep1", "lan", pendulum.datetime(2026, 9, 1, 21, tz=IST), "LAN_COMPLETE")
    _run(db, "sep30", "lan", pendulum.datetime(2026, 9, 30, 21, tz=IST), "LAN_COMPLETE")
    _run(db, "oct", "lan", pendulum.datetime(2026, 10, 1, 2, tz=IST), "LAN_COMPLETE")
    subject, body, attachments = rep.build_summary_email(db, "AAM", 30, "Monthly",
                                                         legs=("lan",), now=first)
    assert "Monthly backup report September 2026" in subject
    assert "2 of 2 runs OK" in body
    csv = attachments[0]["content"].decode("utf-8")
    assert csv.count("LAN_COMPLETE") == 2


def test_fy_switch_sends_an_info_email():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "flow.py").read_text(encoding="utf-8")
    block = src.split('record("ROLLOVER_COMPLETE"', 1)[1].split("return \"ROLLOVER_COMPLETED\"", 1)[0]
    assert 'kind="INFO"' in block and "New financial year" in block


def test_fy_switch_email_reads_as_information(tmp_path):
    from core import email_compose as ec
    from core import notify as nt
    from models.config import AppConfig
    cfg = AppConfig(firm_name="AAM", paths={"source_drive": str(tmp_path / "FY27-28"),
                                             "lan_destination": "\\\\nas\\s\\FY27-28",
                                             "runtime_dir": str(tmp_path / "rt")},
                    wol={"enabled": False}, cloud={"enabled": False},
                    dashboard={"auth_enabled": False, "public_url": "http://backup-server:8080"})
    event = nt.AlertEvent(key="rollover:done:FY27-28", kind="INFO", mode="rollover",
                          title="New financial year FY27-28 started",
                          message="From today your backups use the new folder FY27-28.",
                          status="ROLLOVER_COMPLETE")
    subject, _, text = ec.compose(cfg, event, None)
    assert subject == "[AAM Backup] INFO: New financial year FY27-28 started"
    assert "Unexpected" not in text and "Nothing to do" in text
