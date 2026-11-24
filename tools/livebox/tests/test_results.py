r"""Tests for tools.livebox.scenarios.results (red-first; module not written yet).

build_results(evidence_dir) -> Results reads run_lane evidence (events.jsonl, days.jsonl, status/*.json,
mail_index.csv, mail/*.eml, summary.json, state.json, checks.jsonl, optional approvals.jsonl /
robocopy_runs.jsonl). Every file is optional; unknown/corrupt files are ignored; nothing raises.

FINAL Results FIELDS (all default empty/falsy; empty_results() returns them all):
  rows        list[dict]  one per distinct run (dedup by leg+started_at) from status/*.json recent_runs,
                          sorted by started_at:
                          {day (ISO of started_at), leg ("cloud"|"lan"), fy, status, fault (fault_code or None),
                           started_at, files}
  mails       list[dict]  one per mail_index.csv row: {day (ISO of sim_time), subject, rcpts, severity,
                          code, key, kind, fault}; severity/code/key/kind/fault come from the x_headers
                          column ("k=v;k=v", lower-cased keys; severity upper-cased), default "" / None
  approvals   list[dict]  approvals.jsonl rows {phrase, http_status, applied, fy, leg, status, http}
  notes       list[str]   "<check_id>: <observed>" for every checks.jsonl row whose verdict != PASS
  facts       dict        summary.json top-level values (wake_calls, shutdown_calls, ...) merged over
                          state.json "hooks" values
  config_fy_by_day dict[str,str]  day -> fy_prefix of that day's last status file
  activated_on list[str]  days (ISO of overlap.activated_at) the next FY was activated, unique, sorted
  switched_on  list[str]  days on which config_fy_by_day changed versus the previous day
  clock_proven_by_day dict[str,bool]  day -> cloud run's extended_metrics clock.state == "OK"
  created_paths list[str]; junction_target_entries list[str]   from summary.json keys of the same name
  archive_calls list; orphan_robocopy list                    from summary.json keys of the same name
  robocopy_runs list[dict]  robocopy_runs.jsonl ({start, end, argv})
  lan_runs_0600 int         events kind=="lan" whose "at" time is 06:00-06:59
  switch_before_manual_lan bool   summary.json key of the same name
Adapter: to_driver_scenario(catalog) -> run_lane.Scenario(name=id, before_day=...);
         judge(catalog, evidence_dir) -> list[str] = expect(build_results(evidence_dir)) (None -> []).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

FIELDS = {
    "rows": [], "mails": [], "approvals": [], "notes": [], "facts": {}, "config_fy_by_day": {},
    "activated_on": [], "switched_on": [], "clock_proven_by_day": {}, "created_paths": [],
    "junction_target_entries": [], "archive_calls": [], "orphan_robocopy": [], "robocopy_runs": [],
    "lan_runs_0600": 0, "switch_before_manual_lan": False,
}


def mod():
    from tools.livebox.scenarios import results
    return results


def jl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def run(mode, started, status, fy=None, fault=None, clock="OK", files=3):
    return {"mode": mode, "status": status, "fault_code": fault, "started_at": started, "files": files, "fy": fy,
            "extended_metrics": json.dumps({"clock": {"state": clock}}) if mode == "cloud" else "{}"}


def status(fy, runs, activated_at=None):
    return {"firm": "X", "fy_prefix": fy, "overlap": {"configured_fy": fy, "activated_at": activated_at},
            "recent_runs": runs}


def write_status(ev, name, doc):
    d = ev / "status"
    d.mkdir(exist_ok=True)
    (d / name).write_text(json.dumps(doc), encoding="utf-8")


CSV_HEAD = "sim_time,real_time,rcpts,subject,from,x_headers\n"


def test_empty_dir_gives_empty_results(tmp_path):
    r = mod().build_results(tmp_path)
    for k, v in FIELDS.items():
        assert getattr(r, k) == v, k


def test_empty_results_has_every_field():
    r = mod().empty_results()
    for k, v in FIELDS.items():
        assert getattr(r, k) == v, k


def test_empty_results_instances_are_independent():
    a, b = mod().empty_results(), mod().empty_results()
    a.rows.append({"x": 1})
    a.facts["k"] = 1
    assert b.rows == [] and b.facts == {}


def test_missing_directory_tolerated(tmp_path):
    r = mod().build_results(tmp_path / "nope")
    assert r.rows == [] and r.mails == []


def test_rows_parsed_deduped_from_status(tmp_path):
    a = run("lan", "2090-12-15 21:01 IST", "LAN_COMPLETE", fy="FY90-91")
    b = run("cloud", "2090-12-15 02:01 IST", "CLOUD_FAILED", fault="F-X")
    write_status(tmp_path, "2090-12-15_2330.json", status("FY90-91", [a, b]))
    write_status(tmp_path, "2090-12-16_0900.json", status("FY90-91", [a, b]))
    r = mod().build_results(tmp_path)
    assert len(r.rows) == 2
    lan = next(x for x in r.rows if x["leg"] == "lan")
    cloud = next(x for x in r.rows if x["leg"] == "cloud")
    assert lan["day"] == "2090-12-15" and lan["status"] == "LAN_COMPLETE" and lan["fy"] == "FY90-91"
    assert lan["fault"] is None
    assert cloud["fault"] == "F-X" and cloud["status"] == "CLOUD_FAILED"


def test_rows_sorted_by_day(tmp_path):
    late = run("lan", "2090-12-16 21:01 IST", "LAN_COMPLETE")
    early = run("cloud", "2090-12-15 02:01 IST", "CLOUD_COMPLETE")
    write_status(tmp_path, "2090-12-16_2330.json", status("FY90-91", [late, early]))
    r = mod().build_results(tmp_path)
    assert [x["day"] for x in r.rows] == ["2090-12-15", "2090-12-16"]


def test_config_fy_switch_and_activation(tmp_path):
    write_status(tmp_path, "2091-03-31_2330.json", status("FY90-91", [], activated_at="2091-03-20T06:00:00+05:30"))
    write_status(tmp_path, "2091-04-01_0900.json", status("FY91-92", []))
    write_status(tmp_path, "2091-04-01_2330.json", status("FY91-92", []))
    r = mod().build_results(tmp_path)
    assert r.config_fy_by_day == {"2091-03-31": "FY90-91", "2091-04-01": "FY91-92"}
    assert r.switched_on == ["2091-04-01"]
    assert r.activated_on == ["2091-03-20"]


def test_clock_proven_by_day(tmp_path):
    ok = run("cloud", "2090-12-15 02:01 IST", "CLOUD_COMPLETE", clock="OK")
    bad = run("cloud", "2090-12-16 02:01 IST", "CLOUD_COMPLETE", clock="UNPROVEN")
    write_status(tmp_path, "2090-12-16_2330.json", status("FY90-91", [ok, bad]))
    r = mod().build_results(tmp_path)
    assert r.clock_proven_by_day == {"2090-12-15": True, "2090-12-16": False}


def test_mails_parsed_with_headers(tmp_path):
    (tmp_path / "mail_index.csv").write_text(
        CSV_HEAD
        + "2090-12-18T08:00:00+05:30,2090-12-18T02:30:00+00:00,partners@livebox.invalid,"
          "[LIVEBOX Backup] Weekly report,livebox@livebox.invalid,\n"
        + "2090-12-19T09:00:00+05:30,2090-12-19T03:30:00+00:00,a@b.invalid,Alert,x@y.invalid,"
          "severity=action_required;code=FY_X;key=k1\n", encoding="utf-8")
    r = mod().build_results(tmp_path)
    assert len(r.mails) == 2
    m0, m1 = r.mails
    assert m0["day"] == "2090-12-18" and m0["subject"] == "[LIVEBOX Backup] Weekly report"
    assert m0["rcpts"] == "partners@livebox.invalid"
    assert m0["severity"] == "" and m0["code"] == "" and m0["fault"] is None
    assert m1["day"] == "2090-12-19" and m1["severity"] == "ACTION_REQUIRED"
    assert m1["code"] == "FY_X" and m1["key"] == "k1"


def test_notes_from_non_pass_checks(tmp_path):
    jl(tmp_path / "checks.jsonl", [
        {"check_id": "LOCK", "verdict": "PASS", "observed": "absent"},
        {"check_id": "FLOW_STATE", "verdict": "FINDING", "observed": "audit raised"},
        {"check_id": "TREE_FULL", "verdict": "FAIL", "observed": "diff"},
    ])
    r = mod().build_results(tmp_path)
    assert r.notes == ["FLOW_STATE: audit raised", "TREE_FULL: diff"]


def test_summary_and_state_feed_facts_and_lists(tmp_path):
    (tmp_path / "summary.json").write_text(json.dumps({
        "wake_calls": 48, "archive_calls": ["a1"], "orphan_robocopy": ["robocopy x"],
        "created_paths": ["p"], "junction_target_entries": ["e"], "switch_before_manual_lan": True,
        "seconds_per_day": {"d": 1.0}}), encoding="utf-8")
    (tmp_path / "state.json").write_text(json.dumps({"hooks": {"lan_flows": 30, "wake_calls": 1}}), encoding="utf-8")
    r = mod().build_results(tmp_path)
    assert r.archive_calls == ["a1"] and r.orphan_robocopy == ["robocopy x"]
    assert r.created_paths == ["p"] and r.junction_target_entries == ["e"]
    assert r.switch_before_manual_lan is True
    assert r.facts["wake_calls"] == 48          # summary wins over state hooks
    assert r.facts["lan_flows"] == 30


def test_approvals_and_robocopy_runs_optional_files(tmp_path):
    jl(tmp_path / "approvals.jsonl", [{"phrase": "I APPROVE", "http_status": 200, "applied": True}])
    jl(tmp_path / "robocopy_runs.jsonl",
       [{"start": "2090-12-15T21:01:00", "end": "2090-12-15T21:02:00", "argv": "robocopy"}])
    r = mod().build_results(tmp_path)
    assert r.approvals[0]["applied"] is True and r.approvals[0]["http_status"] == 200
    assert r.robocopy_runs[0]["argv"] == "robocopy"


def test_lan_runs_0600_counted_from_events(tmp_path):
    jl(tmp_path / "events.jsonl", [
        {"day": "2090-12-15", "kind": "lan", "at": "2090-12-15T21:01:00+05:30"},
        {"day": "2090-12-16", "kind": "lan", "at": "2090-12-16T06:00:00+05:30"},
        {"day": "2090-12-16", "kind": "rollover_check", "at": "2090-12-16T06:00:00+05:30"},
    ])
    assert mod().build_results(tmp_path).lan_runs_0600 == 1


def test_corrupt_and_unknown_files_tolerated(tmp_path):
    (tmp_path / "events.jsonl").write_text("{bad\n\n", encoding="utf-8")
    (tmp_path / "checks.jsonl").write_text("not json\n", encoding="utf-8")
    (tmp_path / "summary.json").write_text("[1,2]", encoding="utf-8")
    (tmp_path / "state.json").write_text("{", encoding="utf-8")
    (tmp_path / "mail_index.csv").write_text("", encoding="utf-8")
    (tmp_path / "surprise.bin").write_bytes(b"\x00\xff")
    write_status(tmp_path, "garbage.json", {"unexpected": 1})
    (tmp_path / "status" / "broken.json").write_text("{", encoding="utf-8")
    r = mod().build_results(tmp_path)
    assert r.rows == [] and r.mails == [] and r.notes == [] and r.facts == {}


def test_results_accepts_string_path(tmp_path):
    assert mod().build_results(str(tmp_path)).rows == []


def test_expect_style_plain_attribute_access(tmp_path):
    r = mod().build_results(tmp_path)
    assert [x for x in r.rows if x["leg"] == "lan"] == []
    assert r.facts.get("anything") is None


# ------------------------------------------------------------------ adapter
def catalog(calls, **kw):
    from tools.livebox.scenarios.base import Scenario as S
    base = dict(id="X99", wave=1, title="t", start="2090-12-01", days=3, lane="x99",
                setup=lambda rig: calls.append(("setup", rig)),
                on_day=lambda rig, day: calls.append(("on_day", rig, day)),
                expect=lambda res: calls.append(("expect", res)) or ["boom"])
    base.update(kw)
    return S(**base)


def test_adapter_returns_driver_scenario():
    from tools.livebox import run_lane
    sc = mod().to_driver_scenario(catalog([]))
    assert isinstance(sc, run_lane.Scenario)
    assert sc.name == "X99" and callable(sc.before_day)


def test_adapter_setup_once_then_on_day_in_order():
    calls = []
    sc = mod().to_driver_scenario(catalog(calls))
    rig = object()
    ctx = SimpleNamespace(rig=rig)
    for d in ("d1", "d2", "d3"):
        sc.before_day(ctx, d)
    assert calls == [("setup", rig), ("on_day", rig, "d1"), ("on_day", rig, "d2"), ("on_day", rig, "d3")]


def test_adapter_without_on_day_still_runs_setup():
    calls = []
    sc = mod().to_driver_scenario(catalog(calls, on_day=None))
    ctx = SimpleNamespace(rig="R")
    sc.before_day(ctx, "d1")
    sc.before_day(ctx, "d2")
    assert calls == [("setup", "R")]


def test_adapter_before_day_returns_no_failed_checks():
    sc = mod().to_driver_scenario(catalog([]))
    assert not sc.before_day(SimpleNamespace(rig=1), "d1")


def test_adapters_do_not_share_setup_state():
    c1, c2 = [], []
    a = mod().to_driver_scenario(catalog(c1))
    b = mod().to_driver_scenario(catalog(c2))
    a.before_day(SimpleNamespace(rig=1), "d")
    b.before_day(SimpleNamespace(rig=2), "d")
    assert c1[0] == ("setup", 1) and c2[0] == ("setup", 2)


def test_judge_calls_expect_with_built_results(tmp_path):
    calls = []
    out = mod().judge(catalog(calls), tmp_path)
    assert out == ["boom"]
    ((_, res),) = calls
    assert res.rows == [] and hasattr(res, "facts")


def test_judge_sees_evidence(tmp_path):
    seen = {}
    c = catalog([], expect=lambda res: seen.update(n=len(res.rows)) or [])
    write_status(tmp_path, "2090-12-15_2330.json",
                 status("FY90-91", [run("lan", "2090-12-15 21:01 IST", "LAN_COMPLETE")]))
    assert mod().judge(c, tmp_path) == []
    assert seen["n"] == 1


def test_judge_returns_list_of_str_even_if_expect_returns_none(tmp_path):
    c = catalog([], expect=lambda res: None)
    assert mod().judge(c, tmp_path) == []
