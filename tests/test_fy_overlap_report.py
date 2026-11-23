"""B8 - reports split by financial year (docs/TESTMATRIX_B8_2026-11-24.md, DECISIONS D8-1..D8-14).

One test per row R01-R44 (R45 is the regression run of the existing report files). Layers: M =
core.report.build_report_model, H = HTML / CSV / subject, E = core.email_compose._data_safety, P = plumbing
(flow / ui callers), U = fy_overlap helpers. The database is a real ManifestDB, the config is a SimpleNamespace
(one row, R44, uses a real AppConfig), the FY maths are real; the clock is pendulum.travel_to where the page
carries a "generated" stamp.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pendulum
import pytest

from core import email_compose, fy_overlap
from core import report
from core.manifest import ManifestDB
from core.report_period import ReportPeriod

pytest.importorskip("time_machine")   # pendulum.travel_to needs it

TZ = "Asia/Kolkata"
CFG_FY, NXT = "FY26-27", "FY27-28"
ACTIVATED = "2026-12-31T06:00:00+05:30"
SWITCH = "2027-04-01T06:10:00+05:30"

CLOUD_OK, CLOUD_FAIL, CLOUD_MISS, CLOUD_RUN = "CLOUD_COMPLETE", "CLOUD_FAILED", "CLOUD_MISSED", "CLOUD_RUNNING"
LAN_OK, LAN_FAIL = "LAN_COMPLETE", "LAN_FAILED"
OK = {"cloud": CLOUD_OK, "lan": LAN_OK}
FAIL = {"cloud": CLOUD_FAIL, "lan": LAN_FAIL}
HOUR = {"cloud": 18, "lan": 1}          # default crons: cloud 18:00, LAN 01:00
NAS_DOWN = "AAM-NAS-01"
AUDIT_OK, AUDIT_BAD = "VERIFIED", "VERIFICATION_FAILED"

SERVER_LINE = "Your files on the server: never changed or deleted by the backup."


# ── helpers ───────────────────────────────────────────────────────────────

def at(*parts):
    return pendulum.datetime(*parts, tz=TZ)


def iso(moment) -> str:
    return moment.isoformat()


def cfg_ns(fy=CFG_FY, *, overlap=True, cloud=True, lan=True, cloud_sched=True, lan_sched=True):
    return SimpleNamespace(
        paths=SimpleNamespace(source_drive=rf"E:\{fy}", lan_destination=rf"\\nas\backup\{fy}"),
        rollover=SimpleNamespace(overlap_enabled=overlap),
        cloud=SimpleNamespace(enabled=cloud, scheduled=cloud_sched),
        lan=SimpleNamespace(enabled=lan, scheduled=lan_sched),
        schedule=SimpleNamespace(cloud_cron="0 18 * * *", lan_cron="0 1 * * *", timezone=TZ))


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def fresh_db(tmp_path: Path, name: str) -> ManifestDB:
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    return ManifestDB(folder / "m.db")


def activate(db, activated=ACTIVATED, fy=NXT, **extra):
    db.set_overlap_record(fy, {"activated_at": activated, "from_fy": CFG_FY, **extra})


def add_run(db, run_id, mode, status, started, *, files=0, nbytes=0, metrics=None):
    db.insert_run({"run_id": run_id, "mode": mode, "started_at": iso(started), "ended_at": iso(started),
                   "status": status, "exit_code": 0, "files_copied": files, "bytes_copied": nbytes,
                   "extended_metrics": None if metrics is None else json.dumps(metrics)})


def add_pass(db, leg, when, fy, status=None, *, nxt=False, files=0, nbytes=0, code=None, metrics="fy", rid=None):
    """One backup row. nxt = the suffixed run of a next-FY pass; metrics: fy | null | empty | guards | dict."""
    base = f"{when.format('YYYYMMDDHHmm')}-{leg}"
    run_id = rid or (f"{base}-{fy}" if nxt else base)
    if metrics == "fy":
        data = {"fy": fy, **({"overlap": True} if nxt else {})}
    elif metrics == "null":
        data = None
    elif metrics == "empty":
        data = {}
    elif metrics == "guards":
        data = {"guards": {"fy": fy}}
    else:
        data = metrics
    if code:
        data = {**(data or {}), "fault_code": code}
    add_run(db, run_id, leg, status or OK[leg], when, files=files, nbytes=nbytes, metrics=data)


def add_audit(db, audit_id, mode, status, started):
    db.record_audit({"audit_id": audit_id, "mode": mode, "started_at": iso(started),
                     "ended_at": iso(started), "status": status, "detail": None})


def nightly(db, leg, first, last, fy, *, nxt=False, mode="fy", over=None):
    """One row per day first..last at the leg's cron hour. over[(leg, fy, (month, day))] = status or (status, code)."""
    cur, stop, i = at(*first, HOUR[leg]), at(*last, HOUR[leg]), 0
    while cur <= stop:
        o = (over or {}).get((leg, fy, (cur.month, cur.day)))
        status, code = (o if isinstance(o, tuple) else (o, None)) if o else (None, None)
        m = mode
        if mode == "mixed":
            m = ("fy", "null", "empty")[i % 3] if not nxt else "fy"
        add_pass(db, leg, cur, fy, status, nxt=nxt, code=code, metrics=m)
        cur, i = cur.add(days=1), i + 1


def jan_world(db, *, cloud_next=(2027, 1, 3), lan_next=(2027, 1, 3), legs=("cloud", "lan"), over=None, mode="fy",
              activated=ACTIVATED):
    """Record open since 31 Dec 06:00. Configured nightly 28 Dec - 3 Jan; FY27-28 cloud from 31 Dec, LAN from 1 Jan."""
    activate(db, activated)
    for leg in legs:
        nightly(db, leg, (2026, 12, 28), (2027, 1, 3), CFG_FY, mode=mode, over=over)
    if "cloud" in legs and cloud_next:
        nightly(db, "cloud", (2026, 12, 31), cloud_next, NXT, nxt=True, over=over)
    if "lan" in legs and lan_next:
        nightly(db, "lan", (2027, 1, 1), lan_next, NXT, nxt=True, over=over)


