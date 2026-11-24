r"""Red-first tests: wire the injections of X17, X37, X38, X39, X45, X46, X47, X48 (X42 stays gated).

PINNED API (new unless marked existing)

LaneRig (tools.livebox.rig)
    rig.facts: dict                         facts set during the lane (default {})
    rig.mark_fact(name, value=True)         rig.facts[name] = value; one inputs.jsonl row kind "fact", name = name
    rig.clock_offset: timedelta             default timedelta(0), the lasting clock skew
    rig.clock_skew(delta: timedelta)        clock_offset += delta; one inputs.jsonl row kind "clock_skew",
                                            name "clock_skew", args [str(delta)]; moves the live clock by delta
                                            immediately (like clock_jump). RigError without a simulated clock.
    rig.skewed(at) -> datetime              at + clock_offset
    rig.run_flow(name, at=T, ...)           moves the clock to rig.skewed(T) (today: T), so a skew LASTS over all
                                            later events; scheduled_at is NOT skewed.
    rig.fire_step(step)                     on an armed match: also rig.mark_fact("kill_child_fired") (even when no
                                            process was found); only robocopy.exe / rclone.exe of the lane die
    rig.trigger_during_next_flow(leg)       arm; the next run_flow("backup", mode=leg) starts a thread that, after
                                            rig.trigger_delay_seconds (default 1.0), calls rig.fire_trigger(leg) while
                                            the flow runs; the status goes to rig.trigger_statuses; fact
                                            "trigger_during_flow_fired"; one shot
    rig.race_next_flow(copies=2, mode="lan")  arm; the next run_flow("backup", mode=mode) runs `copies` calls of the
                                            flow released together by a barrier; the first outcome is returned, the
                                            others go to rig.race_outcomes; fact "race_fired"; one shot
    rig.kill_delay_seconds                  default 1.0 (seconds after a robocopy start before the sync kill)

LaneHooks (tools.livebox.run_lane)
    hooks._install_step_hook()              wraps core.manifest.ManifestDB.rollover_step_done (restored by
                                            hooks._stack.close()): after the real call, hooks.rig.fire_step(step)
    hooks._install_run_tool_guard()         extra: when a robocopy call starts and rig.kill_armed == "sync_robocopy",
                                            a thread calls rig.fire_step("sync_robocopy") after
                                            rig.kill_delay_seconds while the tool still runs
    hooks._write_summary()                  extra: rig.facts are merged as top level keys of summary.json
    hooks._run_event_inner(...)             flow events: rig.run_flow(at=ev.at) unchanged (rig applies the skew);
                                            status events: rig.clock.move_to(rig.skewed(ev.at))
    EventOutcome.sim_at                     the skewed instant the event ran at; events.jsonl row key "sim_at"

Scenarios
    X17  injection_wired True, requires fact:kill_child_fired, arms rig.kill_child_after("final_backups")
    X37  injection_wired True, requires fact:trigger_during_flow_fired, arms rig.trigger_during_next_flow("lan")
    X38  injection_wired True, requires fact:race_fired, arms rig.race_next_flow(...)
    X39  injection_wired True, requires fact:kill_child_fired, arms rig.kill_child_after("sync_robocopy")
    X45  an Injection on 2091-01-03 calls rig.clock_skew(-2 days) and rig.mark_fact("clock_back_injected")
    X46  an Injection calls rig.clock_skew(~+13 months, 390..400 days) and mark_fact("clock_jump_injected")
    X47  Scenario.outages cover Monday 2091-10-01 and an Injection marks fact "x47_outage_injected"
    X48  lane window 2099-12-xx .. beyond 2100-01-01, an Injection marks fact "x48_century_window"
    Not firing: verdict_for(...) == NOT_RUN (never PASS) for all eight (and X42).
(The old test_unwired_injection_scenarios_are_not_run in test_review2_runner.py must be updated by the code writer.)
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.livebox import calendar as cal
from tools.livebox import rig as rigmod

IST = cal.IST
D = dt.timedelta


def rl():
    return importlib.import_module("tools.livebox.run_lane")


def results():
    return importlib.import_module("tools.livebox.scenarios.results")


def scn(modname, sid):
    mod = importlib.import_module(f"tools.livebox.scenarios.{modname}")
    return next(s for s in mod.SCENARIOS if s.id == sid)


WIRED = [("w1", "X17", "kill_child_fired"), ("w3", "X37", "trigger_during_flow_fired"),
         ("w3", "X38", "race_fired"), ("w3", "X39", "kill_child_fired")]
INJECTED = [("w4", "X45", "clock_back_injected"), ("w4", "X46", "clock_jump_injected"),
            ("w4", "X47", "x47_outage_injected"), ("w4", "X48", "x48_century_window")]
ALL = WIRED + INJECTED


# ----------------------------------------------------------------------------------------------- fakes
class FakeClock:
    def __init__(self, now=None):
        self.now = now or dt.datetime(2091, 1, 1, tzinfo=IST)
        self.moves = []

    def move_to(self, instant):
        self.now = instant
        self.moves.append(instant)


class FakeRig:
    """Records every knob call; implements the new rig API surface the scenarios use."""

    def __init__(self):
        self.calls = []
        self.facts = {}
        self.kill_armed = None
        self.kill_delay_seconds = 0.0
        self.clock = FakeClock()
        self.clock_offset = D(0)
        self.fired = []
        self.seams = SimpleNamespace(wake_calls=[], shutdown_calls=[], archive_calls=[])

    def mark_fact(self, name, value=True):
        self.calls.append(("mark_fact", (name,)))
        self.facts[name] = value

    def clock_skew(self, delta):
        self.calls.append(("clock_skew", (delta,)))
        self.clock_offset += delta

    def skewed(self, at):
        return at + self.clock_offset

    def kill_child_after(self, step):
        self.calls.append(("kill_child_after", (step,)))
        self.kill_armed = step

    def fire_step(self, step):
        self.fired.append(step)
        return []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def rec(*a, **k):
            self.calls.append((name, a))

        return rec

    def names(self):
        return [c[0] for c in self.calls]


def _hooks(tmp_path, scenario=None, days=()):
    lane = tmp_path / "stamp1" / "laneW"
    lane.mkdir(parents=True, exist_ok=True)
    h = rl().LaneHooks(lane, lane="laneW", stamp="stamp1", days=list(days), agent_runs=tmp_path / "ar",
                       allow_dev_path=True, scenario=scenario)
    h.ev_dir.mkdir(parents=True, exist_ok=True)
    h.rig = FakeRig()
    h._orphan_robocopy = lambda: []
    return h


def _drive_catalog(tmp_path, catalog):
    """Run a catalog scenario's setup, on_day (every day) and all due injections through the real driver code."""
    start = dt.date.fromisoformat(catalog.start)
    days = cal.build_calendar(start, catalog.days)
    h = _hooks(tmp_path, catalog, days)
    for d in days:
        h.scenario.before_day(h, d)
    last = days[-1].date
    h._apply_injections(dt.datetime(last.year, last.month, last.day, 23, 59, tzinfo=IST))
    return h


