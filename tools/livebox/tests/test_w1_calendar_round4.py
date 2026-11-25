r"""Red-first tests (round 4): the W1 diagnosis fixes F1-F7 (docs/LIVEBOX_W1_DIAGNOSIS_2026-11-24.md). Written before
the code. Product behaviour is the reference; only the harness changes.

PINNED API (names are the contract)

scenarios/results.py
  _leg(mode) -> "cloud" | "rollover" | "lan"   ("rollover" for mode rollover, case-insensitive; every other
        non-cloud mode stays "lan"). A blocked ROLLOVER row is therefore never de-duplicated against the final LAN
        row of the same minute.
run_lane.py
  LaneHooks._run_event_inner   out.new_rows = rows of `_rows_now()` whose run_id was NOT in the "before" list
        (set difference by run_id, not a slice by position).
  LaneHooks.rollover_wakes / rollover_shutdowns  (int, 0 at start) the NAS wake / shutdown calls made by rollover
        events; persisted in get_state()/load_state(); FINAL_SPY subtracts them (they stay reported once as
        SPY_ROLLOVER_NAS / F-OWN-1).
  LaneHooks._event_checks      a flow exception on a rollover_check / audit event is NOT a FAIL when
        day.expect_raise contains "rollover" / "audit" (it stays a FAIL otherwise).
  LaneHooks._apply_churn       no churn deletes ("delete" ops) in any tree with fewer than 50 regular files, unless
        the scenario opted in (hooks.n_deletes True, or driver scenario .n_deletes True).
  override_injection_dates(overrides)  also returns dates whose override carries stale_ok or extra_mails.
scenarios/base.py       Scenario GAINS n_deletes: bool = False (carried by results.to_driver_scenario); at least one
        catalog scenario sets it (the deliberate small-tree scenario that keeps F-NEW-G3-SMALL-N reproducible).
oracles.py
  _matches_suffix / _NEXT_SUFFIX: run ids ending "-rollover-final-cloud" / "-rollover-final-lan" are NEVER matched by
        the empty suffix (a normal pass); they match only their own suffix.
calendar.py
  Day.expect_raise: tuple[str, ...] = ()     "rollover" and/or "audit": the flow must raise that day.
  DayOverride.stale_ok: tuple[str, ...] = () merged into Day.stale_ok.
  ExpectedMail.code: str | None = None       fault code the mail must carry.
  MAIL_ROLLOVER_BLOCKED = "rollover_blocked" counted by the mail oracle like an activation-waiting problem mail.
  ROLLOVER_BLOCKED_STATUS = "ROLLOVER_BLOCKED"
  build_calendar semantics:
   * switch-due day, every healthy switch: besides the leg "rollover" row (ROLLOVER_COMPLETE) two rows at 06:00 for
     the old FY: leg cloud, run_suffix "-rollover-final-cloud", statuses CLOUD_OK; leg lan, run_suffix
     "-rollover-final-lan", statuses LAN_OK (only for an enabled leg).
   * switch-due day with override clock_unproven (X02): row leg "rollover", ROLLOVER_BLOCKED_STATUS; NO final rows;
     one ExpectedMail(MAIL_ROLLOVER_BLOCKED, 1, key "rollover:clock_unverified", code "AAM-ROL-03",
     severity "ACTION_REQUIRED"); rollover_result None; is_switch_day False; configured_fy_end unchanged;
     expect_raise contains "rollover". The next day without override switches (is_switch_day, rollover_result
     ROLLOVER_COMPLETED).
   * switch-due day with cloud_down only (X03): ROLLOVER_BLOCKED row, final cloud row CLOUD_FAILED, final LAN row
     LAN_OK, mail key "blocked:rollover" code "AAM-ROL-02" severity ACTION_REQUIRED, expect_raise "rollover".
   * cloud_down while the overlap is active: BOTH cloud passes CLOUD_FAILED, two MAIL_PROBLEM mails (one per pass),
     stale_ok ("cloud", "cloud:<next fy>").
   * cloud_down on a Sunday (audit): audit_cloud rows of every FY pass == ("AUDIT_INCOMPLETE",), one more
     MAIL_PROBLEM (AAM-AUD-02), expect_raise contains "audit"; audit_lan rows unchanged.
   * catch-up cloud row (Outage catchup="latest"): statuses ("CLOUD_MISSED",); one MAIL_PROBLEM key "missed:cloud",
     code "AAM-SYS-07", severity "WARNING"; the next regular cloud run expects one MAIL_RESOLVED. LAN catch-up keeps
     LAN_OK.
scenarios/w1.py
  X02 overrides ("2091-04-01", DayOverride(cloud_down=True, clock_unproven=True)); X03 ("2091-04-01",
  DayOverride(cloud_down=True)); X04 ("2091-01-02", DayOverride(stale_ok=("cloud",), extra_mails=(one MAIL_PROBLEM,
  key "stale:cloud", code "AAM-SYS-03", severity "ACTION_REQUIRED", at 23:30)))  -- 2091-01-03 needs no override.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.livebox import calendar as cal
from tools.livebox import oracles
from tools.livebox import run_lane as rl
from tools.livebox.tests.test_review_fixes_safety import _hooks

D = dt.date
IST = cal.IST
WORKTREE = Path(__file__).resolve().parents[3]
RUNS = WORKTREE / "agent_runs"
BLOCKED = "ROLLOVER_BLOCKED"
FINAL_C, FINAL_L = "-rollover-final-cloud", "-rollover-final-lan"


def scn(sid):
    from tools.livebox.scenarios import w1

    return {s.id: s for s in w1.SCENARIOS}[sid]


def days_of(sid):
    a = SimpleNamespace(scenario_obj=scn(sid), scenario=None, no_overlap=False, cloud_only=False, lan_only=False,
                        both_off=False, outage=[])
    return {d.date: d for d in rl.build_days(a)}


def rows(day, leg=None):
    return [r for r in day.rows if leg is None or r.leg == leg]


def mails(day, cls):
    return [m for m in day.mails if m.mail_class == cls]


def lane(overrides, start=D(2091, 3, 30), days=5, **kw):
    return {d.date: d for d in cal.build_calendar(start, days, overrides=overrides, **kw)}


# ============================================================================ F1: results._leg and de-duplication
def res():
    from tools.livebox.scenarios import results

    return results


def test_f1_leg_rollover():
    assert res()._leg("rollover") == "rollover" and res()._leg("Rollover") == "rollover"
    assert res()._leg("cloud") == "cloud" and res()._leg("lan") == "lan"


def test_f1_blocked_rollover_row_survives_the_final_lan_row_of_the_same_minute(tmp_path):
    (tmp_path / "status").mkdir()
    t = "2091-04-01T06:00:30+05:30"
    doc = {"fy_prefix": "FY90-91", "recent_runs": [
        {"mode": "lan", "status": "LAN_COMPLETE", "started_at": t, "fy": "FY90-91"},
        {"mode": "rollover", "status": BLOCKED, "started_at": t, "fy": "FY90-91"}]}
    (tmp_path / "status" / "2091-04-02_0900.json").write_text(json.dumps(doc), encoding="utf-8")
    got = res().build_results(tmp_path).rows
    assert [(r["leg"], r["status"]) for r in got if r["day"] == "2091-04-01"].count(("rollover", BLOCKED)) == 1
    assert ("lan", "LAN_COMPLETE") in [(r["leg"], r["status"]) for r in got]


# ============================================================================ F2: attribution by run_id
def test_f2_new_rows_are_found_by_run_id_not_by_list_position(tmp_path, monkeypatch):
    h = _hooks(tmp_path, monkeypatch)
    old = [{"run_id": "o1", "mode": "cloud", "status": "CLOUD_COMPLETE", "started_at": "2091-01-03T00:05:00.111"},
           {"run_id": "o2", "mode": "lan", "status": "LAN_COMPLETE", "started_at": "2091-01-03T00:05:00.200"}]
    new = {"run_id": "n1", "mode": "lan", "status": "LAN_COMPLETE", "started_at": "2091-01-03T00:05:00.083"}
    snaps = iter([old, [new, *old]])               # the new row sorts BEFORE both old rows
    monkeypatch.setattr(h, "_rows_now", lambda: next(snaps))
    monkeypatch.setattr(h, "_event_checks", lambda *a, **k: None)
    calls = SimpleNamespace(wake_calls=[], shutdown_calls=[], archive_calls=[])
    h.rig = SimpleNamespace(
        seams=calls, clock=SimpleNamespace(move_to=lambda at: None), ensure_prefix=lambda fy: None,
        run_flow=lambda *a, **k: SimpleNamespace(value=None, exc=None), drain_mail=lambda at: None)
    day = cal.build_calendar(D(2091, 1, 3), 1)[0]
    ev = cal.Event(dt.datetime(2091, 1, 3, 0, 5, tzinfo=IST), cal.CATCHUP_PREFIX + cal.LAN)
    out = rl.EventOutcome(ev)
    h._run_event_inner(day, ev, out)
    assert [r["run_id"] for r in out.new_rows] == ["n1"]


# ============================================================================ F3: switch day, blocked rollover
def test_f3_healthy_switch_day_expects_the_two_final_backups():
    d = lane({})[D(2091, 4, 1)]
    fc = [r for r in rows(d, cal.CLOUD) if r.run_suffix == FINAL_C]
    fl = [r for r in rows(d, cal.LAN) if r.run_suffix == FINAL_L]
    assert len(fc) == 1 and fc[0].fy == "FY90-91" and set(fc[0].statuses) == set(cal.CLOUD_OK)
    assert len(fl) == 1 and fl[0].fy == "FY90-91" and set(fl[0].statuses) == set(cal.LAN_OK)
    assert d.is_switch_day and d.expect_raise == ()


def test_f3_cloud_only_switch_day_has_no_final_lan_row():
    d = lane({}, cloud_enabled=True, lan_enabled=False)[D(2091, 4, 1)]
    assert not [r for r in d.rows if r.run_suffix == FINAL_L]


def test_f3_oracle_suffix_never_matches_a_final_row_as_a_normal_pass():
    assert not oracles._matches_suffix("abc-rollover-final-lan", "")
    assert not oracles._matches_suffix("abc-rollover-final-cloud", "")
    assert oracles._matches_suffix("abc-rollover-final-lan", FINAL_L)
    assert oracles._matches_suffix("abc-lan", "")
    assert not oracles._matches_suffix("abc-lan-FY91-92", "")


X02_OV = {D(2091, 4, 1): cal.DayOverride(cloud_down=True, clock_unproven=True)}
X03_OV = {D(2091, 4, 1): cal.DayOverride(cloud_down=True)}


def test_f3_x02_clock_unproven_blocks_the_switch_and_skips_the_finals():
    c = lane(X02_OV)
    d = c[D(2091, 4, 1)]
    rr = rows(d, "rollover")
    assert [r.statuses for r in rr] == [(BLOCKED,)]
    assert not [r for r in d.rows if r.run_suffix in (FINAL_C, FINAL_L)]
    m = mails(d, cal.MAIL_ROLLOVER_BLOCKED)
    assert len(m) == 1 and m[0].key == "rollover:clock_unverified"
    assert m[0].code == "AAM-ROL-03" and m[0].severity == "ACTION_REQUIRED"
    assert d.rollover_result is None and not d.is_switch_day and d.configured_fy_end == "FY90-91"
    assert "rollover" in d.expect_raise
    nxt = c[D(2091, 4, 2)]
    assert nxt.is_switch_day and nxt.rollover_result == cal.ROLLOVER_COMPLETED and nxt.configured_fy_end == "FY91-92"
    assert [r.statuses for r in rows(nxt, "rollover")] == [(cal.ROLLOVER_ROW_STATUS,)]


def test_f3_x03_incomplete_final_backup_blocks_the_switch():
    c = lane(X03_OV)
    d = c[D(2091, 4, 1)]
    assert [r.statuses for r in rows(d, "rollover")] == [(BLOCKED,)]
    fc = [r for r in rows(d, cal.CLOUD) if r.run_suffix == FINAL_C]
    fl = [r for r in rows(d, cal.LAN) if r.run_suffix == FINAL_L]
    assert [r.statuses for r in fc] == [("CLOUD_FAILED",)]
    assert [r.statuses for r in fl] == [cal.LAN_OK]
    m = mails(d, cal.MAIL_ROLLOVER_BLOCKED)
    assert len(m) == 1 and m[0].key == "blocked:rollover" and m[0].code == "AAM-ROL-02"
    assert m[0].severity == "ACTION_REQUIRED"
    assert not d.is_switch_day and d.rollover_result is None and "rollover" in d.expect_raise
    assert c[D(2091, 4, 2)].is_switch_day


def test_f3_switch_day_row_set_is_complete_for_x03():
    """04-01 (Sunday, cloud down): configured + next cloud pass FAILED, LAN both passes OK, finals, blocked row."""
    d = lane(X03_OV)[D(2091, 4, 1)]
    plain_cloud = [r for r in rows(d, cal.CLOUD) if r.run_suffix in ("", "-FY91-92")]
    assert len(plain_cloud) == 2 and all(r.statuses == ("CLOUD_FAILED",) for r in plain_cloud)
    assert len([r for r in rows(d, cal.LAN) if r.run_suffix in ("", "-FY91-92")]) == 2


def test_f3_x03_problem_mail_count_is_two_cloud_plus_one_audit():
    d = lane(X03_OV)[D(2091, 4, 1)]
    assert d.weekday == 6                               # Sunday: the weekly audit runs
    assert d.mail_count(cal.MAIL_PROBLEM) == 3          # 2 AAM-CLD-03 (one per pass) + 1 AAM-AUD-02
    assert "audit" in d.expect_raise


# ============================================================================ F3: cloud down during the overlap
def _overlap_days(down_weekday):
    start = D(2091, 1, 5)
    days = [start + dt.timedelta(days=i) for i in range(1, 12)]
    day = next(x for x in days if x.weekday() == down_weekday)
    return day, lane({day: cal.DayOverride(cloud_down=True)}, start=start, days=12)[day]


def test_f3_cloud_down_in_the_overlap_fails_both_passes_with_two_mails():
    _, d = _overlap_days(2)                                          # a Wednesday: no audit
    cl = rows(d, cal.CLOUD)
    assert [r.run_suffix for r in cl] == ["", "-FY91-92"] and all(r.statuses == ("CLOUD_FAILED",) for r in cl)
    assert d.mail_count(cal.MAIL_PROBLEM) == 2
    assert set(d.stale_ok) >= {"cloud", "cloud:FY91-92"}
    assert "audit" not in d.expect_raise and "rollover" not in d.expect_raise


def test_f3_cloud_down_on_an_audit_sunday_adds_one_problem_mail_and_incomplete_audits():
    _, d = _overlap_days(6)
    assert d.mail_count(cal.MAIL_PROBLEM) == 3
    ac = rows(d, "audit_cloud")
    assert ac and all(r.statuses == ("AUDIT_INCOMPLETE",) for r in ac)
    assert rows(d, "audit_lan") and all(r.statuses == cal.AUDIT_OK for r in rows(d, "audit_lan"))
    assert "audit" in d.expect_raise


def test_f3_expect_raise_is_honoured_by_the_flow_state_checks(tmp_path, monkeypatch):
    h = _hooks(tmp_path, monkeypatch)
    h.rig = SimpleNamespace(config={"paths": {"runtime_dir": str(tmp_path)}}, runtime_dir=tmp_path)
    day = lane(X02_OV)[D(2091, 4, 1)]

    def verdicts(day_, base, exc):
        at = dt.datetime(2091, 4, 1, 6 if base == cal.ROLLOVER_CHECK else 8, 0, tzinfo=IST)
        ev = cal.Event(at, base)
        out = rl.EventOutcome(ev, flow=SimpleNamespace(exc=exc, value=None))
        h._event_checks(day_, ev, out, 0, 0, 0)
        return [c["verdict"] for c in out.checks if c["check_id"] == "FLOW_STATE"]

    assert "FAIL" not in verdicts(day, cal.ROLLOVER_CHECK, RuntimeError("blocked"))
    assert "FAIL" not in verdicts(day, cal.AUDIT, ExceptionGroup("audit", [RuntimeError("x")]))
    plain = dataclasses.replace(day, expect_raise=())
    assert "FAIL" in verdicts(plain, cal.ROLLOVER_CHECK, RuntimeError("blocked"))
    assert "FAIL" in verdicts(plain, cal.AUDIT, ExceptionGroup("audit", [RuntimeError("x")]))


# ============================================================================ F4: spy counts
def test_f4_rollover_wake_and_shutdown_are_subtracted_from_the_final_spy(tmp_path, monkeypatch):
    h = _hooks(tmp_path, monkeypatch)
    h.rig = SimpleNamespace(config={"paths": {"runtime_dir": str(tmp_path)}}, runtime_dir=tmp_path,
                            seams=SimpleNamespace(wake_calls=[0] * 10, shutdown_calls=[0] * 4, archive_calls=[]))
    day = lane({})[D(2091, 4, 1)]
    ev = cal.Event(dt.datetime(2091, 4, 1, 6, 0, tzinfo=IST), cal.ROLLOVER_CHECK)
    out = rl.EventOutcome(ev, flow=SimpleNamespace(exc=None, value={"x": cal.ROLLOVER_COMPLETED}))
    h._event_checks(day, ev, out, 2, 1, 0)
    assert (h.rollover_wakes, h.rollover_shutdowns) == (2, 1)
    h.lan_passes, h.lan_flows, h.lan_flows_complete = 8, 8, 3
    spy = [c for c in h.final_checks() if c["check_id"] == "FINAL_SPY"]
    assert spy and all(c["verdict"] == "PASS" for c in spy), spy
    st = h.get_state()
    assert st["rollover_wakes"] == 2 and st["rollover_shutdowns"] == 1


def test_f4b_final_spy_uses_lan_flows_not_lan_passes_during_overlap(tmp_path, monkeypatch):
    h = _hooks(tmp_path, monkeypatch)
    h.rig = SimpleNamespace(config={"paths": {"runtime_dir": str(tmp_path)}}, runtime_dir=tmp_path,
                            seams=SimpleNamespace(wake_calls=[0] * 14, shutdown_calls=[0] * 14, archive_calls=[]))
    h.lan_passes, h.lan_flows, h.lan_flows_complete = 22, 14, 14
    spy = [c for c in h.final_checks() if c["check_id"] == "FINAL_SPY"]
    assert spy and all(c["verdict"] == "PASS" for c in spy), spy


# ============================================================================ F5: churn in small trees
def _tree(root, n):
    for i in range(n):
        p = root / "Invoices" / f"f{i:03d}.pdf"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * (100 + i))


def _delete_ops(tmp_path, monkeypatch, n_files, n_deletes=False):
    import shutil

    h = _hooks(tmp_path, monkeypatch)
    h.large_file_mb = 1
    h.n_deletes = n_deletes
    h.rig = SimpleNamespace(lane_dir=h.lane_root)
    kinds = []
    for i in range(28):
        day = cal.build_calendar(D(2091, 3, 1) + dt.timedelta(days=i), 1)[0]
        root = h.src_dir(day.configured_fy_end)
        shutil.rmtree(root, ignore_errors=True)
        _tree(root, n_files)
        kinds += [op.kind for _, _, op in h._apply_churn(day)]
    return kinds


def test_f5_no_deletes_in_a_small_tree(tmp_path, monkeypatch):
    assert "delete" not in _delete_ops(tmp_path, monkeypatch, 12)


def test_f5_big_trees_still_get_deletes(tmp_path, monkeypatch):
    assert "delete" in _delete_ops(tmp_path, monkeypatch, 60)


def test_f5_a_scenario_can_opt_in_to_small_tree_deletes(tmp_path, monkeypatch):
    assert "delete" in _delete_ops(tmp_path, monkeypatch, 12, n_deletes=True)


def test_f5_one_catalog_scenario_keeps_the_small_tree_delete():
    from tools.livebox.make_report import load_catalog

    assert any(getattr(s, "n_deletes", False) for s in load_catalog())


# ============================================================================ F6: stale_ok and X04
def test_f6_day_override_stale_ok_reaches_the_day():
    d = lane({D(2091, 1, 2): cal.DayOverride(stale_ok=("cloud",))}, start=D(2091, 1, 1), days=3)[D(2091, 1, 2)]
    assert "cloud" in d.stale_ok
    assert "cloud" not in lane({}, start=D(2091, 1, 1), days=3)[D(2091, 1, 2)].stale_ok


def test_f6_override_injection_dates_include_stale_ok_and_extra_mails():
    ov = {D(2091, 1, 2): cal.DayOverride(stale_ok=("cloud",)),
          D(2091, 1, 3): cal.DayOverride(extra_mails=(cal.ExpectedMail(cal.MAIL_PROBLEM, 1),)),
          D(2091, 1, 4): cal.DayOverride(note="nothing")}
    assert rl.override_injection_dates(ov) == {D(2091, 1, 2), D(2091, 1, 3)}


def test_f6_x04_catchup_cloud_row_is_missed_with_the_syS07_mail_then_recovered():
    c = days_of("X04")
    d = c[D(2091, 1, 3)]
    cat = [r for r in rows(d, cal.CLOUD) if r.at.hour == 0]
    assert [r.statuses for r in cat] == [("CLOUD_MISSED",)]
    miss = [m for m in d.mails if m.key == "missed:cloud"]
    assert len(miss) == 1 and miss[0].mail_class == cal.MAIL_PROBLEM
    assert miss[0].code == "AAM-SYS-07" and miss[0].severity == "WARNING"
    assert d.mail_count(cal.MAIL_RESOLVED) == 1          # the 02:01 run that follows
    lan = [r for r in rows(d, cal.LAN) if r.at.hour == 0]
    assert lan and lan[0].statuses == cal.LAN_OK


def test_f6_x04_scenario_carries_the_overdue_override():
    ov = cal.overrides_map(scn("X04").overrides)
    o = ov[D(2091, 1, 2)]
    assert "cloud" in o.stale_ok
    stale = [m for m in o.extra_mails if m.key == "stale:cloud"]
    assert len(stale) == 1 and stale[0].code == "AAM-SYS-03" and stale[0].severity == "ACTION_REQUIRED"
    assert stale[0].at.timetz().replace(tzinfo=None) == dt.time(23, 30)
    d = days_of("X04")[D(2091, 1, 2)]
    assert "cloud" in d.stale_ok and d.mail_count(cal.MAIL_PROBLEM) == 1


def test_f6_x02_x03_scenarios_carry_their_overrides():
    assert cal.overrides_map(scn("X02").overrides) == X02_OV
    assert cal.overrides_map(scn("X03").overrides) == X03_OV
    for sid in ("X02", "X03"):
        d = days_of(sid)[D(2091, 4, 1)]
        assert "rollover" in d.expect_raise and d.rollover_result is None


# ============================================================================ re-evaluation of the saved real evidence
EVID = {"X01": RUNS / "livebox_dev54" / "w1_x01", "X02": RUNS / "livebox_dev51" / "w1_x02",
        "X03": RUNS / "livebox_dev52" / "w1_x03", "X04": RUNS / "livebox_dev53" / "w1_x04"}
# X02/X03: from 04-02 the saved runs contain the G3 small-tree blocks (harness churn, fixed by F5), not judged here.
LAST_DAY = {"X01": None, "X02": D(2091, 4, 1), "X03": D(2091, 4, 1), "X04": None}


def _have(sid):
    return (EVID[sid] / "manifest.db").is_file()


@pytest.mark.parametrize("sid", ["X01", "X02", "X03", "X04"])
def test_reeval_run_history_rows_match_the_new_calendar(sid):
    if not _have(sid):
        pytest.skip(f"evidence for {sid} absent")
    conn = oracles.open_ro(EVID[sid] / "manifest.db")
    try:
        bad = []
        for day in days_of(sid).values():
            if LAST_DAY[sid] and day.date > LAST_DAY[sid]:
                continue
            bad += [str(v) for v in oracles.check_run_rows(conn, day)]
    finally:
        conn.close()
    assert not bad, "\n".join(bad[:12])


@pytest.mark.parametrize("sid, ok", [("X01", {"FINDING"}), ("X02", {"PASS", "FINDING"}),
                                     ("X03", {"PASS", "FINDING"}), ("X04", {"PASS", "FINDING"})])
def test_reeval_scenario_verdicts_are_not_harness_fails(sid, ok):
    if not _have(sid):
        pytest.skip(f"evidence for {sid} absent")
    v = res().verdict_for(scn(sid), EVID[sid])
    assert v["verdict"] in ok, v