WEEK_JAN = ReportPeriod.previous_week(at(2027, 1, 4, 8))      # 28 Dec 2026 - 4 Jan 2027
WEEK_OCT = ReportPeriod.previous_week(at(2026, 10, 19, 8))    # 12 - 19 Oct 2026
WEEK_SWITCH = ReportPeriod.previous_week(at(2027, 4, 5, 8))   # 29 Mar - 5 Apr 2027
BOTH = ("cloud", "lan")


def build(db, cfg, rp, legs=BOTH):
    return report.build_report_model(db, rp, legs, schedule=cfg.schedule, config=cfg)


def html_of(db, cfg, rp, legs=BOTH, name="Weekly"):
    with pendulum.travel_to(rp.end.add(hours=8), freeze=True):
        return report.generate_report_html(db, "AAM", 7, name, legs=legs, report_period=rp,
                                           schedule=cfg.schedule, config=cfg)


def sec(model, leg, fy=None):
    hits = [s for s in model["summaries"] if s["leg"] == leg and s.get("fy") == fy]
    assert len(hits) == 1, (leg, fy, [(s["leg"], s.get("fy")) for s in model["summaries"]])
    return hits[0]


def order(model):
    return [(s["leg"], s.get("fy")) for s in model["summaries"]]


def assert_baseline(db, cfg, rp, legs=BOTH):
    """config=cfg gives exactly what config=None gives: model, page, subject, body and CSV."""
    with pendulum.travel_to(rp.end.add(hours=8), freeze=True):
        kw = dict(legs=legs, report_period=rp, schedule=cfg.schedule)
        m0 = report.build_report_model(db, rp, legs, schedule=cfg.schedule)
        m1 = report.build_report_model(db, rp, legs, schedule=cfg.schedule, config=cfg)
        assert m1 == m0
        assert "fy_view" not in m1
        h0 = report.generate_report_html(db, "AAM", 7, "Weekly", **kw)
        h1 = report.generate_report_html(db, "AAM", 7, "Weekly", config=cfg, **kw)
        assert h1 == h0
        e0 = report.build_summary_email(db, "AAM", 7, "Weekly", **kw)
        e1 = report.build_summary_email(db, "AAM", 7, "Weekly", config=cfg, **kw)
        assert e1 == e0
        assert e1[2][0]["content"] == e0[2][0]["content"]
    return m1


def oct_week(db, *, lan=True, lan_fail_day=None):
    for leg in BOTH if lan else ("cloud",):
        over = {("lan", CFG_FY, (10, lan_fail_day)): (LAN_FAIL, NAS_DOWN)} if lan_fail_day else None
        nightly(db, leg, (2026, 10, 12), (2026, 10, 18), CFG_FY, mode="null", over=over)


def switch_world(db, *, ended=True):
    """28 Mar - 3 Apr 2027 around the switch of 1 Apr 06:10; both passes until the switch, one pass after."""
    activate(db, **({"ended_at": SWITCH} if ended else {}))
    for d in (28, 29, 30, 31):
        add_pass(db, "cloud", at(2027, 3, d, 18), CFG_FY)
        add_pass(db, "cloud", at(2027, 3, d, 18), NXT, nxt=True)
    for m, d in ((3, 28), (3, 29), (3, 30), (3, 31), (4, 1)):
        add_pass(db, "lan", at(2027, m, d, 1), CFG_FY)
        add_pass(db, "lan", at(2027, m, d, 1), NXT, nxt=True)
    add_pass(db, "cloud", at(2027, 4, 1, 6), CFG_FY, metrics="null", rid="x-final-cloud")
    add_pass(db, "lan", at(2027, 4, 1, 6), CFG_FY, metrics="null", rid="x-final-lan")
    for d in (1, 2, 3):
        add_pass(db, "cloud", at(2027, 4, d, 18), NXT, metrics="null")
    for d in (2, 3):
        add_pass(db, "lan", at(2027, 4, d, 1), NXT, metrics="null")


# ── baseline guards (R01-R05) ─────────────────────────────────────────────

def test_R01_no_record_is_byte_identical_to_baseline(db):
    oct_week(db, lan_fail_day=15)
    assert_baseline(db, cfg_ns(), WEEK_OCT)


def test_R02_overlap_off_without_record_is_baseline(db):
    oct_week(db, lan_fail_day=15)
    assert_baseline(db, cfg_ns(overlap=False), WEEK_OCT)


def test_R03_period_before_activation_is_baseline(db):
    activate(db)
    for leg in BOTH:
        nightly(db, leg, (2026, 12, 21), (2026, 12, 27), CFG_FY, mode="null")
    assert_baseline(db, cfg_ns(), ReportPeriod.previous_week(at(2026, 12, 28, 8)))


def test_R04_baseline_rollover_week_does_not_split(db):
    cut = at(2027, 4, 1, 6, 10)
    for leg in BOTH:
        cur = at(2027, 3, 29, HOUR[leg])
        while cur <= at(2027, 4, 4, HOUR[leg]):
            add_pass(db, leg, cur, CFG_FY if cur < cut else NXT, metrics="guards")
            cur = cur.add(days=1)
    cfg = cfg_ns(NXT, overlap=False)
    assert_baseline(db, cfg, WEEK_SWITCH)
    page = html_of(db, cfg, WEEK_SWITCH)
    assert "FY26-27" not in page and "FY27-28" not in page


