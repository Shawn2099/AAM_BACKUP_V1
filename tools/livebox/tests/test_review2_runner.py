r"""Red-first tests (review round 2): a real catalog scenario runner.

PINNED API
  CLI           run_lane --scenario tools.livebox.scenarios.w1:X04      one catalog Scenario (looked up by id in the
                                                                        module's SCENARIOS list, or as a module attr)
                run_lane --scenario tools.livebox.scenarios.w1:SCENARIOS  every scenario, each in its own lane
                --lane is NOT required with a catalog scenario; lane = scenario.lane, calendar = scenario.start /
                scenario.days / scenario.outages (--days/--start are ignored).  --dry-run prints one plan line per
                scenario (lane name, "<n> days", start date).
  run_lane.load_catalog(spec) -> list[scenarios.base.Scenario]          ([] for a non-catalog spec)
  scenarios.base.Scenario gains   requires: tuple[str, ...] = ()        evidence / knob prerequisites:
                                    "knob:<name>"  inputs.jsonl has a row kind=="knob", name==<name>
                                    "fact:<name>"  results.facts[<name>] is truthy (summary.json / state hooks)
                                    "file:<rel>"   <evidence>/<rel> exists and is not empty
                                  injection_wired: bool = True           False -> always NOT_RUN
  scenarios.results.verdict_for(catalog, evidence_dir) -> {"verdict": PASS|FAIL|FINDING|NOT_RUN,
                                  "failures": list[str], "reason": str}
        NOT_RUN: injection_wired False; any `requires` unmet; expect() returned a str / first list item starting
        'NOT_RUN:'.  FAIL: expect failures (or expect raised) and no expected_red.  FINDING: failures and
        expected_red set.  PASS otherwise.  Never raises.
  after the lane the runner appends to <ev>/checks.jsonl ONE row per scenario
        {"check_id": "SCENARIO:<id>", "verdict": ..., "observed": "<failures or reason>", "day": last day} and merges
        {"scenario_id", "verdict", "failures", "reason"} into <ev>/summary.json (creating it when hooks wrote none).
  exit code     3 when any scenario is FAIL, else 0 (FINDING / NOT_RUN do not fail the run).
  LaneHooks     never crashes on a catalog Scenario / list as `scenario=` (no .injection_dates attribute needed).
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


def rl():
    return importlib.import_module("tools.livebox.run_lane")


def results():
    return importlib.import_module("tools.livebox.scenarios.results")


def base():
    return importlib.import_module("tools.livebox.scenarios.base")


def jl(path: Path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# --------------------------------------------------------------------------------------------- dry run / loading
def test_single_id_dry_run(tmp_path, capsys):
    rc = rl().main(["--scenario", "tools.livebox.scenarios.w1:X04", "--stamp", "t1", "--root", str(tmp_path),
                    "--allow-dev-path", "--dry-run"])
    out = capsys.readouterr().out
    assert "w1_x04" in out and "7 days" in out and "2090-12-29" in out
    assert rc in (0, 2)


def test_scenarios_list_dry_run_one_line_per_scenario(tmp_path, capsys):
    from tools.livebox.scenarios import w3
    rl().main(["--scenario", "tools.livebox.scenarios.w3:SCENARIOS", "--stamp", "t1", "--root", str(tmp_path),
               "--allow-dev-path", "--dry-run"])
    out = capsys.readouterr().out
    for s in w3.SCENARIOS:
        assert s.lane in out, s.lane


def test_load_catalog_id_and_list():
    from tools.livebox.scenarios import w1
    one = rl().load_catalog("tools.livebox.scenarios.w1:X04")
    assert [s.id for s in one] == ["X04"]
    every = rl().load_catalog("tools.livebox.scenarios.w1:SCENARIOS")
    assert [s.id for s in every] == [s.id for s in w1.SCENARIOS]
    assert rl().load_catalog(None) == []


def test_build_days_uses_scenario_start_days_and_outages(tmp_path):
    a = rl().parse_args(["--scenario", "tools.livebox.scenarios.w1:X04", "--stamp", "t1"])
    days = rl().build_days(a)
    assert len(days) == 7 and days[0].date == dt.date(2090, 12, 29)
    assert any(d.outage for d in days)


def test_lane_hooks_do_not_crash_on_catalog_scenarios(tmp_path):
    from tools.livebox.scenarios import w1
    for scn in (w1.SCENARIOS[0], w1.SCENARIOS):
        lane = tmp_path / "s" / "l"
        lane.mkdir(parents=True, exist_ok=True)
        h = rl().LaneHooks(lane, lane="l", stamp="s", days=[], agent_runs=tmp_path / "ar", allow_dev_path=True,
                           scenario=scn)
        assert h is not None


# --------------------------------------------------------------------------------------------- verdict_for
def mk(**kw):
    d = dict(id="S1", wave=1, title="t", start="2091-02-03", days=1, lane="l1", setup=lambda r: None, on_day=None,
             expect=lambda r: [])
    d.update(kw)
    return base().Scenario(**d)


def test_scenario_defaults():
    s = mk()
    assert s.requires == () and s.injection_wired is True


def test_verdict_pass_fail_finding(tmp_path):
    v = results().verdict_for
    assert v(mk(), tmp_path)["verdict"] == "PASS"
    f = v(mk(expect=lambda r: ["boom"]), tmp_path)
    assert f["verdict"] == "FAIL" and f["failures"] == ["boom"]
    g = v(mk(expect=lambda r: ["F-OWN-1 boom"], expected_red="F-OWN-1"), tmp_path)
    assert g["verdict"] == "FINDING" and g["failures"] == ["F-OWN-1 boom"]


def test_expect_that_raises_is_fail_not_crash(tmp_path):
    def bad(r):
        raise RuntimeError("x")
    out = results().verdict_for(mk(expect=bad), tmp_path)
    assert out["verdict"] == "FAIL" and "RuntimeError" in " ".join(out["failures"])


def test_not_run_markers(tmp_path):
    v = results().verdict_for
    assert v(mk(expect=lambda r: "NOT_RUN: needs 2 GB VHD"), tmp_path)["verdict"] == "NOT_RUN"
    assert v(mk(expect=lambda r: ["NOT_RUN: gated"]), tmp_path)["verdict"] == "NOT_RUN"
    assert v(mk(injection_wired=False), tmp_path)["verdict"] == "NOT_RUN"


def test_requires_unmet_is_not_run_even_when_expect_is_empty(tmp_path):
    v = results().verdict_for
    assert v(mk(requires=("file:mail_index.csv",)), tmp_path)["verdict"] == "NOT_RUN"
    assert v(mk(requires=("fact:thing_injected",)), tmp_path)["verdict"] == "NOT_RUN"
    assert v(mk(requires=("knob:set_cloud",)), tmp_path)["verdict"] == "NOT_RUN"
    (tmp_path / "mail_index.csv").write_text("sim_time,subject\n", encoding="utf-8")
    assert v(mk(requires=("file:mail_index.csv",)), tmp_path)["verdict"] == "PASS"


def test_requires_fact_and_knob_met(tmp_path):
    (tmp_path / "summary.json").write_text(json.dumps({"thing_injected": True}), encoding="utf-8")
    (tmp_path / "inputs.jsonl").write_text(
        json.dumps({"seq": 1, "kind": "knob", "name": "set_cloud", "args": ["blackhole"]}) + "\n", encoding="utf-8")
    s = mk(requires=("fact:thing_injected", "knob:set_cloud"))
    assert results().verdict_for(s, tmp_path)["verdict"] == "PASS"


def test_requires_knob_ignores_non_knob_kinds(tmp_path):
    (tmp_path / "inputs.jsonl").write_text(
        json.dumps({"seq": 1, "kind": "clock_proof", "name": "set_cloud", "args": []}) + "\n", encoding="utf-8")
    assert results().verdict_for(mk(requires=("knob:set_cloud",)), tmp_path)["verdict"] == "NOT_RUN"


@pytest.mark.parametrize("modname,sid,flag", [
    ("w4", "X42", "x42_injected"), ("w4", "X45", "clock_back_injected"), ("w4", "X46", "clock_jump_injected"),
    ("w4", "X47", "x47_outage_injected"), ("w4", "X48", "x48_century_window"),
])
def test_w4_injected_scenarios_not_run_without_flag(tmp_path, modname, sid, flag):
    mod = importlib.import_module(f"tools.livebox.scenarios.{modname}")
    scn = next(s for s in mod.SCENARIOS if s.id == sid)
    assert results().verdict_for(scn, tmp_path)["verdict"] == "NOT_RUN"
    (tmp_path / "summary.json").write_text(json.dumps({flag: True}), encoding="utf-8")
    assert results().verdict_for(scn, tmp_path)["verdict"] != "NOT_RUN"


@pytest.mark.parametrize("modname,sid", [("w3", "X37"), ("w3", "X38"), ("w3", "X39"), ("w1", "X17")])
def test_unwired_injection_scenarios_are_not_run(tmp_path, modname, sid):
    mod = importlib.import_module(f"tools.livebox.scenarios.{modname}")
    scn = next(s for s in mod.SCENARIOS if s.id == sid)
    assert scn.injection_wired is False
    assert results().verdict_for(scn, tmp_path)["verdict"] == "NOT_RUN"


# --------------------------------------------------------------------------------------------- full runner, fake hooks
PACK = textwrap.dedent('''
    from tools.livebox.scenarios.base import Scenario

    def _s(sid, lane, expect, **kw):
        return Scenario(id=sid, wave=1, title=sid, start="2091-02-03", days=2, lane=lane, setup=lambda r: None,
                        on_day=None, expect=expect, **kw)

    OK = _s("P01", "p01_ok", lambda r: [])
    BAD = _s("P02", "p02_bad", lambda r: ["P02: wrong"])
    RED = _s("P03", "p03_red", lambda r: ["P03: F-OWN-1 known"], expected_red="F-OWN-1")
    GATED = _s("P04", "p04_gated", lambda r: [], requires=("fact:never_set",))
    MARK = _s("P05", "p05_marker", lambda r: "NOT_RUN: gated")
    UNWIRED = _s("P06", "p06_unwired", lambda r: ["P06: would fail"], injection_wired=False)
    SCENARIOS = [OK, BAD, RED, GATED, MARK, UNWIRED]
''')


class FakeHooks:
    def churn(self, day):
        pass

    def safety_check(self, ev):
        pass

    def run_event(self, day, ev):
        return SimpleNamespace(resources=None, harness_error=None, shared=False, checks=[])

    def check_day(self, day, results):
        return []


def run_pack(tmp_path, monkeypatch, spec):
    (tmp_path / "pk").mkdir(exist_ok=True)
    (tmp_path / "pk" / "fake_pack_r2.py").write_text(PACK, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "pk"))
    sys.modules.pop("fake_pack_r2", None)
    ar = tmp_path / "ar"
    rc = rl().main(["--scenario", spec, "--stamp", "t1", "--root", str(tmp_path / "root"), "--allow-dev-path",
                    "--agent-runs", str(ar)], hooks_factory=lambda lane_root: FakeHooks(), agent_runs=ar)
    return rc, ar / "livebox_t1"


def test_runner_runs_every_scenario_in_its_own_lane_with_verdicts(tmp_path, monkeypatch):
    rc, evroot = run_pack(tmp_path, monkeypatch, "fake_pack_r2:SCENARIOS")
    want = {"p01_ok": "PASS", "p02_bad": "FAIL", "p03_red": "FINDING", "p04_gated": "NOT_RUN",
            "p05_marker": "NOT_RUN", "p06_unwired": "NOT_RUN"}
    for lane, verdict in want.items():
        ev = evroot / lane
        assert ev.is_dir(), lane
        summ = json.loads((ev / "summary.json").read_text(encoding="utf-8"))
        assert summ["verdict"] == verdict, (lane, summ)
        row = [r for r in jl(ev / "checks.jsonl") if str(r.get("check_id", "")).startswith("SCENARIO:")]
        assert len(row) == 1 and row[0]["verdict"] == verdict, (lane, row)
        assert len(jl(ev / "events.jsonl")) > 0
    assert rc == 3          # P02 is a FAIL


def test_runner_single_id_runs_only_that_scenario(tmp_path, monkeypatch):
    rc, evroot = run_pack(tmp_path, monkeypatch, "fake_pack_r2:OK")
    assert [p.name for p in evroot.iterdir()] == ["p01_ok"]
    assert rc == 0


def test_runner_finding_and_not_run_do_not_fail_the_exit_code(tmp_path, monkeypatch):
    rc, evroot = run_pack(tmp_path, monkeypatch, "fake_pack_r2:RED")
    assert rc == 0
    rc2, _ = run_pack(tmp_path, monkeypatch, "fake_pack_r2:GATED")
    assert rc2 == 0


def test_not_run_scenario_failures_are_not_reported_as_fail(tmp_path, monkeypatch):
    _, evroot = run_pack(tmp_path, monkeypatch, "fake_pack_r2:SCENARIOS")
    rows = jl(evroot / "p06_unwired" / "checks.jsonl")
    assert not any(r.get("verdict") == "FAIL" and str(r.get("check_id", "")).startswith("SCENARIO:") for r in rows)
