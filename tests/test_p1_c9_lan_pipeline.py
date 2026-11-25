"""Plan P1 chunk C9: sync, verify and post wiring of the LAN pipeline
(docs/PLAN_P1_RCLONE_LAN_2026-11-25.md section 2 steps 6-9, matrix rows W1-W13 and W-fx).

Every row runs flow._run_lan_pipeline end to end: the real preflight, guards and access tasks
(C8), the real sync and arrival wrappers (C6, C7) and the real rclone output fixtures, with
fake_rclone_lan as the only rclone. Collaborators outside the LAN leg (database, alerts,
backup slot, WoL, NAS shutdown, permission probes, health, sleep) are mocks, as in
tests/test_fy_overlap_pipelines.py.

Assumed names of the C9 code (not in the tree yet):
  flow.lan_sync_task(config, artefacts_dir, max_delete=..., budget=...) -> lan_sync.run_lan_mirror
  flow.lan_verify_task(config, artefacts_dir, changed_after_epoch=..., budget=...)
      -> lan_sync.check_lan_arrival(config.lan, source, dest, artefacts_dir,
                                    changed_after_epoch=..., timeout=...)
  faults codes AAM-LAN-02/03/04, AAM-DEL-03 (exist in core/faults.py);
  statuses LAN_COMPLETE/PARTIAL/SUSPECT/BLOCKED/FAILED (exist in core/statuses.py).

Against the code before C9 the run stops at the first robocopy call: the fake reports
"no spec for step 'robocopy'" and the pipeline ends LAN_FAILED.
"""

import contextlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pendulum
import pytest

import flow
from core import cloud_sync, faults, fy_overlap, lan_sync
from core import guards as g
from core.budget import RunBudget
from core.run_artefacts import RunArtefacts
from models.config import GuardsConfig, LanConfig
from test_support.fake_tools import fake_rclone_lan

FIXTURES = Path(__file__).resolve().parents[1] / "test_support" / "rclone_logs"
RUN_ID = "3f2a9c1e-77aa-4c6b-9d11-0a1b2c3d4e5f-lan"
STARTED = pendulum.datetime(2026, 11, 25, 9, 0, tz="Asia/Kolkata")
STARTED_ISO = "2026-11-25T09:00:00+05:30"
LAN_BASE = "flowrun-9-lan"
CANARY = ".AAM_TARGET_MOUNTED"
PLAN, SYNC, CHECK = "rclone-lan-plan", "rclone-lan-sync", "rclone-lan-check"
HASH = "rclone-lan-hash"   # R8d: the hash check of tonight's copies (matrix section 8)


# ── fixtures and specs ────────────────────────────────────────────────────

def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def clean_plan() -> dict:
    """Real C0b dry-run: 12 files to copy (13062 bytes), empty destination."""
    return {"exit_code": 0, "log": _fx("lan_clean_dryrun.jsonl"),
            "combined": _fx("lan_clean_combined.txt")}


def plan_spec(dest_files: int = 1000, deletes: int = 0) -> dict:
    """Dry-run with `dest_files` files at the destination, `deletes` of them to be removed."""
    keep = dest_files - deletes
    combined = ("".join(f"= keep{i:04d}.txt\n" for i in range(keep))
                + "".join(f"- gone{i:04d}.txt\n" for i in range(deletes)))
    lines = [json.dumps({"level": "notice", "msg": "Skipped delete as --dry-run is set",
                         "skipped": "delete", "object": f"gone{i:04d}.txt"})
             for i in range(deletes)]
    lines.append(json.dumps({"level": "info", "msg": "stats",
                             "stats": {"bytes": 0, "deletes": deletes, "errors": 0,
                                       "transfers": 0, "renames": 0, "checks": 0}}))
    return {"exit_code": 0, "log": "\n".join(lines) + "\n", "combined": combined}


def sync_ok() -> dict:
    return {"exit_code": 0, "log": _fx("lan_clean.jsonl")}


def check_clean() -> dict:
    """Real C0b arrival check: 9 matching files, no differences."""
    return {"exit_code": 0, "stderr": _fx("lan_check_clean.stderr.txt"),
            "combined": _fx("lan_check_clean_combined.txt")}


def check_missing() -> dict:
    """Real C0b arrival check: sub/deep/c.txt is in the source but not at the destination."""
    return {"exit_code": 1, "stderr": _fx("lan_check_missing.stderr.txt"),
            "combined": _fx("lan_check_missing_combined.txt")}


def hash_clean(listed: int = 12) -> dict:
    """Real clean hash check of the twelve lan_clean files (R11 fixture lan_hash12_clean), exit 0.
    For a list of another length the same output is used with only its count changed, the
    count rclone prints for a clean check of that many files (LAN_FIXTURES.md, R11)."""
    stderr = _fx("lan_hash12_clean.stderr.txt")
    if listed != 12:
        stderr = stderr.replace("12 matching files", f"{listed} matching files")
    return {"exit_code": 0, "stderr": stderr,
            "combined": _fx("lan_hash12_clean_combined.txt")}


