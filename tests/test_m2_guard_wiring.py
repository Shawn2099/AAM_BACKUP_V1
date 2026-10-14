"""M2 2.2 — guards G1-G4 wired into both pipelines (plan WP3, §6.3).

Covers: guards config, per-FY baselines in run_history, LAN planned
deletions from the robocopy /L summary, cloud planned deletions from the
rclone --dry-run log + the --max-delete backstop, *_BLOCKED rows, and G1
replacing the top-level-only emptiness check.
"""

import json
from fractions import Fraction
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

import flow
from core import guards as g
from core.manifest import ManifestDB
from models.config import AppConfig, GuardsConfig


# ── Config ────────────────────────────────────────────────────────────────

def test_guards_config_defaults_are_owner_dec2_values():
    c = GuardsConfig()
    assert (c.max_delete_files, c.max_delete_percent, c.max_source_shrink_percent,
            c.forbid_local_destination) == (500, 10, 20, True)


@pytest.mark.parametrize("key,value", [
    ("max_delete_files", -1), ("max_delete_percent", 101),
    ("max_source_shrink_percent", -0.1), ("max_delete_typo", 1),
])
def test_guards_config_rejects_out_of_bounds_and_unknown_keys(key, value):
    with pytest.raises(ValidationError):
        GuardsConfig(**{key: value})


def test_app_config_has_guards_section():
    assert "guards" in AppConfig.model_fields


# ── Shared helpers in core.guards ─────────────────────────────────────────

def test_enforce_returns_record_when_all_pass():
    rec = g.enforce("lan", [g.g1_source_has_files("x.txt", "S")], fy="FY26-27")
    assert rec["fy"] == "FY26-27"
    assert rec["results"][0]["verdict"] == g.PASS


def test_enforce_raises_guard_blocked_with_every_block_reason():
    results = [
        g.g1_source_has_files(None, "S"),
        g.g3_mass_delete(900, 1000, 500, 10),
        g.g2_source_shrink(100, 100, 20),
    ]
    with pytest.raises(g.GuardBlocked) as ei:
        g.enforce("lan", results, fy="FY26-27", destination="\\\\nas\\s")
    exc = ei.value
    assert exc.leg == "lan"
    assert [r["guard"] for r in exc.record["results"] if r["verdict"] == g.BLOCK] == [
        "G1_SOURCE_HAS_FILES", "G3_MASS_DELETE"]
    assert "900" in str(exc) and "contains no files" in str(exc)
    json.dumps(exc.record)  # must be storable in extended_metrics


@settings(max_examples=300, deadline=None)
@given(dest=st.integers(0, 10**7), files=st.integers(0, 10**5),
       pct=st.sampled_from([0, 0.5, 1, 2.5, 5, 10, 12.5, 20, 33.3, 50, 100]))
def test_max_delete_allowance_matches_g3(dest, files, pct):
    allowed = g.max_delete_allowance(dest, files, pct)
    assert 0 <= allowed <= min(files, dest)
    # The backstop never allows more than G3 would pass ...
    assert g.g3_mass_delete(allowed, dest, files, pct).passed
    # ... and is not stricter than G3 (exact arithmetic on the configured %).
    if allowed < min(files, dest):
        assert allowed + 1 > files or Fraction(allowed + 1) * 100 > Fraction(dest) * Fraction(pct)


# ── ManifestDB per-FY baseline ────────────────────────────────────────────

def _row(db, run_id, status, started, guards=None, mode="lan"):
    em = json.dumps({"guards": guards}) if guards is not None else None
    db.insert_run({"run_id": run_id, "mode": mode, "started_at": started,
                   "ended_at": started, "status": status, "exit_code": 0,
                   "extended_metrics": em})


