"""Report shortfall rule (opus-decision D10 / shortfall, docs/OPUS_XFAIL_DECISIONS_2026-11-23.md):
a scheduled night with NO run row is "action needed: <leg> (<FY>) - N of M scheduled nights have no backup
record" in the weekly, the monthly and the dashboard report (one model). A night is covered when a run row
of that leg and FY starts within [firing, firing + budget + stale_grace_hours); a row with a bad status
covers it too (the problem line reports it). The window never starts before the leg's first run row.

Fixture world: LAN leg only, daily at 21:00 (budget 4 h + grace 2 h = 6 h); configured FY26-27."""
from unittest.mock import patch

import pendulum
import pytest

from core import fy_overlap, report
from core import rollover_journal as rj
from core.manifest import ManifestDB
from core.report_period import ReportPeriod
from core.time_utils import IST
from models.config import AppConfig

A, B = "FY26-27", "FY27-28"


def at(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


def _cfg(tmp_path, fy=A) -> AppConfig:
    return AppConfig(
        paths={"source_drive": str(tmp_path / fy), "lan_destination": "\\\\nas\\backup\\" + fy,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        schedule={"lan_cron": "0 21 * * *"},
        notifications={"recipients": ["o@firm.example"]})


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _row(db, fy, when, status="LAN_COMPLETE"):
    t = when.isoformat()
    db.insert_run({"fy": fy, "run_id": f"{fy}-{t}", "mode": "lan", "started_at": t, "ended_at": t,
                   "status": status, "files_copied": 5, "bytes_copied": 50})


def _nights(db, fy, first, last, skip=(), hour=21, minute=0):
    d = first
    while d <= last:
        if d.date() not in [s.date() for s in skip]:
            _row(db, fy, d.at(hour, minute))
        d = d.add(days=1)


def _model(db, cfg, rp, today):
    with patch.object(fy_overlap, "run_date", return_value=today.date()):
        return report.build_report_model(db, rp, legs=("lan",), schedule=cfg.schedule, config=cfg)


WEEK = ReportPeriod.previous_week(at(2027, 1, 4, 8))          # Mon 28 Dec 2026 - Sun 3 Jan 2027


def test_configured_fy_with_4_of_7_nights_uncovered_is_action_needed(db, tmp_path):
    cfg = _cfg(tmp_path)
    _row(db, A, at(2026, 12, 20, 21))                           # the leg ran before: not an install week
    _nights(db, A, at(2026, 12, 28), at(2026, 12, 30))          # 28, 29, 30 Dec only
    m = _model(db, cfg, WEEK, at(2027, 1, 4))
    assert m["act"] is True
    assert "4 of 7 scheduled nights have no backup record" in m["headline"]
    assert "(FY26-27)" in m["headline"]
    assert "4 of 7 scheduled nights have no backup record" in report.render_report_html(m, "Firm")


def test_complete_week_stays_green(db, tmp_path):
    cfg = _cfg(tmp_path)
    _nights(db, A, at(2026, 12, 21), at(2027, 1, 3))
    m = _model(db, cfg, WEEK, at(2027, 1, 4))
    assert m["act"] is False and m["headline"] == "all backups OK"


def test_install_week_first_run_row_mid_window_stays_green(db, tmp_path):
    cfg = _cfg(tmp_path)
    _nights(db, A, at(2026, 12, 30), at(2027, 1, 3))            # installed on Wednesday: 5 nights
    m = _model(db, cfg, WEEK, at(2027, 1, 4))
    assert m["summaries"][0]["scheduled"] == 7                  # the plan still shows the whole week
    assert m["act"] is False and m["headline"] == "all backups OK"


def test_leg_that_never_ran_keeps_the_no_backup_ran_rule(db, tmp_path):
    cfg = _cfg(tmp_path)
    m = _model(db, cfg, WEEK, at(2027, 1, 4))
    assert m["act"] is True and "no backup ran" in m["headline"]


def test_sunday_run_that_starts_after_midnight_is_covered(db, tmp_path):
    cfg = _cfg(tmp_path)
    _nights(db, A, at(2026, 12, 21), at(2027, 1, 2))
    _row(db, A, at(2027, 1, 4, 0, 30))                          # the Sunday 21:00 night started Monday 00:30
    m = _model(db, cfg, WEEK, at(2027, 1, 4))
    assert m["act"] is False and m["headline"] == "all backups OK"


def test_two_runs_cover_one_night_only(db, tmp_path):
    cfg = _cfg(tmp_path)
    _nights(db, A, at(2026, 12, 21), at(2027, 1, 3), skip=[at(2026, 12, 29)])
    _row(db, A, at(2026, 12, 28, 22, 0))                        # a second run of the SAME night
    m = _model(db, cfg, WEEK, at(2027, 1, 4))
    assert m["act"] is True and "1 of 7 scheduled nights have no backup record" in m["headline"]


def test_blocked_night_is_the_existing_problem_line_not_a_double_count(db, tmp_path):
    cfg = _cfg(tmp_path)
    _nights(db, A, at(2026, 12, 21), at(2027, 1, 3), skip=[at(2027, 1, 3)])
    _row(db, A, at(2027, 1, 3, 21), status="LAN_BLOCKED")
    m = _model(db, cfg, WEEK, at(2027, 1, 4))
    assert m["summaries"][0]["missing"] == 0
    assert "no backup record" not in m["headline"]
    assert m["act"] is True                                     # the open problem is reported as before


def test_activation_mid_week_counts_from_activation(db, tmp_path):
    cfg = _cfg(tmp_path)
    db.record_activation(B, activated_at="2026-12-30T09:00:00+05:30", source_path="x", created_by_app=True)
    _nights(db, A, at(2026, 12, 21), at(2027, 1, 3))
    _nights(db, B, at(2026, 12, 30), at(2027, 1, 3))
    m = _model(db, cfg, WEEK, at(2027, 1, 4))
    assert m["act"] is False, m["headline"]


def test_activation_mid_week_gap_after_activation_is_caught(db, tmp_path):
    cfg = _cfg(tmp_path)
    db.record_activation(B, activated_at="2026-12-30T09:00:00+05:30", source_path="x", created_by_app=True)
    _nights(db, A, at(2026, 12, 21), at(2027, 1, 3))
    _nights(db, B, at(2026, 12, 30), at(2027, 1, 3), skip=[at(2027, 1, 1), at(2027, 1, 2)])
    m = _model(db, cfg, WEEK, at(2027, 1, 4))
    assert m["act"] is True
    assert "(FY27-28) - 2 of 5 scheduled nights have no backup record" in m["headline"]


def test_closed_fy_without_a_switch_says_no_longer_backed_up(db, tmp_path):
    cfg = _cfg(tmp_path)
    db.record_activation(B, activated_at="2027-01-01T09:00:00+05:30", source_path="x", created_by_app=True)
    week = ReportPeriod.previous_week(at(2027, 1, 25, 8))       # Mon 18 - Sun 24 Jan
    _nights(db, A, at(2027, 1, 1), at(2027, 1, 24))
    _nights(db, B, at(2027, 1, 1), at(2027, 1, 20))             # overlap switched off on 21 Jan, no CONFIG_SWITCH
    with patch.object(fy_overlap, "active_folders", return_value=[A]):
        m = _model(db, cfg, week, at(2027, 1, 25))
    assert m["act"] is True
    assert "FY27-28 is no longer backed up - 4 of 7 nights have no record" in m["headline"]


def test_closed_fy_with_a_switch_is_not_flagged_but_a_gap_before_it_is(db, tmp_path):
    cfg = _cfg(tmp_path, fy=B)
    db.record_activation(B, activated_at="2027-01-01T09:00:00+05:30", source_path="x", created_by_app=True)
    sw = "2027-04-01T02:00:00+05:30"
    db.rollover_open(A, B, sw)
    db.rollover_step_done(A, B, rj.CONFIG_SWITCH, sw)
    week = ReportPeriod.previous_week(at(2027, 4, 5, 8))        # Mon 29 Mar - Sun 4 Apr
    _nights(db, A, at(2027, 3, 20), at(2027, 3, 31))
    _nights(db, B, at(2027, 3, 20), at(2027, 4, 4))
    m = _model(db, cfg, week, at(2027, 4, 5))
    assert m["act"] is False and m["headline"] == "all backups OK", m["headline"]
    with db._lock:
        db._get_conn().execute("DELETE FROM run_history WHERE fy = ? AND started_at LIKE '2027-03-30%'", (A,))
        db._get_conn().commit()
    m = _model(db, cfg, week, at(2027, 4, 5))
    assert m["act"] is True
    assert "(FY26-27) - 1 of 3 scheduled nights have no backup record" in m["headline"]
    assert "no longer backed up" not in m["headline"]


def test_monthly_report_counts_the_month(db, tmp_path):
    cfg = _cfg(tmp_path)
    month = ReportPeriod.previous_month(at(2027, 1, 1, 8))      # December 2026
    _row(db, A, at(2026, 11, 30, 21))
    _nights(db, A, at(2026, 12, 1), at(2026, 12, 26))           # 26 of 31 nights
    m = _model(db, cfg, month, at(2027, 1, 1))
    assert m["act"] is True
    assert "5 of 31 scheduled nights have no backup record" in m["headline"]


def test_first_run_start_is_the_minimum_per_leg_and_fy(db):
    assert db.first_run_started_at("lan", A) is None
    _row(db, A, at(2026, 12, 30, 21))
    _row(db, A, at(2026, 12, 29, 21))
    _row(db, B, at(2026, 12, 1, 21))
    assert db.first_run_started_at("lan", A) == at(2026, 12, 29, 21).isoformat()
    assert db.first_run_started_at("cloud", A) is None
    with pytest.raises(ValueError):
        db.first_run_started_at("lan", "")


def test_without_a_schedule_or_config_nothing_changes(db, tmp_path):
    cfg = _cfg(tmp_path)
    _nights(db, A, at(2026, 12, 28), at(2026, 12, 30))
    with patch.object(fy_overlap, "run_date", return_value=at(2027, 1, 4).date()):
        m = report.build_report_model(db, WEEK, legs=("lan",), schedule=cfg.schedule)
        m2 = report.build_report_model(db, WEEK, legs=("lan",), config=cfg)
    assert m["headline"] == "all backups OK" and m2["headline"] == "all backups OK"
