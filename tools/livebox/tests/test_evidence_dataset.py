r"""Red-first tests for the long-term DATASET the harness produces (new module tools/livebox/evidence.py plus wiring).

Every lane persists ALL inputs and ALL outputs in <lane>\evidence (dev: agent_runs/livebox_<stamp>/<lane>).

PINNED API (tools.livebox.evidence unless noted)
  SCHEMA_VERSIONS: dict[file name -> int] (all 1 now): inputs.jsonl, expected.jsonl, config_snapshot.json,
      identity.json, db_dump/<day>.jsonl (key "db_dump/*.jsonl"), trees/*.jsonl (key "trees/*.jsonl"),
      logs_index.json, timing.jsonl, dataset_manifest.json
  InputsLog(path, sim_now=callable|None)  .record(kind, name, args=None, **extra) -> dict ; appends one JSON line:
      {"seq": 1.., "kind", "name", "args", "sim_time" (ISO or None), "real_time" (ISO UTC), **extra}
      kinds used: knob, injection, outage, clock_jump, clock_set, clock_proof, flow, churn.
  LaneRig.inputs: InputsLog at <evidence_dir>/inputs.jsonl, created by build(); every knob method (set_nas, set_mode,
      set_cloud, set_mail, set_clock_proof, ...) records kind "knob" (clock_proof / clock_jump / clock_set use their own
      kind) with name = method name and args = list of positional args; run_flow records kind "flow" with
      args {"flow", "at", "scheduled_at", "kwargs"}.
  LaneHooks.record_input(kind, name, args=None) -> dict   writes to hooks.inputs (same file, ev_dir/inputs.jsonl).
  config_snapshot(raw: dict) -> (redacted: dict, sha256: str)   values of keys containing password / secret / token /
      api_key become "***REDACTED***"; sha256 = hex of canonical JSON (sort_keys, separators (",", ":")) of the REDACTED dict.
      LaneRig.build() writes <evidence_dir>/config_snapshot.json {"sha256", "config"}.
  harness_identity(code_root, harness_dir) -> {"git_commit" (40 hex or None), "harness_files": {relative posix path ->
      sha256 of every *.py under harness_dir}, "time_machine_version", "python_version"}; written to identity.json.
  expected_rows(days) -> list[dict] one per day: {"day", "events": [{"at","kind","fy"}], "rows": [{"leg","at","fy",
      "pass_kind","run_suffix","statuses","order"}], "mails": [{"mail_class","count","key","at","recipients","fy"}],
      "flags": {is_sunday,...,is_switch_day,outage}} ; write_expected(ev_dir, days) -> expected.jsonl.
  dump_db(db_path, dump_dir, day) -> {table: row_count}   read-only; tables run_history, notifications, db_meta,
      approvals (missing table skipped); writes <dump_dir>/<day>.jsonl lines {"day","table","row": {col: value}}.
  copy_manifest_db(db_path, dest) -> Path   consistent copy (sqlite backup API) of the lane manifest.db.
  copy_capped(src, dst, cap_bytes) -> {"src","dst","bytes","kept","truncated","sha256"}  sha256 of the ORIGINAL;
      over the cap: first cap/2 and last cap/2 bytes with a b"\n...[TRUNCATED n bytes]...\n" marker between.
  collect_logs(runtime_dir, ev_dir, cap_bytes) -> list[record]   copies runtime\logs\** and runtime\run_logs\** into
      <ev_dir>\product_logs\<same relative path> and writes logs_index.json (the records).
  tree_listing(root, ignore=()) -> list[{"path" (posix, relative), "size", "mtime_ns", "sha256"}] sorted by path;
  write_tree_listing(ev_dir, day, kind, root) -> Path   trees/<day>_<kind>.jsonl (kind: source|cloud|nas).
  ResourceTimer  context manager; .seconds (float), .peak_rss_bytes (int | None), .as_dict().
  write_dataset_manifest(ev_dir) -> Path   dataset_manifest.json {"schema_version": 1, "files": [{"path","bytes",
      "sha256","format","format_version"}]} for every file under ev_dir except itself; writes README_DATASET.md
      (mentions every file's relative path or its folder pattern with a one-line description).
      format in {"jsonl","json","csv","eml","sqlite","log","text","markdown"}.
  export_evidence(ev_dir, dest_zip) -> Path   rebuilds the manifest, zips the whole evidence folder (manifest inside).
  run_lane.cleanup_lane(lane_root, livebox_root, keep_evidence=True)   removes lane data, NEVER <lane>\evidence.
      NOTE: the older test that expects the whole lane folder gone must be updated to keep_evidence=False.
  CLI: --export-evidence DEST (path of the zip; needs --lane and --stamp; main returns 0, nonzero when there is no evidence).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import json
import sqlite3
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.livebox import calendar as cal


def ev():
    return importlib.import_module("tools.livebox.evidence")


def rl():
    return importlib.import_module("tools.livebox.run_lane")


def rigmod():
    return importlib.import_module("tools.livebox.rig")


def _jsonl(p):
    return [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------------------------------------------ InputsLog
def test_inputs_log_appends_rows_with_both_clocks(tmp_path):
    t = dt.datetime(2091, 4, 1, 6, 0, tzinfo=cal.IST)
    log = ev().InputsLog(tmp_path / "inputs.jsonl", sim_now=lambda: t)
    log.record("knob", "set_nas", ["192.0.2.1"])
    log.record("outage", "server_off", {"start": "2091-04-01"}, note="n")
    rows = _jsonl(tmp_path / "inputs.jsonl")
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[0]["kind"] == "knob" and rows[0]["name"] == "set_nas" and rows[0]["args"] == ["192.0.2.1"]
    assert rows[0]["sim_time"] == t.isoformat()
    assert dt.datetime.fromisoformat(rows[0]["real_time"]).tzinfo is not None
    assert rows[1]["note"] == "n"


def test_inputs_log_sim_time_none_without_clock(tmp_path):
    log = ev().InputsLog(tmp_path / "i.jsonl")
    assert log.record("knob", "x")["sim_time"] is None


def test_inputs_log_continues_seq_after_restart(tmp_path):
    ev().InputsLog(tmp_path / "i.jsonl").record("knob", "a")
    ev().InputsLog(tmp_path / "i.jsonl").record("knob", "b")           # resume
    assert [r["seq"] for r in _jsonl(tmp_path / "i.jsonl")] == [1, 2]


# ------------------------------------------------------------------------------------------------ rig wiring
@pytest.fixture
def rig(tmp_path):
    r = rigmod().LaneRig(tmp_path / "stamp1" / "laneD").build()
    yield r
    r.remove()


def test_build_creates_inputs_log_and_config_snapshot(rig):
    assert rig.inputs is not None
    snap = json.loads((rig.evidence_dir / "config_snapshot.json").read_text(encoding="utf-8"))
    assert snap["sha256"] == ev().config_snapshot(snap["config"])[1]


def test_every_knob_call_is_recorded(rig):
    rig.set_mode("lan")
    rig.set_cloud("blackhole") if rig.seams else None
    rows = _jsonl(rig.evidence_dir / "inputs.jsonl")
    knobs = [r for r in rows if r["kind"] == "knob"]
    assert knobs[0]["name"] == "set_mode" and knobs[0]["args"] == ["lan"]


def test_clock_and_proof_changes_have_their_own_kinds(rig):
    class Clk:
        now = dt.datetime(2091, 4, 1, tzinfo=cal.IST)

        def move_to(self, i):
            self.now = i

    rig.clock = Clk()
    rig.clock_jump(dt.timedelta(hours=-2))
    rig.clock_set(dt.datetime(2091, 4, 2, tzinfo=cal.IST))
    rig.set_clock_proof("skew", 900.0)
    kinds = [r["kind"] for r in _jsonl(rig.evidence_dir / "inputs.jsonl")]
    assert kinds[-3:] == ["clock_jump", "clock_set", "clock_proof"]


def test_run_flow_is_recorded_with_arguments(rig, monkeypatch):
    import flow

    monkeypatch.setattr(flow, "backup", lambda **kw: "ok")
    t = dt.datetime(2091, 4, 1, 2, 1, tzinfo=cal.IST)
    with rig:
        rig.run_flow("backup", scheduled_at=t, mode="cloud")
    row = [r for r in _jsonl(rig.evidence_dir / "inputs.jsonl") if r["kind"] == "flow"][-1]
    assert row["name"] == "backup"
    assert row["args"]["scheduled_at"] == t.isoformat() and row["args"]["kwargs"] == {"mode": "cloud"}


def test_hooks_record_input_goes_to_the_same_file(tmp_path):
    lane = tmp_path / "stamp1" / "laneA"
    lane.mkdir(parents=True)
    h = rl().LaneHooks(lane, lane="laneA", stamp="stamp1", days=[], agent_runs=tmp_path / "ar", allow_dev_path=True)
    h.ev_dir.mkdir(parents=True, exist_ok=True)
    row = h.record_input("outage", "server_off", {"start": "2091-04-01"})
    assert _jsonl(h.ev_dir / "inputs.jsonl")[-1]["name"] == "server_off" and row["kind"] == "outage"


# ------------------------------------------------------------------------------------------------ config / identity
def test_config_snapshot_redacts_secrets_and_hashes_redacted_form():
    raw = {"notifications": {"smtp_password": "hunter2", "sender": "a@b"}, "dashboard": {"api_key": "K", "port": 1},
           "paths": {"gcs_key_path": "C:/k.json"}, "x": {"auth_token": "t"}}
    red, digest = ev().config_snapshot(raw)
    assert red["notifications"]["smtp_password"] == "***REDACTED***" and red["dashboard"]["api_key"] == "***REDACTED***"
    assert red["x"]["auth_token"] == "***REDACTED***"
    assert red["notifications"]["sender"] == "a@b" and red["paths"]["gcs_key_path"] == "C:/k.json"
    assert "hunter2" not in json.dumps(red)
    canon = json.dumps(red, sort_keys=True, separators=(",", ":")).encode()
    assert digest == sha(canon)
    assert raw["notifications"]["smtp_password"] == "hunter2"          # input not mutated
    assert ev().config_snapshot({"a": 1, "b": 2})[1] == ev().config_snapshot({"b": 2, "a": 1})[1]


def test_harness_identity_has_commit_file_hashes_and_versions(tmp_path):
    root = Path(__file__).resolve().parents[3]
    hdir = root / "tools" / "livebox"
    ident = ev().harness_identity(root, hdir)
    assert ident["git_commit"] is None or len(ident["git_commit"]) == 40
    f = ident["harness_files"]
    assert f["run_lane.py"] == sha((hdir / "run_lane.py").read_bytes())
    assert "tests/test_evidence_dataset.py" in f
    assert ident["python_version"] == sys.version.split()[0] or sys.version.startswith(ident["python_version"])
    assert "time_machine_version" in ident


def test_harness_identity_non_git_dir_gives_none_commit(tmp_path):
    (tmp_path / "h").mkdir()
    (tmp_path / "h" / "a.py").write_text("x=1")
    assert ev().harness_identity(tmp_path, tmp_path / "h")["git_commit"] is None


# ------------------------------------------------------------------------------------------------ expected rows
def test_expected_rows_cover_every_day_and_roundtrip_json():
    days = cal.build_calendar(dt.date(2090, 12, 29), 5)
    rows = ev().expected_rows(days)
    assert [r["day"] for r in rows] == [d.date.isoformat() for d in days]
    json.dumps(rows)                                                   # serialisable
    d = days[2]
    assert len(rows[2]["rows"]) == len(d.rows) and len(rows[2]["mails"]) == len(d.mails)
    assert {"leg", "at", "fy", "statuses"} <= set(rows[2]["rows"][0]) if rows[2]["rows"] else True
    assert "is_switch_day" in rows[2]["flags"] and "outage" in rows[2]["flags"]


def test_write_expected_one_line_per_day(tmp_path):
    days = cal.build_calendar(dt.date(2090, 12, 29), 3)
    p = ev().write_expected(tmp_path, days)
    assert p == tmp_path / "expected.jsonl" and len(_jsonl(p)) == 3


# ------------------------------------------------------------------------------------------------ db dump
def _make_db(path):
    c = sqlite3.connect(path)
    c.execute("create table run_history (id integer primary key, mode text, status text)")
    c.execute("create table notifications (id integer primary key, code text)")
    c.execute("create table db_meta (key text, value text)")
    c.execute("insert into run_history (mode,status) values ('cloud','SUCCESS'),('lan','FAILED')")
    c.execute("insert into notifications (code) values ('A')")
    c.execute("insert into db_meta values ('schema','7')")
    c.commit()
    c.close()


def test_dump_db_exports_tables_as_jsonl_and_skips_missing(tmp_path):
    db = tmp_path / "manifest.db"
    _make_db(db)
    counts = ev().dump_db(db, tmp_path / "db_dump", "2091-04-01")
    assert counts == {"run_history": 2, "notifications": 1, "db_meta": 1}      # no approvals table here
    import gzip
    with gzip.open(tmp_path / "db_dump" / "2091-04-01.jsonl.gz", "rt", encoding="utf-8") as fh:
        lines = [json.loads(x) for x in fh.read().splitlines() if x.strip()]
    assert len(lines) == 4
    assert {"day": "2091-04-01", "table": "db_meta", "row": {"key": "schema", "value": "7"}} in lines


def test_dump_db_is_read_only_and_tolerates_missing_db(tmp_path):
    db = tmp_path / "manifest.db"
    _make_db(db)
    before = db.read_bytes()
    ev().dump_db(db, tmp_path / "d", "2091-04-01")
    assert db.read_bytes() == before
    assert ev().dump_db(tmp_path / "nope.db", tmp_path / "d", "2091-04-02") == {}


def test_copy_manifest_db_is_a_usable_copy(tmp_path):
    db = tmp_path / "manifest.db"
    _make_db(db)
    out = ev().copy_manifest_db(db, tmp_path / "evidence" / "manifest.db")
    c = sqlite3.connect(out)
    assert c.execute("select count(*) from run_history").fetchone()[0] == 2
    c.close()


# ------------------------------------------------------------------------------------------------ logs
def test_copy_capped_small_file_copied_whole(tmp_path):
    (tmp_path / "a.log").write_bytes(b"hello")
    r = ev().copy_capped(tmp_path / "a.log", tmp_path / "out" / "a.log", 1000)
    assert (tmp_path / "out" / "a.log").read_bytes() == b"hello"
    assert r["truncated"] is False and r["bytes"] == 5 and r["kept"] == 5 and r["sha256"] == sha(b"hello")


def test_copy_capped_large_file_keeps_head_and_tail_and_says_so(tmp_path):
    data = bytes(range(256)) * 40                                       # 10240 bytes
    (tmp_path / "big.log").write_bytes(data)
    r = ev().copy_capped(tmp_path / "big.log", tmp_path / "out" / "big.log", 1000)
    out = (tmp_path / "out" / "big.log").read_bytes()
    assert r["truncated"] is True and r["bytes"] == 10240 and r["sha256"] == sha(data)
    assert out.startswith(data[:500]) and out.endswith(data[-500:]) and b"[TRUNCATED" in out
    assert len(out) < 1200 and r["kept"] == len(out)


def test_collect_logs_copies_logs_and_run_logs_with_index(tmp_path):
    rt = tmp_path / "runtime"
    (rt / "logs").mkdir(parents=True)
    (rt / "logs" / "agent.log").write_text("a")
    rl_ = rt / "run_logs" / "2091-04-01" / "2091-04-01_0201_cloud_ab12"
    rl_.mkdir(parents=True)
    (rl_ / "x_rclone.log").write_text("b" * 5000)
    evd = tmp_path / "evidence"
    recs = ev().collect_logs(rt, evd, 1000)
    assert (evd / "product_logs" / "logs" / "agent.log").read_text() == "a"
    big = evd / "product_logs" / "run_logs" / "2091-04-01" / "2091-04-01_0201_cloud_ab12" / "x_rclone.log"
    assert big.exists() and big.stat().st_size < 1200
    idx = json.loads((evd / "logs_index.json").read_text(encoding="utf-8"))
    assert idx == recs and sorted(r["truncated"] for r in idx) == [False, True]


def test_collect_logs_is_idempotent(tmp_path):
    rt = tmp_path / "runtime"
    (rt / "logs").mkdir(parents=True)
    (rt / "logs" / "agent.log").write_text("a")
    ev().collect_logs(rt, tmp_path / "e", 1000)
    assert len(ev().collect_logs(rt, tmp_path / "e", 1000)) == 1


# ------------------------------------------------------------------------------------------------ tree listing / timing
def test_tree_listing_sorted_with_hashes_and_ignore(tmp_path):
    root = tmp_path / "t"
    (root / "b").mkdir(parents=True)
    (root / "b" / "z.txt").write_bytes(b"zz")
    (root / "a.txt").write_bytes(b"a")
    (root / ".AAM_TARGET_MOUNTED").write_bytes(b"")
    rows = ev().tree_listing(root, ignore=[".AAM_TARGET_MOUNTED"])
    assert [r["path"] for r in rows] == ["a.txt", "b/z.txt"]
    assert rows[0] == {"path": "a.txt", "size": 1, "mtime_ns": (root / "a.txt").stat().st_mtime_ns, "sha256": sha(b"a")}


def test_write_tree_listing_names_day_and_kind(tmp_path):
    root = tmp_path / "nas"
    root.mkdir()
    (root / "f").write_bytes(b"1")
    p = ev().write_tree_listing(tmp_path / "evidence", "2091-04-01", "nas", root)
    assert p == tmp_path / "evidence" / "trees" / "2091-04-01_nas.jsonl" and len(_jsonl(p)) == 1


def test_resource_timer_reports_duration_and_peak_rss():
    with ev().ResourceTimer() as t:
        _ = bytearray(1_000_000)
    d = t.as_dict()
    assert t.seconds >= 0 and set(d) == {"seconds", "peak_rss_bytes"}
    assert t.peak_rss_bytes is None or t.peak_rss_bytes > 0


# ------------------------------------------------------------------------------------------------ packaging
def _evidence_folder(tmp_path):
    e = tmp_path / "evidence"
    (e / "mail").mkdir(parents=True)
    (e / "db_dump").mkdir()
    (e / "inputs.jsonl").write_bytes(b'{"seq":1}\n')
    (e / "mail" / "0001_x.eml").write_text("Subject: s\n\nbody")
    (e / "db_dump" / "2091-04-01.jsonl").write_text('{"day":"d"}\n')
    (e / "manifest.db").write_bytes(b"SQLite format 3\x00")
    (e / "checks.jsonl").write_text("")
    return e


def test_dataset_manifest_lists_every_file_with_hash_size_format(tmp_path):
    e = _evidence_folder(tmp_path)
    p = ev().write_dataset_manifest(e)
    m = json.loads(p.read_text(encoding="utf-8"))
    assert p == e / "dataset_manifest.json" and m["schema_version"] == 1
    by = {f["path"]: f for f in m["files"]}
    assert set(by) == {"inputs.jsonl", "mail/0001_x.eml", "db_dump/2091-04-01.jsonl", "manifest.db", "checks.jsonl",
                       "README_DATASET.md"}
    assert by["inputs.jsonl"]["sha256"] == sha(b'{"seq":1}\n') and by["inputs.jsonl"]["bytes"] == 10
    assert by["inputs.jsonl"]["format"] == "jsonl" and by["mail/0001_x.eml"]["format"] == "eml"
    assert by["manifest.db"]["format"] == "sqlite" and by["README_DATASET.md"]["format"] == "markdown"
    assert isinstance(by["inputs.jsonl"]["format_version"], int)


def test_readme_describes_every_file(tmp_path):
    e = _evidence_folder(tmp_path)
    ev().write_dataset_manifest(e)
    text = (e / "README_DATASET.md").read_text(encoding="utf-8")
    for needle in ("inputs.jsonl", "manifest.db", "checks.jsonl", "db_dump", "mail", "dataset_manifest.json"):
        assert needle in text, needle


def test_manifest_is_stable_and_excludes_itself(tmp_path):
    e = _evidence_folder(tmp_path)
    ev().write_dataset_manifest(e)
    first = json.loads((e / "dataset_manifest.json").read_text(encoding="utf-8"))
    ev().write_dataset_manifest(e)
    second = json.loads((e / "dataset_manifest.json").read_text(encoding="utf-8"))
    assert first == second and "dataset_manifest.json" not in {f["path"] for f in second["files"]}


def test_export_evidence_zip_contains_files_and_manifest(tmp_path):
    e = _evidence_folder(tmp_path)
    z = ev().export_evidence(e, tmp_path / "out" / "ev.zip")
    with zipfile.ZipFile(z) as zf:
        names = set(zf.namelist())
        assert {"dataset_manifest.json", "inputs.jsonl", "mail/0001_x.eml", "manifest.db"} <= names
        m = json.loads(zf.read("dataset_manifest.json"))
        for f in m["files"]:
            assert sha(zf.read(f["path"])) == f["sha256"]


def test_export_evidence_missing_folder_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ev().export_evidence(tmp_path / "nope", tmp_path / "x.zip")


# ------------------------------------------------------------------------------------------------ cleanup keeps evidence
def _lane(tmp_path):
    root = tmp_path / "AAMLivebox"
    lane = root / "stamp1" / "laneA"
    for d in ("src/FY90-91", "nas", "runtime", "evidence/mail"):
        (lane / d).mkdir(parents=True)
    (lane / "src" / "FY90-91" / "f.txt").write_text("f")
    (lane / "evidence" / "inputs.jsonl").write_text("{}\n")
    (lane / "evidence" / "mail" / "1.eml").write_text("m")
    (lane / rl().LANE_MARKER).write_text("lane")          # cleanup refuses a folder the rig did not make (M2)
    return root, lane


def test_cleanup_lane_keeps_evidence_by_default(tmp_path):
    root, lane = _lane(tmp_path)
    rl().cleanup_lane(lane, root)
    assert sorted(p.name for p in lane.iterdir()) == ["evidence"]
    assert (lane / "evidence" / "mail" / "1.eml").read_text() == "m"


def test_cleanup_lane_can_still_remove_everything_explicitly(tmp_path):
    root, lane = _lane(tmp_path)
    rl().cleanup_lane(lane, root, keep_evidence=False)
    assert not lane.exists()


def test_cleanup_lane_still_refuses_outside_root(tmp_path):
    root, lane = _lane(tmp_path)
    with pytest.raises(Exception):
        rl().cleanup_lane(tmp_path, root)
    assert (lane / "evidence" / "inputs.jsonl").exists()


# ------------------------------------------------------------------------------------------------ CLI export
def test_cli_export_evidence_writes_zip(tmp_path):
    ar = tmp_path / "ar"
    e = ar / "livebox_s1" / "laneA"
    e.mkdir(parents=True)
    (e / "inputs.jsonl").write_text("{}\n")
    dest = tmp_path / "dest" / "laneA.zip"
    rc = rl().main(["--export-evidence", str(dest), "--lane", "laneA", "--stamp", "s1", "--agent-runs", str(ar)])
    assert rc == 0 and zipfile.ZipFile(dest).testzip() is None
    assert "dataset_manifest.json" in zipfile.ZipFile(dest).namelist()


def test_cli_export_evidence_without_evidence_fails(tmp_path):
    rc = rl().main(["--export-evidence", str(tmp_path / "z.zip"), "--lane", "nolane", "--stamp", "s1",
                    "--agent-runs", str(tmp_path / "ar")])
    assert rc != 0 and not (tmp_path / "z.zip").exists()