def _jsonl(p):
    return [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]


# ----------------------------------------------------------------------------------------------- (a) wired
@pytest.mark.parametrize("mod,sid,fact", ALL)
def test_scenario_is_wired_and_gated_on_its_fact(mod, sid, fact):
    s = scn(mod, sid)
    assert s.injection_wired is True
    assert f"fact:{fact}" in s.requires


@pytest.mark.parametrize("mod,sid,fact", ALL)
def test_not_firing_means_not_run_never_pass(tmp_path, mod, sid, fact):
    s = scn(mod, sid)
    assert results().verdict_for(s, tmp_path)["verdict"] == "NOT_RUN"
    (tmp_path / "summary.json").write_text(json.dumps({fact: True}), encoding="utf-8")
    assert results().verdict_for(s, tmp_path)["verdict"] != "NOT_RUN"


def test_x42_stays_gated(tmp_path):
    s = scn("w4", "X42")
    assert "fact:x42_injected" in s.requires
    assert results().verdict_for(s, tmp_path)["verdict"] == "NOT_RUN"


# ----------------------------------------------------------------------------------------------- (b) arming via driver
ARMS = [("w1", "X17", "kill_child_after", ("final_backups",)),
        ("w3", "X37", "trigger_during_next_flow", ("lan",)),
        ("w3", "X38", "race_next_flow", None),
        ("w3", "X39", "kill_child_after", ("sync_robocopy",))]


