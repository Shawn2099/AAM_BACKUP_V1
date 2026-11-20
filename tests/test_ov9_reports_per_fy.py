"""OV-9.1 - reports have one section per FY (new first); the scheduled-run count of a FY covers
max(period start, activation) to min(period end, switch) (plan 5.9, R-10), so the first January report,
the December report sent on 1 Jan, the March report and the 30 Mar - 5 Apr report never raise a false
"no backup ran". With one FY the report is unchanged.

Fixture world: LAN leg only, daily at 21:00; configured FY26-27 (FY27-28 after the April switch)."""
import re
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


def _run(db, fy, day: pendulum.DateTime, status="LAN_COMPLETE"):
    t = day.at(21, 0).isoformat()
    db.insert_run({"fy": fy, "run_id": f"{fy}-{t}", "mode": "lan", "started_at": t, "ended_at": t,
                   "status": status, "files_copied": 5, "bytes_copied": 50})


def _nightly(db, fy, first, last):
    d = first
    while d <= last:
        _run(db, fy, d)
        d = d.add(days=1)


def _model(db, cfg, rp, today):
    with patch.object(fy_overlap, "run_date", return_value=today.date()):
        return report.build_report_model(db, rp, legs=("lan",), schedule=cfg.schedule, config=cfg)


def _by_fy(model):
    return {s["fy"]: s for s in model["sections"]}


def _switch(db, hour_iso="2027-04-01T02:00:00+05:30"):
    db.rollover_open(A, B, hour_iso)
    db.rollover_step_done(A, B, rj.CONFIG_SWITCH, hour_iso)


def test_first_january_weekly_counts_from_activation(db, tmp_path):
    cfg = _cfg(tmp_path)
    db.record_activation(B, activated_at="2027-01-01T09:00:00+05:30", source_path="x", created_by_app=True)
    _nightly(db, A, at(2026, 12, 28), at(2027, 1, 3))
    _nightly(db, B, at(2027, 1, 1), at(2027, 1, 3))
    rp = ReportPeriod.previous_week(at(2027, 1, 4, 8))
    m = _model(db, cfg, rp, at(2027, 1, 4))
    assert [sec["fy"] for sec in m["sections"]] == [B, A]                    # new first
    sec = _by_fy(m)
    assert sec[B]["summaries"][0]["scheduled"] == 3                          # 1, 2, 3 Jan at 21:00
    assert sec[A]["summaries"][0]["scheduled"] == 7
    assert sec[B]["totals"]["total"] == 3 and sec[A]["totals"]["total"] == 7
    assert m["totals"]["total"] == 10
    assert m["act"] is False


def test_first_report_after_activation_with_no_run_yet_is_not_a_missing_backup(db, tmp_path):
    cfg = _cfg(tmp_path)
    db.record_activation(B, activated_at="2027-01-03T22:00:00+05:30", source_path="x", created_by_app=True)
    _nightly(db, A, at(2026, 12, 28), at(2027, 1, 3))
    rp = ReportPeriod.previous_week(at(2027, 1, 4, 8))
    m = _model(db, cfg, rp, at(2027, 1, 4))
    sec = _by_fy(m)
    assert sec[B]["summaries"][0]["scheduled"] == 0                          # activated after the last 21:00
    assert sec[B]["summaries"][0]["total"] == 0
    assert m["act"] is False and "no backup ran" not in m["headline"]
    html = report.render_report_html(m, "Firm")
    assert "No backup was scheduled in this period" in html


def test_december_monthly_sent_on_1_jan_has_no_false_section_for_the_new_fy(db, tmp_path):
    cfg = _cfg(tmp_path)
    db.record_activation(B, activated_at="2027-01-01T09:00:00+05:30", source_path="x", created_by_app=True)
    _nightly(db, A, at(2026, 12, 1), at(2026, 12, 31))
    rp = ReportPeriod.previous_month(at(2027, 1, 1, 8))                      # December 2026
    m = _model(db, cfg, rp, at(2027, 1, 1))
    assert [sec["fy"] for sec in m["sections"]] == [A]                      # FY27-28 had not started
    assert m["summaries"][0]["scheduled"] == 31 and m["summaries"][0]["total"] == 31
    assert m["act"] is False and m["headline"] == "all backups OK"
    assert "FY27-28" not in report.render_report_html(m, "Firm")