def test_R05_partial_config_is_baseline(db):
    oct_week(db)
    partial = SimpleNamespace(cloud=SimpleNamespace(enabled=True), lan=SimpleNamespace(enabled=True),
                              schedule=cfg_ns().schedule)
    m0 = report.build_report_model(db, WEEK_OCT, BOTH, schedule=partial.schedule)
    assert report.build_report_model(db, WEEK_OCT, BOTH, schedule=partial.schedule, config=partial) == m0
    mock = MagicMock()
    mock.get_runs_between.return_value = []
    mock.last_successful_run.return_value = None
    mock.latest_audit.return_value = None
    mock.open_approvals.return_value = []
    mock.overlap_record.return_value = None
    base = report.build_report_model(mock, WEEK_OCT, BOTH, schedule=partial.schedule)
    assert report.build_report_model(mock, WEEK_OCT, BOTH, schedule=partial.schedule, config=partial) == base


# ── the FY view (R06-R13) ─────────────────────────────────────────────────

def test_R06_first_january_week_two_sections_per_leg(db):
    jan_world(db)
    cfg = cfg_ns()
    model = build(db, cfg, WEEK_JAN)
    assert order(model) == [("cloud", CFG_FY), ("cloud", NXT), ("lan", CFG_FY), ("lan", NXT)]
    got = [(s["ok"], s["total"], s["scheduled"]) for s in model["summaries"]]
    assert got == [(7, 7, 7), (4, 4, 4), (7, 7, 7), (3, 3, 3)]
    assert model["headline"] == "all backups OK" and model["act"] is False
    assert sec(model, "cloud", NXT)["label"] == "Online backup (Google Cloud) - FY27-28"
    assert model["labels"]["cloud:FY27-28"] == "Online backup (Google Cloud) - FY27-28"
    assert set(model["per_leg"]) == {"cloud:FY26-27", "cloud:FY27-28", "lan:FY26-27", "lan:FY27-28"}
    assert model["fy_view"]["configured"] == CFG_FY and model["fy_view"]["next"] == NXT


def test_R07_december_monthly_no_false_no_backup_ran(db):
    activate(db)
    for leg in BOTH:
        nightly(db, leg, (2026, 12, 1), (2026, 12, 31), CFG_FY)
    add_pass(db, "cloud", at(2026, 12, 31, 18), NXT, nxt=True)
    cfg = cfg_ns()
    rp = ReportPeriod.previous_month(at(2027, 1, 1, 8, 30))
    model = build(db, cfg, rp)
    lan_next = sec(model, "lan", NXT)
    assert lan_next["scheduled"] == 0 and lan_next["total"] == 0
    cloud_next = sec(model, "cloud", NXT)
    assert (cloud_next["ok"], cloud_next["total"], cloud_next["scheduled"]) == (1, 1, 1)
    assert "no backup ran" not in model["headline"] and model["act"] is False
    assert "No backup was scheduled in this period." in html_of(db, cfg, rp, name="Monthly")


def test_R08_late_activation_moves_the_window(db):
    jan_world(db, activated="2027-01-01T06:05:00+05:30", cloud_next=(2027, 1, 3), lan_next=(2027, 1, 3))
    model = build(db, cfg_ns(), WEEK_JAN)
    assert sec(model, "cloud", NXT)["scheduled"] == 3
    assert sec(model, "lan", NXT)["scheduled"] == 2


def test_R09_next_fy_missing_runs_are_reported(db):
    jan_world(db, lan_next=None)
    model = build(db, cfg_ns(), WEEK_JAN)
    assert model["headline"] == "action needed: Office NAS backup - FY27-28 - no backup ran"
    assert model["act"] is True


def test_R10_problem_resolved_only_by_same_fy(db, tmp_path):
    jan_world(db, over={("lan", NXT, (1, 3)): (LAN_FAIL, NAS_DOWN)})
    add_pass(db, "lan", at(2027, 1, 4, 1), CFG_FY)                  # FY26-27 is fine, also after the period
    model = build(db, cfg_ns(), WEEK_JAN)
    problem = sec(model, "lan", NXT)["problems"]
    assert len(problem) == 1 and problem[0]["resolved"] is False
    assert sec(model, "lan", CFG_FY)["problems"] == []
    assert model["act"] is True
    mirror = fresh_db(tmp_path, "mirror")
    jan_world(mirror, over={("lan", CFG_FY, (1, 3)): (LAN_FAIL, NAS_DOWN)})
    add_pass(mirror, "lan", at(2027, 1, 4, 1), NXT, nxt=True)       # FY27-28 is fine, also after the period
    model = build(mirror, cfg_ns(), WEEK_JAN)
    problem = sec(model, "lan", CFG_FY)["problems"]
    assert len(problem) == 1 and problem[0]["resolved"] is False
    assert sec(model, "lan", NXT)["problems"] == []
    assert model["act"] is True
    mirror.close()


def test_R11_resolved_after_period_per_fy(db):
    jan_world(db, over={("lan", NXT, (1, 3)): (LAN_FAIL, NAS_DOWN), ("lan", CFG_FY, (1, 3)): (LAN_FAIL, NAS_DOWN)})
    add_pass(db, "lan", at(2027, 1, 4, 1), NXT, nxt=True)           # FY27-28 recovers after the period
    model = build(db, cfg_ns(), WEEK_JAN)
    assert [p["resolved"] for p in sec(model, "lan", NXT)["problems"]] == [True]
    assert [p["resolved"] for p in sec(model, "lan", CFG_FY)["problems"]] == [False]


def test_R12_last_good_copy_per_fy(db):
    jan_world(db, lan_next=(2027, 1, 2))
    model = build(db, cfg_ns(), WEEK_JAN)
    assert sec(model, "lan", CFG_FY)["last_good"].startswith("3 Jan 2027, 1:00 AM")
    assert sec(model, "lan", NXT)["last_good"].startswith("2 Jan 2027, 1:00 AM")


