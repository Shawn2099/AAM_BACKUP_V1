r"""Red-first tests: day overrides (cloud_down / clock_unproven) wired end to end, X01 judged by notification kind.

Origin: docs/LIVEBOX_X01_DIAGNOSIS_2026-11-24.md (the real X01 run had 22 FAIL rows, almost all because the
calendar did not know that the cloud was blackholed and the clock proof unproven on 2090-12-31 / 2091-01-01).

PINNED API (the code agent must implement exactly this; names are the contract)

calendar.py
  DayOverride(skip, statuses, extra_mails, note)  GAINS two bool fields, both default False:
      cloud_down       cloud unreachable all day
      clock_unproven   clock proof missing, so the activation / switch must wait
  overrides_map(x) -> dict[dt.date, DayOverride]
      x is a Mapping (key dt.date or ISO "YYYY-MM-DD" str -> DayOverride) OR an iterable of
      (date_or_iso, DayOverride) pairs OR None/() -> {}.
  ACT_WAITING_CLOCK                      activation_result on a clock_unproven day when the activation was due
  MAIL_ACTIVATION_WAITING = "activation_waiting"
  MAIL_RESOLVED = "resolved"
  ExpectedMail GAINS `severity: str | None = None` (the notifications.kind the product must store:
      "WARNING" | "ACTION_REQUIRED" | ...).
  Day GAINS `stale_ok: tuple[str, ...] = ()` : legs the watchdog may report overdue that day.
  build_calendar(..., overrides=...) semantics, per day with cloud_down and/or clock_unproven (activation due):
      - cloud rows: statuses == ("CLOUD_FAILED",), configured pass only; LAN and audits unchanged
      - no activation: activation_result == ACT_WAITING_CLOCK, next_fy_end None, is_activation_day False,
        overlap_active_end False, cards 2 / 2, no rows with pass_kind "next"
      - cloud_down day: day.stale_ok == ("cloud",); one MAIL_PROBLEM (count 1; the cloud failure mail)
      - activation-due day with clock_unproven: ONE ExpectedMail(MAIL_ACTIVATION_WAITING, 1,
        key "rollover:activation:<next fy>") with severity "WARNING" on the first waiting day and
        "ACTION_REQUIRED" on every later waiting day (product spec: ACTION NEEDED from the next day)
      - the first later day WITHOUT an override: activation happens at its 06:00 check as normal
        (ACT_ACTIVATED, activated mail), cloud 02:01 that day has only the configured pass (the check is
        after it), and ONE ExpectedMail(MAIL_RESOLVED, 1) (cloud recovery mail) is expected that day
  Days without any override are byte-identical to before (X02..X19 and every other lane).

scenarios/base.py       Scenario GAINS `overrides: tuple = ()`  (pairs or mapping, see overrides_map).
scenarios/results.py    to_driver_scenario carries `overrides` into run_lane.Scenario.overrides.
run_lane.py
  Scenario GAINS `overrides: tuple = ()`.
  build_days passes overrides_map(cat.overrides) as `overrides=` to cal.build_calendar.
  override_injection_dates(overrides) -> set[dt.date]   dates with cloud_down or clock_unproven; the
        LaneRunner must union them into its injection_dates (M2 mail rule storm check).
  unexpected_stale_legs(stale, day) -> list[str]   stale legs not in day.stale_ok (WATCHDOG verdict FAIL only
        when this list is not empty).
  apply_known_findings(day, checks) -> checks (in place)  a FAIL check whose observed text names a key of
        an expected ExpectedMail with severity "ACTION_REQUIRED" (and the text says sent 0) becomes
        verdict "FINDING" with finding "F-P1-ESCALATION-SUPPRESSED"; every other check untouched.
oracles.check_expected_mails  counts the day's MAIL_ACTIVATION_WAITING mails as expected problem-severity
        mails (their kind WARNING / ACTION_REQUIRED is in PROBLEM_SEVERITIES) next to MAIL_PROBLEM.
scenarios/w1.py  X01: overrides for 2090-12-31 and 2091-01-01 (cloud_down + clock_unproven; the blackout
        really lasts until 2 Jan), expected_red == "F-P1-ESCALATION-SUPPRESSED"; _x01_expect judges WARNING /
        ACTION_REQUIRED by the notifications `kind` (falling back to `severity` when a mail has no kind).
scenarios/results.py  mails keep BOTH `kind` (notifications) and `severity` (subject word).

Note: tools/livebox/tests/test_scenarios_w1.py::KNOWN_RED must learn "F-P1-ESCALATION-SUPPRESSED"
(that file is not edited here).
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.livebox import calendar as cal
from tools.livebox import oracles

D = dt.date
KEY = "rollover:activation:FY91-92"
FINDING_NAME = "F-P1-ESCALATION-SUPPRESSED"
WORKTREE = Path(__file__).resolve().parents[3]
EVIDENCE = WORKTREE / "agent_runs" / "livebox_dev54" / "w1_x01"


def x01():
    from tools.livebox.scenarios import w1
    return {s.id: s for s in w1.SCENARIOS}["X01"]


def blackout():
    return {D(2090, 12, 31): cal.DayOverride(cloud_down=True, clock_unproven=True),
            D(2091, 1, 1): cal.DayOverride(cloud_down=True, clock_unproven=True)}


def days_x01():
    return cal.build_calendar(D(2090, 12, 29), 6, overrides=blackout())


def by_date(days):
    return {d.date: d for d in days}


# ================================================================ calendar: DayOverride fields
def test_day_override_new_fields_default_false():
    o = cal.DayOverride()
    assert o.cloud_down is False and o.clock_unproven is False
    o2 = cal.DayOverride(cloud_down=True, clock_unproven=True)
    assert o2.cloud_down and o2.clock_unproven


def test_overrides_map_accepts_mapping_pairs_iso_and_none():
    o = cal.DayOverride(cloud_down=True)
    assert cal.overrides_map(None) == {}
    assert cal.overrides_map(()) == {}
    assert cal.overrides_map({D(2090, 12, 31): o}) == {D(2090, 12, 31): o}
    assert cal.overrides_map({"2090-12-31": o}) == {D(2090, 12, 31): o}
    assert cal.overrides_map((("2090-12-31", o), (D(2091, 1, 1), o))) == {D(2090, 12, 31): o, D(2091, 1, 1): o}


def test_new_constants_exist():
    assert cal.MAIL_ACTIVATION_WAITING == "activation_waiting"
    assert cal.MAIL_RESOLVED == "resolved"
    assert cal.ACT_WAITING_CLOCK not in (cal.ACT_ACTIVATED, cal.ACT_NOT_DUE, cal.ACT_ALREADY)
    assert cal.ExpectedMail("x", 1, severity="WARNING").severity == "WARNING"
    assert cal.ExpectedMail("x", 1).severity is None


# ================================================================ calendar: blackout across the activation date
@pytest.mark.parametrize("date", [D(2090, 12, 31), D(2091, 1, 1)])
def test_blackout_day_cloud_failed_and_no_activation(date):
    d = by_date(days_x01())[date]
    cloud = d.rows_of(cal.CLOUD)
    assert [(r.fy, r.pass_kind, r.statuses) for r in cloud] == [("FY90-91", "single", ("CLOUD_FAILED",))]
    assert d.activation_result == cal.ACT_WAITING_CLOCK
    assert d.activation_result != cal.ACT_ACTIVATED
    assert d.is_activation_day is False
    assert d.next_fy_end is None
    assert d.overlap_active_end is False and d.overlap_active_at_0900 is False
    assert d.cards_0900 == 2 and d.cards_2330 == 2
    assert not [r for r in d.rows if r.pass_kind == "next"]
    assert [r.statuses for r in d.rows_of(cal.LAN)] == [cal.LAN_OK]
    assert d.mail_count(cal.MAIL_ACTIVATED) == 0
    assert d.stale_ok == ("cloud",)


def test_blackout_audit_rows_are_configured_fy_only():
    d = by_date(days_x01())[D(2090, 12, 31)]          # a Sunday
    assert d.is_sunday
    assert {r.fy for r in d.rows if r.leg.startswith("audit_")} == {"FY90-91"}


def test_blackout_mails_waiting_warning_then_action_then_problem_counts():
    ds = by_date(days_x01())
    d1, d2 = ds[D(2090, 12, 31)], ds[D(2091, 1, 1)]
    w1_ = [m for m in d1.mails if m.mail_class == cal.MAIL_ACTIVATION_WAITING]
    w2_ = [m for m in d2.mails if m.mail_class == cal.MAIL_ACTIVATION_WAITING]
    assert [(m.key, m.count, m.severity) for m in w1_] == [(KEY, 1, "WARNING")]
    assert [(m.key, m.count, m.severity) for m in w2_] == [(KEY, 1, "ACTION_REQUIRED")]
    # 12-31 is a Sunday with the cloud down: the audit mail (AAM-AUD-02) adds a 2nd problem mail
    assert d1.mail_count(cal.MAIL_PROBLEM) == 2 and d2.mail_count(cal.MAIL_PROBLEM) == 1


def test_recovery_day_activates_at_next_daily_check_with_resolved_mail():
    d = by_date(days_x01())[D(2091, 1, 2)]
    assert d.activation_result == cal.ACT_ACTIVATED and d.is_activation_day
    assert d.next_fy_end == "FY91-92" and d.overlap_active_end
    assert d.mail_count(cal.MAIL_ACTIVATED) == 1
    assert [m.key for m in d.mails if m.mail_class == cal.MAIL_ACTIVATED] == ["rollover:activated:FY91-92"]
    assert d.mail_count(cal.MAIL_RESOLVED) == 1
    assert d.mail_count(cal.MAIL_PROBLEM) == 0 and d.mail_count(cal.MAIL_ACTIVATION_WAITING) == 0
    # the 02:01 cloud run is before the 06:00 activation: configured pass only; the LAN run has both passes
    assert [(r.fy, r.statuses) for r in d.rows_of(cal.CLOUD)] == [("FY90-91", cal.CLOUD_OK)]
    assert [r.fy for r in d.rows_of(cal.LAN)] == ["FY90-91", "FY91-92"]
    assert d.stale_ok == ()
    assert by_date(days_x01())[D(2091, 1, 3)].activation_result == cal.ACT_ALREADY


def test_days_before_blackout_unchanged():
    ds = by_date(days_x01())
    for date in (D(2090, 12, 29), D(2090, 12, 30)):
        assert ds[date].activation_result == cal.ACT_NOT_DUE
        assert ds[date].stale_ok == () and ds[date].mail_count(cal.MAIL_PROBLEM) == 0


def test_clock_unproven_alone_blocks_activation():
    ov = {D(2090, 12, 31): cal.DayOverride(clock_unproven=True)}
    ds = by_date(cal.build_calendar(D(2090, 12, 29), 4, overrides=ov))
    d = ds[D(2090, 12, 31)]
    assert d.activation_result == cal.ACT_WAITING_CLOCK and d.next_fy_end is None
    assert d.rows_of(cal.CLOUD)[0].statuses == cal.CLOUD_OK          # cloud itself is fine
    assert ds[D(2091, 1, 1)].activation_result == cal.ACT_ACTIVATED


def test_cloud_down_alone_fails_cloud_row_and_goes_stale():
    ov = {D(2090, 12, 30): cal.DayOverride(cloud_down=True)}
    d = by_date(cal.build_calendar(D(2090, 12, 29), 3, overrides=ov))[D(2090, 12, 30)]
    assert d.rows_of(cal.CLOUD)[0].statuses == ("CLOUD_FAILED",)
    assert d.stale_ok == ("cloud",)


# ================================================================ calendar: no overrides -> unchanged (X02..X19)
def test_no_override_calendar_identical_with_empty_or_inert_overrides():
    base = cal.build_calendar(D(2091, 3, 29), 8)
    assert cal.build_calendar(D(2091, 3, 29), 8, overrides={}) == base
    assert cal.build_calendar(D(2091, 3, 29), 8, overrides={D(2091, 3, 30): cal.DayOverride()}) == base
    for d in base:
        assert d.stale_ok == ()
        assert d.mail_count(cal.MAIL_ACTIVATION_WAITING) == 0 and d.mail_count(cal.MAIL_RESOLVED) == 0


def test_healthy_activation_unchanged():
    d = by_date(cal.build_calendar(D(2090, 12, 29), 5))[D(2090, 12, 31)]
    assert d.activation_result == cal.ACT_ACTIVATED and d.is_activation_day
    assert d.rows_of(cal.CLOUD)[0].statuses == cal.CLOUD_OK
    assert d.mail_count(cal.MAIL_ACTIVATED) == 1 and d.mail_count(cal.MAIL_PROBLEM) == 0
    assert d.stale_ok == ()


def test_existing_statuses_override_still_works():
    ov = {D(2090, 12, 30): cal.DayOverride(statuses={"cloud": ("CLOUD_FAILED",)})}
    d = by_date(cal.build_calendar(D(2090, 12, 29), 3, overrides=ov))[D(2090, 12, 30)]
    assert d.rows_of(cal.CLOUD)[0].statuses == ("CLOUD_FAILED",)


# ================================================================ Scenario plumbing
def test_base_scenario_has_overrides_default_empty():
    from tools.livebox.scenarios.base import Scenario
    s = Scenario(id="Z", wave=1, title="t", start="2090-12-29", days=1, lane="l", setup=lambda r: None,
                 on_day=None, expect=lambda r: [])
    assert s.overrides == ()
    s2 = Scenario(id="Z", wave=1, title="t", start="2090-12-29", days=1, lane="l", setup=lambda r: None,
                  on_day=None, expect=lambda r: [], overrides=(("2090-12-31", cal.DayOverride(cloud_down=True)),))
    assert cal.overrides_map(s2.overrides)[D(2090, 12, 31)].cloud_down


def test_x01_carries_blackout_overrides_and_expected_red():
    sc = x01()
    m = cal.overrides_map(sc.overrides)
    assert set(m) == {D(2090, 12, 31), D(2091, 1, 1)}
    assert all(o.cloud_down and o.clock_unproven for o in m.values())
    assert sc.expected_red == FINDING_NAME


def test_other_wave1_scenarios_have_no_overrides():
    from tools.livebox.scenarios import w1
    for s in w1.SCENARIOS:
        if s.id not in ("X01", "X02", "X03", "X04"):
            assert cal.overrides_map(s.overrides) == {}, s.id


def test_to_driver_scenario_carries_overrides():
    from tools.livebox.scenarios import results
    drv = results.to_driver_scenario(x01())
    assert cal.overrides_map(drv.overrides) == cal.overrides_map(x01().overrides)


def test_to_driver_scenario_without_overrides_is_empty():
    from tools.livebox.scenarios import w1, results
    sc = {s.id: s for s in w1.SCENARIOS}["X05"]
    assert cal.overrides_map(results.to_driver_scenario(sc).overrides) == {}


def _ns(**kw):
    base = dict(outage=[], no_overlap=False, lan_only=False, cloud_only=False, scenario=None,
                start=None, days=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_build_days_passes_overrides_to_calendar():
    from tools.livebox import run_lane
    days = run_lane.build_days(_ns(scenario_obj=x01()))
    d = by_date(days)[D(2090, 12, 31)]
    assert d.activation_result == cal.ACT_WAITING_CLOCK
    assert d.rows_of(cal.CLOUD)[0].statuses == ("CLOUD_FAILED",)
    assert by_date(days)[D(2091, 1, 2)].is_activation_day


def test_build_days_without_overrides_is_the_plain_calendar():
    from tools.livebox import run_lane
    from tools.livebox.scenarios import w1
    sc = {s.id: s for s in w1.SCENARIOS}["X05"]
    got = run_lane.build_days(_ns(scenario_obj=sc))
    want = cal.build_calendar(D.fromisoformat(sc.start), sc.days, outages=list(sc.outages or ()))
    assert got == want


def test_run_lane_scenario_dataclass_has_overrides():
    from tools.livebox import run_lane
    assert run_lane.Scenario().overrides == ()


# ================================================================ driver helpers (pure)
def test_override_injection_dates():
    from tools.livebox import run_lane
    ov = {D(2090, 12, 31): cal.DayOverride(cloud_down=True), D(2091, 1, 1): cal.DayOverride(clock_unproven=True),
          D(2091, 1, 5): cal.DayOverride(skip=frozenset({"lan"}))}
    assert run_lane.override_injection_dates(ov) == {D(2090, 12, 31), D(2091, 1, 1)}
    assert run_lane.override_injection_dates(None) == set()
    assert run_lane.override_injection_dates((("2090-12-31", cal.DayOverride(cloud_down=True)),)) == {D(2090, 12, 31)}


def test_unexpected_stale_legs():
    from tools.livebox import run_lane
    ds = by_date(days_x01())
    assert run_lane.unexpected_stale_legs(["cloud"], ds[D(2090, 12, 31)]) == []
    assert run_lane.unexpected_stale_legs(["cloud", "lan"], ds[D(2090, 12, 31)]) == ["lan"]
    assert run_lane.unexpected_stale_legs(["cloud"], ds[D(2090, 12, 30)]) == ["cloud"]     # healthy day: still FAIL
    assert run_lane.unexpected_stale_legs([], ds[D(2090, 12, 30)]) == []


def _mail_fail(date, key):
    return {"check_id": "MAIL:MAIL", "verdict": "FAIL", "expected": "keyed mails exactly as expected",
            "observed": f"{date}: key {key} sent 0, expected 1", "evidence": []}


def test_apply_known_findings_marks_suppressed_escalation_only():
    from tools.livebox import run_lane
    day = by_date(days_x01())[D(2091, 1, 1)]
    other = _mail_fail("2091-01-01", "rollover:activated:FY91-92")
    esc = _mail_fail("2091-01-01", KEY)
    rows = {"check_id": "ROWS:ROWS", "verdict": "FAIL", "expected": "x", "observed": "2091-01-01: sent 0", "evidence": []}
    checks = [esc, other, rows]
    out = run_lane.apply_known_findings(day, checks)
    assert esc["verdict"] == "FINDING" and esc["finding"] == FINDING_NAME
    assert other["verdict"] == "FAIL" and "finding" not in other
    assert rows["verdict"] == "FAIL"
    assert out is checks or out == checks


def test_apply_known_findings_does_not_hide_missing_first_warning():
    from tools.livebox import run_lane
    day = by_date(days_x01())[D(2090, 12, 31)]               # expected severity there is WARNING, not the escalation
    c = _mail_fail("2090-12-31", KEY)
    run_lane.apply_known_findings(day, [c])
    assert c["verdict"] == "FAIL"


def test_apply_known_findings_noop_on_healthy_day():
    from tools.livebox import run_lane
    day = by_date(cal.build_calendar(D(2090, 12, 29), 3))[D(2090, 12, 30)]
    c = _mail_fail("2090-12-30", KEY)
    run_lane.apply_known_findings(day, [c])
    assert c["verdict"] == "FAIL"


# ================================================================ X01.expect judged by kind (synthetic)
def _r(mails, activated_on=("2091-01-02",), rows=()):
    return SimpleNamespace(rows=list(rows), mails=mails, config_fy_by_day={}, activated_on=list(activated_on),
                           switched_on=[], clock_proven_by_day={}, created_paths=[], junction_target_entries=[],
                           approvals=[])


def _m(day, severity, kind, key, code=""):
    return {"day": day, "severity": severity, "kind": kind, "key": key, "code": code}


def test_x01_expect_uses_kind_not_subject_severity():
    # ROL-07 subject says "ACTION NEEDED" (severity ACTION_REQUIRED) but the stored kind is WARNING
    good = _r([_m("2090-12-31", "ACTION_REQUIRED", "WARNING", KEY),
               _m("2091-01-01", "ACTION_REQUIRED", "ACTION_REQUIRED", KEY),
               _m("2091-01-02", "INFO", "INFO", "rollover:activated:FY91-92")])
    assert x01().expect(good) == []


def test_x01_expect_reports_suppressed_escalation_by_kind():
    r = _r([_m("2090-12-31", "ACTION_REQUIRED", "WARNING", KEY),
            _m("2091-01-02", "INFO", "INFO", "rollover:activated:FY91-92")])
    out = x01().expect(r)
    assert len(out) == 1
    assert "ACTION_REQUIRED" in out[0] and KEY in out[0] and "observed 0" in out[0]


def test_x01_expect_two_warnings_by_kind_flagged():
    r = _r([_m("2090-12-31", "ACTION_REQUIRED", "WARNING", KEY),
            _m("2091-01-01", "ACTION_REQUIRED", "WARNING", KEY),
            _m("2091-01-02", "INFO", "INFO", "rollover:activated:FY91-92")])
    assert any("WARNING" in x for x in x01().expect(r))


def test_x01_expect_falls_back_to_severity_without_kind():
    r = _r([{"day": "2090-12-31", "severity": "WARNING", "code": "AAM-ROL-07", "key": KEY},
            {"day": "2091-01-01", "severity": "ACTION_REQUIRED", "code": "AAM-ROL-07", "key": KEY},
            {"day": "2091-01-02", "severity": "INFO", "code": "", "key": "rollover:activated:FY91-92"}])
    assert x01().expect(r) == []


def test_x01_expect_counts_failure_mails_by_kind():
    base = [_m("2090-12-31", "ACTION_REQUIRED", "WARNING", KEY),
            _m("2091-01-01", "ACTION_REQUIRED", "ACTION_REQUIRED", KEY),
            _m("2091-01-02", "INFO", "INFO", "rollover:activated:FY91-92")]
    fails = [_m("2090-12-31", "PROBLEM", "FAILURE", "backup:cloud:CLOUD_FAILED"),
             _m("2091-01-01", "PROBLEM", "FAILURE", "backup:cloud:CLOUD_FAILED"),
             _m("2091-01-01", "PROBLEM", "FAILURE", "backup:cloud:CLOUD_FAILED")]
    assert any("FAILURE" in x for x in x01().expect(_r(base + fails)))
    assert x01().expect(_r(base + fails[:2])) == []


# ================================================================ results._mails exposes kind and severity
def test_results_mails_expose_kind_and_subject_severity(tmp_path):
    from tools.livebox.scenarios import results
    csv = tmp_path / "mail_index.csv"
    subj = "[LIVEBOX Backup] ACTION NEEDED: Financial-year switch - not added yet (AAM-ROL-07)"
    csv.write_text("sim_time,real_time,rcpts,subject,from,x_headers\n"
                   f"2090-12-31T06:00:00+05:30,2090-12-31T00:30:01+00:00,ops@x.invalid,{subj},l@x.invalid,\n",
                   encoding="utf-8")
    con = sqlite3.connect(tmp_path / "manifest.db")
    con.execute("create table notifications (id integer primary key, event_key text, kind text, status text, "
                "subject text, mode text, fault_code text, audience text, created_at text)")
    con.execute("insert into notifications values (1, ?, 'WARNING', 'SENT', ?, 'cloud', 'AAM-ROL-07', 'ops', "
                "'2090-12-31T06:00:01+05:30')", (KEY, subj))
    con.commit()
    con.close()
    (m,) = results.build_results(tmp_path).mails
    assert m["severity"] == "ACTION_REQUIRED"
    assert m["kind"] == "WARNING"
    assert m["key"] == KEY


# ================================================================ real X01 evidence re-judged (dev PC only)
needs_evidence = pytest.mark.skipif(not (EVIDENCE / "manifest.db").is_file(), reason="saved X01 evidence absent")


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("x01db")
    for n in ("manifest.db", "manifest.db-wal", "manifest.db-shm"):
        if (EVIDENCE / n).exists():
            shutil.copy(EVIDENCE / n, tmp / n)
    conn = oracles.open_ro(tmp / "manifest.db")
    mails = oracles.load_mail_index(EVIDENCE / "mail_index.csv")
    mails = oracles.join_notifications(mails, oracles.notification_rows(conn))
    for m in mails:                                   # same as run_lane.LaneRunner._mails
        m["severity"] = (m.get("kind") or "").upper()
    yield SimpleNamespace(conn=conn, mails=mails, days=by_date(days_x01()))
    conn.close()


@needs_evidence
def test_real_x01_rows_all_six_days_match_new_expectations(real):
    for date, day in sorted(real.days.items()):
        v = oracles.check_run_rows(real.conn, day)
        assert v == [], f"{date}: {[str(x) for x in v]}"


@needs_evidence
def test_real_x01_overlap_meta_matches(real):
    for date, day in sorted(real.days.items()):
        nxt = day.next_fy_end
        if nxt:
            assert oracles.check_overlap_meta(real.conn, nxt, active=True, ended=False) == [], date
        elif date >= D(2091, 1, 2):
            # the saved manifest.db is the END state (overlap:FY91-92 activated 2091-01-02),
            # so the "not active" meta check is only meaningful on days after that
            assert oracles.check_overlap_meta(real.conn, "FY91-92", active=False) == [], date


@needs_evidence
@pytest.mark.parametrize("date", [D(2090, 12, 29), D(2090, 12, 30), D(2090, 12, 31), D(2091, 1, 2), D(2091, 1, 3)])
def test_real_x01_mail_oracle_clean_days(real, date):
    assert oracles.check_expected_mails(real.mails, real.days[date]) == [], date


@needs_evidence
def test_real_x01_escalation_missing_is_the_only_mail_violation_and_becomes_finding(real):
    from tools.livebox import run_lane
    day = real.days[D(2091, 1, 1)]
    v = oracles.check_expected_mails(real.mails, day)
    assert len(v) == 1 and KEY in str(v[0]) and "sent 0" in str(v[0])
    checks = [{"check_id": f"MAIL:{v[0].rule}", "verdict": "FAIL", "expected": "e", "observed": str(v[0]),
               "evidence": []}]
    run_lane.apply_known_findings(day, checks)
    assert checks[0]["verdict"] == "FINDING" and checks[0]["finding"] == FINDING_NAME


@needs_evidence
def test_real_x01_m2_storm_rule_clean_with_override_days(real):
    from tools.livebox import run_lane
    inj = run_lane.override_injection_dates(x01().overrides)
    assert inj == {D(2090, 12, 31), D(2091, 1, 1)}
    assert oracles.check_m2(real.mails, inj) == []
    assert oracles.check_m3(real.mails) == []


@needs_evidence
def test_real_x01_status_cards_expected_two_during_blackout(real):
    for date in (D(2090, 12, 31), D(2091, 1, 1)):
        d = real.days[date]
        assert (d.cards_0900, d.cards_2330) == (2, 2) and not d.overlap_active_at_0900
    assert real.days[D(2091, 1, 2)].cards_2330 == 4


@needs_evidence
def test_real_x01_judged_by_kind_yields_exactly_the_p1_finding():
    from tools.livebox.scenarios import results
    r = results.build_results(EVIDENCE)
    kinds = {(m["day"], m["kind"]) for m in r.mails if m["key"] == KEY}
    assert kinds == {("2090-12-31", "WARNING")}                       # the escalation never left the box
    sev = [m["severity"] for m in r.mails if m["key"] == KEY]
    assert sev == ["ACTION_REQUIRED"]                                  # subject word differs from kind (P2)
    out = x01().expect(r)
    assert len(out) == 1, out
    assert "ACTION_REQUIRED" in out[0] and KEY in out[0]
    assert x01().expected_red == FINDING_NAME
