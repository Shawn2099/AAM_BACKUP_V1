"""Tests for tools.livebox.scenarios.variants (X01b, X01c, X04c, X04l, X06w, X08b) and the rig wake-fail knob."""
from __future__ import annotations

import datetime as dt
import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from tools.livebox import calendar as cal
from tools.livebox import rig as rigmod
from tools.livebox import run_lane
from tools.livebox.scenarios import results as res_mod

IDS = ["X01b", "X01c", "X04c", "X04l", "X06w", "X08b"]
IST = cal.IST


def V():
    return importlib.import_module("tools.livebox.scenarios.variants")


def scn(sid):
    return next(s for s in V().SCENARIOS if s.id == sid)


def fake(**kw):
    base = dict(rows=[], mails=[], config_fy_by_day={}, activated_on=[], switched_on=[], facts={})
    base.update(kw)
    return SimpleNamespace(**base)


def mail(day, kind, key="", code=""):
    return {"day": day, "kind": kind, "severity": kind, "key": key, "code": code}


def row(day, leg, status):
    return {"day": day, "leg": leg, "status": status}


# ---------------------------------------------------------------- registration
def test_ids_lanes_unique_and_catalog_lookup():
    assert [s.id for s in V().SCENARIOS] == IDS
    assert len({s.lane for s in V().SCENARIOS}) == len(IDS)
    for sid in IDS:
        got = run_lane.load_catalog(f"tools.livebox.scenarios.variants:{sid}")
        assert [s.id for s in got] == [sid]
    assert len(run_lane.load_catalog("tools.livebox.scenarios.variants:SCENARIOS")) == len(IDS)


@pytest.mark.parametrize("sid", IDS)
def test_contract_and_driver_wrapping(sid):
    s = scn(sid)
    assert callable(s.setup) and callable(s.expect) and s.injection_wired is True
    assert isinstance(s.expect(fake()), list)
    start = dt.date.fromisoformat(s.start)
    assert cal.LANE_A_START <= start and start + dt.timedelta(days=s.days) <= cal.LANE_A_START + dt.timedelta(
        days=cal.LANE_A_DAYS)
    res_mod.to_driver_scenario(s)             # wraps without error


def test_x17_x39_stay_unwired_with_a_reason():
    w1 = importlib.import_module("tools.livebox.scenarios.w1")
    w3 = importlib.import_module("tools.livebox.scenarios.w3")
    assert next(s for s in w1.SCENARIOS if s.id == "X17").injection_wired is False
    assert next(s for s in w3.SCENARIOS if s.id == "X39").injection_wired is False
    assert V().NOT_WIRED["X17"] and V().NOT_WIRED["X39"]


# ---------------------------------------------------------------- calendars follow the lane mode
def _days(sid, mode="both"):
    s = scn(sid)
    return {d.date: d for d in cal.build_calendar(
        dt.date.fromisoformat(s.start), s.days, overlap_enabled=True,
        cloud_enabled=mode in ("cloud", "both"), lan_enabled=mode in ("lan", "both"),
        outages=list(s.outages), overrides=cal.overrides_map(s.overrides))}


def test_x01b_three_blackout_days_and_activation_on_third_day():
    d = _days("X01b")
    assert [x for x in d if d[x].activation_result == cal.ACT_WAITING_CLOCK] == [
        dt.date(2090, 12, 31), dt.date(2091, 1, 1), dt.date(2091, 1, 2)]
    assert d[dt.date(2091, 1, 3)].is_activation_day


def test_x01c_same_shape_as_x01():
    d = _days("X01c")
    assert d[dt.date(2091, 1, 2)].is_activation_day


def test_x04c_cloud_only_has_no_lan_rows_and_stale_cloud_ok():
    d = _days("X04c", "cloud")[dt.date(2091, 1, 2)]
    assert not [r for r in d.rows if r.leg == cal.LAN] and "cloud" in d.stale_ok


def test_x04l_lan_only_has_no_cloud_rows_and_no_cloud_stale_mail():
    d = _days("X04l", "lan")[dt.date(2091, 1, 2)]
    assert not [r for r in d.rows if r.leg == cal.CLOUD]
    assert "lan" in d.stale_ok and not [m for m in d.mails if m.key == "stale:cloud"]


def test_x08b_day_override_allows_failed_lan_and_expects_one_problem_mail():
    d = _days("X08b")[dt.date(2091, 2, 4)]
    lan = [r for r in d.rows if r.leg == cal.LAN]
    assert lan and all("LAN_FAILED" in r.statuses and "LAN_COMPLETE" not in r.statuses for r in lan)
    assert d.mail_count(cal.MAIL_PROBLEM) == 1


