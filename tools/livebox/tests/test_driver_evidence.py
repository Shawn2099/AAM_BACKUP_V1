r"""Red-first tests for the driver side of the w1-w4 scenario needs (run_lane.py, scenarios/base.py, scenarios/results.py).

PINNED API
  run_tool guard (LaneHooks._install_run_tool_guard): every robocopy call appends one row to
      <ev_dir>\robocopy_runs.jsonl  {"start": ISO IST, "end": ISO IST, "argv": [..]}  (start <= end, written even
      when the tool raises, rclone is NOT recorded).
  summary.json (LaneHooks._write_summary) gains: orphan_robocopy (list, from LaneHooks._orphan_robocopy() -> list[int]
      = robocopy pids whose cmdline contains the lane root, still alive at close), created_paths (list[str]),
      junction_target_entries (list[str]), switch_before_manual_lan (bool), spies (dict wake/shutdown/archive counts);
      the three lists and the bool are LaneHooks attributes of the same names; wake_calls / shutdown_calls stay.
  scenarios.base.Scenario gains   outages: tuple = ()   (cal.Outage items), lateness: Mapping[str, timedelta] = {},
      injections: tuple[Injection, ...] = ()  with  scenarios.base.Injection(date: "YYYY-MM-DD", time: "HH:MM" IST,
      fn: Callable[[rig], None]).
  run_lane.Scenario gains the same three fields; results.to_driver_scenario(catalog) copies them;
      run_lane.build_days(a) adds the outages of the --scenario to those of --outage.
  LaneHooks._scheduled_at(ev): key f"{ev.at.date().isoformat()} {ev.kind}" in scenario.lateness -> ev.at - delta.
  LaneHooks._apply_injections(at: datetime) -> list[str]: runs, once each and in time order, every injection whose
      date+time (IST) <= `at`, passing hooks.rig; returns the "date time" strings applied this call.
      run_event calls it with ev.at before the flow.
  LaneHooks.churn(day): the day's churn (_apply_churn) is applied BEFORE scenario.before_day runs, so guard injections
      (wipe, mass delete, shrink) are never undone by the same day's churn.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.livebox import calendar as cal

IST = cal.IST


def rl():
    return importlib.import_module("tools.livebox.run_lane")


def base():
    return importlib.import_module("tools.livebox.scenarios.base")


def results():
    return importlib.import_module("tools.livebox.scenarios.results")


def _hooks(tmp_path, monkeypatch=None, **kw):
    lane = tmp_path / "stamp1" / "laneA"
    lane.mkdir(parents=True, exist_ok=True)
    h = rl().LaneHooks(lane, lane="laneA", stamp="stamp1", days=kw.pop("days", []), agent_runs=tmp_path / "ar",
                       allow_dev_path=True, **kw)
    h.ev_dir.mkdir(parents=True, exist_ok=True)
    return h, lane


def _jsonl(p):
    return [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]


# ------------------------------------------------------------------------------------------ robocopy_runs.jsonl
def test_guard_records_every_robocopy_call_with_start_end_argv(tmp_path, monkeypatch):
    import core.lan_sync as ls
    import core.tool_runner as tr

    calls = []

    def fake_run_tool(name, argv, **kw):
        calls.append(argv)
        return SimpleNamespace(exit_code=1)

    monkeypatch.setattr(tr, "run_tool", fake_run_tool)
    monkeypatch.setattr(ls, "run_tool", fake_run_tool)
    h, lane = _hooks(tmp_path)
    try:
        h._install_run_tool_guard()
        argv = ["robocopy.exe", str(lane / "src"), str(lane / "nas"), "/MIR"]
        ls.run_tool("robocopy_main", argv)
        ls.run_tool("robocopy_main", argv)
        ls.run_tool("rclone_sync", ["rclone.exe", "sync", str(lane / "src"), "x:"])
    finally:
        h._stack.close()
    rows = _jsonl(h.ev_dir / "robocopy_runs.jsonl")
    assert len(rows) == 2 and len(calls) == 3
    for r in rows:
        assert set(r) >= {"start", "end", "argv"} and r["argv"] == argv
        assert dt.datetime.fromisoformat(r["start"]) <= dt.datetime.fromisoformat(r["end"])


def test_guard_records_robocopy_even_when_tool_raises(tmp_path, monkeypatch):
    import core.lan_sync as ls
    import core.tool_runner as tr

    def boom(name, argv, **kw):
        raise TimeoutError("killed")

    monkeypatch.setattr(tr, "run_tool", boom)
    monkeypatch.setattr(ls, "run_tool", boom)
    h, lane = _hooks(tmp_path)
    try:
        h._install_run_tool_guard()
        with pytest.raises(TimeoutError):
            ls.run_tool("robocopy_main", ["robocopy.exe", str(lane / "src"), str(lane / "nas")])
    finally:
        h._stack.close()
    assert len(_jsonl(h.ev_dir / "robocopy_runs.jsonl")) == 1


# ------------------------------------------------------------------------------------------ summary.json
def test_summary_has_new_evidence_keys(tmp_path):
    h, lane = _hooks(tmp_path)
    h.rig = SimpleNamespace(seams=SimpleNamespace(wake_calls=[1, 2], shutdown_calls=[1], archive_calls=[]))
    h._orphan_robocopy = lambda: [4242]
    h.created_paths = [str(lane / "src" / "FY91-92")]
    h.junction_target_entries = []
    h.switch_before_manual_lan = True
    h._write_summary()
    s = json.loads((h.ev_dir / "summary.json").read_text(encoding="utf-8"))
    assert s["orphan_robocopy"] == [4242]
    assert s["created_paths"] == [str(lane / "src" / "FY91-92")]
    assert s["junction_target_entries"] == []
    assert s["switch_before_manual_lan"] is True
    assert s["wake_calls"] == 2 and s["shutdown_calls"] == 1
    assert s["spies"] == {"wake": 2, "shutdown": 1, "archive": 0}


def test_summary_keys_exist_with_neutral_defaults(tmp_path):
    h, _ = _hooks(tmp_path)
    h.rig = SimpleNamespace(seams=SimpleNamespace(wake_calls=[], shutdown_calls=[], archive_calls=[]))
    h._orphan_robocopy = lambda: []
    h._write_summary()
    s = json.loads((h.ev_dir / "summary.json").read_text(encoding="utf-8"))
    assert s["orphan_robocopy"] == [] and s["created_paths"] == [] and s["junction_target_entries"] == []
    assert s["switch_before_manual_lan"] is False


def test_orphan_robocopy_only_lists_lane_processes(tmp_path):
    h, _ = _hooks(tmp_path)
    assert h._orphan_robocopy() == []                      # nothing of this lane is running


# ------------------------------------------------------------------------------------------ outages on Scenario
def _catalog(**kw):
    b = base()
    return b.Scenario(id="X99", wave=1, title="t", start="2090-12-28", days=6, lane="w1_x99", setup=lambda r: None,
                      on_day=None, expect=lambda r: [], **kw)


def test_base_scenario_new_fields_default_empty():
    s = _catalog()
    assert s.outages == () and dict(s.lateness) == {} and s.injections == ()


def test_to_driver_scenario_copies_new_fields():
    out = cal.Outage(dt.date(2090, 12, 30), dt.date(2090, 12, 31), "latest")
    inj = base().Injection("2090-12-30", "03:00", lambda rig: None)
    s = _catalog(outages=(out,), lateness={"2090-12-30 rollover_check": dt.timedelta(hours=8)}, injections=(inj,))
    d = results().to_driver_scenario(s)
    assert d.outages == (out,) and d.injections == (inj,)
    assert d.lateness == {"2090-12-30 rollover_check": dt.timedelta(hours=8)}


def test_w1_outage_scenarios_carry_their_outages():
    w1 = importlib.import_module("tools.livebox.scenarios.w1")
    by = {s.id: s for s in w1.SCENARIOS}
    for sid in ("X04", "X05", "X06"):
        assert tuple(by[sid].outages) == tuple(w1.OUTAGES[sid]), sid


def test_build_days_applies_scenario_outages():
    mod = types.ModuleType("livebox_fake_scn")
    mod.SCENARIO = rl().Scenario(name="o", outages=(cal.Outage(dt.date(2090, 12, 30), dt.date(2090, 12, 31), "none"),))
    sys.modules["livebox_fake_scn"] = mod
    try:
        a = rl().parse_args(["--lane", "l", "--stamp", "s", "--days", "6", "--start", "2090-12-28",
                             "--scenario", "livebox_fake_scn:SCENARIO"])
        days = rl().build_days(a)
    finally:
        sys.modules.pop("livebox_fake_scn", None)
    assert [d.outage for d in days if d.date == dt.date(2090, 12, 30)] == [True]
    assert [d.outage for d in days if d.date == dt.date(2090, 12, 28)] == [False]


# ------------------------------------------------------------------------------------------ lateness override (X07)
def _event(kind, date=dt.date(2091, 1, 5)):
    day = cal.build_calendar(date, 1)[0]
    return next(e for e in day.events if e.base_kind == kind)


def test_scheduled_at_lateness_override(tmp_path):
    ev = _event(cal.ROLLOVER_CHECK)
    key = f"{ev.at.date().isoformat()} {ev.kind}"
    h, _ = _hooks(tmp_path, scenario=rl().Scenario(name="x07", lateness={key: dt.timedelta(hours=8)}))
    assert h._scheduled_at(ev) == ev.at - dt.timedelta(hours=8)


def test_scheduled_at_without_override_unchanged(tmp_path):
    ev = _event(cal.ROLLOVER_CHECK)
    h, _ = _hooks(tmp_path, scenario=rl().Scenario(name="none"))
    assert h._scheduled_at(ev) == ev.at


# ------------------------------------------------------------------------------------------ granular injections
def test_apply_injections_in_time_order_once_each(tmp_path):
    applied = []
    B = base()
    inj = (B.Injection("2091-01-05", "07:30", lambda rig: applied.append("late")),
           B.Injection("2091-01-05", "03:00", lambda rig: applied.append("early")),
           B.Injection("2091-01-06", "01:00", lambda rig: applied.append("tomorrow")))
    h, _ = _hooks(tmp_path, scenario=rl().Scenario(name="t", injections=inj))
    h.rig = object()
    at = lambda hh, mm, d=5: dt.datetime(2091, 1, d, hh, mm, tzinfo=IST)
    assert h._apply_injections(at(2, 0)) == []
    assert h._apply_injections(at(6, 0)) == ["2091-01-05 03:00"]
    assert h._apply_injections(at(6, 0)) == []                        # never twice
    assert h._apply_injections(at(8, 0)) == ["2091-01-05 07:30"]
    assert applied == ["early", "late"]
    assert h._apply_injections(at(1, 0, 6)) == ["2091-01-06 01:00"]


def test_injection_receives_the_rig(tmp_path):
    got = []
    inj = (base().Injection("2091-01-05", "03:00", lambda rig: got.append(rig)),)
    h, _ = _hooks(tmp_path, scenario=rl().Scenario(name="t", injections=inj))
    h.rig = SimpleNamespace(tag="rig")
    h._apply_injections(dt.datetime(2091, 1, 5, 4, 0, tzinfo=IST))
    assert got == [h.rig]


# ------------------------------------------------------------------------------------------ churn before injections
def test_day_churn_runs_before_before_day_hook(tmp_path):
    order = []
    day = cal.build_calendar(dt.date(2091, 1, 5), 1)[0]
    sc = rl().Scenario(name="wipe", before_day=lambda ctx, d: order.append("before_day"))
    h, _ = _hooks(tmp_path, scenario=sc)
    h.rig = SimpleNamespace(clock=None)
    h._apply_churn = lambda d: order.append("churn") or []
    h.churn(day)
    assert order == ["churn", "before_day"]
    assert h.pending_churn is None                                   # not applied a second time at noon