def test_R13_legacy_rows_fall_into_configured_fy(db):
    jan_world(db, mode="mixed")
    model = build(db, cfg_ns(), WEEK_JAN)
    assert sec(model, "cloud", CFG_FY)["total"] == 7 and sec(model, "lan", CFG_FY)["total"] == 7
    assert sec(model, "cloud", NXT)["total"] == 4 and sec(model, "lan", NXT)["total"] == 3
    configured = [r["run_id"] for r in model["runs"] if not r["run_id"].endswith("-FY27-28")]
    assert len(configured) == 14
    assert {model["run_fy"][rid] for rid in configured} == {CFG_FY}


# ── helpers of fy_overlap (R14, R23 unit) ─────────────────────────────────

def _row(run_id="a-cloud", started="2027-01-02T18:00:00+05:30", metrics=None):
    return {"run_id": run_id, "started_at": started,
            "extended_metrics": json.dumps(metrics) if metrics is not None else None}


def test_R14_row_fy_or_default_order():
    f = fy_overlap.row_fy_or_default
    assert f(_row(metrics={"guards": {"fy": "FY25-26"}}), CFG_FY, None) == "FY25-26"
    assert f(_row(metrics={"fy": NXT}), CFG_FY, None) == NXT
    assert f(_row("b-cloud-FY27-28", metrics={}), CFG_FY, None) == NXT
    assert f(_row("c-cloud-FY27-28", metrics={"fy": CFG_FY}), CFG_FY, None) == NXT            # suffix > fy
    assert f(_row(metrics={"fy": NXT, "guards": {"fy": CFG_FY}}), CFG_FY, None) == NXT         # fy > guards.fy
    assert f(_row(metrics=None), CFG_FY, None) == CFG_FY
    assert f(_row(metrics={}), CFG_FY, None) == CFG_FY


def test_R14_row_fy_or_default_closed_at_instants():
    f = fy_overlap.row_fy_or_default
    closed = SWITCH
    assert f(_row(started="2027-03-31T18:00:00+05:30"), NXT, closed) == CFG_FY
    assert f(_row(started="2027-04-01T06:09:59+05:30"), NXT, closed) == CFG_FY
    assert f(_row(started="2027-04-01T06:10:00+05:30"), NXT, closed) == NXT
    assert f(_row(started="2027-03-31T20:00:00+00:00"), NXT, closed) == CFG_FY                 # 01:30 IST on 1 Apr
    assert f(_row(started="2027-04-01T01:00:00+00:00"), NXT, closed) == NXT                    # 06:30 IST
    assert f(_row(started="garbage"), NXT, closed) == NXT
    assert f(_row(started="2027-03-31T18:00:00+05:30", metrics={"fy": NXT}), NXT, closed) == NXT


def test_R15_data_per_leg_and_fy_never_summed(db):
    activate(db)
    add_pass(db, "cloud", at(2027, 1, 1, 18), CFG_FY, files=100, nbytes=1000)
    add_pass(db, "cloud", at(2027, 1, 1, 18), NXT, nxt=True, files=40, nbytes=400)
    cfg = cfg_ns()
    model = build(db, cfg, WEEK_JAN, legs=("cloud",))
    assert model["per_leg"]["cloud:FY26-27"] == {"files": 100, "bytes": 1000}
    assert model["per_leg"]["cloud:FY27-28"] == {"files": 40, "bytes": 400}
    page = html_of(db, cfg, WEEK_JAN, legs=("cloud",))
    assert "Files copied (Online backup (Google Cloud) - FY27-28)" in page
    assert "Files copied (Online backup (Google Cloud) - FY26-27)" in page
    assert "Files copied (Online backup (Google Cloud))" not in page


def test_R16_run_table_names_the_fy(db, tmp_path):
    jan_world(db)
    page = html_of(db, cfg_ns(), WEEK_JAN)
    assert page.count("<td>CLOUD FY26-27</td>") == 7 and page.count("<td>CLOUD FY27-28</td>") == 4
    assert page.count("<td>LAN FY26-27</td>") == 7 and page.count("<td>LAN FY27-28</td>") == 3
    assert "<td>CLOUD</td>" not in page and "<td>LAN</td>" not in page
    base = fresh_db(tmp_path, "base")
    oct_week(base)
    plain = html_of(base, cfg_ns(), WEEK_OCT)
    assert "<td>CLOUD</td>" in plain and "<td>LAN</td>" in plain and "FY2" not in plain
    base.close()


def test_R17_csv_has_fy_column_only_when_split(db, tmp_path):
    jan_world(db)
    _, _, attachments = report.build_summary_email(db, "AAM", 7, "Weekly", legs=BOTH, report_period=WEEK_JAN,
                                                   schedule=cfg_ns().schedule, config=cfg_ns())
    rows = list(csv.reader(io.StringIO(attachments[0]["content"].decode("utf-8"))))
    assert rows[0][-1] == "Financial Year" and len(rows) == 1 + 21
    for line in rows[1:]:
        assert line[-1] == json.loads(line[10])["fy"]
    base = fresh_db(tmp_path, "base")
    oct_week(base)
    _, _, attachments = report.build_summary_email(base, "AAM", 7, "Weekly", legs=BOTH, report_period=WEEK_OCT,
                                                   schedule=cfg_ns().schedule, config=cfg_ns())
    header = next(csv.reader(io.StringIO(attachments[0]["content"].decode("utf-8"))))
    assert header[0] == "Started At" and len(header) == 11 and header[-1] == "Extended Metrics"
    base.close()