def test_guard_baseline_is_last_success_of_same_leg_and_fy(tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        assert db.guard_baseline("lan", "FY26-27") is None
        _row(db, "r1", "LAN_COMPLETE", "2026-10-01T21:00:00",
             {"fy": "FY26-27", "source_file_count": 100, "destination": "\\\\a\\s"})
        _row(db, "r2", "LAN_COMPLETE", "2026-10-02T21:00:00",
             {"fy": "FY26-27", "source_file_count": 110, "destination": "\\\\a\\s"})
        _row(db, "r3", "LAN_FAILED", "2026-10-03T21:00:00",          # not a success
             {"fy": "FY26-27", "source_file_count": 5, "destination": "\\\\b\\s"})
        _row(db, "r4", "LAN_COMPLETE", "2026-10-04T21:00:00",        # other FY
             {"fy": "FY25-26", "source_file_count": 9, "destination": "\\\\c\\s"})
        _row(db, "r5", "CLOUD_COMPLETE", "2026-10-05T21:00:00",      # other leg
             {"fy": "FY26-27", "source_file_count": 7, "destination": "gs://x"}, mode="cloud")
        _row(db, "r6", "LAN_COMPLETE", "2026-10-06T21:00:00")         # legacy row
        base = db.guard_baseline("lan", "FY26-27")
        assert base["source_file_count"] == 110 and base["destination"] == "\\\\a\\s"
        assert db.guard_baseline("cloud", "FY26-27")["source_file_count"] == 7
    finally:
        db.close()


# ── LAN: planned deletions from the robocopy /L job summary ───────────────

_ROBOCOPY_L_STDOUT = (
    "\t  *EXTRA File \t\t       2\tx.txt\n"
    "------------------------------------------------------------------------------\n"
    "\n"
    "               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
    "    Dirs :         3         1         2         0         0         2\n"
    "   Files :      1000        12       988         0         0         3\n"
    "   Bytes :     1.2 m     3.4 k     1.1 m         0         0       6 k\n"
    "   Ended : Wednesday, October 14, 2026 10:00:00 PM\n"
)


def test_lan_dry_run_keeps_job_summary_and_returns_counts(tmp_path):
    from core import lan_preflight

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / ".AAM_TARGET_MOUNTED").write_text("")
    done = MagicMock(returncode=1, stdout=_ROBOCOPY_L_STDOUT, stderr="")
    with patch.object(lan_preflight.subprocess, "run", return_value=done) as run:
        res = lan_preflight.run_lan_dry_run(str(tmp_path / "src"), str(dest))
    argv = run.call_args.args[0]
    assert "/L" in argv and "/NJS" not in argv
    assert res["ok"] is True
    assert res["counts"]["total"] == 1000 and res["counts"]["extras"] == 3


# Real robocopy 10.0 /L /MIR output (probe 2026-10-14): 3 files really get
# deleted (all listed as *EXTRA File, nested ones too), but the Files Extras
# column says 5 - it also counts the two /XF-excluded mount markers, which
# /MIR keeps. Planned deletions must come from the lines, not the column.
_ROBOCOPY_L_WITH_MARKERS = (
    "\t                   1\tC:\\p\\rs\\\n"
    "\t                   1\tC:\\p\\rd\\extra_dir\\\n"
    "\t  *EXTRA File \t\t       1\t1.txt\n"
    "\t                   1\tC:\\p\\rd\\extra_dir\\deep\\\n"
    "\t  *EXTRA File \t\t       0\t2.txt\n"
    "\t  *EXTRA File \t\t       2\tx_extra.txt\n"
    "\t    Older     \t\t       2\ta.txt\n"
    "------------------------------------------------------------------------------\n"
    "\n"
    "               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
    "    Dirs :         2         0         2         0         0         4\n"
    "   Files :         1         1         0         0         0         5\n"
    "   Bytes :         2         2         0         0         0         5\n"
    "   Ended : 14 October 2026 23:06:43\n"
)