@pytest.mark.parametrize("mod,sid,knob,args", ARMS)
def test_driver_arms_the_knob(tmp_path, mod, sid, knob, args):
    h = _drive_catalog(tmp_path, scn(mod, sid))
    got = [c for c in h.rig.calls if c[0] == knob]
    assert got, f"{sid} never called rig.{knob}; calls: {h.rig.names()}"
    if args is not None:
        assert got[0][1] == args


def test_x17_arming_is_on_or_before_the_switch_day(tmp_path):
    h = _drive_catalog(tmp_path, scn("w1", "X17"))
    assert h.rig.kill_armed == "final_backups"


@pytest.mark.parametrize("mod,sid,fact", INJECTED)
def test_injection_sets_fact_through_driver_and_summary(tmp_path, mod, sid, fact):
    h = _drive_catalog(tmp_path, scn(mod, sid))
    assert h.rig.facts.get(fact) is True
    h._write_summary()
    summ = json.loads((h.ev_dir / "summary.json").read_text(encoding="utf-8"))
    assert summ.get(fact) is True
    assert results().verdict_for(scn(mod, sid), h.ev_dir)["verdict"] != "NOT_RUN"


def test_summary_merges_rig_facts_without_clobbering(tmp_path):
    h = _hooks(tmp_path)
    h.rig.facts = {"race_fired": True, "kill_child_fired": True}
    h._write_summary()
    s = json.loads((h.ev_dir / "summary.json").read_text(encoding="utf-8"))
    assert s["race_fired"] is True and s["kill_child_fired"] is True
    assert "orphan_robocopy" in s and "spies" in s


# ----------------------------------------------------------------------------------------------- X45 / X46 shape
def _skews(s, tmp_path):
    h = _drive_catalog(tmp_path, s)
    return [c[1][0] for c in h.rig.calls if c[0] == "clock_skew"]


def test_x45_skews_back_two_days_on_jan_3(tmp_path):
    s = scn("w4", "X45")
    assert _skews(s, tmp_path) == [D(days=-2)]
    assert any(i.date == "2091-01-03" for i in s.injections)


def test_x46_skews_forward_about_13_months(tmp_path):
    (delta,) = _skews(scn("w4", "X46"), tmp_path)
    assert D(days=390) <= delta <= D(days=400)


# ----------------------------------------------------------------------------------------------- X47 / X48 shape
def test_x47_outage_covers_monday_the_first():
    s = scn("w4", "X47")
    assert dt.date(2091, 10, 1).weekday() == 0
    assert s.outages
    assert any(o.covers(dt.datetime(2091, 10, 1, 8, 30, tzinfo=IST)) for o in s.outages)
    assert any(i.date <= "2091-10-01" for i in s.injections)


def test_x48_lane_window_straddles_the_century():
    s = scn("w4", "X48")
    start = dt.date.fromisoformat(s.start)
    assert dt.date(2099, 12, 1) <= start <= dt.date(2099, 12, 31)
    assert start + D(days=s.days) > dt.date(2100, 1, 1)
    assert len(cal.build_calendar(start, s.days)) == s.days          # the calendar copes with year 2100
    assert any(i.date <= "2100-01-01" for i in s.injections)


# ----------------------------------------------------------------------------------------------- real rig
@pytest.fixture
def rig(tmp_path):
    r = rigmod.LaneRig(tmp_path / "stamp1" / "laneK").build()
    yield r
    r.remove()