def copy_log(files: list[tuple[str, int | None]]) -> str:
    """A synthetic sync JSON log: one `Copied (new)` entry per (name, size) (size None = no size
    field), then a stats line with the transfer totals, as rclone prints at exit."""
    lines = []
    for name, size in files:
        entry = {"level": "info", "msg": "Copied (new)", "object": name,
                 "objectType": "*local.Object"}
        if size is not None:
            entry["size"] = size
        lines.append(json.dumps(entry))
    total = sum(size for _, size in files if size is not None)
    lines.append(json.dumps({"level": "info", "msg": "stats", "stats": {
        "transfers": len(files), "bytes": total, "deletes": 0, "errors": 0,
        "renames": 0, "checks": 0}}))
    return "\n".join(lines) + "\n"


def _argv_value(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


class _Direct:
    """Stands in for a Prefect task: with_options() returns it, calling it runs the body."""

    def __init__(self, fn):
        self.fn = fn

    def with_options(self, **kwargs):
        return self

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)


# ── configuration and source tree ─────────────────────────────────────────

def make_source(root: Path) -> Path:
    src = root / "src"
    (src / "sub" / "deep").mkdir(parents=True, exist_ok=True)
    (src / "photos").mkdir(exist_ok=True)
    (src / "ledger.xlsx").write_text("ledger", encoding="utf-8")
    (src / "sub" / "deep" / "c.txt").write_text("c" * 10, encoding="utf-8")
    (src / "photos" / "p1.jpg").write_text("p" * 20, encoding="utf-8")
    return src


def make_destination(root: Path, name: str = "dest") -> Path:
    dest = root / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / CANARY).write_text("canary", encoding="utf-8")
    return dest


def make_config(root: Path, *, dest_name: str = "dest", max_attempts: int = 2,
                modify_window_seconds: int = 2, lan_hours: float = 6.0, **lan_overrides):
    src = make_source(root)
    dest = make_destination(root, dest_name)
    cfg = MagicMock()
    cfg.firm_name = "TestCA"
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = str(src)
    cfg.paths.lan_destination = str(dest)
    cfg.paths.gcs_key_path = None
    cfg.budget.lan_max_hours = lan_hours
    cfg.budget.lan_post_reserve_minutes = 10
    cfg.budget.cloud_max_hours = lan_hours
    cfg.budget.cloud_verify_reserve_minutes = 10
    cfg.wol.smb_connect_timeout_seconds = 1.0
    cfg.guards = GuardsConfig()
    cfg.lan = LanConfig(max_attempts=max_attempts, retry_delay_seconds=60,
                        preflight_retries=0, shutdown_on_all_retries_exhausted=False,
                        modify_window_seconds=modify_window_seconds, **lan_overrides)
    return cfg


# ── the world: every collaborator outside the LAN leg is a mock ───────────