def test_march_monthly_has_both_fys_for_the_whole_month(db, tmp_path):
    cfg = _cfg(tmp_path)
    db.record_activation(B, activated_at="2027-01-01T09:00:00+05:30", source_path="x", created_by_app=True)
    _nightly(db, A, at(2027, 3, 1), at(2027, 3, 31))
    _nightly(db, B, at(2027, 3, 1), at(2027, 3, 31))
    rp = ReportPeriod.previous_month(at(2027, 4, 1, 8))                      # March 2027
    m = _model(db, cfg, rp, at(2027, 3, 31))                                 # both still active
    sec = _by_fy(m)
    assert list(sec) == [B, A]
    assert sec[B]["summaries"][0]["scheduled"] == 31 and sec[A]["summaries"][0]["scheduled"] == 31
    assert m["act"] is False
    html = report.render_report_html(m, "Firm")
    assert html.index(B) < html.index(A)                                     # new FY first on the page
    assert "LAN (FY27-28)" not in m["headline"]


def test_r10_week_over_the_switch_stops_counting_the_old_fy_at_the_switch(db, tmp_path):
    cfg = _cfg(tmp_path, fy=B)                                               # config already switched
    db.record_activation(B, activated_at="2027-01-01T09:00:00+05:30", source_path="x", created_by_app=True)
    _switch(db)
    _nightly(db, A, at(2027, 3, 29), at(2027, 3, 31))                        # nothing for A after the switch
    _nightly(db, B, at(2027, 3, 29), at(2027, 4, 4))
    rp = ReportPeriod.previous_week(at(2027, 4, 5, 8))                       # Mon 29 Mar - Sun 4 Apr
    m = _model(db, cfg, rp, at(2027, 4, 5))
    sec = _by_fy(m)
    assert list(sec) == [B, A]                                               # A has runs in the period
    assert sec[A]["summaries"][0]["scheduled"] == 3                          # 29, 30, 31 Mar; not 1-4 Apr
    assert sec[B]["summaries"][0]["scheduled"] == 7
    assert m["act"] is False


def test_r10_old_fy_missing_a_night_before_the_switch_is_still_caught(db, tmp_path):
    cfg = _cfg(tmp_path, fy=B)
    db.record_activation(B, activated_at="2027-01-01T09:00:00+05:30", source_path="x", created_by_app=True)
    _switch(db)
    _nightly(db, A, at(2027, 3, 29), at(2027, 3, 29))
    _nightly(db, B, at(2027, 3, 29), at(2027, 4, 4))
    rp = ReportPeriod.previous_week(at(2027, 4, 5, 8))
    m = _model(db, cfg, rp, at(2027, 4, 5))
    assert _by_fy(m)[A]["summaries"][0]["scheduled"] == 3
    assert _by_fy(m)[A]["totals"]["total"] == 1                              # 2 of 3 missing: visible in counts


def test_week_after_the_switch_has_no_section_for_the_old_fy(db, tmp_path):
    cfg = _cfg(tmp_path, fy=B)
    db.record_activation(B, activated_at="2027-01-01T09:00:00+05:30", source_path="x", created_by_app=True)
    _switch(db)
    _nightly(db, B, at(2027, 4, 5), at(2027, 4, 11))
    rp = ReportPeriod.previous_week(at(2027, 4, 12, 8))
    m = _model(db, cfg, rp, at(2027, 4, 12))
    assert [sec["fy"] for sec in m["sections"]] == [B]
    assert m["act"] is False


def test_failure_in_one_fy_names_the_fy_in_the_headline(db, tmp_path):
    cfg = _cfg(tmp_path)
    db.record_activation(B, activated_at="2027-01-01T09:00:00+05:30", source_path="x", created_by_app=True)
    _nightly(db, A, at(2027, 3, 1), at(2027, 3, 31))
    _nightly(db, B, at(2027, 3, 1), at(2027, 3, 30))
    _run(db, B, at(2027, 3, 31), status="LAN_FAILED")
    rp = ReportPeriod.previous_month(at(2027, 4, 1, 8))
    m = _model(db, cfg, rp, at(2027, 3, 31))
    assert m["act"] is True and "(FY27-28)" in m["headline"]


def _strip_clock(html: str) -> str:
    return re.sub(r"\(generated [^)]*\)", "", html)