def test_lan_planned_deletes_exclude_kept_mount_markers(tmp_path):
    from core import lan_preflight

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / ".AAM_TARGET_MOUNTED").write_text("")
    done = MagicMock(returncode=3, stdout=_ROBOCOPY_L_WITH_MARKERS, stderr="")
    with patch.object(lan_preflight.subprocess, "run", return_value=done):
        res = lan_preflight.run_lan_dry_run(str(tmp_path / "src"), str(dest))
    assert res["planned_deletes"] == 3
    assert res["counts"]["extras"] == 5


def test_lan_guards_use_planned_deletes_not_extras_column(tmp_path):
    """Lab regression: a fresh destination holding only the mount marker
    (Extras=1) was blocked as 'would delete 1 of 1'."""
    cfg = _lan_cfg(tmp_path)
    rec = _run_lan_guards(cfg, _counts(3, 1), 1, planned_deletes=0)
    assert [r for r in rec["results"] if r["guard"] == "G3_MASS_DELETE"][0]["metrics"][
        "planned_deletes"] == 0


def test_lan_guards_fail_when_extra_lines_exceed_extras_column(tmp_path):
    with pytest.raises(RuntimeError, match="inconsistent") as ei:
        _run_lan_guards(_lan_cfg(tmp_path), _counts(3, 1), 10, planned_deletes=2)
    assert not isinstance(ei.value, g.GuardBlocked)


def test_lan_snapshot_before_failure_is_unknown_not_empty():
    cfg = MagicMock()
    with patch("flow.walk_lan_destination", side_effect=OSError("SMB timeout")):
        assert flow.lan_snapshot_before_task.fn(cfg) is None


# ── LAN guards task ───────────────────────────────────────────────────────

def _lan_cfg(tmp_path, dest="\\\\10.0.0.9\\backup\\FY26-27", forbid_local=True):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "ledger.xlsx").write_text("x")
    cfg = MagicMock()
    cfg.paths.source_drive = str(src)
    cfg.paths.lan_destination = dest
    cfg.guards = GuardsConfig(forbid_local_destination=forbid_local)
    return cfg


def _run_lan_guards(cfg, counts, dest_files, baseline=None, dest_ips=frozenset({"10.0.0.9"}),
                    planned_deletes="extras"):
    db = MagicMock()
    db.guard_baseline.return_value = baseline
    if planned_deletes == "extras":  # default: every Extras entry really gets deleted
        planned_deletes = counts["extras"] if counts else None
    dry_run = {"ok": True, "counts": counts, "planned_deletes": planned_deletes}
    with patch("flow.ManifestDB") as mdb, \
         patch("flow.guards.resolve_host", return_value=set(dest_ips)), \
         patch("flow.guards.local_addresses", return_value={"127.0.0.1", "10.0.0.5"}):
        mdb.from_config.return_value = db
        return flow.lan_guards_task.fn(cfg, "FY26-27", dry_run, dest_files)


def _counts(total, extras):
    return {"total": total, "copied": 0, "skipped": total, "mismatch": 0,
            "failed": 0, "extras": extras}


def test_lan_guards_pass_and_return_baseline_record(tmp_path):
    rec = _run_lan_guards(_lan_cfg(tmp_path), _counts(1000, 3), 1000,
                          baseline={"fy": "FY26-27", "source_file_count": 990,
                                    "destination": "\\\\10.0.0.9\\backup\\FY26-27"})
    assert rec["fy"] == "FY26-27"
    assert rec["source_file_count"] == 1000
    assert rec["destination"] == "\\\\10.0.0.9\\backup\\FY26-27"
    assert rec["destination_files"] == 1000
    assert {r["guard"] for r in rec["results"]} == {
        "G1_SOURCE_HAS_FILES", "G2_SOURCE_SHRINK", "G3_MASS_DELETE", "G4_DESTINATION_IDENTITY"}