@contextlib.contextmanager
def lan_world(tmp_path: Path, script: dict, *, spend_budget_after_sync: bool = False):
    """Enter the fake rclone and the mocks. Yields a namespace:
    fake (FakeRcloneLan), stamps (wall clock just before each sync attempt), arrival_kwargs
    (kwargs of every arrival check), notify, record_run, block_request, shutdown, mocks (the
    NAS-side tasks that must never run on the LAN leg), sleep."""
    base = tmp_path / "runlogs"
    w = SimpleNamespace(stamps=[], arrival_kwargs=[], spent={"value": False})
    with contextlib.ExitStack() as st:
        def p(target, **kw):
            return st.enter_context(patch(target, **kw))

        w.fake = st.enter_context(fake_rclone_lan(script))
        inner = lan_sync.run_tool   # the fake (patched by fake_rclone_lan)

        def spy(name, argv, **kw):
            if name == SYNC:
                w.stamps.append(time.time())
            result = inner(name, argv, **kw)
            if name == SYNC and spend_budget_after_sync:
                w.spent["value"] = True
            return result

        p("core.lan_sync.run_tool", side_effect=spy)
        real_check = lan_sync.check_lan_arrival
        arrival_a = p("core.lan_sync.check_lan_arrival", wraps=real_check)
        arrival_b = p("flow.check_lan_arrival", wraps=real_check, create=True)
        w.arrival_spies = (arrival_a, arrival_b)
        real_remaining = RunBudget.remaining
        st.enter_context(patch.object(
            RunBudget, "remaining",
            lambda self, clock=time.monotonic: 0.0 if w.spent["value"] else real_remaining(self, clock)))

        # The LAN tasks run their bodies directly: a Prefect engine call outside a flow needs a
        # Prefect API server (the existing LAN tests do the same, see test_p1_c8).
        for name in ("lan_preflight_task", "lan_guards_task", "lan_access_task",
                     "lan_sync_task", "lan_verify_task"):
            if hasattr(flow, name):
                p(f"flow.{name}", new=_Direct(getattr(flow, name).fn))
        p("flow.health_check_task")
        p("flow.wol_check_task")
        p("flow._complete_pending_rollover_nas_folder")
        p("flow._ensure_pass_nas_folder")
        p("flow._record_running")
        p("flow._backup_slot", side_effect=lambda config: contextlib.nullcontext())
        p("flow._open_run_artefacts", side_effect=lambda config, run_id, started_at=None:
          RunArtefacts(base, run_id, started=STARTED))
        p("flow._finalize_run_artefacts")
        p("flow._send_recovery_if_needed")
        w.notify = p("flow.notify")
        w.record_run = p("flow._record_run")
        w.block_request = p("flow._open_block_request")
        w.shutdown = p("flow.lan_shutdown_task")
        w.sleep = p("flow.time.sleep")
        w.mocks = {name: p(f"flow.{name}", create=True) for name in (
            "lan_snapshot_before_task", "lan_snapshot_after_task", "lan_record_task",
            "lan_publish_artifact_task")}
        db = p("flow.ManifestDB")
        db.from_config.return_value = db.return_value
        db.return_value.guard_baseline.return_value = None
        db.return_value.get_approval.return_value = None
        db.return_value.previous_run_status.return_value = None
        db.return_value.previous_run_fault_code.return_value = None
        db.return_value.overlap_record.return_value = None
        w.db = db.return_value
        p("flow.guards.resolve_host", return_value={"10.10.151.53"})
        p("flow.guards.local_addresses", return_value={"127.0.0.1"})
        p("flow.permissions.probe_source_read")
        p("flow.permissions.probe_destination_write")
        p("flow.permissions.check_destination_free_space")
        yield w


def run_lan(w, cfg, *, budget_hours=None, fy_pass=None, allow_shutdown=True):
    """Run the LAN pipeline once. Returns exc (None or the raised exception), and the row
    written for this run (status, exit code, kwargs) when the run wrote one."""
    out = SimpleNamespace(exc=None, result=None)
    before = len(w.record_run.call_args_list)
    try:
        out.result = flow._run_lan_pipeline(
            cfg, LAN_BASE, STARTED_ISO, time.monotonic(), budget_hours=budget_hours,
            allow_shutdown=allow_shutdown, fy_pass=fy_pass)
    except BaseException as e:  # noqa: BLE001 - every outcome is asserted by the rows
        out.exc = e
    calls = w.record_run.call_args_list[before:]
    out.rows = [{"status": c.args[4], "exit_code": c.args[5], "kwargs": c.kwargs,
                 "metrics": json.loads(c.kwargs["extended_metrics"])
                 if c.kwargs.get("extended_metrics") else {}} for c in calls]
    out.row = out.rows[-1] if out.rows else None
    out.metrics = out.row["metrics"] if out.row else {}
    out.status = out.row["status"] if out.row else None
    return out


def failures(w) -> list:
    """The FAILURE alerts of this world (one per failed run is the rule)."""
    events = [c.args[1] for c in w.notify.call_args_list]
    return [e for e in events if getattr(e, "kind", None) == "FAILURE"]


def arrival_kwargs(w) -> list[dict]:
    calls = []
    for spy in w.arrival_spies:
        calls += [c.kwargs for c in spy.call_args_list]
    return calls


def steps(w) -> list[str]:
    return w.fake.steps()


def sync_argvs(w) -> list[list[str]]:
    return [c["argv"] for c in w.fake.calls if c["step"] == SYNC]


# ── W1 happy path ──────────────────────────────────────────────────────────

def test_w1_happy_path_is_complete_with_three_rclone_calls_and_the_post_metrics(tmp_path):
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: hash_clean()}
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"LAN run raised: {out.exc!r}; rclone calls: {steps(w)}"
    assert out.status == "LAN_COMPLETE"
    # the four rclone calls in order (the hash runs because files were copied), no robocopy
    assert steps(w) == [PLAN, SYNC, CHECK, HASH]
    assert not any("robocopy" in " ".join(c["argv"]).lower() for c in w.fake.calls)
    # run row: the rclone transfer totals
    stats = cloud_sync.final_stats(_fx("lan_clean.jsonl"))
    assert out.row["kwargs"]["files_copied"] == stats["transfers"]
    assert out.row["kwargs"]["bytes_copied"] == stats["bytes"]
    m = out.metrics
    assert m["counts_source"] == "rclone"
    assert set(m["tool_counts"]) >= {"transfers", "bytes", "deletes", "renames", "errors", "checks"}
    assert {"files_deleted", "moves", "deletes_held", "failed_paths"} <= set(m)
    arrival = m["arrival"]
    assert set(arrival) >= {"verified", "matching", "missing", "differ", "changed_during_backup"}
    assert arrival["verified"] is True and arrival["matching"] == 9
    assert m["total_files"] == arrival["matching"]
    assert "sync_s" in m["stage_seconds"]