def test_R18_integrity_rows_per_fy(db):
    jan_world(db)
    add_audit(db, "u-cloud-full", "cloud", AUDIT_OK, at(2027, 1, 2, 3))
    add_audit(db, "u-cloud-full-FY27-28", "cloud", AUDIT_BAD, at(2027, 1, 3, 3))
    cfg = cfg_ns()
    model = build(db, cfg, WEEK_JAN)
    by = {(a["leg"], a["fy"]): a["status"] for a in model["audits"]}
    assert by == {("cloud", CFG_FY): AUDIT_OK, ("cloud", NXT): AUDIT_BAD,
                  ("lan", CFG_FY): "NOT VERIFIED", ("lan", NXT): "NOT VERIFIED"}
    assert model["headline"] == ("action needed: Online backup (Google Cloud) - FY27-28 - "
                                 "safety check found differences")
    page = html_of(db, cfg, WEEK_JAN)
    assert re.search(r'CLOUD FY26-27 Integrity</td><td style="[^"]*">VERIFIED', page)
    assert re.search(r'CLOUD FY27-28 Integrity</td><td style="[^"]*">VERIFICATION_FAILED', page)
    assert re.search(r'LAN FY27-28 Integrity</td><td style="[^"]*">NOT VERIFIED', page)


# ── the switch (R19-R24) ──────────────────────────────────────────────────

def test_R19_switch_week_labels_both_fys(db):
    switch_world(db)
    cfg = cfg_ns(NXT)
    rp = ReportPeriod(at(2027, 3, 28), at(2027, 4, 4), "28 Mar - 3 Apr 2027", "Weekly")
    model = build(db, cfg, rp)
    assert order(model) == [("cloud", CFG_FY), ("cloud", NXT), ("lan", CFG_FY), ("lan", NXT)]
    got = {(s["leg"], s["fy"]): (s["total"], s["scheduled"]) for s in model["summaries"]}
    assert got == {("cloud", CFG_FY): (5, 4), ("cloud", NXT): (7, 7), ("lan", CFG_FY): (6, 5), ("lan", NXT): (7, 7)}
    assert model["run_fy"]["x-final-cloud"] == CFG_FY and model["run_fy"]["x-final-lan"] == CFG_FY
    page = html_of(db, cfg, rp)
    assert ("FY26-27 was closed on 1 Apr 2027, 06:10 (switch to FY27-28); "
            "it is not backed up any more.") in page
    assert "CLOUD FY27-28 Integrity" in page and "LAN FY27-28 Integrity" in page
    assert "FY26-27 Integrity" not in page
    assert {(a["leg"], a["fy"]) for a in model["audits"]} == {("cloud", NXT), ("lan", NXT)}


def test_R20_split_is_the_same_before_and_after_switch(db, tmp_path):
    switch_world(db)
    after = build(db, cfg_ns(NXT), ReportPeriod(at(2027, 3, 28), at(2027, 4, 4), "x", "Weekly"))
    before_db = fresh_db(tmp_path, "before")
    switch_world(before_db, ended=False)
    rp = ReportPeriod(at(2027, 3, 28), at(2027, 4, 1), "28-31 Mar 2027", "Weekly")
    before = build(before_db, cfg_ns(CFG_FY), rp)
    assert before["run_fy"] and {k: v for k, v in after["run_fy"].items() if k in before["run_fy"]} == before["run_fy"]
    for leg in BOTH:
        assert sec(before, leg, CFG_FY)["total"] == 4 and sec(before, leg, NXT)["total"] == 4
    before_db.close()


def test_R21_blocked_window_ends_at_the_switch(db):
    activate(db, ended_at="2027-04-03T06:10:00+05:30")
    nightly(db, "cloud", (2027, 3, 29), (2027, 4, 2), CFG_FY)
    nightly(db, "lan", (2027, 3, 29), (2027, 4, 3), CFG_FY)
    nightly(db, "cloud", (2027, 3, 29), (2027, 4, 2), NXT, nxt=True)
    nightly(db, "lan", (2027, 3, 29), (2027, 4, 3), NXT, nxt=True)
    for d in (3, 4):
        add_pass(db, "cloud", at(2027, 4, d, 18), NXT, metrics="null")
    add_pass(db, "lan", at(2027, 4, 4, 1), NXT, metrics="null")
    for leg in BOTH:
        add_pass(db, leg, at(2027, 4, 1, 6), CFG_FY, FAIL[leg], metrics="null", rid=f"f1-final-{leg}")
        add_pass(db, leg, at(2027, 4, 2, 6), CFG_FY, FAIL[leg], metrics="null", rid=f"f2-final-{leg}")
        add_pass(db, leg, at(2027, 4, 3, 6), CFG_FY, OK[leg], metrics="null", rid=f"f3-final-{leg}")
    add_pass(db, "lan", at(2027, 4, 2, 14), CFG_FY, metrics="null", rid="manual-lan")
    model = build(db, cfg_ns(NXT), WEEK_SWITCH)
    assert sec(model, "cloud", CFG_FY)["scheduled"] == 5 and sec(model, "lan", CFG_FY)["scheduled"] == 6
    for leg in BOTH:
        problems = sec(model, leg, CFG_FY)["problems"]
        assert len(problems) == 2 and all(p["resolved"] for p in problems)
    assert model["run_fy"]["manual-lan"] == CFG_FY
    assert "no backup ran" not in model["headline"]


def test_R22_march_monthly_after_switch_keeps_true_fy(db, tmp_path):
    rp = ReportPeriod.previous_month(at(2027, 4, 1, 8, 30))
    blocked = fresh_db(tmp_path, "blocked")
    for target, ended in ((db, True), (blocked, False)):
        activate(target, **({"ended_at": SWITCH} if ended else {}))
        for d in range(1, 32):
            add_pass(target, "cloud", at(2027, 3, d, 18), CFG_FY, metrics="null")
            add_pass(target, "cloud", at(2027, 3, d, 18), NXT, nxt=True)
    shapes = []
    for target, cfg in ((db, cfg_ns(NXT)), (blocked, cfg_ns(CFG_FY))):
        model = build(target, cfg, rp, legs=("cloud",))
        old, new = sec(model, "cloud", CFG_FY), sec(model, "cloud", NXT)
        assert (old["ok"], old["total"]) == (31, 31) and (new["ok"], new["total"]) == (31, 31)
        shapes.append([(s["fy"], s["ok"], s["total"]) for s in model["summaries"]])
    assert shapes[0] == shapes[1]
    assert "was closed" not in html_of(blocked, cfg_ns(CFG_FY), rp, legs=("cloud",), name="Monthly")
    blocked.close()