def test_lan_guards_block_mass_delete_from_extras(tmp_path):
    with pytest.raises(g.GuardBlocked) as ei:
        _run_lan_guards(_lan_cfg(tmp_path), _counts(400, 600), 1000)
    assert "would delete 600 of 1,000" in str(ei.value)


def test_lan_guards_block_source_shrink_against_same_fy_baseline(tmp_path):
    with pytest.raises(g.GuardBlocked, match="had 1,000"):
        _run_lan_guards(_lan_cfg(tmp_path), _counts(500, 0), 500,
                        baseline={"fy": "FY26-27", "source_file_count": 1000,
                                  "destination": "\\\\10.0.0.9\\backup\\FY26-27"})


def test_lan_guards_block_changed_and_loopback_destination(tmp_path):
    with pytest.raises(g.GuardBlocked, match="Destination changed"):
        _run_lan_guards(_lan_cfg(tmp_path), _counts(10, 0), 10,
                        baseline={"fy": "FY26-27", "source_file_count": 10,
                                  "destination": "\\\\10.0.0.8\\backup\\FY26-27"})
    loop = _lan_cfg(tmp_path, dest="\\\\127.0.0.1\\backup\\FY26-27")
    with pytest.raises(g.GuardBlocked, match="THIS machine"):
        _run_lan_guards(loop, _counts(10, 0), 10, dest_ips={"127.0.0.1"})
    no_forbid = _lan_cfg(tmp_path, dest="\\\\127.0.0.1\\backup\\FY26-27", forbid_local=False)
    assert _run_lan_guards(no_forbid, _counts(10, 0), 10, dest_ips={"127.0.0.1"})


def test_lan_guards_block_dirs_only_source(tmp_path):
    cfg = _lan_cfg(tmp_path)
    (tmp_path / "src" / "ledger.xlsx").unlink()
    (tmp_path / "src" / "Clients" / "2026").mkdir(parents=True)
    with pytest.raises(g.GuardBlocked, match="contains no files"):
        _run_lan_guards(cfg, _counts(0, 0), 0)


@pytest.mark.parametrize("counts,dest_files,match", [
    (None, 10, "job summary"),
    (_counts(10, 0), None, "destination could not be listed"),
])
def test_lan_guard_measurement_failure_fails_the_run(tmp_path, counts, dest_files, match):
    """A measurement that could not be taken is a FAILED run (retried next
    night), not a guard verdict a human could approve."""
    with pytest.raises(RuntimeError, match=match) as ei:
        _run_lan_guards(_lan_cfg(tmp_path), counts, dest_files)
    assert not isinstance(ei.value, g.GuardBlocked)


# ── LAN pipeline: BLOCKED row, nothing transferred ────────────────────────

def _pipeline_cfg():
    cfg = MagicMock()
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.lan.max_attempts = 2
    cfg.lan.retry_delay_seconds = 600
    cfg.lan.shutdown_on_all_retries_exhausted = False
    cfg.cloud.max_attempts = 2
    cfg.cloud.retry_delay_seconds = 60
    return cfg


def _blocked(leg):
    return g.GuardBlocked(leg, {"fy": "FY26-27", "source_file_count": 4,
                                "destination": "D", "results": [
                                    {"guard": "G3_MASS_DELETE", "verdict": g.BLOCK,
                                     "reason": "would delete 9 of 10", "metrics": {}}]})


@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.lan_shutdown_task")
@patch("flow.lan_sync_task")
@patch("flow.lan_guards_task")
@patch("flow.lan_snapshot_before_task", return_value={"a": (1, 1.0)})
@patch("flow.lan_preflight_task")
@patch("flow.wol_check_task")
@patch("flow.health_check_task")
@patch("flow._record_run")
def test_lan_pipeline_guard_block_records_lan_blocked(
    rec, _h, _w, _pre, _snap, guards_task, sync, shutdown, _art,
):
    guards_task.side_effect = _blocked("lan")
    with pytest.raises(g.GuardBlocked):
        flow._run_lan_pipeline(_pipeline_cfg(), "run-b", "2026-10-14T21:00:00")
    sync.with_options.return_value.assert_not_called()
    shutdown.assert_not_called()
    args, kwargs = rec.call_args
    assert args[4] == "LAN_BLOCKED"
    assert "would delete 9 of 10" in args[6]
    em = json.loads(kwargs["extended_metrics"])
    assert em["guards"]["results"][0]["guard"] == "G3_MASS_DELETE"
    assert em["failure_phase"] == "pre"