# ── W2 locked file ─────────────────────────────────────────────────────────

def test_w2_locked_file_is_partial_one_alert_naming_it_no_shutdown_no_arrival_check(tmp_path):
    script = {PLAN: clean_plan(),
              SYNC: {"exit_code": 1, "log": _fx("locked_file_exit1.jsonl")}}
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_PARTIAL", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, flow.PartialRun)
    alerts = failures(w)
    assert len(alerts) == 1, [a.message for a in alerts]
    assert "locked" in alerts[0].message
    assert "No deletions" in alerts[0].message
    assert alerts[0].fault_code == "AAM-LAN-02"
    assert w.shutdown.call_count == 0
    assert steps(w) == [PLAN, SYNC], "a PARTIAL run must not run the arrival check"
    assert arrival_kwargs(w) == []


# ── W3 max-delete backstop ─────────────────────────────────────────────────

def test_w3_delete_backstop_is_blocked_with_an_approval_request_and_no_retry(tmp_path):
    script = {PLAN: [plan_spec(1000, 3), plan_spec(1000, 3)],
              SYNC: {"exit_code": 7, "log": _fx("max_delete_exit7.jsonl")}}
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_BLOCKED", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, g.GuardBlocked)
    assert w.block_request.call_count == 1, "the backstop must open one approval request"
    assert out.metrics.get("fault_code") == "AAM-DEL-03"
    assert [s for s in steps(w) if s == SYNC] == [SYNC], "the backstop is never retried"
    assert w.shutdown.call_count == 0


# ── W4 G3 block in the plan ────────────────────────────────────────────────

def test_w4_g3_block_in_the_plan_blocks_and_the_sync_is_never_called(tmp_path):
    # 400 of 1000 destination files planned for deletion: far above the G3 floor
    script = {PLAN: plan_spec(1000, 400), SYNC: sync_ok(), CHECK: check_clean()}
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_BLOCKED", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, g.GuardBlocked)
    assert SYNC not in steps(w)
    assert w.block_request.call_count == 1
    assert w.shutdown.call_count == 0


# ── W5 real missing file at arrival ────────────────────────────────────────

def test_w5_missing_file_at_arrival_is_suspect_one_alert_and_no_shutdown(tmp_path):
    cfg = make_config(tmp_path, modify_window_seconds=0)
    # the source file is older than the sync: it was not changed during the backup
    time.sleep(0.3)
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_missing()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_SUSPECT", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, flow.PartialRun), "SUSPECT must raise, never a Prefect COMPLETED"
    alerts = failures(w)
    assert len(alerts) == 1
    assert alerts[0].fault_code == "AAM-LAN-04"
    assert w.shutdown.call_count == 0
    assert out.metrics["arrival"]["verified"] is False


# ── W6 file changed during the backup only ────────────────────────────────

def test_w6_file_changed_during_the_backup_only_is_complete_and_counted(tmp_path):
    cfg = make_config(tmp_path)
    changed = cfg.paths.source_drive
    future = time.time() + 3600
    os.utime(Path(changed) / "sub" / "deep" / "c.txt", (future, future))
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_missing(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert out.status == "LAN_COMPLETE"
    assert out.metrics["arrival"]["changed_during_backup"] >= 1


# ── W7 exit 5 then exit 0: remeasure, lowered cap, two attempts ───────────

def _w7_script():
    return {PLAN: [plan_spec(1000, 0), plan_spec(1000, 0)],
            SYNC: [{"exit_code": 5, "log": _fx("lan_deletes.jsonl")}, sync_ok()],
            CHECK: check_clean(), HASH: hash_clean()}


def test_w7_retryable_failure_remeasures_and_lowers_the_second_cap(tmp_path):
    cfg = make_config(tmp_path)
    deleted_first = cloud_sync.final_stats(_fx("lan_deletes.jsonl"))["deletes"]
    with lan_world(tmp_path, _w7_script()) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert out.status == "LAN_COMPLETE"
    assert steps(w) == [PLAN, SYNC, PLAN, SYNC, CHECK, HASH], "each retry measures again first"
    first, second = (int(_argv_value(a, "--max-delete")) for a in sync_argvs(w))
    assert second == first - deleted_first
    assert len(out.metrics["sync_attempts"]) == 2


# ── W8 budget-limited run ──────────────────────────────────────────────────

def test_w8_budget_limited_sync_is_soft_cut_partial_and_has_no_arrival_check(tmp_path):
    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(),
              SYNC: {"exit_code": 10, "log": _fx("max_duration_exit10.jsonl")}}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg, budget_hours=0.5)
    assert out.status == "LAN_PARTIAL", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, flow.PartialRun)
    argv = sync_argvs(w)[0]
    assert "--max-duration" in argv and _argv_value(argv, "--cutoff-mode") == "SOFT"
    assert any("Stopped at the run budget" in a.message for a in failures(w))
    assert steps(w) == [PLAN, SYNC], "a budget stop skips the arrival check"
    assert arrival_kwargs(w) == []
    assert w.shutdown.call_count == 0


