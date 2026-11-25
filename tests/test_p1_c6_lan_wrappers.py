"""Plan P1 chunk C6: LAN rclone wrappers (core/lan_sync.py).

Matrix rows (docs/PLAN_P1_RCLONE_LAN_2026-11-25.md, "R wrapper (U)", "PL plan (U)" and
§2, §2.1, §2.2, §3):
  R1 rclone binary missing -> LAN_FAILED, fault code AAM-SYS-04.
  R2 timeout -> LAN_FAILED, timed_out, stats taken from the log when it has them.
  R3 the run dict carries every key, always.
  R4 the full log is kept under the step name rclone-lan-sync.
  PL1-PL5 the dry-run plan dict (planned counts come from the dry-run JSON, not from
     combined '-' lines, see the C0b consequences), its ok=False rules and its argv.

Extra rows (not numbered in the matrix, each a guard of a plan rule, flagged in the report):
  X1 real-sync argv is exactly the builder's argv (max-delete and budget duration included).
  X2 a clean sync maps to LAN_COMPLETE through decide_lan_rclone_result.
  X3 --max-delete backstop (exit 7) maps to LAN_FAILED with max_delete_reached.
  X4 timeouts default to lan.subprocess_timeout_seconds (sync) and
     lan.rclone_measure_timeout_seconds (plan); an explicit timeout wins.
  X5 a spawn error that is not "file not found" carries no AAM-SYS-04 code.

The wrappers are looked up on the module at call time, so each test fails with
AttributeError until C6 lands, not at collection.
"""

import json
from pathlib import Path

import pendulum

import core.lan_sync as lan_sync
from core.permissions import PROBE_PATTERN
from core.run_artefacts import RunArtefacts
from models.config import LanConfig
from test_support.fake_tools import fake_rclone_lan

FIXTURES = Path(__file__).resolve().parents[1] / "test_support" / "rclone_logs"
RUN_ID = "3f2a9c1e-77aa-4c6b-9d11-0a1b2c3d4e5f-lan"
STARTED = pendulum.datetime(2026, 11, 25, 9, 0, tz="Asia/Kolkata")

RESULT_KEYS = {"status", "reason", "retryable", "max_delete_reached", "time_limit_stopped",
               "files_failed", "failed_paths", "deletes_held", "stats", "abnormal",
               "exit_code", "timed_out", "log_path"}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _setup(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("content", encoding="utf-8")
    dst = str(tmp_path / "dst")
    artefacts = RunArtefacts(tmp_path / "runlogs", RUN_ID, started=STARTED)
    return str(src), dst, str(artefacts.dir)


def _flag(argv, flag):
    return Path(argv[argv.index(flag) + 1])


def _run(config, src, dst, art, **kwargs):
    return lan_sync.run_lan_mirror(config, src, dst, art, **kwargs)


def _plan(config, src, dst, art, **kwargs):
    return lan_sync.plan_lan_mirror(config, src, dst, art, **kwargs)


# ── R: run_lan_mirror ───────────────────────────────────────────────

def test_r1_missing_rclone_binary_is_failed_with_fault_code_aam_sys_04(tmp_path):
    src, dst, art = _setup(tmp_path)
    spec = {"exit_code": None,
            "spawn_error": "FileNotFoundError: [WinError 2] The system cannot find the file"}
    with fake_rclone_lan({"rclone-lan-sync": spec}):
        result = _run(LanConfig(), src, dst, art, max_delete=0)
    assert result["status"] == "LAN_FAILED"
    assert result["fault_code"] == "AAM-SYS-04"
    assert result["exit_code"] is None
    assert result["timed_out"] is False


def test_r2_timeout_is_failed_timed_out_and_keeps_stats_the_log_has(tmp_path):
    src, dst, art = _setup(tmp_path)
    spec = {"exit_code": None, "timed_out": True, "log": _fixture("lan_clean.jsonl")}
    with fake_rclone_lan({"rclone-lan-sync": spec}):
        result = _run(LanConfig(), src, dst, art, max_delete=0)
    assert result["status"] == "LAN_FAILED"
    assert result["timed_out"] is True
    assert result["exit_code"] is None
    assert isinstance(result["stats"], dict)
    assert result["stats"]["transfers"] == 12


def test_r2_timeout_without_a_stats_line_has_stats_none(tmp_path):
    src, dst, art = _setup(tmp_path)
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": None, "timed_out": True,
                                              "log": "partial, no stats yet"}}):
        result = _run(LanConfig(), src, dst, art, max_delete=0)
    assert result["status"] == "LAN_FAILED"
    assert result["timed_out"] is True
    assert result["stats"] is None