@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.lan_publish_artifact_task")
@patch("flow.lan_record_task")
@patch("flow.lan_snapshot_after_task", return_value={"a": (1, 1.0)})
@patch("flow.lan_shutdown_task")
@patch("flow.lan_sync_task")
@patch("flow.lan_guards_task")
@patch("flow.lan_snapshot_before_task", return_value={"a": (1, 1.0)})
@patch("flow.lan_preflight_task")
@patch("flow.wol_check_task")
@patch("flow.health_check_task")
@patch("flow._record_run")
def test_lan_pipeline_success_records_guard_baseline(
    rec, _h, _w, _pre, _snap, guards_task, sync, _shut, _after, _rt, _pub, _art,
):
    guards_task.return_value = {"fy": "FY26-27", "source_file_count": 1,
                                "destination": "D", "results": []}
    sync.with_options.return_value.return_value = {"status": "LAN_COMPLETE", "exit_code": 0}
    flow._run_lan_pipeline(_pipeline_cfg(), "run-ok", "2026-10-14T21:00:00")
    args, kwargs = rec.call_args
    assert args[4] == "LAN_COMPLETE"
    assert json.loads(kwargs["extended_metrics"])["guards"]["source_file_count"] == 1


# ── G1 replaces the top-level-only emptiness check (CL2, CL10, LA1) ───────

def test_health_no_longer_fails_an_empty_source_g1_blocks_it_instead(tmp_path):
    """Emptiness is a guard verdict (BLOCKED + approval), not a health
    failure; health still fails a missing source."""
    from core.health import check_source_drive
    assert check_source_drive(str(tmp_path)) == (True, "")
    assert check_source_drive(str(tmp_path / "missing"))[0] is False


@pytest.mark.parametrize("layout", ["empty", "dirs_only", "system_only"])
def test_sync_builders_refuse_a_source_without_real_files(tmp_path, layout):
    """Last line of defence at the moment the mirror command is built
    (also covers callers outside the pipelines, e.g. the rollover final
    backup until 2.6). Dirs-only passed the old top-level check (probe P6)."""
    from core.cloud_sync import build_rclone_sync_command
    from core.lan_sync import build_robocopy_command
    from models.config import LanConfig

    if layout == "dirs_only":
        (tmp_path / "Clients" / "2026").mkdir(parents=True)
    if layout == "system_only":
        (tmp_path / "System Volume Information").mkdir()
        (tmp_path / "desktop.ini").write_text("x")
    with pytest.raises(ValueError, match="contains no files"):
        build_robocopy_command(str(tmp_path), "\\\\nas\\s", LanConfig())
    with pytest.raises(ValueError, match="contains no files"):
        build_rclone_sync_command(str(tmp_path), "b", "FY26-27", "/c", "STANDARD")


def test_sync_builders_accept_a_deep_file(tmp_path):
    from core.cloud_sync import build_rclone_sync_command
    from core.lan_sync import build_robocopy_command
    from models.config import LanConfig

    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "ledger.xlsx").write_text("x")
    assert build_robocopy_command(str(tmp_path), "\\\\nas\\s", LanConfig())
    assert build_rclone_sync_command(str(tmp_path), "b", "FY26-27", "/c", "STANDARD")


# ── Cloud: planned deletions from the rclone --dry-run log ────────────────