# ── W9 no budget left for the arrival check ───────────────────────────────

def test_w9_no_budget_for_the_arrival_check_is_partial_copied_not_verified(tmp_path):
    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean()}
    with lan_world(tmp_path, script, spend_budget_after_sync=True) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_PARTIAL", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, flow.PartialRun)
    alerts = failures(w)
    assert len(alerts) == 1
    assert alerts[0].fault_code == "AAM-LAN-03"
    assert "copied but not verified" in alerts[0].message
    assert SYNC in steps(w) and CHECK not in steps(w)
    assert w.shutdown.call_count == 0


# ── W10 FY overlap: two passes, own calls and destination each ────────────

def test_w10_two_passes_each_make_three_calls_on_their_own_destination(tmp_path):
    cfg_cur = make_config(tmp_path, dest_name="FY26-27")
    cfg_next = make_config(tmp_path, dest_name="FY27-28")
    script = {PLAN: [clean_plan(), clean_plan()], SYNC: [sync_ok(), sync_ok()],
              CHECK: [check_clean(), check_clean()], HASH: hash_clean()}
    cur = fy_overlap.FyPass(fy="FY26-27", extra=False, config=cfg_cur)
    nxt = fy_overlap.FyPass(fy="FY27-28", extra=True, config=cfg_next)
    with lan_world(tmp_path, script) as w:
        first = run_lan(w, cfg_cur, fy_pass=cur, allow_shutdown=False)
        after_first = w.shutdown.call_count
        second = run_lan(w, cfg_next, fy_pass=nxt, allow_shutdown=True)
    assert first.exc is None and second.exc is None, (first.exc, second.exc)
    assert first.status == "LAN_COMPLETE" and second.status == "LAN_COMPLETE"
    assert steps(w) == [PLAN, SYNC, CHECK, HASH] * 2
    dest_now, dest_next = str(cfg_cur.paths.lan_destination), str(cfg_next.paths.lan_destination)
    sync_dests = [c["argv"] for c in w.fake.calls if c["step"] == SYNC]
    assert dest_now in " ".join(sync_dests[0]) and dest_next not in " ".join(sync_dests[0])
    assert dest_next in " ".join(sync_dests[1]) and dest_now not in " ".join(sync_dests[1])
    # the shutdown rule is the flags' rule: only the last pass powers the NAS off
    assert after_first == 0
    assert w.shutdown.call_count == 1


# ── W11 health check: rclone binary, no privilege probe ───────────────────

def test_w11_health_check_for_lan_needs_rclone_and_runs_no_privilege_probe(tmp_path):
    cfg = make_config(tmp_path)
    checked: list[str] = []

    def binary(name):
        checked.append(name)
        return True

    art = RunArtefacts(tmp_path / "health", RUN_ID, started=STARTED)
    with patch("core.health.source_drive_fault", return_value=(None, "")), \
            patch("core.health.check_binary_exists", side_effect=binary), \
            patch("core.health.rclone_version_fault", return_value=None), \
            patch("flow.permissions.check_backup_privilege", create=True) as privilege, \
            patch("core.health.run_tool") as health_tool, \
            patch("core.permissions.run_tool", create=True) as perm_tool:
        flow.health_check_task.fn(cfg, "lan", str(art.dir))
    assert "rclone" in checked and "robocopy" not in checked
    privilege.assert_not_called()
    health_tool.assert_not_called()
    perm_tool.assert_not_called()


def test_w11b_missing_rclone_binary_fails_coded_for_lan(tmp_path):
    cfg = make_config(tmp_path)
    art = RunArtefacts(tmp_path / "health", RUN_ID, started=STARTED)

    def binary(name):
        return name != "rclone"   # only rclone is missing; robocopy is not needed any more

    with patch("core.health.source_drive_fault", return_value=(None, "")), \
            patch("core.health.check_binary_exists", side_effect=binary):
        with pytest.raises(Exception) as exc:
            flow.health_check_task.fn(cfg, "lan", str(art.dir))
    assert faults.code_of(exc.value) == "AAM-SYS-04"


# ── W12 the run row is written on every exception path ────────────────────