def _install_fake_flow(monkeypatch, rig, fn, clock=True):
    mod = types.ModuleType("flow")
    mod.backup = fn
    monkeypatch.setitem(sys.modules, "flow", mod)
    rig._stack = contextlib.ExitStack()
    rig.seams = SimpleNamespace(scheduled_at=None)
    if clock:
        rig.clock = FakeClock()


def test_mark_fact_records(rig):
    assert rig.facts == {}
    rig.mark_fact("x47_outage_injected")
    assert rig.facts == {"x47_outage_injected": True}
    rows = [r for r in _jsonl(rig.evidence_dir / "inputs.jsonl") if r["kind"] == "fact"]
    assert [r["name"] for r in rows] == ["x47_outage_injected"]


# ----------------------------------------------------------------------------------------------- (c) lasting clock
def test_clock_skew_lasts_over_later_run_flow_calls(rig, monkeypatch):
    seen = []
    _install_fake_flow(monkeypatch, rig, lambda **kw: seen.append(rig.clock.now))
    t1 = dt.datetime(2091, 1, 3, 8, 0, tzinfo=IST)
    t2 = dt.datetime(2091, 1, 4, 2, 1, tzinfo=IST)
    rig.run_flow("backup", at=t1, mode="cloud")
    rig.clock_skew(D(days=-2))
    rig.run_flow("backup", at=t2, mode="cloud")
    rig.run_flow("backup", at=t2 + D(days=1), mode="cloud")
    assert seen == [t1, t2 - D(days=2), t2 + D(days=1) - D(days=2)]
    assert rig.clock_offset == D(days=-2)
    assert rig.skewed(t1) == t1 - D(days=2)


def test_clock_skew_accumulates_and_is_recorded(rig, monkeypatch):
    _install_fake_flow(monkeypatch, rig, lambda **kw: None)
    rig.clock_skew(D(days=-2))
    rig.clock_skew(D(days=395))
    assert rig.clock_offset == D(days=393)
    rows = [r for r in _jsonl(rig.evidence_dir / "inputs.jsonl") if r["kind"] == "clock_skew"]
    assert [r["args"] for r in rows] == [[str(D(days=-2))], [str(D(days=395))]]


def test_clock_skew_without_clock_is_an_error(rig):
    with pytest.raises(rigmod.RigError):
        rig.clock_skew(D(days=1))


def test_scheduled_at_is_not_skewed(rig, monkeypatch):
    seen = []
    _install_fake_flow(monkeypatch, rig, lambda **kw: seen.append(rig.seams.scheduled_at))
    rig.clock_skew(D(days=-2))
    t = dt.datetime(2091, 1, 4, 2, 1, tzinfo=IST)
    rig.run_flow("backup", at=t, scheduled_at=t, mode="cloud")
    assert seen == [t]


def test_driver_status_event_uses_skewed_clock(tmp_path, monkeypatch):
    day = cal.build_calendar(dt.date(2091, 1, 5), 1)[0]
    ev = next(e for e in day.events if e.base_kind == cal.STATUS_AM)
    h = _hooks(tmp_path, days=[day])
    h.rig.clock_offset = D(days=-2)
    h._status_event = lambda *a, **k: None
    h._run_event_inner(day, ev, SimpleNamespace())
    assert h.rig.clock.moves == [ev.at - D(days=2)]


def test_driver_flow_event_reports_sim_at(tmp_path):
    day = cal.build_calendar(dt.date(2091, 1, 5), 1)[0]
    ev = next(e for e in day.events if e.base_kind == cal.ROLLOVER_CHECK)
    h = _hooks(tmp_path, days=[day])
    h.rig.clock_offset = D(days=-2)
    calls = []

    def run_flow(name, **kw):
        calls.append(kw["at"])
        return SimpleNamespace(value=None, exc=None)

    h.rig.run_flow = run_flow
    h.rig.ensure_prefix = lambda fy: None
    h.rig.runtime_dir = tmp_path / "rt"
    h._rows_now = lambda: []
    h._event_checks = lambda *a, **k: None
    out = rl().EventOutcome(ev)
    h._run_event_inner(day, ev, out)
    assert calls == [ev.at]                                   # the rig, not the driver, applies the skew
    assert out.sim_at == ev.at - D(days=2)