# ---------------------------------------------------------------- expectations
FULL3 = dict(
    activated_on=["2091-01-03"],
    mails=[mail("2090-12-31", "WARNING", "rollover:activation:FY91-92"),
           mail("2091-01-01", "ACTION_REQUIRED", "rollover:activation:FY91-92"),
           mail("2091-01-03", "INFO", "rollover:activated:FY91-92"),
           mail("2090-12-31", "FAILURE", "cloud"), mail("2091-01-01", "FAILURE", "cloud2")],
    rows=[row("2090-12-31", "lan", "LAN_COMPLETE")])


def test_x01b_clean_passes_and_wrong_activation_day_fails():
    assert scn("X01b").expect(fake(**FULL3)) == []
    bad = dict(FULL3, activated_on=["2091-01-02"])
    assert any("activation expected exactly on 2091-01-03" in f for f in scn("X01b").expect(fake(**bad)))


def test_x01b_too_many_failure_mails_and_red_lan():
    many = dict(FULL3, mails=FULL3["mails"] + [mail("2091-01-02", "FAILURE", "c3"), mail("2091-01-02", "FAILURE", "c4")],
                rows=[row("2091-01-01", "lan", "LAN_FAILED")])
    out = scn("X01b").expect(fake(**many))
    assert any("FAILURE mails" in f for f in out) and any("LAN must stay green" in f for f in out)


def test_x01_family_escalation_failure_is_masked_as_finding(tmp_path):
    no_act = dict(FULL3, mails=[m for m in FULL3["mails"] if m["kind"] != "ACTION_REQUIRED"])
    fails = scn("X01c").expect(fake(**dict(no_act, activated_on=["2091-01-02"])))
    assert fails and all("F-P1-ESCALATION-SUPPRESSED" in f for f in fails)
    assert scn("X01c").expected_red == "F-P1-ESCALATION-SUPPRESSED"


def test_x01c_uses_dns_variant_and_x01b_blackhole():
    calls = []
    rig = SimpleNamespace(set_cloud=lambda v: calls.append(("cloud", v)),
                          set_clock_proof=lambda m, s=0.0: calls.append(("proof", m)))
    for sid, first, last in (("X01c", "2090-12-31", "2091-01-02"), ("X01b", "2090-12-31", "2091-01-03")):
        calls.clear()
        s = scn(sid)
        s.on_day(rig, SimpleNamespace(date=dt.date.fromisoformat(first)))
        assert calls[0][1] == ("dns" if sid == "X01c" else "blackhole") and calls[1] == ("proof", "nointernet")
        calls.clear()
        s.on_day(rig, SimpleNamespace(date=dt.date.fromisoformat(last)))
        assert calls == [("cloud", "local"), ("proof", "agree")]


def test_x04_variants_reuse_x04_checks_with_own_id():
    twice = fake(activated_on=["2091-01-02", "2091-01-03"])
    for sid in ("X04c", "X04l"):
        out = scn(sid).expect(twice)
        assert out and all(f.startswith(sid + ":") for f in out)
        assert scn(sid).expect(fake(activated_on=["2091-01-02"])) == []


def test_x06w_dedupe_resolution_and_nothing_stale():
    ok = fake(rows=[row("2091-02-14", "cloud", "CLOUD_COMPLETE"), row("2091-02-15", "cloud", "CLOUD_COMPLETE")],
              mails=[mail("2091-02-11", "ACTION_REQUIRED", "stale:cloud"),
                     mail("2091-02-12", "ACTION_REQUIRED", "stale:cloud")])
    assert scn("X06w").expect(ok) == []
    dup = fake(rows=ok.rows, mails=ok.mails + [mail("2091-02-11", "ACTION_REQUIRED", "stale:cloud")])
    assert any("max 1" in f for f in scn("X06w").expect(dup))
    still = fake(rows=ok.rows, mails=ok.mails + [mail("2091-02-15", "ACTION_REQUIRED", "stale:lan")])
    assert any("last day" in f for f in scn("X06w").expect(still))
    assert any("no overdue mail" in f for f in scn("X06w").expect(fake(rows=ok.rows)))
    assert any("no successful cloud run" in f for f in scn("X06w").expect(fake(mails=ok.mails)))


