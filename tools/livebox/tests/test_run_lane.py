r"""Tests for tools.livebox.run_lane (plan 1.5, 1.8, 1.9, row L8). Written before the module exists.

ASSUMED API (pure driver logic; hooks are injected, nothing here touches rclone, robocopy, Prefect or the real clock):

    evidence_dir(agent_runs, stamp, lane) -> Path
        == Path(agent_runs) / f"livebox_{stamp}" / lane

    run_lane(days, *, lane, stamp, agent_runs, hooks, stop_on_fail=False, resume=False,
             sleep=time.sleep, pause_poll_seconds=60.0) -> LaneResult
        days   : list[tools.livebox.calendar.Day]; events inside a day are sorted by `.at` by the driver.
        hooks  : object with
            safety_check(ev)            may raise SafetyPause (driver sleep()s pause_poll_seconds, re-checks the SAME
                                        event, never skips it) or SafetyRefusal (propagates at once, lane aborted)
            churn(day)                  once per day, before the first event
            run_event(day, ev) -> obj   once per event, in time order
            check_day(day, results)     results = [(ev, obj), ...]; returns list[dict] with keys
                                        check_id, verdict ("PASS"|"FAIL"|...), expected, observed, evidence (list[str])
        Per day: churn -> (safety_check, run_event) per event -> check_day -> state saved.
        Evidence under evidence_dir(...): events.jsonl (one JSON per event, keys day, kind, at),
            checks.jsonl (one JSON per check dict, plus lane and day), state.json
            ({"last_completed_day": "YYYY-MM-DD"}; written after each finished day).
        A FAIL check is recorded and the lane goes on, unless stop_on_fail=True (then the lane stops after that day's
        checks are recorded, state saved for that day).
        resume=True: read state.json and skip every day whose date <= last_completed_day.
        LaneResult: .status ("completed" | "stopped"), .days_completed (int, days run in THIS call),
            .fails (list of the FAIL check dicts), .last_day (dt.date | None)

    cleanup_lane(lane_root, livebox_root) -> None
        deletes lane_root only when it is exactly <livebox_root>/<stamp>/<lane>; raises SafetyRefusal otherwise
        (outside the root, the root itself, a stamp folder, a production path). Siblings are untouched.

    resolve_lane_root(root, stamp, lane, allow_dev_path=False) -> Path
        == Path(root) / stamp / lane; SafetyRefusal when root is not C:\AAMLivebox (or under it) and
        allow_dev_path is False.

    parse_args(argv) -> namespace with: lane (str), stamp (str), days (int, default 728), start (dt.date, ISO input,
        default 2090-12-01), root (Path, default C:\AAMLivebox), dry_run (bool), allow_dev_path (bool),
        cleanup (str | None, a lane name; with --cleanup --lane is not required)

    main(argv, *, hooks_factory=None, agent_runs=None, sleep=time.sleep) -> int
        exit code 0 ok, 2 on SafetyRefusal. --dry-run prints the plan (first date, number of days, lane) to stdout,
        creates nothing, calls no hooks. hooks_factory(lane_root) -> hooks, used for a real run.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
from dataclasses import replace
from pathlib import Path

import pytest


def rl():
    return importlib.import_module("tools.livebox.run_lane")


def safety():
    return importlib.import_module("tools.livebox.safety")


def cal():
    return importlib.import_module("tools.livebox.calendar")


START = dt.date(2090, 12, 1)


def days(n=3, start=START):
    return cal().build_calendar(start, n)


class Hooks:
    def __init__(self, *, pauses=None, refuse_at=None, fail_days=(), boom_at=None, evidence=None):
        self.log = []
        self.pauses = dict(pauses or {})       # event index -> number of SafetyPause raises
        self.refuse_at = refuse_at             # event counter at which SafetyRefusal is raised
        self.fail_days = set(fail_days)
        self.boom_at = boom_at                 # date at which run_event raises KeyboardInterrupt
        self.evidence = evidence or []
        self.n_safety = 0

    def safety_check(self, ev):
        self.n_safety += 1
        key = ("safety", ev.at, ev.kind)
        left = self.pauses.get(ev.kind, 0)
        if left > 0:
            self.pauses[ev.kind] = left - 1
            self.log.append(("pause", ev.kind))
            raise safety().SafetyPause("S10", "production backup.lock exists")
        if self.refuse_at is not None and self.n_safety >= self.refuse_at:
            raise safety().SafetyRefusal("S2", "path outside lane")
        self.log.append(("safety", ev.kind))

    def churn(self, day):
        self.log.append(("churn", day.date))

    def run_event(self, day, ev):
        if self.boom_at == day.date:
            raise KeyboardInterrupt("crash")
        self.log.append(("event", day.date, ev.kind))
        return {"ok": True}

    def check_day(self, day, results):
        self.log.append(("check", day.date, len(results)))
        verdict = "FAIL" if day.date in self.fail_days else "PASS"
        return [{"check_id": "ROWS", "verdict": verdict, "expected": "x", "observed": "y",
                 "evidence": list(self.evidence)}]


def run(tmp_path, ds=None, hooks=None, **kw):
    sleeps = []
    kw.setdefault("sleep", sleeps.append)
    res = rl().run_lane(ds if ds is not None else days(), lane="A", stamp="20261201_0900",
                        agent_runs=tmp_path / "agent_runs", hooks=hooks or Hooks(), **kw)
    return res, sleeps


# ---------------------------------------------------------------- loop order

def test_per_day_order_churn_events_checks(tmp_path):
    h = Hooks()
    res, _ = run(tmp_path, days(2), h)
    assert res.status == "completed" and res.days_completed == 2
    for d in days(2):
        pos = [i for i, e in enumerate(h.log) if e[0] != "safety" and len(e) > 1 and e[1] == d.date]
        kinds = [h.log[i][0] for i in pos]
        assert kinds[0] == "churn"
        assert kinds[-1] == "check"
        assert set(kinds[1:-1]) == {"event"}
        assert kinds.count("event") == len(d.events)
    # day 1 completely before day 2
    first_day2 = next(i for i, e in enumerate(h.log) if e[:2] == ("churn", START + dt.timedelta(days=1)))
    last_check1 = max(i for i, e in enumerate(h.log) if e[:2] == ("check", START))
    assert last_check1 < first_day2


def test_events_run_in_time_order_even_if_given_shuffled(tmp_path):
    d = days(1)[0]
    shuffled = replace(d, events=tuple(reversed(d.events)))
    assert len(d.events) >= 2
    h = Hooks()
    run(tmp_path, [shuffled], h)
    ran = [e[2] for e in h.log if e[0] == "event"]
    assert ran == [e.kind for e in sorted(d.events, key=lambda e: e.at)]


def test_safety_check_precedes_every_event(tmp_path):
    h = Hooks()
    run(tmp_path, days(1), h)
    evs = [e for e in h.log if e[0] in ("safety", "event")]
    assert [e[0] for e in evs] == ["safety", "event"] * (len(evs) // 2)


# ---------------------------------------------------------------- evidence layout

def test_evidence_dir_layout(tmp_path):
    p = rl().evidence_dir(tmp_path / "agent_runs", "20261201_0900", "A")
    assert p == tmp_path / "agent_runs" / "livebox_20261201_0900" / "A"


def test_evidence_files_written(tmp_path):
    h = Hooks(evidence=["evidence/status/0001.json"])
    run(tmp_path, days(2), h)
    ev = rl().evidence_dir(tmp_path / "agent_runs", "20261201_0900", "A")
    events = [json.loads(x) for x in (ev / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(events) == sum(len(d.events) for d in days(2))
    assert {"day", "kind", "at"} <= set(events[0])
    checks = [json.loads(x) for x in (ev / "checks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(checks) == 2
    assert checks[0]["lane"] == "A" and checks[0]["check_id"] == "ROWS"
    assert checks[0]["verdict"] == "PASS" and checks[0]["evidence"] == ["evidence/status/0001.json"]
    state = json.loads((ev / "state.json").read_text(encoding="utf-8"))
    assert state["last_completed_day"] == "2090-12-02"


# ---------------------------------------------------------------- resume

def test_resume_after_interruption_continues_at_unfinished_day(tmp_path):
    ds = days(4)
    h1 = Hooks(boom_at=ds[2].date)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, ds, h1)
    ev = rl().evidence_dir(tmp_path / "agent_runs", "20261201_0900", "A")
    assert json.loads((ev / "state.json").read_text(encoding="utf-8"))["last_completed_day"] == "2090-12-02"

    h2 = Hooks()
    res, _ = run(tmp_path, ds, h2, resume=True)
    assert res.status == "completed" and res.days_completed == 2
    churned = [e[1] for e in h2.log if e[0] == "churn"]
    assert churned == [ds[2].date, ds[3].date]
    # earlier evidence is kept (appended, not truncated)
    lines = (ev / "checks.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 + 2


def test_no_resume_flag_starts_from_the_first_day(tmp_path):
    ds = days(2)
    run(tmp_path, ds, Hooks())
    h = Hooks()
    run(tmp_path, ds, h, resume=False)
    assert [e[1] for e in h.log if e[0] == "churn"] == [d.date for d in ds]


def test_resume_with_all_days_done_runs_nothing(tmp_path):
    ds = days(2)
    run(tmp_path, ds, Hooks())
    h = Hooks()
    res, _ = run(tmp_path, ds, h, resume=True)
    assert res.days_completed == 0 and h.log == []


# ---------------------------------------------------------------- safety

def test_safety_pause_waits_then_continues_and_never_skips(tmp_path):
    ds = days(2)
    clean = Hooks()
    run(tmp_path / "c", ds, clean)
    h = Hooks(pauses={"cloud": 3})
    res, sleeps = run(tmp_path / "p", ds, h, pause_poll_seconds=7.0)
    assert res.status == "completed"
    assert len(sleeps) >= 3 and set(sleeps) == {7.0}
    assert [e for e in h.log if e[0] == "event"] == [e for e in clean.log if e[0] == "event"]
    assert [e for e in h.log if e[0] == "check"] == [e for e in clean.log if e[0] == "check"]


def test_safety_refusal_aborts_lane_immediately(tmp_path):
    ds = days(3)
    h = Hooks(refuse_at=2)
    with pytest.raises(safety().SafetyRefusal) as ei:
        run(tmp_path, ds, h)
    assert ei.value.rule == "S2"
    assert len([e for e in h.log if e[0] == "event"]) == 1    # only the event before the refusal
    assert not [e for e in h.log if e[0] == "check"]          # no oracle after an abort
    ev = rl().evidence_dir(tmp_path / "agent_runs", "20261201_0900", "A")
    assert not (ev / "state.json").exists() or \
        "last_completed_day" not in json.loads((ev / "state.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- failed oracle

def test_failed_check_recorded_and_lane_continues(tmp_path):
    ds = days(3)
    h = Hooks(fail_days={ds[1].date}, evidence=["trees/a.json"])
    res, _ = run(tmp_path, ds, h)
    assert res.status == "completed" and res.days_completed == 3
    assert len(res.fails) == 1 and res.fails[0]["verdict"] == "FAIL"
    ev = rl().evidence_dir(tmp_path / "agent_runs", "20261201_0900", "A")
    rows = [json.loads(x) for x in (ev / "checks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["verdict"] for r in rows] == ["PASS", "FAIL", "PASS"]
    assert rows[1]["evidence"] == ["trees/a.json"]


def test_stop_on_fail_stops_after_recording(tmp_path):
    ds = days(3)
    h = Hooks(fail_days={ds[1].date})
    res, _ = run(tmp_path, ds, h, stop_on_fail=True)
    assert res.status == "stopped" and res.days_completed == 2 and len(res.fails) == 1
    assert [e[1] for e in h.log if e[0] == "churn"] == [ds[0].date, ds[1].date]
    ev = rl().evidence_dir(tmp_path / "agent_runs", "20261201_0900", "A")
    assert len((ev / "checks.jsonl").read_text(encoding="utf-8").splitlines()) == 2


# ---------------------------------------------------------------- cleanup

def test_cleanup_removes_only_the_lane_root(tmp_path):
    root = tmp_path / "AAMLivebox"
    lane = root / "20261201_0900" / "A"
    sib = root / "20261201_0900" / "B"
    for p in (lane / "src" / "FY90-91", sib / "src"):
        p.mkdir(parents=True)
        (p / "f.txt").write_text("x")
    (lane / rl().LANE_MARKER).write_text("lane")          # cleanup refuses a folder the rig did not make (M2)
    rl().cleanup_lane(lane, root, keep_evidence=False)
    assert not lane.exists()
    assert (sib / "src" / "f.txt").exists()
    assert (root / "20261201_0900").exists()


@pytest.mark.parametrize("rel", ["..", "", "20261201_0900", "../elsewhere/20261201_0900/A"])
def test_cleanup_refuses_paths_not_exactly_stamp_lane_under_root(tmp_path, rel):
    root = tmp_path / "AAMLivebox"
    (root / "20261201_0900" / "A").mkdir(parents=True)
    (tmp_path / "elsewhere" / "20261201_0900" / "A").mkdir(parents=True)
    target = (root / rel).resolve() if rel else root
    with pytest.raises(safety().SafetyRefusal):
        rl().cleanup_lane(target, root)
    assert (root / "20261201_0900" / "A").exists()
    assert (tmp_path / "elsewhere" / "20261201_0900" / "A").exists()


@pytest.mark.parametrize("prod", [r"C:\AAMBackup", r"C:\BackupAgent", "E:\\"])
def test_cleanup_never_touches_production(prod):
    with pytest.raises(safety().SafetyRefusal):
        rl().cleanup_lane(prod, r"C:\AAMLivebox")


# ---------------------------------------------------------------- CLI

def test_parse_args_full():
    a = rl().parse_args(["--lane", "A", "--stamp", "20261201_0900", "--days", "60", "--start", "2090-12-15",
                         "--root", r"D:\scratch", "--dry-run", "--allow-dev-path"])
    assert a.lane == "A" and a.stamp == "20261201_0900" and a.days == 60
    assert a.start == dt.date(2090, 12, 15)
    assert Path(a.root) == Path(r"D:\scratch")
    assert a.dry_run is True and a.allow_dev_path is True and a.cleanup is None


def test_parse_args_defaults():
    a = rl().parse_args(["--lane", "A", "--stamp", "s1"])
    assert a.days == 728 and a.start == dt.date(2090, 12, 1)
    assert Path(a.root) == Path(r"C:\AAMLivebox")
    assert a.dry_run is False and a.allow_dev_path is False


def test_parse_args_cleanup_needs_no_lane():
    a = rl().parse_args(["--cleanup", "A", "--stamp", "s1"])
    assert a.cleanup == "A"


def test_parse_args_rejects_bad_start():
    with pytest.raises(SystemExit):
        rl().parse_args(["--lane", "A", "--stamp", "s1", "--start", "not-a-date"])


def test_resolve_lane_root_outside_livebox_refused_without_dev_flag(tmp_path):
    with pytest.raises(safety().SafetyRefusal):
        rl().resolve_lane_root(tmp_path, "s1", "A")
    assert rl().resolve_lane_root(tmp_path, "s1", "A", allow_dev_path=True) == tmp_path / "s1" / "A"


def test_resolve_lane_root_inside_livebox_ok():
    assert rl().resolve_lane_root(r"C:\AAMLivebox", "s1", "A") == Path(r"C:\AAMLivebox") / "s1" / "A"


def test_resolve_lane_root_refuses_production_even_with_dev_flag():
    for prod in (r"C:\AAMBackup", r"C:\BackupAgent"):
        with pytest.raises(safety().SafetyRefusal):
            rl().resolve_lane_root(prod, "s1", "A", allow_dev_path=True)


def test_main_refuses_dev_root_without_flag(tmp_path, capsys):
    called = []
    code = rl().main(["--lane", "A", "--stamp", "s1", "--days", "2", "--root", str(tmp_path)],
                     hooks_factory=lambda root: called.append(root) or Hooks(), agent_runs=tmp_path / "ar")
    assert code == 2 and called == []
    assert not (tmp_path / "s1").exists()


def test_main_dry_run_prints_plan_and_touches_nothing(tmp_path, capsys):
    called = []
    code = rl().main(["--lane", "A", "--stamp", "s1", "--days", "3", "--root", str(tmp_path),
                      "--allow-dev-path", "--dry-run"],
                     hooks_factory=lambda root: called.append(root) or Hooks(), agent_runs=tmp_path / "ar")
    out = capsys.readouterr().out
    assert code == 0 and called == []
    assert "2090-12-01" in out and "3" in out and "A" in out
    assert not (tmp_path / "s1").exists() and not (tmp_path / "ar").exists()


def test_main_real_run_with_dev_flag_uses_hooks_and_writes_evidence(tmp_path):
    h = Hooks()
    code = rl().main(["--lane", "A", "--stamp", "s1", "--days", "2", "--root", str(tmp_path),
                      "--allow-dev-path"], hooks_factory=lambda root: h, agent_runs=tmp_path / "ar",
                     sleep=lambda s: None)
    assert code == 0
    assert [e[1] for e in h.log if e[0] == "churn"] == [START, START + dt.timedelta(days=1)]
    assert (tmp_path / "ar" / "livebox_s1" / "A" / "state.json").exists()


def test_main_cleanup_removes_lane_under_root(tmp_path):
    lane = tmp_path / "s1" / "A"
    other = tmp_path / "s1" / "B"
    lane.mkdir(parents=True)
    other.mkdir(parents=True)
    (lane / rl().LANE_MARKER).write_text("lane")          # cleanup refuses a folder the rig did not make (M2)
    code = rl().main(["--cleanup", "A", "--stamp", "s1", "--root", str(tmp_path), "--allow-dev-path"],
                     agent_runs=tmp_path / "ar")
    assert code == 0 and not lane.exists() and other.exists()