def test_r3_every_key_is_present_on_a_completed_run(tmp_path):
    src, dst, art = _setup(tmp_path)
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0, "log": _fixture("lan_clean.jsonl")}}):
        result = _run(LanConfig(), src, dst, art, max_delete=0)
    assert set(result) >= RESULT_KEYS


def test_r3_every_key_is_present_on_a_partial_run(tmp_path):
    src, dst, art = _setup(tmp_path)
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 1, "log": _fixture("locked_file_exit1.jsonl")}}):
        result = _run(LanConfig(), src, dst, art, max_delete=0)
    assert set(result) >= RESULT_KEYS


def test_r3_every_key_is_present_on_a_spawn_error(tmp_path):
    src, dst, art = _setup(tmp_path)
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": None, "spawn_error": "OSError: boom"}}):
        result = _run(LanConfig(), src, dst, art, max_delete=0)
    assert set(result) >= RESULT_KEYS


def test_r4_full_log_is_kept_under_the_step_name_rclone_lan_sync(tmp_path):
    src, dst, art = _setup(tmp_path)
    log_text = _fixture("lan_clean.jsonl")
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0, "log": log_text}}) as fake:
        result = _run(LanConfig(), src, dst, art, max_delete=0)
    kept = list(Path(art).glob("*_rclone-lan-sync.log"))
    assert len(kept) == 1
    assert kept[0].read_text(encoding="utf-8") == log_text
    assert Path(result["log_path"]) == kept[0]
    assert _flag(fake[0]["argv"], "--log-file") == kept[0]
    assert fake.steps() == ["rclone-lan-sync"]


def test_x1_real_sync_argv_is_the_builder_argv_with_max_delete_and_budget_duration(tmp_path):
    src, dst, art = _setup(tmp_path)
    config = LanConfig()
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0, "log": _fixture("lan_clean.jsonl")}}) as fake:
        _run(config, src, dst, art, max_delete=0, max_duration_s=600)
    argv = fake[0]["argv"]
    log = _flag(argv, "--log-file")
    expected = lan_sync.build_lan_rclone_command(config, src, dst, str(log),
                                                 max_delete=0, max_duration_s=600)
    assert argv[1:] == expected[1:]
    assert "--dry-run" not in argv
    assert argv[argv.index("--max-delete") + 1] == "0"
    assert argv[argv.index("--max-duration") + 1] == "600s"
    assert argv[argv.index("--cutoff-mode") + 1] == "SOFT"


def test_x2_clean_sync_maps_to_lan_complete(tmp_path):
    src, dst, art = _setup(tmp_path)
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0, "log": _fixture("lan_clean.jsonl")}}):
        result = _run(LanConfig(), src, dst, art, max_delete=0)
    assert result["status"] == "LAN_COMPLETE"
    assert result["exit_code"] == 0
    assert result["retryable"] is False


def test_x3_max_delete_backstop_exit_7_is_failed_with_max_delete_reached(tmp_path):
    src, dst, art = _setup(tmp_path)
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 7, "log": _fixture("lan_maxdelete.jsonl")}}):
        result = _run(LanConfig(), src, dst, art, max_delete=1)
    assert result["status"] == "LAN_FAILED"
    assert result["max_delete_reached"] is True
    assert result["retryable"] is False


def test_x4_sync_timeout_defaults_to_subprocess_timeout_and_an_explicit_one_wins(tmp_path):
    src, dst, art = _setup(tmp_path)
    config = LanConfig()
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0, "log": _fixture("lan_clean.jsonl")}}) as fake:
        _run(config, src, dst, art, max_delete=0)
        _run(config, src, dst, art, max_delete=0, timeout=100)
    assert fake[0]["timeout_s"] == config.subprocess_timeout_seconds
    assert fake[1]["timeout_s"] == 100