def test_x06w_ticks_call_real_stale_check_three_times_and_never_move_clock_back(monkeypatch):
    calls, moves = [], []
    fake_wd = types.SimpleNamespace(_check_stale_legs=lambda cfg: calls.append(cfg) or ["cloud"])
    monkeypatch.setitem(sys.modules, "watchdog", fake_wd)
    clock = SimpleNamespace(now=dt.datetime(2091, 2, 10, 23, 30, tzinfo=IST),
                            move_to=lambda at: (moves.append(at), setattr(clock, "now", at)))
    drained = []
    rig = SimpleNamespace(clock=clock, load_config=lambda: "CFG", drain_mail=lambda at: drained.append(at))
    V()._watchdog_ticks(rig, SimpleNamespace(date=dt.date(2091, 2, 11)))
    assert calls == ["CFG"] * 3 and len(drained) == 3 and len(moves) == 3
    assert [t["stale"] for t in rig.watchdog_ticks] == [["cloud"]] * 3
    clock.now = dt.datetime(2091, 2, 11, 5, 0, tzinfo=IST)           # clock already later: no move back
    moves.clear()
    V()._watchdog_ticks(rig, SimpleNamespace(date=dt.date(2091, 2, 11)))
    assert moves == []


def test_x06w_tick_error_is_recorded_not_raised(monkeypatch):
    def boom(cfg):
        raise RuntimeError("x")
    monkeypatch.setitem(sys.modules, "watchdog", types.SimpleNamespace(_check_stale_legs=boom))
    rig = SimpleNamespace(clock=SimpleNamespace(now=None, move_to=lambda at: None), load_config=lambda: 1,
                          drain_mail=lambda at: None)
    V()._watchdog_ticks(rig, SimpleNamespace(date=dt.date(2091, 2, 11)))
    assert all("error" in t for t in rig.watchdog_ticks)


def test_x08b_knob_schedule():
    calls = []
    rig = SimpleNamespace(set_wake_fail=lambda f: calls.append(f))
    for d in ("2091-02-03", "2091-02-04", "2091-02-05", "2091-02-06"):
        scn("X08b").on_day(rig, SimpleNamespace(date=dt.date.fromisoformat(d)))
    assert calls == [True, False]


def test_x08b_expectations():
    good = fake(rows=[row("2091-02-04", "cloud", "CLOUD_COMPLETE"), row("2091-02-04", "lan", "LAN_FAILED"),
                      row("2091-02-05", "lan", "LAN_COMPLETE")],
                mails=[mail("2091-02-04", "FAILURE", "lan:wake")])
    assert scn("X08b").expect(good) == []
    ok_lan = fake(rows=[row("2091-02-04", "cloud", "CLOUD_COMPLETE"), row("2091-02-04", "lan", "LAN_COMPLETE")],
                  mails=good.mails)
    assert any("never woke" in f for f in scn("X08b").expect(ok_lan))
    crash = fake(rows=[row("2091-02-04", "cloud", "CLOUD_FAILED"), row("2091-02-04", "lan", "LAN_ABANDONED")])
    out = scn("X08b").expect(crash)
    assert any("crash" in f for f in out) and any("cloud leg" in f for f in out) and any("no problem mail" in f for f in out)
    assert any("no LAN row" in f for f in scn("X08b").expect(fake()))
    stuck = fake(rows=good.rows[:2] + [row("2091-02-05", "lan", "LAN_FAILED")], mails=good.mails)
    assert any("did not recover" in f for f in scn("X08b").expect(stuck))


# ---------------------------------------------------------------- rig knob
def test_rig_set_wake_fail_makes_wake_spy_raise_and_restores():
    from core.wol import WolTimeout

    rig = rigmod.LaneRig.__new__(rigmod.LaneRig)
    rig._record = lambda *a, **k: None
    rig.seams = SimpleNamespace(wake_error=None)
    rig.set_wake_fail(True)
    assert isinstance(rig.seams.wake_error, WolTimeout)
    rig.set_wake_fail(False)
    assert rig.seams.wake_error is None
    rig.seams = None
    with pytest.raises(rigmod.RigError):
        rig.set_wake_fail(True)


def test_seams_wake_spy_raises_when_wake_error_set():
    import flow

    seams = rigmod.Seams(lane_root=".", bucket_dir=None, remote_config_text=None)
    seams.install()
    try:
        flow.ensure_server_online(object())            # works, recorded
        seams.wake_error = RuntimeError("down")
        with pytest.raises(RuntimeError):
            flow.ensure_server_online(object())
        assert len(seams.wake_calls) == 2
    finally:
        seams.uninstall()
