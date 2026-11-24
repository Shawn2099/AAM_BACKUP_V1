r"""Red-first tests (review round 2): evidence completeness.

PINNED API
  events.jsonl row (run_lane.run_lane loop) gains, read from the hooks' event outcome object `obj`:
        ok            bool   False when obj.error is set or obj.harness_error is set (default True)
        error         None | {"type": "ValueError", "message": "x", "traceback": "..."}
        returned      str    str(obj.value), truncated to 500 chars ("" when None)
        scheduled_at  ISO str of obj.scheduled_at (None when absent)
        new_run_ids   list[str]  obj.new_run_ids (default [])
  LaneHooks.EventOutcome gains   ok, error ({"type","message","traceback"}), scheduled_at (datetime), new_run_ids;
        LaneHooks.run_event fills them: scheduled_at = _scheduled_at(ev); new_run_ids = run_id of out.new_rows;
        an exception in the event sets error + harness_error and ok False (the lane keeps going).
  timing.jsonl   one row per event written by the loop from obj.resources:
        {"day", "kind", "at", "seconds", "peak_rss_bytes"}
  run_tool guard rows: robocopy_runs.jsonl rows gain "exit_code" (int | None); rclone calls are recorded in
        rclone_runs.jsonl {"start","end","argv","exit_code"}; a tool that raises records exit_code None and
        "error": "<ExcType>: msg".
  inputs.jsonl   LaneHooks._evidence_at_start() records one row kind "cli", name "run_lane", args = dict of the parsed
        command line (LaneHooks.from_args stores it as hooks.cli_args; Path values as str).
  config_hashes.jsonl   LaneHooks._evidence_after_day(day) appends {"day": ISO, "sha256": evidence.config_snapshot(
        rig.config)[1]} every day.
  evidence.dump_db(db_path, dump_dir, day, day_index=None) -> {table: rows_written}
        writes <dump_dir>/<day>.jsonl.gz (gzip, one JSON row per line: {"day","table","row"}); FULL when day_index is
        None or day_index % 7 == 0, otherwise a DELTA holding only rows new or changed (by content) since the previous
        dump.  The delta file exists even when empty.  No plain <day>.jsonl is written.
        LaneHooks._evidence_after_day passes day.index.
  dataset_manifest.json   db_dump/*.jsonl.gz entries: "format": "jsonl.gz", "format_version": 1.
  evidence.copy_capped    never inserts ASCII bytes into a UTF-16 file: the kept bytes decode as UTF-16 and contain
        no b"TRUNCATED"; the cut is reported by a sidecar "<dst>.truncated" note (text with the omitted byte count)
        and the returned dict has "encoding": "utf-16".
"""
from __future__ import annotations

import datetime as dt
import gzip
import importlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.livebox import calendar as cal


def rl():
    return importlib.import_module("tools.livebox.run_lane")


def ev():
    return importlib.import_module("tools.livebox.evidence")


