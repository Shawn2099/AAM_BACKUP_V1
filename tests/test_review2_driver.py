r"""Red-first tests for round-2 review items of the driver tools.livebox.run_lane (new tests only).

PINNED API / BEHAVIOUR
  B1  run_lane.prepare_env(lane_root, a): a.code_root (not the module CODE_ROOT) goes to sys.path[0] and its
          deploy\bin to the front of PATH.
      LaneHooks._fetch_status: the ui import chdirs into self.code_root.
      run_lane.default_agent_runs(a, lane_root): <lane>\evidence whenever not a.allow_dev_path (also with the default
          code root); with --allow-dev-path it stays <CODE_ROOT>\agent_runs.
      main(): without --allow-dev-path an evidence path that is not inside the lane (S2) or is inside the code root
          (S3) is refused: exit 2, hooks never built.
      main() --dry-run (and the new alias flag --preflight, args.preflight): runs S1,S2,S3,S5,S6,S7,S8,S9 as far as
          they need no flows and prints one line per rule (the text "S<n>" appears in stdout); a refusal exits 2
          ("REFUSED Sn: ..." on stderr). Nothing is created. Off the dev path S1 fails closed without a commit.
      --days help documents "--days 0" as the box preflight (the word "preflight" is in the option's help); main with
          --days 0 opens the hooks, runs no day, exits 0.
  B2  main(): a lane dir or evidence dir that exists and is non-empty without --resume -> exit 2, message names
          "--resume", hooks never built; with --resume it continues (exit 0); an existing EMPTY dir is fine.
      LaneHooks(..., resume: bool = False) new kwarg (from_args passes a.resume); LaneHooks._resume_config() -> dict|None
          returns the on-disk config.livebox.yaml only when resume is true, None for a fresh start (never reuse an old
          config or manifest.db). open() uses it.
  B3  run_lane.cleanup_lane(lane_root, root, keep_evidence=True) with NO evidence\result.json (crash): first runs
          evidence.collect_logs (-> evidence\product_logs\...), copy_manifest_db (-> evidence\manifest.db) and
          write_dataset_manifest (-> evidence\<evidence.MANIFEST_NAME>) from runtime\ and only then deletes runtime\ and
          prefect_home\; or it refuses (SafetyRefusal) leaving runtime\ intact.
  Knob errors  run_lane.run_lane catches Exception from hooks.churn / run_event / check_day (a scenario knob or
          before_day blowing up): one check row {check_id: "HARNESS...", verdict: "FAIL", observed contains the error}
          is written to checks.jsonl, result.json is written, run_lane returns (no raise); main exits 3.
          SafetyRefusal and KeyboardInterrupt still propagate.
  S8  run_lane.parse_rclone_conf(text, remote="aam_gcs") -> {"type","endpoint","remote"} of that section only (comments
          and other sections ignored, values lower-cased only for the type); LaneHooks._safety_preflight("post") uses it.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def rl():
    return importlib.import_module("tools.livebox.run_lane")


def safety():
    return importlib.import_module("tools.livebox.safety")


def evmod():
    return importlib.import_module("tools.livebox.evidence")


def cal():
    return importlib.import_module("tools.livebox.calendar")


START = dt.date(2090, 12, 1)


class Hooks:
    """Minimal fake hooks with open/close, a churn log and optional knob explosions."""

    def __init__(self, churn_boom=None, check_boom=None):
        self.opened = self.closed = False
        self.churned = []
        self.churn_boom, self.check_boom = churn_boom, check_boom

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    def safety_check(self, ev):
        pass

    def churn(self, day):
        if self.churn_boom is not None:
            raise self.churn_boom
        self.churned.append(day.date)

    def run_event(self, day, ev):
        return {"ok": True}

    def check_day(self, day, results):
        if self.check_boom is not None:
            raise self.check_boom
        return [{"check_id": "ROWS", "verdict": "PASS", "expected": "x", "observed": "y"}]


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A livebox root below tmp (so --allow-dev-path is NOT needed) and a separate code root."""
    root = tmp_path / "AAMLivebox"
    root.mkdir()
    monkeypatch.setattr(rl(), "LIVEBOX_ROOT", root)
    code = tmp_path / "boxcode"
    (code / "deploy" / "bin").mkdir(parents=True)
    return SimpleNamespace(root=root, code=code, lane=root / "s1" / "A", tmp=tmp_path)


