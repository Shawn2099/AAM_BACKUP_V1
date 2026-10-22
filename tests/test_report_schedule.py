"""Reports follow the schedule (plan Part B6): a period in which no backup was SCHEDULED is not a
missing backup. "action needed: no backup ran" only when a scheduled run should have happened."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pendulum
import pytest

from core import overdue, report
from core.manifest import ManifestDB
from core.report_period import ReportPeriod
from core.time_utils import IST

TZ = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _schedule(lan="0 2 * * SUN", cloud="0 2 * * SUN"):
    return SimpleNamespace(lan_cron=lan, cloud_cron=cloud, timezone="Asia/Kolkata")


def _period(start, end, label="x"):
    return ReportPeriod(pendulum.datetime(*start, tz=IST), pendulum.datetime(*end, tz=IST), label, "Weekly")


def test_firings_between_counts_scheduled_runs_in_a_half_open_interval():
    a, b = datetime(2026, 10, 19, tzinfo=TZ), datetime(2026, 10, 26, tzinfo=TZ)
    assert overdue.firings_between("0 2 * * SUN", "Asia/Kolkata", a, b) == 1       # Sun 25 Oct
    assert overdue.firings_between("0 21 * * *", "Asia/Kolkata", a, b) == 7
    assert overdue.firings_between("0 21 * * MON-FRI", "Asia/Kolkata", a, b) == 5
    assert overdue.firings_between("0 2 * * SUN", "Asia/Kolkata", a,
                                   datetime(2026, 10, 25, 2, 0, tzinfo=TZ)) == 0   # end is exclusive


def test_no_run_in_a_period_with_no_scheduled_run_is_not_a_missing_backup(db):
    rp = _period((2026, 10, 19), (2026, 10, 22))                         # Mon-Wed: no Sunday inside
    model = report.build_report_model(db, rp, ("lan",), schedule=_schedule())
    assert model["headline"] == "no backup was scheduled in this period" and model["act"] is False
    assert model["summaries"][0]["scheduled"] == 0


def test_no_run_in_a_period_with_a_scheduled_run_is_still_action_needed(db):
    rp = _period((2026, 10, 19), (2026, 10, 26))                         # contains Sunday 25 Oct
    model = report.build_report_model(db, rp, ("lan",), schedule=_schedule())
    assert model["act"] is True and "no backup ran" in model["headline"]
    assert model["summaries"][0]["scheduled"] == 1


def test_without_a_schedule_nothing_changes(db):
    rp = _period((2026, 10, 19), (2026, 10, 22))
    model = report.build_report_model(db, rp, ("lan",))
    assert model["act"] is True and "no backup ran" in model["headline"]


def test_the_body_says_nothing_was_scheduled_instead_of_no_backup_ran(db):
    rp = _period((2026, 10, 19), (2026, 10, 22), label="19-21 Oct 2026")
    model = report.build_report_model(db, rp, ("lan",), schedule=_schedule())
    html = report.render_report_html(model, "Firm", is_email=True, name="Weekly")
    assert "No backup was scheduled in this period" in html
    assert "No backup ran in this period" not in html
    assert "No backups in 19-21 Oct 2026" not in html


def test_the_send_paths_pass_the_schedule(db, monkeypatch):
    rp = _period((2026, 10, 19), (2026, 10, 22))
    subject, _body, _att = report.build_summary_email(db, "Firm", 7, "Weekly", legs=("lan",),
                                                      report_period=rp, schedule=_schedule())
    assert "no backup was scheduled in this period" in subject