def test_events_jsonl_carries_sim_at(tmp_path):
    days = cal.build_calendar(dt.date(2091, 1, 5), 1)
    skew = D(days=-2)

    class H:
        def churn(self, day): pass
        def safety_check(self, ev): pass
        def run_event(self, day, ev):
            return SimpleNamespace(sim_at=ev.at + skew, ok=True, error=None, harness_error=None)
        def check_day(self, day, results): return []

    rl().run_lane(days, lane="laneW", stamp="s", agent_runs=tmp_path, hooks=H())
    rows = _jsonl(rl().evidence_dir(tmp_path, "s", "laneW") / "events.jsonl")
    assert rows and all(r["sim_at"] == (dt.datetime.fromisoformat(r["at"]) + skew).isoformat() for r in rows)


# ----------------------------------------------------------------------------------------------- (d) kills
def test_fire_step_marks_fact_only_on_match(rig, monkeypatch):
    monkeypatch.setattr(rig, "kill_lane_processes", lambda: [])
    rig.kill_child_after("final_backups")
    assert rig.fire_step("other") == [] and "kill_child_fired" not in rig.facts
    rig.fire_step("final_backups")
    assert rig.facts.get("kill_child_fired") is True
    assert rig.kill_armed is None


def test_fire_step_never_kills_non_robocopy_rclone_lane_processes(rig):
    assert rigmod.KILLABLE_IMAGES == frozenset({"robocopy.exe", "rclone.exe"})
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(rig.lane_dir)])
    try:
        rig.kill_child_after("final_backups")
        assert victim.pid not in rig.fire_step("final_backups")
        time.sleep(0.3)
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


def test_driver_fires_step_after_journal_step_done(tmp_path, monkeypatch):
    from core.manifest import ManifestDB

    order = []
    monkeypatch.setattr(ManifestDB, "rollover_step_done",
                        lambda self, f, t, step, now: order.append(("real", step)))
    h = _hooks(tmp_path)
    h.rig.fire_step = lambda step: order.append(("fire", step)) or []
    original = ManifestDB.rollover_step_done
    h._install_step_hook()
    try:
        assert ManifestDB.rollover_step_done is not original
        ManifestDB.rollover_step_done(object(), "FY90-91", "FY91-92", "final_backups", "now")
        assert order == [("real", "final_backups"), ("fire", "final_backups")]
    finally:
        h._stack.close()
    assert ManifestDB.rollover_step_done is original


def test_guard_fires_sync_kill_while_robocopy_runs(tmp_path, monkeypatch):
    import core.lan_sync as ls
    import core.tool_runner as tr

    state = {"running": False, "fired_while_running": None}

    def fake_run_tool(name, argv, **kw):
        state["running"] = True
        time.sleep(0.6)
        state["running"] = False
        return SimpleNamespace(exit_code=1)

    monkeypatch.setattr(tr, "run_tool", fake_run_tool)
    monkeypatch.setattr(ls, "run_tool", fake_run_tool)
    h = _hooks(tmp_path)
    h.rig.kill_armed = "sync_robocopy"
    h.rig.kill_delay_seconds = 0.1

    def fire(step):
        state["fired_while_running"] = state["running"]
        h.rig.fired.append(step)
        return []

    h.rig.fire_step = fire
    lane = h.lane_root
    try:
        h._install_run_tool_guard()
        ls.run_tool("robocopy_main", ["robocopy.exe", str(lane / "src"), str(lane / "nas"), "/MIR"])
        time.sleep(0.2)
    finally:
        h._stack.close()
    assert h.rig.fired == ["sync_robocopy"]
    assert state["fired_while_running"] is True