# Real lines from rclone v1.74.3 `sync --dry-run --use-json-log` (probe
# 2026-10-14): 3 files would be deleted (2 inside a folder that goes too).
_RCLONE_DRY_RUN_LOG = "\n".join([
    '{"level":"notice","msg":"Skipped copy as --dry-run is set (size 2)","skipped":"copy","size":2,"object":"c.txt","objectType":"*local.Object"}',
    '{"level":"notice","msg":"Skipped update modification time as --dry-run is set (size 2)","skipped":"update modification time","size":2,"object":"a.txt","objectType":"*local.Object"}',
    '{"level":"notice","msg":"Skipped delete as --dry-run is set (size 2)","skipped":"delete","size":2,"object":"x.txt","objectType":"*local.Object"}',
    '{"level":"notice","msg":"Skipped delete as --dry-run is set (size 2)","skipped":"delete","size":2,"object":"extra_dir/deep/z.txt","objectType":"*local.Object"}',
    '{"level":"notice","msg":"Skipped delete as --dry-run is set (size 2)","skipped":"delete","size":2,"object":"extra_dir/y.txt","objectType":"*local.Object"}',
    '{"level":"notice","msg":"Skipped remove directory as --dry-run is set","skipped":"remove directory","object":"extra_dir","objectType":"string"}',
    "not json at all",
])


def test_count_planned_deletes_counts_file_deletes_only():
    from core.cloud_sync import count_planned_deletes
    assert count_planned_deletes(_RCLONE_DRY_RUN_LOG) == 3
    assert count_planned_deletes("") == 0


def test_sync_command_carries_max_delete_backstop(tmp_path):
    from core.cloud_sync import build_rclone_sync_command
    (tmp_path / "f.txt").write_text("x")
    argv = build_rclone_sync_command(str(tmp_path), "b", "FY26-27", "/c", "STANDARD", max_delete=7)
    assert argv[argv.index("--max-delete") + 1] == "7"
    assert "--max-delete" not in build_rclone_sync_command(
        str(tmp_path), "b", "FY26-27", "/c", "STANDARD")


def _fake_cfg_cm():
    import contextlib

    @contextlib.contextmanager
    def fake_cfg(*a, **k):
        yield "/tmp/fake.conf"
    return fake_cfg


@pytest.mark.parametrize("exit_code,log,expected", [
    (0, _RCLONE_DRY_RUN_LOG, 3),
    (9, _RCLONE_DRY_RUN_LOG, 3),          # nothing to copy, deletes still planned
    (7, _RCLONE_DRY_RUN_LOG, None),       # did not complete -> unknown
    (0, _RCLONE_DRY_RUN_LOG + '\n{"level":"error","msg":"listing failed"}', None),
])
def test_run_cloud_sync_dry_run_reports_planned_deletes(tmp_path, exit_code, log, expected):
    from core.cloud_sync import run_cloud_sync
    from test_support.fake_tools import fake_run_tool

    (tmp_path / "f.txt").write_text("x")
    with patch("core.cloud_sync.temp_rclone_config", _fake_cfg_cm()), \
         fake_run_tool("core.cloud_sync", exit_code=exit_code, log=log) as calls:
        res = run_cloud_sync(source=str(tmp_path), bucket="b", fy_prefix="FY26-27",
                             gcs_key_path="/k", project_number="1",
                             storage_class="STANDARD", dry_run=True)
    assert "--dry-run" in calls[0]["argv"] and "--max-delete" not in calls[0]["argv"]
    assert res["planned_deletes"] == expected


def test_real_sync_passes_max_delete(tmp_path):
    from core.cloud_sync import run_cloud_sync
    from test_support.fake_tools import fake_run_tool

    (tmp_path / "f.txt").write_text("x")
    with patch("core.cloud_sync.temp_rclone_config", _fake_cfg_cm()), \
         fake_run_tool("core.cloud_sync", exit_code=0) as calls:
        res = run_cloud_sync(source=str(tmp_path), bucket="b", fy_prefix="FY26-27",
                             gcs_key_path="/k", project_number="1",
                             storage_class="STANDARD", max_delete=12)
    argv = calls[0]["argv"]
    assert "--dry-run" not in argv and argv[argv.index("--max-delete") + 1] == "12"
    assert "planned_deletes" not in res