def jl(path: Path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _hooks(tmp_path, **kw):
    lane = tmp_path / "stamp1" / "laneA"
    lane.mkdir(parents=True, exist_ok=True)
    h = rl().LaneHooks(lane, lane="laneA", stamp="stamp1", days=kw.pop("days", []), agent_runs=tmp_path / "ar",
                       allow_dev_path=True, **kw)
    h.ev_dir.mkdir(parents=True, exist_ok=True)
    return h, lane


# --------------------------------------------------------------------------------------------- events.jsonl / timing
class OutcomeHooks:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def churn(self, day):
        pass

    def safety_check(self, ev_):
        pass

    def run_event(self, day, ev_):
        return self.outcomes.pop(0)

    def check_day(self, day, results):
        return []


def _run_loop(tmp_path, outcomes):
    day = cal.build_calendar(dt.date(2091, 1, 5), 1)[0]
    n = len(day.events)
    outs = [outcomes[i % len(outcomes)] for i in range(n)]
    ar = tmp_path / "ar"
    rl().run_lane([day], lane="L", stamp="s", agent_runs=ar, hooks=OutcomeHooks(outs))
    return rl().evidence_dir(ar, "s", "L"), day


def test_events_rows_carry_outcome_success(tmp_path):
    sched = dt.datetime(2091, 1, 5, 2, 1, tzinfo=cal.IST)
    o = SimpleNamespace(ok=True, error=None, harness_error=None, value="SUCCESS", scheduled_at=sched,
                        new_run_ids=["r1", "r2"], resources={"seconds": 1.5, "peak_rss_bytes": 123})
    evd, day = _run_loop(tmp_path, [o])
    rows = jl(evd / "events.jsonl")
    assert len(rows) == len(day.events)
    r = rows[0]
    assert r["ok"] is True and r["error"] is None
    assert "SUCCESS" in r["returned"]
    assert r["scheduled_at"] == sched.isoformat()
    assert r["new_run_ids"] == ["r1", "r2"]


def test_events_rows_carry_exception_type_message_traceback(tmp_path):
    o = SimpleNamespace(ok=False, harness_error="Traceback (most recent call last):\n...\nValueError: x",
                        error={"type": "ValueError", "message": "x", "traceback": "Traceback...ValueError: x"},
                        value=None, scheduled_at=None, new_run_ids=[], resources=None)
    evd, _ = _run_loop(tmp_path, [o])
    r = jl(evd / "events.jsonl")[0]
    assert r["ok"] is False
    assert r["error"]["type"] == "ValueError" and r["error"]["message"] == "x"
    assert "Traceback" in r["error"]["traceback"]
    assert r["returned"] == "" and r["new_run_ids"] == []


def test_events_row_ok_false_when_only_harness_error_is_set(tmp_path):
    o = SimpleNamespace(harness_error="Traceback...\nKeyError: 'a'", value=None, resources=None)
    evd, _ = _run_loop(tmp_path, [o])
    r = jl(evd / "events.jsonl")[0]
    assert r["ok"] is False


def test_timing_jsonl_is_written(tmp_path):
    o = SimpleNamespace(ok=True, error=None, harness_error=None, value=None,
                        resources={"seconds": 2.5, "peak_rss_bytes": 4096})
    evd, day = _run_loop(tmp_path, [o])
    rows = jl(evd / "timing.jsonl")
    assert len(rows) == len(day.events)
    assert set(rows[0]) >= {"day", "kind", "at", "seconds", "peak_rss_bytes"}
    assert rows[0]["seconds"] == 2.5 and rows[0]["peak_rss_bytes"] == 4096 and rows[0]["day"] == "2091-01-05"


def test_run_event_sets_error_and_ok_false_on_exception(tmp_path):
    h, _ = _hooks(tmp_path)
    day = cal.build_calendar(dt.date(2091, 1, 5), 1)[0]
    e = next(x for x in day.events if x.base_kind == cal.ROLLOVER_CHECK)

    def boom(day_, ev_, out):
        raise ValueError("x")

    h._run_event_inner = boom
    out = h.run_event(day, e)
    assert out.ok is False
    assert out.error["type"] == "ValueError" and out.error["message"] == "x"
    assert "ValueError" in out.error["traceback"]
    assert out.scheduled_at == h._scheduled_at(e)


def test_run_event_success_records_new_run_ids(tmp_path):
    h, _ = _hooks(tmp_path)
    day = cal.build_calendar(dt.date(2091, 1, 5), 1)[0]
    e = next(x for x in day.events if x.base_kind == cal.ROLLOVER_CHECK)

    def inner(day_, ev_, out):
        out.new_rows = [{"run_id": "2091-01-05_0600_rollover_ab12"}, {"run_id": "r2"}]

    h._run_event_inner = inner
    out = h.run_event(day, e)
    assert out.ok is True and out.error is None
    assert out.new_run_ids == ["2091-01-05_0600_rollover_ab12", "r2"]


# --------------------------------------------------------------------------------------------- tool runs
def test_guard_records_rclone_and_exit_codes(tmp_path, monkeypatch):
    import core.lan_sync as ls
    import core.tool_runner as tr

    def fake(name, argv, **kw):
        return SimpleNamespace(exit_code=9 if "robocopy" in str(argv[0]) else 0)

    monkeypatch.setattr(tr, "run_tool", fake)
    monkeypatch.setattr(ls, "run_tool", fake)
    h, lane = _hooks(tmp_path)
    try:
        h._install_run_tool_guard()
        ls.run_tool("robocopy_main", ["robocopy.exe", str(lane / "src"), str(lane / "nas"), "/MIR"])
        ls.run_tool("rclone_sync", ["rclone.exe", "sync", str(lane / "src"), "x:"])
    finally:
        h._stack.close()
    rob = jl(h.ev_dir / "robocopy_runs.jsonl")
    rc = jl(h.ev_dir / "rclone_runs.jsonl")
    assert len(rob) == 1 and rob[0]["exit_code"] == 9
    assert len(rc) == 1 and rc[0]["exit_code"] == 0 and rc[0]["argv"][1] == "sync"
    assert set(rc[0]) >= {"start", "end", "argv", "exit_code"}


def test_guard_records_error_when_tool_raises(tmp_path, monkeypatch):
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
            ls.run_tool("rclone_sync", ["rclone.exe", "sync", str(lane / "src"), "x:"])
        with pytest.raises(TimeoutError):
            ls.run_tool("robocopy_main", ["robocopy.exe", str(lane / "src"), str(lane / "nas")])
    finally:
        h._stack.close()
    for name in ("rclone_runs.jsonl", "robocopy_runs.jsonl"):
        (r,) = jl(h.ev_dir / name)
        assert r["exit_code"] is None and r["error"].startswith("TimeoutError")


# --------------------------------------------------------------------------------------------- inputs / config hash
def test_cli_args_recorded_in_inputs(tmp_path):
    a = rl().parse_args(["--lane", "laneA", "--stamp", "stamp1", "--seed", "abc", "--root", str(tmp_path)])
    lane = tmp_path / "stamp1" / "laneA"
    lane.mkdir(parents=True)
    h = rl().LaneHooks.from_args(a, lane, [], tmp_path / "ar")
    h.ev_dir.mkdir(parents=True, exist_ok=True)
    h._evidence_at_start()
    rows = [r for r in jl(h.ev_dir / "inputs.jsonl") if r["kind"] == "cli"]
    assert len(rows) == 1 and rows[0]["name"] == "run_lane"
    assert rows[0]["args"]["lane"] == "laneA" and rows[0]["args"]["seed"] == "abc"
    assert rows[0]["args"]["root"] == str(tmp_path)


def test_config_hash_recorded_per_day(tmp_path):
    h, lane = _hooks(tmp_path)
    cfg = {"paths": {"x": "y"}, "notifications": {"smtp_password": "s3cret"}}
    h.rig = SimpleNamespace(runtime_dir=lane / "runtime", lane_dir=lane, bucket_root=lane / "b", bucket_name="bk",
                            config=cfg)
    days = cal.build_calendar(dt.date(2091, 1, 5), 2)
    h.days = days
    h._evidence_after_day(days[0])
    cfg["paths"]["x"] = "z"
    h._evidence_after_day(days[1])
    rows = jl(h.ev_dir / "config_hashes.jsonl")
    assert [r["day"] for r in rows] == ["2091-01-05", "2091-01-06"]
    assert rows[0]["sha256"] != rows[1]["sha256"]
    assert rows[1]["sha256"] == ev().config_snapshot(cfg)[1]


# --------------------------------------------------------------------------------------------- db dump delta
def _db(path):
    con = sqlite3.connect(path)
    con.execute("create table notifications (id integer primary key, event_key text, status text)")
    con.execute("create table db_meta (key text primary key, value text)")
    con.execute("insert into notifications values (1,'a','SENT')")
    con.execute("insert into db_meta values ('fy','FY90-91')")
    con.commit()
    return con


def _read_gz(p):
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        return [json.loads(x) for x in fh.read().splitlines() if x.strip()]


def test_dump_is_gzipped_and_full_on_day_zero(tmp_path):
    con = _db(tmp_path / "m.db")
    con.close()
    counts = ev().dump_db(tmp_path / "m.db", tmp_path / "d", "2091-01-01", day_index=0)
    assert counts == {"notifications": 1, "db_meta": 1}
    assert not (tmp_path / "d" / "2091-01-01.jsonl").exists()
    rows = _read_gz(tmp_path / "d" / "2091-01-01.jsonl.gz")
    assert {r["table"] for r in rows} == {"notifications", "db_meta"}


def test_daily_dump_is_delta_with_weekly_full(tmp_path):
    db, d = tmp_path / "m.db", tmp_path / "d"
    con = _db(db)
    ev().dump_db(db, d, "2091-01-01", day_index=0)
    # day 1: nothing changed -> an empty delta file exists
    assert ev().dump_db(db, d, "2091-01-02", day_index=1) in ({}, {"notifications": 0, "db_meta": 0})
    assert _read_gz(d / "2091-01-02.jsonl.gz") == []
    # day 2: one new row and one changed row
    con.execute("insert into notifications values (2,'b','SENT')")
    con.execute("update db_meta set value='FY91-92' where key='fy'")
    con.commit()
    ev().dump_db(db, d, "2091-01-03", day_index=2)
    delta = _read_gz(d / "2091-01-03.jsonl.gz")
    assert sorted((r["table"], json.dumps(r["row"], sort_keys=True)) for r in delta) == sorted([
        ("notifications", json.dumps({"id": 2, "event_key": "b", "status": "SENT"}, sort_keys=True)),
        ("db_meta", json.dumps({"key": "fy", "value": "FY91-92"}, sort_keys=True))])
    # day 7: full again
    ev().dump_db(db, d, "2091-01-08", day_index=7)
    assert len(_read_gz(d / "2091-01-08.jsonl.gz")) == 3
    con.close()


def test_dump_without_day_index_is_full(tmp_path):
    con = _db(tmp_path / "m.db")
    con.close()
    ev().dump_db(tmp_path / "m.db", tmp_path / "d", "2091-01-01")
    ev().dump_db(tmp_path / "m.db", tmp_path / "d", "2091-01-02")
    assert len(_read_gz(tmp_path / "d" / "2091-01-02.jsonl.gz")) == 2


def test_after_day_passes_day_index_to_dump(tmp_path, monkeypatch):
    h, lane = _hooks(tmp_path)
    h.rig = SimpleNamespace(runtime_dir=lane / "runtime", lane_dir=lane, bucket_root=lane / "b", bucket_name="bk",
                            config={})
    calls = []
    monkeypatch.setattr(ev(), "dump_db", lambda db, d, day, day_index=None: calls.append((day, day_index)) or {})
    days = cal.build_calendar(dt.date(2091, 1, 5), 3)
    h.days = days
    h._evidence_after_day(days[2])
    assert calls == [("2091-01-07", 2)]


def test_manifest_lists_gz_format(tmp_path):
    (tmp_path / "db_dump").mkdir()
    with gzip.open(tmp_path / "db_dump" / "2091-01-01.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write("")
    p = ev().write_dataset_manifest(tmp_path)
    files = {f["path"]: f for f in json.loads(p.read_text(encoding="utf-8"))["files"]}
    f = files["db_dump/2091-01-01.jsonl.gz"]
    assert f["format"] == "jsonl.gz" and f["format_version"] == 1


# --------------------------------------------------------------------------------------------- copy_capped UTF-16
def test_copy_capped_keeps_utf16_decodable_without_ascii_marker(tmp_path):
    src = tmp_path / "robocopy.log"
    text = "".join(f"line {i:05d} new file C:\\data\\f{i}.txt\r\n" for i in range(400))
    src.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    dst = tmp_path / "out" / "robocopy.log"
    info = ev().copy_capped(src, dst, 1002)          # half = 501 (odd): a naive byte cut splits a UTF-16 unit
    assert info["truncated"] is True and info["encoding"] == "utf-16"
    raw = dst.read_bytes()
    assert b"TRUNCATED" not in raw
    decoded = raw.decode("utf-16")                    # must not raise
    assert "line 00000" in decoded and "line 00399" in decoded
    note = dst.with_name(dst.name + ".truncated")
    assert note.is_file() and str(info["bytes"] - info["kept"]) in note.read_text(encoding="utf-8")


def test_copy_capped_ascii_log_unchanged_behaviour(tmp_path):
    src = tmp_path / "a.log"
    src.write_text("x" * 100, encoding="ascii")
    info = ev().copy_capped(src, tmp_path / "o" / "a.log", 1000)
    assert info["truncated"] is False and (tmp_path / "o" / "a.log").read_text() == "x" * 100
