"""Plan C1/C2: ReportPeriod value object and ManifestDB.get_runs_between."""
import pendulum
import pytest

from core.manifest import ManifestDB
from core.report_period import ReportPeriod
from core.time_utils import IST


def _ist(*a):
    return pendulum.datetime(*a, tz=IST)


def test_previous_week_is_monday_to_monday_ist():
    # Monday 19 Oct 2026 08:00 (report time): last full week = Mon 12 Oct .. Mon 19 Oct 00:00
    p = ReportPeriod.previous_week(_ist(2026, 10, 19, 8, 0))
    assert p.start == _ist(2026, 10, 12) and p.end == _ist(2026, 10, 19)
    assert p.kind == "Weekly" and p.label == "12-18 Oct 2026"


def test_previous_week_late_send_still_covers_the_right_week():
    # G-9: the flow was scheduled Mon 19 Oct 08:00 but ran Thursday; the caller passes the
    # SCHEDULED time, so the same week is reported either way
    on_time = ReportPeriod.previous_week(_ist(2026, 10, 19, 8, 0))
    assert (on_time.start, on_time.end) == (_ist(2026, 10, 12), _ist(2026, 10, 19))


def test_previous_week_across_month_label():
    p = ReportPeriod.previous_week(_ist(2026, 11, 2, 8, 0))
    assert p.label == "26 Oct - 1 Nov 2026"


def test_previous_month_lengths():
    feb = ReportPeriod.previous_month(_ist(2026, 3, 1, 8, 0))
    assert feb.start == _ist(2026, 2, 1) and feb.end == _ist(2026, 3, 1) and feb.label == "February 2026"
    leap = ReportPeriod.previous_month(_ist(2028, 3, 1, 8, 0))
    assert (leap.end - leap.start).in_days() == 29
    dec = ReportPeriod.previous_month(_ist(2027, 1, 1, 8, 0))
    assert dec.start == _ist(2026, 12, 1) and (dec.end - dec.start).in_days() == 31
    assert dec.kind == "Monthly"


def test_previous_month_sent_late_in_the_month_still_means_previous_calendar_month():
    p = ReportPeriod.previous_month(_ist(2026, 11, 3, 8, 0))
    assert p.start == _ist(2026, 10, 1) and p.end == _ist(2026, 11, 1)


def test_last_days_is_rolling_up_to_now():
    now = _ist(2026, 10, 19, 8, 30)
    p = ReportPeriod.last_days(7, now)
    assert p.end == now and p.start == now.subtract(days=7)
    assert p.kind == "Weekly" and ReportPeriod.last_days(30, now).kind == "Monthly"
    assert ReportPeriod.last_days(3, now).kind == "Last 3 days"


def test_contains_is_half_open():
    p = ReportPeriod.previous_week(_ist(2026, 10, 19, 8, 0))
    assert p.contains(_ist(2026, 10, 12)) and not p.contains(_ist(2026, 10, 19))
    assert p.contains(_ist(2026, 10, 18, 23, 59, 59))


def _run(db, rid, started, mode="cloud", status="CLOUD_COMPLETE"):
    db.insert_run({"run_id": rid, "mode": mode, "started_at": started, "status": status})


def test_get_runs_between_boundaries_and_midnight_run(tmp_path):
    db = ManifestDB(tmp_path / "m.db")
    try:
        _run(db, "a", "2026-10-11T23:59:59+05:30")      # before
        _run(db, "b", "2026-10-12T00:00:00+05:30")      # start included
        _run(db, "c", "2026-10-18T23:30:00+05:30")      # over-midnight run belongs to its START day
        _run(db, "d", "2026-10-19T00:00:00+05:30")      # end excluded
        _run(db, "e", "2026-10-15T01:00:00+05:30", status="RUNNING")   # in progress still returned
        got = db.get_runs_between(_ist(2026, 10, 12), _ist(2026, 10, 19))
        assert [r["run_id"] for r in got] == ["c", "e", "b"]       # newest first
        assert db.get_runs_between(_ist(2026, 10, 12), _ist(2026, 10, 19), mode="lan") == []
    finally:
        db.close()