def last_good_world(db):
    activate(db, ended_at=SWITCH)
    add_pass(db, "lan", at(2027, 3, 31, 1), CFG_FY, metrics="null")
    add_pass(db, "lan", at(2027, 4, 1, 1), NXT, nxt=True)
    add_pass(db, "lan", at(2027, 4, 1, 6), CFG_FY, metrics="null", rid="x-final-lan")


def test_R23_configured_last_good_after_switch(db):
    last_good_world(db)
    model = build(db, cfg_ns(NXT), WEEK_SWITCH, legs=("lan",))
    assert sec(model, "lan", NXT)["last_good"].startswith("1 Apr 2027, 1:00 AM")
    assert sec(model, "lan", CFG_FY)["last_good"].startswith("1 Apr 2027, 6:00 AM")


def test_R23_last_good_of_fy_baseline_equals_last_successful_run(db):
    add_pass(db, "lan", at(2027, 1, 2, 1), CFG_FY, metrics="null")
    add_pass(db, "lan", at(2027, 1, 3, 1), CFG_FY, LAN_FAIL, metrics="null")
    got = fy_overlap.last_good_of_fy(db, "lan", CFG_FY, CFG_FY, None)
    assert got == db.last_successful_run("lan")
    assert fy_overlap.last_good_of_fy(db, "cloud", CFG_FY, CFG_FY, None) is None


def test_R23_last_good_of_fy_after_switch(db):
    last_good_world(db)
    configured = fy_overlap.last_good_of_fy(db, "lan", NXT, NXT, SWITCH)
    assert configured["run_id"].endswith("-FY27-28") and configured["started_at"].startswith("2027-04-01T01:00")
    closed = fy_overlap.last_good_of_fy(db, "lan", CFG_FY, NXT, SWITCH)
    assert closed["run_id"] == "x-final-lan"


def test_R23_last_good_of_fy_next_fy_and_none(db):
    jan_world(db, lan_next=(2027, 1, 2))
    nxt = fy_overlap.last_good_of_fy(db, "lan", NXT, CFG_FY, None)
    assert nxt["run_id"].endswith("-FY27-28") and nxt["started_at"].startswith("2027-01-02T01:00")
    assert fy_overlap.last_good_of_fy(db, "lan", "FY28-29", CFG_FY, None) is None


def test_R24_configured_audit_after_switch_is_its_own(db):
    activate(db, ended_at=SWITCH)
    nightly(db, "cloud", (2027, 3, 29), (2027, 4, 3), CFG_FY)
    nightly(db, "cloud", (2027, 3, 29), (2027, 3, 31), NXT, nxt=True)
    add_audit(db, "a-cloud-full", "cloud", AUDIT_BAD, at(2027, 3, 28, 3))
    add_audit(db, "a-cloud-full-FY27-28", "cloud", AUDIT_OK, at(2027, 3, 28, 3))
    model = build(db, cfg_ns(NXT), WEEK_SWITCH, legs=("cloud",))
    assert [(a["fy"], a["status"]) for a in model["audits"]] == [(NXT, AUDIT_OK)]
    assert "safety check found differences" not in model["headline"]


# ── overlap off, bad records, disabled legs (R25-R30, R34) ────────────────

def test_R25_overlap_off_keeps_rows_apart_no_false_alarm(db):
    activate(db)
    for leg in BOTH:
        nightly(db, leg, (2026, 12, 28), (2027, 1, 3), CFG_FY)
    nightly(db, "cloud", (2026, 12, 31), (2027, 1, 1), NXT, nxt=True)
    nightly(db, "lan", (2027, 1, 1), (2027, 1, 1), NXT, nxt=True)
    model = build(db, cfg_ns(overlap=False), WEEK_JAN)
    cloud, lan = sec(model, "cloud", NXT), sec(model, "lan", NXT)
    assert cloud["total"] == 2 and cloud["scheduled"] == 0
    assert cloud["label"] == "Online backup (Google Cloud) - FY27-28 (not backed up - switched off in Settings)"
    assert lan["total"] == 1 and lan["scheduled"] == 0
    assert lan["label"] == "Office NAS backup - FY27-28 (not backed up - switched off in Settings)"
    assert sec(model, "cloud", CFG_FY)["total"] == 7 and sec(model, "lan", CFG_FY)["total"] == 7
    assert model["act"] is False


def test_R26_overlap_off_no_rows_is_baseline(db):
    activate(db)
    for leg in BOTH:
        nightly(db, leg, (2027, 1, 11), (2027, 1, 17), CFG_FY)
    assert_baseline(db, cfg_ns(overlap=False), ReportPeriod.previous_week(at(2027, 1, 18, 8)))


def test_R27_unreadable_activation_fails_loud(db):
    db.set_overlap_record(NXT, {"activated_at": "garbage", "from_fy": CFG_FY})
    for leg in BOTH:
        nightly(db, leg, (2026, 12, 28), (2027, 1, 3), CFG_FY)
    model = build(db, cfg_ns(), WEEK_JAN)
    assert "scheduled" not in sec(model, "cloud", NXT)
    assert model["headline"] == "action needed: Online backup (Google Cloud) - FY27-28 - no backup ran"
    assert model["act"] is True