@pytest.mark.parametrize("failing_step", [PLAN, SYNC, CHECK])
def test_w12_run_row_is_written_when_a_tool_raises(tmp_path, failing_step):
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean()}
    script[failing_step] = {"raise": RuntimeError("boom from the fake")}
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is not None, "the raised error must reach the caller"
    assert len(out.rows) == 1, "exactly one run_history row on the exception path"
    assert out.status != "LAN_COMPLETE"


# ── W13 no LAN file_entries rows; no snapshot, record or publish calls ────

def test_w13_no_file_entries_and_no_snapshot_record_or_publish_calls(tmp_path):
    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_COMPLETE", f"exc {out.exc!r}"
    for name, mock in w.mocks.items():
        assert mock.call_count == 0, f"{name} must not be called by the LAN pipeline"
    w.db.record_sync_results.assert_not_called()


# ── W-fx the arrival check gets the clock taken just before the last sync attempt ──

def test_wfx_arrival_check_gets_the_clock_from_just_before_the_last_sync_attempt(tmp_path):
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, _w7_script()) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_COMPLETE", f"exc {out.exc!r}"
    kwargs = arrival_kwargs(w)
    assert len(kwargs) == 1, "one arrival check, after the last attempt"
    epoch = kwargs[0]["changed_after_epoch"]
    first_start, last_start = w.stamps[0], w.stamps[-1]
    assert first_start < epoch <= last_start, "not from the last sync attempt"
    assert last_start - epoch < 5, "taken well before the sync: a stale clock would miss changes"


# ── NAS shutdown result is recorded in the row; the status stays COMPLETE ──
# (moved here from test_m4_shutdown.py when the robocopy pipeline mocks were removed, C11)

def test_shutdown_result_is_recorded_in_the_row_and_status_stays_complete(tmp_path):
    from core import shutdown as sd
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: hash_clean()}
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        w.shutdown.return_value = {"outcome": sd.FAILED, "server_ip": "10.0.0.5", "exit_code": 5,
                                   "detail": "Access is denied.(5)", "elapsed_s": 0.1}
        out = run_lan(w, cfg)
    assert out.exc is None, out.exc
    assert out.status == "LAN_COMPLETE"
    assert out.metrics["nas_shutdown"]["outcome"] == sd.FAILED


# ── W14 hang after the SOFT cutoff (plan review amendment S5, chunk R6) ───
# Matrix: docs/TESTMATRIX_P1_REVIEW_2026-11-25.md section 6. rclone was given --max-duration
# with --cutoff-mode SOFT and still did not stop: the run is LAN_PARTIAL with killed_after_cutoff,
# fault AAM-LAN-04, not "Stopped at the run budget".

HANG_SYNC = {"timed_out": True, "log": ""}   # fake: no exit code, the hard timeout killed rclone


def _capture_sync_result(captured: list):
    """Stands in for flow.lan_sync_task and keeps what the task returned."""
    real = flow.lan_sync_task.fn

    def capturing(*args, **kwargs):
        result = real(*args, **kwargs)
        captured.append(result)
        return result

    return _Direct(capturing)


def test_w14_hang_after_cutoff_is_partial_killed_after_cutoff_no_check_no_shutdown(tmp_path):
    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(), SYNC: HANG_SYNC}
    captured: list = []
    with lan_world(tmp_path, script) as w:
        with patch.object(flow, "lan_sync_task", _capture_sync_result(captured)):
            out = run_lan(w, cfg, budget_hours=0.5)
    assert out.status == "LAN_PARTIAL", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, flow.PartialRun)
    assert captured, "the sync task returned no result"
    assert captured[0].get("killed_after_cutoff") is True
    assert not captured[0].get("budget_stopped")
    alerts = failures(w)
    assert len(alerts) == 1
    assert alerts[0].fault_code == "AAM-LAN-04"
    assert "did not stop" in alerts[0].message
    assert "Stopped at the run budget" not in alerts[0].message
    assert steps(w) == [PLAN, SYNC], "a hang skips the arrival check"
    assert arrival_kwargs(w) == []
    assert w.shutdown.call_count == 0
    assert len(sync_argvs(w)) == 1, "a hang is never retried"
    # R6 (matrix section 6): the exact text, with the 300 s margin of MAX_DURATION_MARGIN_S,
    # --max-duration as resolve_max_duration_seconds gives it for the killed timeout, and the
    # hard timeout that was used. No stats in HANG_SYNC, so the progress part is "unknown".
    sync_calls = [c for c in w.fake.calls if c["step"] == SYNC]
    killed_after = sync_calls[0]["timeout_s"]
    max_duration = cloud_sync.resolve_max_duration_seconds(killed_after, None)
    assert max_duration is not None
    expected = (f"rclone did not stop within {cloud_sync.MAX_DURATION_MARGIN_S} s after its time "
                f"limit (--max-duration {max_duration}s, --cutoff-mode SOFT) and was killed after "
                f"{killed_after}s - a large file may still have been copying, or NAS I/O may be "
                "hung. progress unknown (no rclone stats)")
    assert captured[0].get("error") == expected, captured[0].get("error")
    assert "did not stop within" in alerts[0].message