def test_count_objects_parses_rclone_size_and_fails_loudly(tmp_path):
    from core.cloud_reporter import CloudReporterError, count_objects
    from core.run_artefacts import RunArtefacts
    from test_support.fake_tools import fake_run_tool

    art = RunArtefacts(tmp_path, "r1")
    with fake_run_tool("core.cloud_reporter", stdout='{"count":3,"bytes":6,"sizeless":0}') as calls:
        assert count_objects("aam_gcs:b/FY26-27", "/c", 30, art) == 3
    assert calls[0]["argv"][1:3] == ["size", "aam_gcs:b/FY26-27"]
    for kwargs in ({"exit_code": 3, "stderr": "directory not found"},
                   {"timed_out": True}, {"stdout": "garbage"}, {"stdout": "{}"}):
        with fake_run_tool("core.cloud_reporter", **kwargs), pytest.raises(CloudReporterError):
            count_objects("aam_gcs:b/FY26-27", "/c", 30, art)


def test_cloud_dry_run_timeout_is_config():
    from models.config import CloudConfig
    assert CloudConfig.model_fields["dry_run_timeout_seconds"].default == 1800


# ── Cloud guards task ─────────────────────────────────────────────────────

def _cloud_cfg(tmp_path, with_file=True):
    src = tmp_path / "csrc"
    src.mkdir(exist_ok=True)
    if with_file:
        (src / "ledger.xlsx").write_text("x")
    cfg = MagicMock()
    cfg.paths.source_drive = str(src)
    cfg.cloud.bucket = "bkt"
    cfg.guards = GuardsConfig()
    return cfg


def _run_cloud_guards(cfg, source_files, dest_files, planned, baseline=None):
    """Run the real cloud-guards task with faked measurements.

    Returns (record or raised exception, run_cloud_sync mock)."""
    db = MagicMock()
    db.guard_baseline.return_value = baseline
    counts = {cfg.paths.source_drive: source_files, "aam_gcs:bkt/FY26-27": dest_files}

    def fake_count(target, *a, **k):
        v = counts[target]
        if isinstance(v, Exception):
            raise v
        return v

    dry = {"status": "CLOUD_COMPLETE", "error": None, "planned_deletes": planned}
    with patch("flow.ManifestDB") as mdb, \
         patch("flow.temp_rclone_config", _fake_cfg_cm()), \
         patch("flow.count_objects", side_effect=fake_count), \
         patch("flow.run_cloud_sync", return_value=dry) as run_sync:
        mdb.from_config.return_value = db
        try:
            return flow.cloud_guards_task.fn(cfg, "FY26-27", None), run_sync
        except Exception as e:  # noqa: BLE001 - returned for the assertions
            return e, run_sync


def test_cloud_guards_pass_and_return_backstop(tmp_path):
    rec, run_sync = _run_cloud_guards(_cloud_cfg(tmp_path), 1000, 1000, 3,
                                      baseline={"fy": "FY26-27", "source_file_count": 990,
                                                "destination": "gs://bkt/FY26-27"})
    assert run_sync.call_args.kwargs["dry_run"] is True
    assert rec["destination"] == "gs://bkt/FY26-27"
    assert rec["source_file_count"] == 1000 and rec["destination_files"] == 1000
    assert rec["max_delete"] == 100           # min(500 files, 10 % of 1000)


def test_cloud_guards_block_mass_delete(tmp_path):
    exc, _ = _run_cloud_guards(_cloud_cfg(tmp_path), 400, 1000, 600)
    assert isinstance(exc, g.GuardBlocked) and "would delete 600 of 1,000" in str(exc)