def _argv(box, *extra, dev=False):
    a = ["--lane", "A", "--stamp", "s1", "--root", str(box.root), "--code-root", str(box.code), *extra]
    return a + (["--allow-dev-path"] if dev else [])


# ================================================================================================== B1 code root
def test_prepare_env_puts_the_given_code_root_first_on_sys_path_and_path(tmp_path, monkeypatch):
    import psutil

    code = tmp_path / "boxcode"
    (code / "deploy" / "bin").mkdir(parents=True)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    monkeypatch.setattr(psutil, "Process", lambda *a, **k: SimpleNamespace(nice=lambda *_: None))
    lane = tmp_path / "root" / "s1" / "A"
    rl().prepare_env(lane, SimpleNamespace(code_root=code))
    assert os.path.normcase(sys.path[0]) == os.path.normcase(str(code))
    first = os.environ["PATH"].split(os.pathsep)[0]
    assert os.path.normcase(first) == os.path.normcase(str(code / "deploy" / "bin"))
    assert os.path.normcase(str(rl().CODE_ROOT / "deploy" / "bin")) not in os.path.normcase(first)


def test_ui_import_chdirs_into_the_given_code_root(tmp_path, monkeypatch):
    code = tmp_path / "boxcode"
    code.mkdir()
    (code / "ui.py").write_text("import os\nCWD = os.getcwd()\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(code)] + list(sys.path))
    monkeypatch.delitem(sys.modules, "ui", raising=False)
    lane = tmp_path / "root" / "s1" / "A"
    lane.mkdir(parents=True)
    h = rl().LaneHooks(lane, lane="A", stamp="s1", days=[], agent_runs=tmp_path / "ar", allow_dev_path=True,
                       code_root=code)
    try:
        try:
            h._fetch_status(SimpleNamespace(), SimpleNamespace(kind="backup"))
        except Exception:  # noqa: BLE001 - the fake ui has nothing else; only the import cwd matters
            pass
        assert "ui" in sys.modules
        assert os.path.normcase(sys.modules["ui"].CWD) == os.path.normcase(str(code))
    finally:
        sys.modules.pop("ui", None)


# ================================================================================================== B1 evidence
def _ns(**kw):
    base = dict(agent_runs=None, code_root=rl().CODE_ROOT, allow_dev_path=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_evidence_defaults_to_lane_evidence_without_dev_flag_even_with_the_default_code_root(tmp_path):
    lane = tmp_path / "root" / "s1" / "A"
    assert rl().default_agent_runs(_ns(), lane) == lane / "evidence"


def test_evidence_default_with_dev_flag_stays_under_the_code_root_agent_runs(tmp_path):
    lane = tmp_path / "root" / "s1" / "A"
    assert rl().default_agent_runs(_ns(allow_dev_path=True), lane) == rl().CODE_ROOT / "agent_runs"


def test_main_refuses_evidence_outside_the_lane(box, capsys):
    built = []
    code = rl().main(_argv(box, "--days", "1"), hooks_factory=lambda r: built.append(r) or Hooks(),
                     agent_runs=box.tmp / "elsewhere", sleep=lambda s: None)
    assert code == 2 and built == []
    assert not (box.tmp / "elsewhere").exists()
    assert "REFUSED" in capsys.readouterr().err


def test_main_refuses_evidence_under_the_code_root(box):
    built = []
    code = rl().main(_argv(box, "--days", "1"), hooks_factory=lambda r: built.append(r) or Hooks(),
                     agent_runs=box.code / "agent_runs", sleep=lambda s: None)
    assert code == 2 and built == []


def test_main_accepts_evidence_inside_the_lane(box):
    h = Hooks()
    code = rl().main(_argv(box, "--days", "1"), hooks_factory=lambda r: h, agent_runs=box.lane / "evidence",
                     sleep=lambda s: None)
    assert code == 0 and h.churned == [START]


# ================================================================================================== B1 preflight
def test_parse_args_has_preflight_flag():
    a = rl().parse_args(["--stamp", "s1", "--lane", "A", "--preflight"])
    assert a.preflight is True
    assert rl().parse_args(["--stamp", "s1", "--lane", "A"]).preflight is False


@pytest.mark.parametrize("flag", ["--dry-run", "--preflight"])
def test_dry_run_and_preflight_run_the_s_checks_and_print_them(tmp_path, capsys, flag):
    def no_hooks(root):
        raise AssertionError("a dry run / preflight must not build the hooks")

    code = rl().main(["--lane", "A", "--stamp", "s1", "--days", "2", "--root", str(tmp_path), "--allow-dev-path",
                      flag], hooks_factory=no_hooks, agent_runs=tmp_path / "ar")
    out = capsys.readouterr().out
    assert code == 0
    for rule in ("S1", "S2", "S3", "S5", "S6", "S7", "S8", "S9"):
        assert re.search(rf"\b{rule}\b", out), f"{rule} missing from the preflight output:\n{out}"
    assert not (tmp_path / "s1").exists() and not (tmp_path / "ar").exists()


def test_dry_run_exits_nonzero_on_a_refusal(box, capsys):
    """Off the dev path without --expected-commit S1 fails closed: the dry run must say so and exit nonzero."""
    code = rl().main(_argv(box, "--days", "2", "--dry-run"), hooks_factory=lambda r: Hooks(),
                     agent_runs=box.tmp / "ar")
    cap = capsys.readouterr()
    assert code != 0
    assert re.search(r"\bS1\b", cap.out + cap.err)


def test_dry_run_refuses_evidence_outside_the_lane(box, capsys):
    code = rl().main(_argv(box, "--days", "2", "--dry-run"), hooks_factory=lambda r: Hooks(),
                     agent_runs=box.tmp / "elsewhere")
    cap = capsys.readouterr()
    assert code != 0
    assert "evidence" in (cap.out + cap.err).lower()


def test_days_option_documents_zero_as_the_box_preflight():
    ap = rl().build_parser()
    days_action = next(x for x in ap._actions if "--days" in x.option_strings)
    assert "preflight" in (days_action.help or "").lower()
    assert "0" in (days_action.help or "")


def test_days_zero_opens_the_hooks_and_runs_no_day(box):
    h = Hooks()
    code = rl().main(_argv(box, "--days", "0"), hooks_factory=lambda r: h, agent_runs=box.lane / "evidence",
                     sleep=lambda s: None)
    assert code == 0 and h.opened and h.closed and h.churned == []


# ================================================================================================== B2 fresh vs resume
def test_main_refuses_a_non_empty_lane_dir_without_resume(box, capsys):
    box.lane.mkdir(parents=True)
    (box.lane / "config.livebox.yaml").write_text("old: 1\n", encoding="utf-8")
    built = []
    code = rl().main(_argv(box, "--days", "1"), hooks_factory=lambda r: built.append(r) or Hooks(),
                     agent_runs=box.lane / "evidence", sleep=lambda s: None)
    err = capsys.readouterr().err
    assert code != 0 and built == []
    assert "--resume" in err
    assert (box.lane / "config.livebox.yaml").read_text(encoding="utf-8") == "old: 1\n"


def test_main_refuses_a_non_empty_evidence_dir_without_resume(box, capsys):
    ev = box.lane / "evidence"
    ev.mkdir(parents=True)
    (ev / "state.json").write_text("{}", encoding="utf-8")
    built = []
    code = rl().main(_argv(box, "--days", "1"), hooks_factory=lambda r: built.append(r) or Hooks(),
                     agent_runs=ev, sleep=lambda s: None)
    assert code != 0 and built == []
    assert "--resume" in capsys.readouterr().err
    assert (ev / "state.json").read_text(encoding="utf-8") == "{}"


def test_main_refuses_a_non_empty_agent_runs_evidence_folder_without_resume(tmp_path, capsys):
    ev = tmp_path / "ar" / "livebox_s1" / "A"
    ev.mkdir(parents=True)
    (ev / "checks.jsonl").write_text("{}\n", encoding="utf-8")
    built = []
    code = rl().main(["--lane", "A", "--stamp", "s1", "--days", "1", "--root", str(tmp_path / "root"),
                      "--allow-dev-path"], hooks_factory=lambda r: built.append(r) or Hooks(),
                     agent_runs=tmp_path / "ar", sleep=lambda s: None)
    assert code != 0 and built == []


def test_main_with_resume_continues_in_a_non_empty_lane(box):
    box.lane.mkdir(parents=True)
    (box.lane / "config.livebox.yaml").write_text("old: 1\n", encoding="utf-8")
    h = Hooks()
    code = rl().main(_argv(box, "--days", "1", "--resume"), hooks_factory=lambda r: h,
                     agent_runs=box.lane / "evidence", sleep=lambda s: None)
    assert code == 0 and h.churned == [START]


def test_main_accepts_an_existing_empty_lane_dir(box):
    box.lane.mkdir(parents=True)
    h = Hooks()
    code = rl().main(_argv(box, "--days", "1"), hooks_factory=lambda r: h, agent_runs=box.lane / "evidence",
                     sleep=lambda s: None)
    assert code == 0


def _hooks(tmp_path, **kw):
    lane = tmp_path / "root" / "s1" / "A"
    lane.mkdir(parents=True, exist_ok=True)
    return rl().LaneHooks(lane, lane="A", stamp="s1", days=[], agent_runs=tmp_path / "ar", allow_dev_path=True, **kw)


def test_fresh_start_does_not_reuse_an_old_config(tmp_path):
    h = _hooks(tmp_path)                          # resume defaults to False
    (h.lane_root / "config.livebox.yaml").write_text("paths: {source_drive: old}\n", encoding="utf-8")
    assert h._resume_config() is None


def test_resume_reuses_the_config_on_disk(tmp_path):
    h = _hooks(tmp_path, resume=True)
    (h.lane_root / "config.livebox.yaml").write_text("paths: {source_drive: old}\n", encoding="utf-8")
    assert h._resume_config() == {"paths": {"source_drive": "old"}}
    h2 = _hooks(tmp_path / "other", resume=True)
    assert h2._resume_config() is None            # resume without a config on disk: nothing to reuse


def test_from_args_passes_resume(tmp_path):
    lane = tmp_path / "root" / "s1" / "A"
    for flag, want in (([], False), (["--resume"], True)):
        a = rl().parse_args(["--stamp", "s1", "--lane", "A", "--allow-dev-path", *flag])
        assert rl().LaneHooks.from_args(a, lane, [], tmp_path / "ar").resume is want


# ================================================================================================== B3 cleanup after crash
def _crashed_lane(tmp_path):
    root = tmp_path / "AAMLivebox"
    lane = root / "s1" / "A"
    (lane / "runtime" / "logs").mkdir(parents=True)
    (lane / "runtime" / "logs" / "backup.log").write_text("line one\nline two\n", encoding="utf-8")
    (lane / "prefect_home").mkdir()
    (lane / "prefect_home" / "p.db").write_text("x", encoding="utf-8")
    db = lane / "runtime" / "manifest.db"
    con = sqlite3.connect(db)
    con.execute("create table run_history (id integer primary key, status text)")
    con.execute("insert into run_history (status) values ('SUCCESS')")
    con.commit()
    con.close()
    ev = lane / "evidence"
    ev.mkdir()
    (ev / "checks.jsonl").write_text("{}\n", encoding="utf-8")          # but NO result.json: the lane crashed
    return root, lane, ev


def test_cleanup_after_a_crash_saves_logs_db_and_manifest_first(tmp_path):
    root, lane, ev = _crashed_lane(tmp_path)
    try:
        rl().cleanup_lane(lane, root)
    except safety().SafetyRefusal:
        assert (lane / "runtime" / "manifest.db").exists(), "a refusal must leave runtime\\ intact"
        return
    assert not (lane / "runtime").exists() and not (lane / "prefect_home").exists()
    assert (ev / "product_logs" / "logs" / "backup.log").read_text(encoding="utf-8") == "line one\nline two\n"
    saved = ev / "manifest.db"
    assert saved.is_file()
    con = sqlite3.connect(saved)
    try:
        assert con.execute("select status from run_history").fetchall() == [("SUCCESS",)]
    finally:
        con.close()
    assert (ev / evmod().MANIFEST_NAME).is_file()
    assert (ev / "checks.jsonl").exists()


def test_cleanup_after_a_crash_listed_the_saved_files_in_the_manifest(tmp_path):
    root, lane, ev = _crashed_lane(tmp_path)
    try:
        rl().cleanup_lane(lane, root)
    except safety().SafetyRefusal:
        return
    manifest = json.loads((ev / evmod().MANIFEST_NAME).read_text(encoding="utf-8"))
    paths = {f["path"] for f in manifest["files"]}
    assert "manifest.db" in paths
    assert any(p.endswith("backup.log") for p in paths)


# ================================================================================================== knob errors
def _days(n=2):
    return cal().build_calendar(START, n)


def _run(tmp_path, hooks, n=2):
    return rl().run_lane(_days(n), lane="A", stamp="s1", agent_runs=tmp_path / "ar", hooks=hooks,
                         sleep=lambda s: None)


def test_a_knob_exception_in_churn_is_a_recorded_harness_fail_and_result_json_is_written(tmp_path):
    res = _run(tmp_path, Hooks(churn_boom=RuntimeError("icacls failed: boom")))
    ev = rl().evidence_dir(tmp_path / "ar", "s1", "A")
    rows = [json.loads(x) for x in (ev / "checks.jsonl").read_text(encoding="utf-8").splitlines()]
    bad = [r for r in rows if str(r.get("check_id", "")).upper().startswith("HARNESS")]
    assert bad and all(r["verdict"].upper() == "FAIL" for r in bad)
    assert "boom" in json.dumps(bad[0])
    assert (ev / "result.json").is_file()
    assert res.fails


def test_a_check_day_exception_is_also_a_recorded_harness_fail(tmp_path):
    _run(tmp_path, Hooks(check_boom=ValueError("oracle exploded")))
    ev = rl().evidence_dir(tmp_path / "ar", "s1", "A")
    text = (ev / "checks.jsonl").read_text(encoding="utf-8")
    assert "HARNESS" in text.upper() and "oracle exploded" in text
    assert (ev / "result.json").is_file()


def test_main_exits_3_after_a_harness_fail(box):
    h = Hooks(churn_boom=RuntimeError("knob boom"))
    code = rl().main(_argv(box, "--days", "1"), hooks_factory=lambda r: h, agent_runs=box.lane / "evidence",
                     sleep=lambda s: None)
    assert code == 3 and h.closed


def test_keyboard_interrupt_still_propagates(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, Hooks(churn_boom=KeyboardInterrupt()))


# ================================================================================================== S8 parsing
GCS_NO_ENDPOINT_WITH_COMMENT = (
    "[aam_gcs]\n# type = local\ntype = google cloud storage\nservice_account_file = k.json\n")
GCS_WITH_DECOY_SECTION = (
    "[aam_gcs]\ntype = google cloud storage\nservice_account_file = k.json\n\n[decoy]\ntype = local\n")


def test_parse_rclone_conf_ignores_comments_and_other_sections():
    f = rl().parse_rclone_conf(GCS_NO_ENDPOINT_WITH_COMMENT)
    assert f["type"] == "google cloud storage" and not f.get("endpoint")
    f = rl().parse_rclone_conf(GCS_WITH_DECOY_SECTION)
    assert f["type"] == "google cloud storage"
    f = rl().parse_rclone_conf("[aam_gcs]\n; type = local\ntype = alias\nremote = C:\\lane\\bucket\n")
    assert f["type"] == "alias" and f["remote"] == "C:\\lane\\bucket"


@pytest.mark.parametrize("text", [GCS_NO_ENDPOINT_WITH_COMMENT, GCS_WITH_DECOY_SECTION])
def test_s8_refuses_a_gcs_remote_without_endpoint_whatever_the_comments_say(tmp_path, text):
    f = rl().parse_rclone_conf(text)
    conf = str(tmp_path / "rclone_local.conf")
    with pytest.raises(safety().SafetyRefusal) as e:
        safety().check_rclone_config(conf, conf, f["type"], f.get("endpoint"), alias_remote=f.get("remote"),
                                     lane_root=str(tmp_path))
    assert e.value.rule == "S8"


@pytest.mark.parametrize("text", [GCS_NO_ENDPOINT_WITH_COMMENT, GCS_WITH_DECOY_SECTION])
def test_driver_s8_refuses_when_a_comment_or_decoy_says_type_local(tmp_path, monkeypatch, text):
    """Driver level: LaneHooks._safety_preflight('post') reads the lane's rclone_local.conf."""
    from tools.livebox.tests.test_review_fixes_safety import (FakeRig, _hooks as safety_hooks, _prep_preflight,
                                                              _set_url, _stub_checks)

    h = safety_hooks(tmp_path, monkeypatch)
    _stub_checks(monkeypatch, real=("check_rclone_config",))
    rig = FakeRig(h.lane_root, api_url="http://127.0.0.1:5000/api")
    rig.seams.rclone_conf.write_text(text, encoding="utf-8")
    rig.seams._installed = False
    _prep_preflight(h, monkeypatch, rig)
    _set_url(monkeypatch, "http://127.0.0.1:5000/api")
    with pytest.raises(safety().SafetyRefusal) as e:
        h._safety_preflight("post")
    assert e.value.rule == "S8"