def test_x5_a_spawn_error_that_is_not_file_not_found_has_no_aam_sys_04(tmp_path):
    src, dst, art = _setup(tmp_path)
    with fake_rclone_lan({"rclone-lan-sync": {"exit_code": None,
                                              "spawn_error": "PermissionError: [WinError 5] denied"}}):
        result = _run(LanConfig(), src, dst, art, max_delete=0)
    assert result["status"] == "LAN_FAILED"
    assert result.get("fault_code") is None


# ── PL: plan_lan_mirror ─────────────────────────────────────────────

PLAN_KEYS = {"ok", "error", "exit_code", "source_files", "dest_files", "planned_deletes",
             "moves", "planned_copies", "planned_bytes", "planned_move_bytes", "log_path", "combined_path"}


def _plan_fixture(tmp_path, dryrun_log, combined, exit_code=0, stderr="", **spec_extra):
    src, dst, art = _setup(tmp_path)
    spec = {"exit_code": exit_code, "log": dryrun_log, "stderr": stderr}
    if combined is not None:
        spec["combined"] = combined
    spec.update(spec_extra)
    return src, dst, art, spec


def test_pl1_clean_plan_dict(tmp_path):
    src, dst, art, spec = _plan_fixture(tmp_path, _fixture("lan_clean_dryrun.jsonl"),
                                        _fixture("lan_clean_combined.txt"))
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        plan = _plan(LanConfig(), src, dst, art)
    assert set(plan) == PLAN_KEYS
    assert plan["ok"] is True
    assert plan["error"] is None
    assert plan["exit_code"] == 0
    assert plan["source_files"] == 12
    assert plan["dest_files"] == 0
    assert plan["planned_deletes"] == 0
    assert plan["moves"] == 0
    assert plan["planned_copies"] == 12
    assert plan["planned_bytes"] == 13062
    assert Path(plan["log_path"]).exists()
    assert Path(plan["combined_path"]).exists()


def test_pl1_rename_plan_counts_moves_not_deletes(tmp_path):
    src, dst, art, spec = _plan_fixture(tmp_path, _fixture("lan_rename_dryrun.jsonl"),
                                        _fixture("lan_rename_combined.txt"))
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        plan = _plan(LanConfig(), src, dst, art)
    assert plan["ok"] is True
    assert plan["source_files"] == 12
    assert plan["dest_files"] == 12
    assert plan["moves"] == 5
    assert plan["planned_deletes"] == 0
    assert plan["planned_copies"] == 0
    assert plan["planned_bytes"] == 0


def test_pl1_mixed_plan_deletes_come_from_the_json_not_from_combined_minus_lines(tmp_path):
    # C0b contradiction (plan 2.3 / C0b consequences): the combined file has 7 '-' lines
    # (5 move sources + 2 deletes) but the dry-run JSON says deletes 2, moves 5.
    src, dst, art, spec = _plan_fixture(tmp_path, _fixture("lan_mixed_dryrun.jsonl"),
                                        _fixture("lan_mixed_combined.txt"))
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        plan = _plan(LanConfig(), src, dst, art)
    assert plan["ok"] is True
    assert plan["source_files"] == 6
    assert plan["dest_files"] == 8
    assert plan["planned_deletes"] == 2
    assert plan["moves"] == 5


def test_pl1_deletes_plan_counts_three_deletes(tmp_path):
    src, dst, art, spec = _plan_fixture(tmp_path, _fixture("lan_deletes_dryrun.jsonl"),
                                        _fixture("lan_deletes_combined.txt"))
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        plan = _plan(LanConfig(), src, dst, art)
    assert plan["ok"] is True
    assert plan["source_files"] == 9
    assert plan["dest_files"] == 12
    assert plan["planned_deletes"] == 3
    assert plan["moves"] == 0


def test_pl2_nonzero_exit_is_not_ok(tmp_path):
    src, dst, art, spec = _plan_fixture(tmp_path, _fixture("lan_clean_dryrun.jsonl"),
                                        _fixture("lan_clean_combined.txt"), exit_code=1)
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        plan = _plan(LanConfig(), src, dst, art)
    assert plan["ok"] is False
    assert plan["error"]