def test_R28_record_read_error_is_reported(db):
    jan_world(db)
    cfg = cfg_ns()
    with patch.object(db, "overlap_record", side_effect=RuntimeError("boom")):
        model = build(db, cfg, WEEK_JAN)
        page = html_of(db, cfg, WEEK_JAN)
    assert sec(model, "cloud", NXT)["total"] == 4 and sec(model, "lan", NXT)["total"] == 3
    assert sec(model, "cloud", CFG_FY)["total"] == 7
    assert "scheduled" not in sec(model, "cloud", NXT) and "scheduled" not in sec(model, "lan", NXT)
    assert ("The financial-year state could not be read; the next financial year may not be backed up. "
            "Open the dashboard.") in page
    assert model["act"] is True


def test_R29_cloud_only_sections(db):
    jan_world(db, legs=("cloud",))
    cfg = cfg_ns(lan=False)
    model = build(db, cfg, WEEK_JAN, legs=("cloud",))
    assert order(model) == [("cloud", CFG_FY), ("cloud", NXT)]
    assert set(model["per_leg"]) == {"cloud:FY26-27", "cloud:FY27-28"}
    assert {a["leg"] for a in model["audits"]} == {"cloud"}
    page = html_of(db, cfg, WEEK_JAN, legs=("cloud",))
    assert "NAS" not in page and "LAN" not in page


def test_R30_lan_only_sections(db):
    jan_world(db, legs=("lan",))
    cfg = cfg_ns(cloud=False)
    model = build(db, cfg, WEEK_JAN, legs=("lan",))
    assert order(model) == [("lan", CFG_FY), ("lan", NXT)]
    assert {a["leg"] for a in model["audits"]} == {"lan"}


def test_R34_disabled_leg_no_section_any_fy(db):
    jan_world(db)                                  # LAN rows of both FYs are in the database
    model = build(db, cfg_ns(lan=False), WEEK_JAN, legs=("cloud",))
    assert order(model) == [("cloud", CFG_FY), ("cloud", NXT)]
    assert [r for r in model["runs"] if r["mode"] == "lan"] == []
    assert not [k for k in model["per_leg"] if k.startswith("lan")]


# ── manual-only legs (R31-R33) ────────────────────────────────────────────

MANUAL_DETAIL = ("No backup was scheduled in this period - this backup runs only when started "
                 "from the dashboard.")


def test_R31_manual_only_leg_is_not_missing(db):
    oct_week(db, lan=False)
    cfg = cfg_ns(lan_sched=False)
    model = build(db, cfg, WEEK_OCT)
    lan = sec(model, "lan")
    assert lan["scheduled"] == 0 and lan["label"] == "Office NAS backup (run by hand)"
    assert model["headline"] == "all backups OK"
    assert MANUAL_DETAIL in html_of(db, cfg, WEEK_OCT)


def test_R32_manual_only_failure_still_needs_action(db):
    oct_week(db, lan=False)
    add_pass(db, "lan", at(2026, 10, 15, 14), CFG_FY, LAN_FAIL, metrics="null")
    model = build(db, cfg_ns(lan_sched=False), WEEK_OCT)
    problems = sec(model, "lan")["problems"]
    assert len(problems) == 1 and problems[0]["resolved"] is False
    assert model["act"] is True and model["headline"].startswith("action needed")


def test_R33_manual_only_leg_both_fys(db):
    jan_world(db, lan_next=None)
    model = build(db, cfg_ns(lan_sched=False), WEEK_JAN)
    for fy in (CFG_FY, NXT):
        lan = sec(model, "lan", fy)
        assert "(run by hand)" in lan["label"] and fy in lan["label"] and lan["scheduled"] == 0
    assert sec(model, "cloud", NXT)["scheduled"] == 4
    assert "no backup ran" not in model["headline"]
    assert "no backup ran" not in html_of(db, cfg_ns(lan_sched=False), WEEK_JAN).lower()


# ── windows, subject, odd rows, leap year (R35-R39) ───────────────────────

def test_R35_activation_on_period_boundaries(db, tmp_path):
    def world(name, activated):
        target = fresh_db(tmp_path, name)
        for leg in BOTH:
            nightly(target, leg, (2026, 12, 28), (2027, 1, 3), CFG_FY)
        activate(target, activated)
        return target

    at_end = world("end", iso(WEEK_JAN.end))
    model = build(at_end, cfg_ns(), WEEK_JAN)
    assert NXT not in [s.get("fy") for s in model["summaries"]]
    at_start = world("start", iso(WEEK_JAN.start))
    model = build(at_start, cfg_ns(), WEEK_JAN)
    assert sec(model, "cloud", NXT)["scheduled"] == 7 and sec(model, "lan", NXT)["scheduled"] == 7
    late = world("late", iso(WEEK_JAN.end.subtract(seconds=1)))
    model = build(late, cfg_ns(), WEEK_JAN)
    assert sec(model, "cloud", NXT)["scheduled"] == 0 and sec(model, "lan", NXT)["scheduled"] == 0
    for target in (at_end, at_start, late):
        target.close()


def test_R36_dashboard_rolling_window_counts_from_activation(db):
    jan_world(db)
    rp = ReportPeriod.last_days(7, at(2027, 1, 2, 10))
    model = build(db, cfg_ns(), rp)
    assert sec(model, "cloud", NXT)["scheduled"] == 2
    assert sec(model, "lan", NXT)["scheduled"] == 2


def test_R37_subject_names_the_fy(db):
    jan_world(db, lan_next=None)
    cfg = cfg_ns()
    subject, _, _ = report.build_summary_email(db, "AAM", 7, "Weekly", legs=BOTH, report_period=WEEK_JAN,
                                               schedule=cfg.schedule, config=cfg)
    assert subject.endswith("- action needed: Office NAS backup - FY27-28 - no backup ran")