def test_cloud_guards_block_changed_bucket(tmp_path):
    exc, _ = _run_cloud_guards(_cloud_cfg(tmp_path), 10, 10, 0,
                               baseline={"fy": "FY26-27", "source_file_count": 10,
                                         "destination": "gs://other/FY26-27"})
    assert isinstance(exc, g.GuardBlocked) and "Destination changed" in str(exc)


def test_cloud_guards_dirs_only_source_blocks_without_dry_run(tmp_path):
    cfg = _cloud_cfg(tmp_path, with_file=False)
    (tmp_path / "csrc" / "Clients").mkdir()
    exc, run_sync = _run_cloud_guards(cfg, 0, 1000, None)
    assert isinstance(exc, g.GuardBlocked) and "contains no files" in str(exc)
    run_sync.assert_not_called()


def test_cloud_guard_measurement_failures_fail_the_run(tmp_path):
    from core.cloud_reporter import CloudReporterError

    for src, dst, planned, match in (
        (CloudReporterError("size exited 7"), 10, 0, "size exited 7"),
        (10, CloudReporterError("dial tcp: no such host"), 0, "no such host"),
        (10, 10, None, "dry-run"),
    ):
        exc, _ = _run_cloud_guards(_cloud_cfg(tmp_path), src, dst, planned)
        assert isinstance(exc, RuntimeError) and not isinstance(exc, g.GuardBlocked)
        assert match in str(exc)


# ── Cloud pipeline: BLOCKED row; backstop handed to the sync ──────────────

@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.cloud_sync_task")
@patch("flow.cloud_guards_task")
@patch("flow.cloud_preflight_task")
@patch("flow.health_check_task")
@patch("flow.ManifestDB")
@patch("flow._record_run")
def test_cloud_pipeline_guard_block_records_cloud_blocked(
    rec, _db, _h, _pre, guards_task, sync, _art,
):
    guards_task.side_effect = _blocked("cloud")
    with pytest.raises(g.GuardBlocked):
        flow._run_cloud_pipeline(_pipeline_cfg(), "run-cb", "2026-10-14T22:00:00")
    sync.with_options.return_value.assert_not_called()
    args, kwargs = rec.call_args
    assert args[4] == "CLOUD_BLOCKED"
    em = json.loads(kwargs["extended_metrics"])
    assert em["guards"]["results"][0]["verdict"] == g.BLOCK


@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.cloud_publish_artifact_task")
@patch("flow.cloud_record_task")
@patch("flow.cloud_verify_and_report_task")
@patch("flow.cloud_sync_task")
@patch("flow.cloud_guards_task")
@patch("flow.cloud_preflight_task")
@patch("flow.health_check_task")
@patch("flow.ManifestDB")
@patch("flow._record_run")
def test_cloud_pipeline_hands_backstop_to_sync_and_records_baseline(
    rec, _db, _h, _pre, guards_task, sync, verify, _rt, _pub, _art,
):
    guards_task.return_value = {"fy": "FY26-27", "source_file_count": 5,
                                "destination": "gs://bkt/FY26-27", "max_delete": 42,
                                "results": []}
    sync.with_options.return_value.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0}
    verify.with_options.return_value.return_value = {
        "verified": True, "verify_termination": "normal",
        "size": {"count": 1, "bytes": 1}, "manifest": [],
        "diff": {"added": [], "removed": [], "modified": []},
    }
    flow._run_cloud_pipeline(_pipeline_cfg(), "run-cok", "2026-10-14T22:00:00")
    assert sync.with_options.return_value.call_args.kwargs["max_delete"] == 42
    args, kwargs = rec.call_args
    assert args[4] == "CLOUD_COMPLETE"
    assert json.loads(kwargs["extended_metrics"])["guards"]["max_delete"] == 42