def test_pl2_an_error_level_line_in_the_log_is_not_ok(tmp_path):
    error_line = json.dumps({"level": "error", "msg": "Failed to copy: boom", "object": "x.txt"})
    log = _fixture("lan_clean_dryrun.jsonl") + "\n" + error_line + "\n"
    src, dst, art, spec = _plan_fixture(tmp_path, log, _fixture("lan_clean_combined.txt"))
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        plan = _plan(LanConfig(), src, dst, art)
    assert plan["ok"] is False
    assert plan["error"]


def test_pl2_stats_errors_above_zero_is_not_ok(tmp_path):
    log = _fixture("lan_clean_dryrun.jsonl").replace('"errors":0', '"errors":2')
    assert '"errors":2' in log
    src, dst, art, spec = _plan_fixture(tmp_path, log, _fixture("lan_clean_combined.txt"))
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        plan = _plan(LanConfig(), src, dst, art)
    assert plan["ok"] is False


def test_pl3_a_bang_line_in_the_combined_file_is_not_ok(tmp_path):
    combined = _fixture("lan_clean_combined.txt") + "! unreadable.txt\n"
    src, dst, art, spec = _plan_fixture(tmp_path, _fixture("lan_clean_dryrun.jsonl"), combined)
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        plan = _plan(LanConfig(), src, dst, art)
    assert plan["ok"] is False


def test_pl3_a_missing_combined_file_is_not_ok(tmp_path):
    src, dst, art, spec = _plan_fixture(tmp_path, _fixture("lan_clean_dryrun.jsonl"), None)
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        plan = _plan(LanConfig(), src, dst, art)
    assert plan["ok"] is False


def test_pl4_timeout_is_not_ok(tmp_path):
    src, dst, art, spec = _plan_fixture(tmp_path, "", None, exit_code=None, timed_out=True)
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        plan = _plan(LanConfig(), src, dst, art)
    assert plan["ok"] is False
    assert plan["error"]


def test_pl4_spawn_error_is_not_ok(tmp_path):
    src, dst, art, spec = _plan_fixture(tmp_path, "", None, exit_code=None,
                                        spawn_error="FileNotFoundError: missing")
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        plan = _plan(LanConfig(), src, dst, art)
    assert plan["ok"] is False
    assert plan["error"]


def test_pl5_plan_argv_is_the_builder_dry_run_argv_without_budget_flags(tmp_path):
    config = LanConfig()
    src, dst, art, spec = _plan_fixture(tmp_path, _fixture("lan_clean_dryrun.jsonl"),
                                        _fixture("lan_clean_combined.txt"))
    with fake_rclone_lan({"rclone-lan-plan": spec}) as fake:
        _plan(config, src, dst, art)
    assert fake.steps() == ["rclone-lan-plan"]
    argv = fake[0]["argv"]
    log = _flag(argv, "--log-file")
    combined = _flag(argv, "--combined")
    expected = lan_sync.build_lan_rclone_command(config, src, dst, str(log), dry_run=True,
                                                 combined_path=str(combined))
    assert argv[1:] == expected[1:]
    assert "--dry-run" in argv
    assert "--max-delete" not in argv
    assert "--max-duration" not in argv
    assert "--cutoff-mode" not in argv
    assert argv[argv.index(PROBE_PATTERN) - 1] == "--exclude"  # the probe file is never measured


def test_pl5b_plan_timeout_defaults_to_rclone_measure_timeout(tmp_path):
    config = LanConfig()
    src, dst, art, spec = _plan_fixture(tmp_path, _fixture("lan_clean_dryrun.jsonl"),
                                        _fixture("lan_clean_combined.txt"))
    with fake_rclone_lan({"rclone-lan-plan": spec}) as fake:
        _plan(config, src, dst, art)
        _plan(config, src, dst, art, timeout=77)
    assert fake[0]["timeout_s"] == config.rclone_measure_timeout_seconds
    assert fake[1]["timeout_s"] == 77