def test_R38_next_fy_missed_and_running(db):
    jan_world(db, cloud_next=(2027, 1, 3),
              over={("cloud", NXT, (1, 2)): CLOUD_MISS, ("cloud", NXT, (1, 3)): CLOUD_RUN})
    model = build(db, cfg_ns(), WEEK_JAN, legs=("cloud",))
    nxt = sec(model, "cloud", NXT)
    assert nxt["total"] == 3 and nxt["ok"] == 2
    assert len(nxt["problems"]) == 1 and nxt["problems"][0]["resolved"] is False
    assert sec(model, "cloud", CFG_FY)["problems"] == []


def test_R39_february_2028_has_29_scheduled(db):
    activate(db, "2027-12-31T06:00:00+05:30", fy="FY28-29")
    nightly(db, "cloud", (2028, 2, 1), (2028, 2, 29), "FY27-28")
    nightly(db, "cloud", (2028, 2, 1), (2028, 2, 29), "FY28-29", nxt=True)
    rp = ReportPeriod.previous_month(at(2028, 3, 1, 8, 30))
    model = build(db, cfg_ns("FY27-28"), rp, legs=("cloud",))
    for fy in ("FY27-28", "FY28-29"):
        s = sec(model, "cloud", fy)
        assert (s["ok"], s["total"], s["scheduled"]) == (29, 29, 29)


# ── alert mails: "Is my data safe?" (R40-R43) ─────────────────────────────

NOW_MAIL = at(2027, 1, 4, 9)


def test_R40_data_safety_one_line_per_fy(db):
    activate(db)
    add_pass(db, "lan", at(2027, 1, 3, 1), CFG_FY)
    add_pass(db, "cloud", at(2027, 1, 3, 18), CFG_FY)
    add_pass(db, "cloud", at(2027, 1, 3, 18), NXT, nxt=True)
    lines = email_compose._data_safety(cfg_ns(), db, NOW_MAIL)
    assert len(lines) == 5 and lines[0] == SERVER_LINE
    assert lines[1].startswith("Office NAS backup (FY26-27): last good copy 3 Jan 2027, 1:00 AM")
    assert lines[2] == "Office NAS backup (FY27-28): no good copy recorded yet."
    assert lines[3].startswith("Online backup (Google Cloud) (FY26-27): last good copy 3 Jan 2027, 6:00 PM")
    assert lines[4].startswith("Online backup (Google Cloud) (FY27-28): last good copy 3 Jan 2027, 6:00 PM")


def test_R41_data_safety_baseline_unchanged(db):
    add_pass(db, "lan", at(2027, 1, 3, 1), CFG_FY, metrics="null")
    lines = email_compose._data_safety(cfg_ns(), db, at(2027, 1, 3, 9))
    assert lines == [SERVER_LINE,
                     "Office NAS backup: last good copy 3 Jan 2027, 1:00 AM IST (8 hours old).",
                     "Online backup (Google Cloud): no good copy recorded yet."]


def test_R42_data_safety_after_switch_uses_true_fy(db):
    last_good_world(db)
    lines = email_compose._data_safety(cfg_ns(NXT, cloud=False), db, at(2027, 4, 2, 9))
    lan = [ln for ln in lines if ln.startswith("Office NAS backup")]
    assert len(lan) == 1 and "1 Apr 2027, 1:00 AM" in lan[0] and "FY2" not in lan[0]


def test_R43_data_safety_record_error_line(db):
    add_pass(db, "lan", at(2027, 1, 3, 1), CFG_FY, metrics="null")
    with patch.object(db, "overlap_record", side_effect=RuntimeError("boom")):
        lines = email_compose._data_safety(cfg_ns(cloud=False), db, at(2027, 1, 3, 9))
    assert lines == [SERVER_LINE,
                     "Office NAS backup: last good copy 3 Jan 2027, 1:00 AM IST (8 hours old).",
                     "The next financial year's backup state could not be read - it may not be backed up."]


# ── plumbing (R44) ────────────────────────────────────────────────────────

def test_R44_report_callers_pass_the_config(tmp_path, db, monkeypatch):
    import flow
    from test_support import ui_login
    from test_support.smtp_server import smtp_server
    from tests.test_m5_dashboard_audit import _client
    from models.config import AppConfig

    def real_cfg(port):
        return AppConfig(
            firm_name="AAM",
            paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": "\\\\nas\\share\\FY26-27",
                   "runtime_dir": str(tmp_path / "rt")},
            wol={"enabled": False}, cloud={"enabled": False},
            dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
            notifications={"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@firm.example",
                           "recipients": ["o@firm.example"], "smtp_security": "none", "smtp_auth": False,
                           "retry_attempts": 1, "retry_base_seconds": 0.01})

    seen = {}

    def capture(name, result):
        def fake(*args, **kwargs):
            seen.setdefault(name, []).append(kwargs)
            return result
        return fake

    with smtp_server() as srv:
        cfg = real_cfg(srv.server_address[1])
        with patch.object(flow.report, "build_summary_email",
                          side_effect=capture("flow", ("Weekly report", "<p>x</p>", []))):
            flow._send_summary_report(cfg, 7, "Weekly")
    assert seen["flow"][0].get("config") is cfg

    cfg = real_cfg(25)
    Path(cfg.paths.database_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.paths.database_path).touch()
    client = _client(monkeypatch, cfg, db)
    monkeypatch.setattr(report, "generate_report_html", capture("html", "<p>x</p>"))
    monkeypatch.setattr(report, "send_weekly_report", capture("weekly", True))
    monkeypatch.setattr(report, "send_monthly_report", capture("monthly", True))
    assert client.post("/trigger/report/weekly/email").status_code == 200
    assert client.post("/trigger/report/monthly/email").status_code == 200
    assert client.get("/report/weekly").status_code == 200
    assert len(seen["html"]) == 3 and len(seen["weekly"]) == 1 and len(seen["monthly"]) == 1
    for name in ("html", "weekly", "monthly"):
        for kwargs in seen[name]:
            assert kwargs.get("config") is cfg, name
