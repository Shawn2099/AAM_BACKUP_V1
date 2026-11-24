"""Start-year derived FY leaf, progress.json heartbeat, flow_state in events.jsonl, flow counts in the report."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.livebox import calendar as cal
from tools.livebox import run_lane as rl
from tools.livebox import safety
from tools.livebox.derive_config import derive_config
from tools.livebox.rig import dev_prod_raw


# ------------------------------------------------------------------ FY derived from the start date
@pytest.mark.parametrize("start,fy", [
    ("2026-10-01", "FY26-27"), ("2027-01-15", "FY26-27"), ("2027-03-31", "FY26-27"),
    ("2027-04-01", "FY27-28"), ("2090-12-01", "FY90-91"), ("2099-12-31", "FY99-00"),
])
def test_fy_leaf_from_start(start, fy):
    assert cal.fy_of_date(dt.date.fromisoformat(start)) == fy


def test_sandbox_leaves_from_wraps_the_century():
    assert safety.sandbox_leaves_from("FY26-27") == ("FY26-27", "FY27-28", "FY28-29", "FY29-30")
    assert safety.sandbox_leaves_from("FY98-99", 3) == ("FY98-99", "FY99-00", "FY00-01")


def test_derive_config_uses_test_fy_and_stays_under_the_lane(tmp_path):
    lane = Path("C:/AAMLivebox/stamp1/laneA") if False else tmp_path / "stamp1" / "laneA"
    raw = derive_config(dev_prod_raw(), lane, smtp_port=1, test_fy="FY26-27")
    assert Path(raw["paths"]["source_drive"]).name == "FY26-27"
    assert raw["paths"]["lan_destination"].endswith("FY26-27")
    default = derive_config(dev_prod_raw(), lane, smtp_port=1)       # no override: still the 2090 leaf
    assert Path(default["paths"]["source_drive"]).name == "FY90-91"


def test_s4_derived_leaves_accept_a_2026_leaf_even_when_production_has_that_name():
    safety.check_fy_leaves(["FY26-27"], ["FY26-27"], sandbox_leaves=safety.sandbox_leaves_from("FY26-27"))
    with pytest.raises(safety.SafetyRefusal):
        safety.check_fy_leaves(["FY25-26"], [], sandbox_leaves=safety.sandbox_leaves_from("FY26-27"))
    with pytest.raises(safety.SafetyRefusal):                       # the old list is unchanged
        safety.check_fy_leaves(["FY26-27"], [])


def test_rig_paths_follow_test_fy(tmp_path):
    from tools.livebox import rig as rigmod

    r = rigmod.LaneRig(tmp_path / "stamp1" / "laneX", test_fy="FY26-27")
    assert r.source_dir.name == "FY26-27" and r.nas_dir.name == "FY26-27"
    assert r.bucket_prefix().name == "FY26-27"
    assert rigmod.LaneRig(tmp_path / "stamp1" / "laneY").source_dir.name == "FY90-91"


# ------------------------------------------------------------------ progress.json + flow_state
class _Hooks:
    def __init__(self, outs):
        self.outs = list(outs)

    def churn(self, day):
        pass

    def safety_check(self, ev):
        pass

    def run_event(self, day, ev):
        return self.outs.pop(0)

    def check_day(self, day, results):
        return []


def _run(tmp_path, outcome_for_all):
    day = cal.build_calendar(dt.date(2026, 10, 5), 1)[0]
    outs = [outcome_for_all for _ in day.events]
    ar = tmp_path / "ar"
    rl.run_lane([day], lane="L", stamp="s", agent_runs=ar, hooks=_Hooks(outs))
    ev = rl.evidence_dir(ar, "s", "L")
    return ev, day


def _jl(p):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_progress_json_written_and_final(tmp_path):
    ev, day = _run(tmp_path, SimpleNamespace(ok=True, error=None, harness_error=None, value=None, resources=None))
    prog = json.loads((ev / "progress.json").read_text(encoding="utf-8"))
    assert prog["status"] == "completed"
    assert prog["events_done"] == len(day.events) and prog["days_total"] == 1
    assert prog["last_event"] == day.events[-1].kind and "real_time" in prog
    assert not list(ev.glob("progress.tmp"))                      # atomic: no leftover temp file


def test_flow_state_completed_failed_crashed(tmp_path):
    ok = SimpleNamespace(ok=True, error=None, harness_error=None, value="x", resources=None,
                         flow=SimpleNamespace(exc=None))
    ev, _ = _run(tmp_path / "a", ok)
    assert {r["flow_state"] for r in _jl(ev / "events.jsonl")} == {"Completed"}

    failed = SimpleNamespace(ok=True, error=None, harness_error=None, value=None, resources=None,
                             flow=SimpleNamespace(exc=RuntimeError("boom")))
    ev, _ = _run(tmp_path / "b", failed)
    rows = _jl(ev / "events.jsonl")
    assert {r["flow_state"] for r in rows} == {"Failed"} and rows[0]["exc_type"] == "RuntimeError"
    assert rows[0]["ok"] is True                                  # `ok` keeps its meaning

    crashed = SimpleNamespace(ok=False, error={"type": "KeyError", "message": "a"}, harness_error="tb",
                              value=None, resources=None)
    ev, _ = _run(tmp_path / "c", crashed)
    rows = _jl(ev / "events.jsonl")
    assert {r["flow_state"] for r in rows} == {"Crashed"} and rows[0]["exc_type"] == "KeyError"


def test_report_counts_flows_from_flow_state(tmp_path):
    from tools.livebox import make_report as mr

    lane = tmp_path / "L"
    lane.mkdir()
    (lane / "events.jsonl").write_text(
        '{"flow_state": "Completed"}\n{"flow_state": "Failed"}\n{"flow_state": "Failed"}\nnot json\n',
        encoding="utf-8")
    assert mr._flow_counts(lane) == "Completed 1, Failed 2"
    assert "n/a" in mr._flow_counts(tmp_path / "nope")