def test_one_fy_report_is_unchanged(db, tmp_path):
    cfg = _cfg(tmp_path)
    _nightly(db, A, at(2026, 10, 12), at(2026, 10, 17))
    rp = ReportPeriod.previous_week(at(2026, 10, 19, 8))
    with patch.object(fy_overlap, "run_date", return_value=at(2026, 10, 19).date()):
        with_cfg = report.build_report_model(db, rp, legs=("lan",), schedule=cfg.schedule, config=cfg)
        without = report.build_report_model(db, rp, legs=("lan",), schedule=cfg.schedule)
    assert len(with_cfg["sections"]) == 1 and len(without["sections"]) == 1
    assert with_cfg["headline"] == without["headline"]
    assert with_cfg["summaries"][0]["scheduled"] == without["summaries"][0]["scheduled"] == 7
    html = _strip_clock(report.render_report_html(with_cfg, "Firm"))
    assert html == _strip_clock(report.render_report_html(without, "Firm"))
    assert "FY26-27" not in html and "<h4" not in html
    assert "LAN Integrity" in html and "(FY" not in html


def test_build_summary_email_threads_config(db, tmp_path):
    cfg = _cfg(tmp_path)
    db.record_activation(B, activated_at="2027-01-01T09:00:00+05:30", source_path="x", created_by_app=True)
    _nightly(db, A, at(2027, 3, 1), at(2027, 3, 31))
    _nightly(db, B, at(2027, 3, 1), at(2027, 3, 31))
    rp = ReportPeriod.previous_month(at(2027, 4, 1, 8))
    with patch.object(fy_overlap, "run_date", return_value=at(2027, 3, 31).date()):
        subject, body, att = report.build_summary_email(db, "Firm", 30, "Monthly", legs=("lan",),
                                                        report_period=rp, schedule=cfg.schedule, config=cfg)
    assert "FY27-28" in body and "FY26-27" in body
    assert att[0]["content"]


# ── review follow-ups (Opus review of WP9) ───────────────────────────────────────────────────

def _after_switch_no_activation(db, tmp_path, prev, new, overlap_off=True):
    cfg = _cfg(tmp_path, fy=new)                                             # config switched at 06:05
    sw = f"20{new[2:4]}-04-01T06:05:00+05:30"
    db.rollover_open(prev, new, sw)
    db.rollover_step_done(prev, new, rj.CONFIG_SWITCH, sw)
    return cfg


def test_b1_monthly_on_1_april_after_the_switch_without_activation_is_not_a_missing_backup(db, tmp_path):
    cfg = _after_switch_no_activation(db, tmp_path, A, B)
    _nightly(db, A, at(2027, 3, 1), at(2027, 3, 31))
    m = _model(db, cfg, ReportPeriod.previous_month(at(2027, 4, 1, 8, 30)), at(2027, 4, 1))
    assert m["act"] is False, m["headline"]
    assert "no backup ran" not in m["headline"].lower()
    for sec in m["sections"]:
        for s in sec["summaries"]:
            assert s.get("scheduled") != 0 or sec["fy"] != B                  # B planned nothing in March


def test_b1_weekly_on_monday_1_april_after_the_switch_without_activation(db, tmp_path):
    a, b = "FY23-24", "FY24-25"
    cfg = _after_switch_no_activation(db, tmp_path, a, b)
    _nightly(db, a, at(2024, 3, 25), at(2024, 3, 31))
    rp = ReportPeriod.previous_week(at(2024, 4, 1, 8, 30))
    m = _model(db, cfg, rp, at(2024, 4, 1))
    assert m["act"] is False, m["headline"]
    assert "no backup ran" not in m["headline"].lower()


def test_n4_sections_sort_new_first_across_the_century_wrap(db, tmp_path):
    cfg = _cfg(tmp_path, fy="FY00-01")
    for fy, d in (("FY99-00", at(2000, 3, 30)), ("FY00-01", at(2000, 4, 2))):
        _run(db, fy, d)
    rp = ReportPeriod(start=at(2000, 3, 25), end=at(2000, 4, 5), label="x", kind="week")
    plan = report.fy_sections(db, db.get_runs_between(rp.start, rp.end, fy="*"), rp, None)
    assert [fy for fy, _ in plan] == ["FY00-01", "FY99-00"]