def test_guard_does_not_fire_for_rclone_or_when_not_armed(tmp_path, monkeypatch):
    import core.lan_sync as ls
    import core.tool_runner as tr

    fake = lambda name, argv, **kw: SimpleNamespace(exit_code=0)
    monkeypatch.setattr(tr, "run_tool", fake)
    monkeypatch.setattr(ls, "run_tool", fake)
    h = _hooks(tmp_path)
    h.rig.kill_delay_seconds = 0.0
    lane = h.lane_root
    try:
        h._install_run_tool_guard()
        ls.run_tool("robocopy_main", ["robocopy.exe", str(lane / "src"), str(lane / "nas")])   # not armed
        h.rig.kill_armed = "sync_robocopy"
        ls.run_tool("rclone_sync", ["rclone.exe", "sync", str(lane / "src"), "x:"])            # not robocopy
        time.sleep(0.2)
    finally:
        h._stack.close()
    assert h.rig.fired == []


# ----------------------------------------------------------------------------------------------- X37 / X38 rig
def test_trigger_during_next_flow_fires_while_flow_runs_once(rig, monkeypatch):
    state = {"running": False, "during": []}

    def flow_fn(**kw):
        state["running"] = True
        time.sleep(0.8)
        state["running"] = False

    _install_fake_flow(monkeypatch, rig, flow_fn)
    rig.trigger_delay_seconds = 0.1

    def fire_trigger(leg, **kw):
        state["during"].append((leg, state["running"]))
        return 409

    monkeypatch.setattr(rig, "fire_trigger", fire_trigger)
    t = dt.datetime(2091, 1, 11, 21, 1, tzinfo=IST)
    rig.run_flow("backup", at=t, mode="lan")                      # not armed: nothing
    assert state["during"] == []
    rig.trigger_during_next_flow("lan")
    rig.run_flow("backup", at=t, mode="cloud")                    # other leg: stays armed
    assert state["during"] == []
    rig.run_flow("backup", at=t, mode="lan")
    assert state["during"] == [("lan", True)]
    assert rig.trigger_statuses == [409]
    assert rig.facts.get("trigger_during_flow_fired") is True
    rig.run_flow("backup", at=t, mode="lan")                      # one shot
    assert len(state["during"]) == 1


def test_race_next_flow_runs_copies_together_and_only_once(rig, monkeypatch):
    lock = threading.Lock()
    cur = {"n": 0, "max": 0, "calls": 0}

    def flow_fn(**kw):
        with lock:
            cur["n"] += 1
            cur["calls"] += 1
            cur["max"] = max(cur["max"], cur["n"])
        time.sleep(0.4)
        with lock:
            cur["n"] -= 1
        return "v"

    _install_fake_flow(monkeypatch, rig, flow_fn)
    t = dt.datetime(2091, 1, 11, 21, 1, tzinfo=IST)
    rig.run_flow("backup", at=t, mode="lan")
    assert cur["calls"] == 1 and "race_fired" not in rig.facts
    rig.race_next_flow(copies=2, mode="lan")
    out = rig.run_flow("backup", at=t, mode="lan")
    assert cur["calls"] == 3 and cur["max"] == 2                  # really simultaneous
    assert out.value == "v" and len(rig.race_outcomes) == 1
    assert rig.facts.get("race_fired") is True
    rig.run_flow("backup", at=t, mode="lan")                      # one shot
    assert cur["calls"] == 4


def test_race_captures_exception_of_a_copy(rig, monkeypatch):
    n = {"i": 0}
    lock = threading.Lock()

    def flow_fn(**kw):
        with lock:
            n["i"] += 1
            me = n["i"]
        if me == 2:
            raise RuntimeError("slot busy")
        time.sleep(0.2)

    _install_fake_flow(monkeypatch, rig, flow_fn)
    rig.race_next_flow(copies=2, mode="lan")
    first = rig.run_flow("backup", at=dt.datetime(2091, 1, 11, 21, 1, tzinfo=IST), mode="lan")
    errs = [o.exc for o in [first, *rig.race_outcomes] if o.exc is not None]
    assert len(rig.race_outcomes) == 1 and len(errs) == 1 and "slot busy" in str(errs[0])