def test_w14b_limited_timeout_too_small_for_a_cutoff_keeps_the_budget_stop(tmp_path):
    # budget 850 s: after the 600 s post reserve about 250 s are left, so the hard timeout is
    # <= 300 s, max_duration is None and no cutoff margin exists: a timeout here is a budget stop.
    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(), SYNC: HANG_SYNC}
    captured: list = []
    with lan_world(tmp_path, script) as w:
        with patch.object(flow, "lan_sync_task", _capture_sync_result(captured)):
            out = run_lan(w, cfg, budget_hours=850 / 3600)
    assert out.status == "LAN_PARTIAL", f"status {out.status}; exc {out.exc!r}"
    assert captured and captured[0].get("budget_stopped") is True
    assert not captured[0].get("killed_after_cutoff")
    alerts = failures(w)
    assert len(alerts) == 1
    assert "Stopped at the run budget" in alerts[0].message
    assert "did not stop" not in alerts[0].message


def test_w14c_exit_10_with_a_limit_is_still_a_budget_stop(tmp_path):
    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(),
              SYNC: {"exit_code": 10, "log": _fx("max_duration_exit10.jsonl")}}
    captured: list = []
    with lan_world(tmp_path, script) as w:
        with patch.object(flow, "lan_sync_task", _capture_sync_result(captured)):
            out = run_lan(w, cfg, budget_hours=0.5)
    assert out.status == "LAN_PARTIAL", f"status {out.status}; exc {out.exc!r}"
    assert captured and captured[0].get("budget_stopped") is True
    assert not captured[0].get("killed_after_cutoff")


def test_w14_row_carries_fault_code_aam_lan_04_and_verify_text_says_killed(tmp_path):
    # Matrix W14 names "the run row/alert". The row is written in the finally block, which only
    # sees a fault code set on the exception path today, so this row check is the one the
    # R6 builder must satisfy on the PARTIAL path (flagged to Opus, see the report).
    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(), SYNC: HANG_SYNC}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg, budget_hours=0.5)
    assert out.status == "LAN_PARTIAL", f"status {out.status}; exc {out.exc!r}"
    assert out.metrics.get("fault_code") == "AAM-LAN-04", out.metrics
    assert out.metrics.get("verify") == "skipped - rclone was killed after its time limit", (
        out.metrics.get("verify"))


# ── R8d: S3 hash check of tonight's transfers, flow wiring ───────────────
# Matrix: docs/TESTMATRIX_P1_REVIEW_2026-11-25.md section 8 (W15, W16, A13b-A13g). The hash
# runs only after an arrival that verified, when at least one file was copied, when
# arrival_hash_transferred is on and the copies fit arrival_hash_max_gb. Its metrics live in
# metrics["arrival"]["hash"]. The hash fixtures are the R8b ones (three matching files in their
# summary); the verdict does not read that count, so W15 asserts files/bytes from the sync log.

HASH_TIMEOUT_MARK = 4321   # distinct from every other timeout in the LAN leg (see A13b)


def _hash_metrics(out) -> dict:
    return out.metrics["arrival"]["hash"]


def _cap_raises_for_timeout(timeout_value: int):
    """Patches RunBudget.cap: the one call asking for `timeout_value` (the hash timeout) raises
    BudgetExceeded, every other call is the real one."""
    from core.budget import BudgetExceeded
    real_cap = RunBudget.cap

    def capped(self, timeout_s, reserve_s=0.0, clock=time.monotonic):
        if timeout_s == timeout_value:
            raise BudgetExceeded("test: no time left for the hash check")
        return real_cap(self, timeout_s, reserve_s, clock=clock)

    return patch.object(RunBudget, "cap", capped)


def _hash_argv(w) -> list[str] | None:
    calls = [c["argv"] for c in w.fake.calls if c["step"] == HASH]
    return calls[0] if calls else None


# ── W15 happy path with copies: the hash runs and its metrics are recorded ──

def test_w15_happy_path_with_copies_runs_the_hash_and_records_it(tmp_path):
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: hash_clean()}
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}; rclone calls: {steps(w)}"
    assert out.status == "LAN_COMPLETE"
    assert steps(w) == [PLAN, SYNC, CHECK, HASH]
    paths, total = lan_sync.transferred_files([_fx("lan_clean.jsonl")])
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is True
    assert hash_["verified"] is True
    assert hash_["files"] == len(paths) == 12
    assert hash_["bytes"] == total == 13062


# ── W16 retry union: the hash list holds the copies of every attempt ──────

def test_w16_hash_list_is_the_union_of_both_attempts_copies(tmp_path):
    first_attempt = copy_log([("retry_first_a.txt", 5), ("retry_first_b.txt", 7)])
    script = _w7_script()
    script[SYNC] = [{"exit_code": 5, "log": first_attempt}, sync_ok()]
    script[HASH] = hash_clean(14)   # the union: 2 copies of attempt 1 + 12 of attempt 2
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert out.status == "LAN_COMPLETE"
    assert steps(w) == [PLAN, SYNC, PLAN, SYNC, CHECK, HASH]
    argv = _hash_argv(w)
    assert argv is not None
    listed = Path(_argv_value(argv, "--files-from-raw")).read_text(encoding="utf-8").splitlines()
    expected = ["retry_first_a.txt", "retry_first_b.txt"] + lan_sync.transferred_files(
        [_fx("lan_clean.jsonl")])[0]
    assert listed == expected, "only the last attempt's copies were hash-checked"


# ── A13b no run budget for the hash: partial, not suspect ─────────────────

def test_a13b_no_budget_for_the_hash_is_partial_budget_stopped_and_no_shutdown(tmp_path):
    cfg = make_config(tmp_path, arrival_hash_timeout_seconds=HASH_TIMEOUT_MARK)
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w, _cap_raises_for_timeout(HASH_TIMEOUT_MARK):
        out = run_lan(w, cfg)
    assert out.status == "LAN_PARTIAL", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, flow.PartialRun)
    assert steps(w) == [PLAN, SYNC, CHECK], "no time left: the hash is never run"
    alerts = failures(w)
    assert len(alerts) == 1, [a.message for a in alerts]
    assert alerts[0].fault_code == "AAM-LAN-03"
    assert "no time left for tonight's hash check" in alerts[0].message
    assert w.shutdown.call_count == 0


# ── A13c over the size cap: the hash is not run, the size-only verdict stands ──

def test_a13c_copies_over_the_size_cap_skip_the_hash_with_the_reason(tmp_path):
    cfg = make_config(tmp_path, arrival_hash_max_gb=0)
    one_byte = copy_log([("tiny.txt", 1)])
    script = {PLAN: clean_plan(), SYNC: {"exit_code": 0, "log": one_byte},
              CHECK: check_clean(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert out.status == "LAN_COMPLETE"
    assert steps(w) == [PLAN, SYNC, CHECK]
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is False
    assert "arrival_hash_max_gb" in hash_["reason"]


# ── A13d switch off: no hash step, nothing else changes ───────────────────

def test_a13d_switch_off_runs_no_hash_and_the_run_is_complete(tmp_path):
    cfg = make_config(tmp_path, arrival_hash_transferred=False)
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert out.status == "LAN_COMPLETE"
    assert steps(w) == [PLAN, SYNC, CHECK]
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is False
    assert hash_["reason"] == "off (lan.arrival_hash_transferred)"


# ── A13e nothing copied tonight: no hash step ─────────────────────────────

def test_a13e_nothing_copied_runs_no_hash_and_the_run_is_complete(tmp_path):
    script = {PLAN: clean_plan(), SYNC: {"exit_code": 0, "log": _fx("lan_nochange.jsonl")},
              CHECK: check_clean(), HASH: hash_clean()}
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert out.status == "LAN_COMPLETE"
    assert steps(w) == [PLAN, SYNC, CHECK], "an empty list must never reach rclone check"
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is False
    assert hash_["reason"] == "nothing copied tonight"


# ── A13f size-only arrival already failed: no hash after a failed arrival ──

def test_a13f_failed_arrival_runs_no_hash_and_is_suspect(tmp_path):
    cfg = make_config(tmp_path, modify_window_seconds=0)
    time.sleep(0.3)   # sub/deep/c.txt is older than the sync: not changed during the backup
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_missing(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_SUSPECT", f"status {out.status}; exc {out.exc!r}"
    assert steps(w) == [PLAN, SYNC, CHECK], "no hash after a failed size-only arrival"
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is False
    assert hash_["reason"] == "arrival not verified"


# ── A13g copies without a size: the hash cannot be budgeted, so it is not run ──

def test_a13g_copies_with_unknown_bytes_skip_the_hash_and_complete(tmp_path):
    cfg = make_config(tmp_path)
    no_size = copy_log([("no_size.txt", None), ("sized.txt", 4)])
    script = {PLAN: clean_plan(), SYNC: {"exit_code": 0, "log": no_size},
              CHECK: check_clean(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert out.status == "LAN_COMPLETE"
    assert steps(w) == [PLAN, SYNC, CHECK]
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is False
    assert "size" in hash_["reason"] and "unknown" in hash_["reason"]
