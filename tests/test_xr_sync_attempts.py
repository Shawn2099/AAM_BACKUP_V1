"""XR-1 / XR-2 / XR-5 / XR-6: the sync attempt loop of both pipelines.

docs/audit/EXIT_CODES_AND_RETRIES_2026-10-16.md. Before: Prefect re-ran the
sync task with the SAME arguments - a fresh --max-delete N each time (3 x the
G3 limit deleted, verified on the real binary) and a robocopy /MIR repeat
without re-checking the guards. Now: retries only for failures a repeat can
change, guards re-measured cumulatively before every retry, the --max-delete
backstop always ends BLOCKED for a human.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import flow
from core import approvals as ap
from core import guards as g
from core.budget import RunBudget
from core.time_utils import now_iso
from models.config import ApprovalsConfig, GuardsConfig

NOW = "2026-10-14T10:00:00+05:30"
LATER = "2026-10-14T11:00:00+05:30"


def _cfg():
    cfg = MagicMock()
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.cloud.max_attempts = 3
    cfg.cloud.retry_delay_seconds = 300
    cfg.lan.max_attempts = 2
    cfg.lan.retry_delay_seconds = 600
    cfg.lan.shutdown_on_all_retries_exhausted = False
    cfg.budget.cloud_max_hours = 6
    cfg.budget.lan_max_hours = 6
    cfg.budget.cloud_verify_reserve_minutes = 30
    cfg.budget.lan_post_reserve_minutes = 15
    return cfg


def _cloud_fail(exit_code=5, deletes=0, retryable=True, status="CLOUD_FAILED", **over):
    stats = {"transfers": 0, "deletes": deletes, "errors": 1, "fatalError": not retryable}
    result = {"status": status, "exit_code": exit_code, "error": f"rclone exit {exit_code}",
              "stats": stats, "retryable": retryable, "max_delete_reached": False,
              "suspect_reason": None}
    result.update(over)
    return flow.SyncFailed(result["error"], result)


_OK = {"status": "CLOUD_COMPLETE", "exit_code": 0, "error": None,
       "stats": {"transfers": 1, "deletes": 0, "bytes": 10}, "retryable": False}
_VERIFIED = {"verified": True, "verify_termination": "normal", "diff": {}, "size": {},
             "manifest": [], "manifest_error": None, "stage_seconds": {}}


def _guard_record(max_delete=50):
    return {"max_delete": max_delete, "results": [], "fy": "FY26-27"}


def cloud_patches(fn):
    for target in reversed([
        "flow._record_running", "flow._open_run_artefacts", "flow.notify", "flow.time.sleep",
        "flow.cloud_publish_artifact_task", "flow.cloud_record_task",
        "flow.cloud_verify_and_report_task", "flow.cloud_sync_task", "flow.cloud_access_task",
        "flow.cloud_guards_task", "flow.cloud_preflight_task", "flow.health_check_task",
        "flow.ManifestDB", "flow._record_run", "flow._open_block_request",
    ]):
        fn = patch(target)(fn)
    return fn


class _Cloud:
    def __init__(self, mocks):
        (self.block_request, self.rec, self.db, self.health, self.pre, self.guards, self.access,
         self.sync_task, self.verify, self.record, self.publish, self.sleep, self.notify,
         self.art, self.running) = mocks
        self.art.return_value = None
        self.sync = self.sync_task.with_options.return_value
        self.verify.with_options.return_value.return_value = _VERIFIED
        self.guards.return_value = _guard_record()
        self.health.return_value = None

    def run(self, cfg=None, **kw):
        with pytest.raises(BaseException) as info:
            flow._run_cloud_pipeline(cfg or _cfg(), "run-x", now_iso(), **kw)
        return info.value

    def run_ok(self, cfg=None):
        return flow._run_cloud_pipeline(cfg or _cfg(), "run-x", now_iso())

    @property
    def row(self):
        args, kwargs = self.rec.call_args
        return {"status": args[4], "exit_code": args[5],
                "metrics": json.loads(kwargs["extended_metrics"])}


# ── XR-1: the --max-delete backstop is never retried; a human decides ─────

@cloud_patches
def test_max_delete_stop_runs_once_and_ends_blocked(*mocks):
    c = _Cloud(mocks)
    c.sync.side_effect = _cloud_fail(exit_code=7, deletes=50, retryable=False,
                                     max_delete_reached=True)
    c.guards.side_effect = [_guard_record(50), _guard_record(0)]
    exc = c.run()
    assert isinstance(exc, g.GuardBlocked)
    assert c.sync.call_count == 1                          # was 3 (Prefect retries)
    # the re-measure counts what this run already deleted
    assert c.guards.call_args_list[1].kwargs["already_deleted"] == 50
    assert c.row["status"] == "CLOUD_BLOCKED" and c.row["exit_code"] == 7
    c.block_request.assert_called_once()
    record = c.block_request.call_args.args[3]
    g3 = [r for r in record["results"] if r["guard"] == "G3_MASS_DELETE"]
    assert g3 and g3[0]["verdict"] == g.BLOCK and g3[0]["metrics"]["already_deleted"] == 50
    c.sleep.assert_not_called()


@cloud_patches
def test_backstop_block_from_the_re_measure_itself_is_kept(*mocks):
    c = _Cloud(mocks)
    c.sync.side_effect = _cloud_fail(exit_code=7, deletes=50, retryable=False,
                                     max_delete_reached=True)
    blocked = g.GuardBlocked("cloud", {"results": [], "fy": "FY26-27"})
    c.guards.side_effect = [_guard_record(50), blocked]
    assert c.run() is blocked
    assert c.row["status"] == "CLOUD_BLOCKED"


# ── XR-1 / XR-6: retries re-measure and the deletion cap is cumulative ────

@cloud_patches
def test_retry_re_measures_and_passes_the_remaining_cap(*mocks):
    c = _Cloud(mocks)
    c.sync.side_effect = [_cloud_fail(exit_code=5, deletes=30), dict(_OK)]
    c.guards.side_effect = [_guard_record(50), _guard_record(20)]
    assert c.run_ok()["status"] == "CLOUD_COMPLETE"
    assert c.sync.call_count == 2
    assert c.sync.call_args_list[0].kwargs["max_delete"] == 50
    assert c.sync.call_args_list[1].kwargs["max_delete"] == 20
    assert c.guards.call_args_list[1].kwargs["already_deleted"] == 30
    assert c.access.call_count == 2
    c.sleep.assert_called_once_with(300)
    attempts = c.row["metrics"]["sync_attempts"]
    assert [a["exit_code"] for a in attempts] == [5, 0]
    assert [a["deleted"] for a in attempts] == [30, 0]


@cloud_patches
def test_non_retryable_failure_runs_once_and_keeps_its_exit_code(*mocks):
    c = _Cloud(mocks)
    c.sync.side_effect = _cloud_fail(exit_code=7, retryable=False)   # auth / bucket
    c.run()
    assert c.sync.call_count == 1 and c.guards.call_count == 1
    assert c.row["status"] == "CLOUD_FAILED"
    assert c.row["exit_code"] == 7                         # was recorded as -1


@cloud_patches
def test_suspect_is_not_retried_and_stays_suspect(*mocks):
    c = _Cloud(mocks)
    c.sync.side_effect = _cloud_fail(exit_code=0, retryable=False, status="CLOUD_SUSPECT")
    c.run()
    assert c.sync.call_count == 1
    assert c.row["status"] == "CLOUD_SUSPECT"


@cloud_patches
def test_attempts_stop_at_max_attempts(*mocks):
    c = _Cloud(mocks)
    c.sync.side_effect = [_cloud_fail(), _cloud_fail(), _cloud_fail(), dict(_OK)]
    c.guards.return_value = _guard_record(50)
    c.run()
    assert c.sync.call_count == 3
    assert c.row["status"] == "CLOUD_FAILED" and c.row["exit_code"] == 5


@cloud_patches
def test_retry_that_does_not_fit_the_budget_is_skipped(*mocks):
    c = _Cloud(mocks)
    c.sync.side_effect = [_cloud_fail(), dict(_OK)]
    with patch.object(RunBudget, "remaining", return_value=600.0):   # 300 s delay + 30 min reserve
        c.run()
    assert c.sync.call_count == 1
    c.sleep.assert_not_called()
    assert c.row["status"] == "CLOUD_FAILED"


@cloud_patches
def test_re_measure_that_blocks_ends_the_run_blocked(*mocks):
    c = _Cloud(mocks)
    c.sync.side_effect = [_cloud_fail(deletes=0), dict(_OK)]
    c.guards.side_effect = [_guard_record(50), g.GuardBlocked("cloud", {"results": []})]
    c.run()
    assert c.sync.call_count == 1
    assert c.row["status"] == "CLOUD_BLOCKED"


@cloud_patches
def test_unexpected_exception_is_not_retried(*mocks):
    c = _Cloud(mocks)
    c.sync.side_effect = ValueError("G1: the source holds no file")
    c.run()
    assert c.sync.call_count == 1


# ── XR-5: exit 10 is always a time-limit stop, never "verify failed" ──────

@cloud_patches
def test_time_limit_stop_skips_verify_and_is_partial(*mocks):
    c = _Cloud(mocks)
    c.sync.return_value = {"status": "CLOUD_PARTIAL", "exit_code": 10, "error": "x",
                           "stats": {"transfers": 3, "bytes": 30, "deletes": 0},
                           "time_limit_stopped": True, "retryable": False}
    exc = c.run()
    assert isinstance(exc, flow.PartialRun)
    c.verify.with_options.return_value.assert_not_called()
    assert c.row["status"] == "CLOUD_PARTIAL"


def _task_cfg():
    cfg = _cfg()
    cfg.cloud.subprocess_timeout_seconds = 18000
    cfg.cloud.max_duration_seconds = None
    return cfg


@pytest.mark.parametrize("budget", [None, "loose"])
def test_exit_10_from_rclone_cap_is_a_time_limit_stop(budget):
    import time
    # a budget far looser than the upload's own limits: rclone's cap stopped it
    rb = RunBudget("cloud", 48 * 3600, time.monotonic()) if budget else None
    sync_out = {"status": "CLOUD_PARTIAL", "exit_code": 10, "error": "rclone exit 10",
                "stats": {"transfers": 2, "bytes": 2 * 1024 ** 3}, "retryable": False}
    with patch("flow.run_cloud_sync", return_value=sync_out):
        out = flow.cloud_sync_task.fn(_task_cfg(), "FY26-27", budget=rb)
    assert out["status"] == "CLOUD_PARTIAL" and out["time_limit_stopped"] is True
    assert "--max-duration" in out["error"] and "next run continues" in out["error"]
    assert not out.get("budget_stopped")


def test_failed_sync_raises_syncfailed_with_its_result():
    sync_out = {"status": "CLOUD_FAILED", "exit_code": 7, "error": "auth error"}
    with patch("flow.run_cloud_sync", return_value=sync_out), \
            pytest.raises(flow.SyncFailed, match="auth error") as info:
        flow.cloud_sync_task.fn(_task_cfg(), "FY26-27")
    assert info.value.result["exit_code"] == 7
    assert isinstance(info.value, RuntimeError)


def test_syncfailed_survives_pickling():
    import pickle
    e = pickle.loads(pickle.dumps(flow.SyncFailed("boom", {"exit_code": 5})))
    assert str(e) == "boom" and e.result == {"exit_code": 5}


# ── XR-1 arithmetic: the guard counts this run's earlier deletions ────────

def test_remaining_allowance_is_cumulative():
    # 1000 files originally, 5% -> 50; 30 already deleted -> 20 left
    assert g.remaining_delete_allowance(970, 30, 500, 5.0) == 20
    assert g.remaining_delete_allowance(950, 50, 500, 5.0) == 0
    assert g.remaining_delete_allowance(1000, 0, 500, 5.0) == 50
    assert g.remaining_delete_allowance(900, 100, 500, 5.0) == 0     # never negative


def _cloud_guard_cfg():
    cfg = _cfg()
    cfg.paths.source_drive = "E:\\DATA"
    cfg.cloud.bucket = "b"
    cfg.guards = GuardsConfig(max_delete_files=500, max_delete_percent=5.0)
    cfg.approvals = ApprovalsConfig()
    return cfg


def _cloud_guards(planned, destination_now, already_deleted, approval=None, approval_id=None,
                  run_id="run-2"):
    db = MagicMock()
    db.guard_baseline.return_value = None
    db.get_approval.return_value = approval
    db.mark_approval_used.return_value = True
    dry = {"status": "CLOUD_COMPLETE", "planned_deletes": planned}
    with patch("flow.ManifestDB") as mdb, \
            patch("flow.temp_rclone_config"), \
            patch("flow.now_iso", return_value=LATER), \
            patch("flow.guards.first_regular_file", return_value="E:\\DATA\\a.txt"), \
            patch("flow.count_objects", side_effect=[2000, destination_now]), \
            patch("flow.run_cloud_sync", return_value=dry):
        mdb.from_config.return_value = db
        try:
            out = flow.cloud_guards_task.fn(_cloud_guard_cfg(), "FY26-27",
                                            approval_id=approval_id, run_id=run_id,
                                            already_deleted=already_deleted)
        except Exception as e:  # noqa: BLE001 - returned for assertions
            out = e
    return out, db


def test_cloud_guard_counts_earlier_deletions_against_g3():
    # 30 deleted + 25 still planned = 55 of 1000 > 5 % -> BLOCK (alone each is fine)
    out, _ = _cloud_guards(planned=25, destination_now=970, already_deleted=30)
    assert isinstance(out, g.GuardBlocked)
    g3 = [r for r in out.record["results"] if r["guard"] == "G3_MASS_DELETE"][0]
    assert g3["metrics"]["planned_deletes"] == 55
    assert g3["metrics"]["destination_files"] == 1000
    assert g3["metrics"]["already_deleted"] == 30


def test_cloud_guard_cap_is_what_is_left_of_the_allowance():
    out, _ = _cloud_guards(planned=10, destination_now=970, already_deleted=30)
    assert isinstance(out, dict), out
    assert out["max_delete"] == 20


# ── Approvals: one approval covers the retries of ITS run only ────────────

def _g3_approval(planned, used_by=None):
    b = g.g3_mass_delete(planned, 1000, 500, 5.0)
    return {"approval_id": "a1", "leg": "cloud", "status": ap.USED if used_by else ap.APPROVED,
            "guards": [{"guard": b.guard, "reason": b.reason, "metrics": b.metrics}],
            "approved_at": NOW, "expires_at": "2026-10-14T22:00:00+05:30",
            "used_at": LATER if used_by else None, "used_by_run_id": used_by,
            "dismissed_at": None}


def test_retry_of_the_approved_run_reuses_its_approval_without_consuming_again():
    out, db = _cloud_guards(planned=500, destination_now=900, already_deleted=100,
                            approval=_g3_approval(600, used_by="run-2"), approval_id="a1")
    assert isinstance(out, dict), out
    assert out["approved_guards"] == ["G3_MASS_DELETE"]
    db.mark_approval_used.assert_not_called()


def test_an_approval_used_by_another_run_is_not_reused():
    out, _ = _cloud_guards(planned=500, destination_now=900, already_deleted=100,
                           approval=_g3_approval(600, used_by="run-1"), approval_id="a1")
    assert isinstance(out, g.GuardBlocked)


def test_reused_approval_still_rejects_worse_cumulative_numbers():
    out, _ = _cloud_guards(planned=550, destination_now=900, already_deleted=100,
                           approval=_g3_approval(600, used_by="run-2"), approval_id="a1")
    assert isinstance(out, g.GuardBlocked) and "worse than approved" in str(out)


# ── XR-2: the LAN mirror is never repeated without re-checking ────────────

def lan_patches(fn):
    for target in reversed([
        "flow._record_running", "flow._open_run_artefacts", "flow.notify", "flow.time.sleep",
        "flow.lan_publish_artifact_task", "flow.lan_shutdown_task", "flow.lan_record_task",
        "flow.lan_snapshot_after_task", "flow.lan_sync_task", "flow.lan_access_task",
        "flow.lan_guards_task", "flow.lan_snapshot_before_task", "flow.lan_preflight_task",
        "flow.wol_check_task", "flow.health_check_task", "flow.ManifestDB", "flow._record_run",
        "flow._open_block_request",
    ]):
        fn = patch(target)(fn)
    return fn


class _Lan:
    def __init__(self, mocks):
        (self.block_request, self.rec, self.db, self.health, self.wol, self.pre, self.before,
         self.guards, self.access, self.sync_task, self.after, self.record, self.shutdown,
         self.publish, self.sleep, self.notify, self.art, self.running) = mocks
        self.art.return_value = None
        self.sync = self.sync_task.with_options.return_value
        self.pre.with_options.return_value.return_value = {"counts": {}, "planned_deletes": 0}
        self.before.return_value = {"a": (1, 1.0)}
        self.after.return_value = None
        self.guards.return_value = {"results": []}
        self.shutdown.return_value = None

    def run(self):
        with pytest.raises(BaseException) as info:
            flow._run_lan_pipeline(_cfg(), "run-l", now_iso())
        return info.value

    @property
    def row(self):
        args, kwargs = self.rec.call_args
        return {"status": args[4], "exit_code": args[5],
                "metrics": json.loads(kwargs["extended_metrics"])}


def _lan_fail(files_deleted=3, status="LAN_FAILED", **over):
    result = {"status": status, "exit_code": 16, "error": "robocopy exit 16",
              "files_deleted": files_deleted, "files_failed": 0}
    result.update(over)
    return flow.SyncFailed(result["error"], result)


_LAN_OK = {"status": "LAN_COMPLETE", "exit_code": 1, "error": None, "files_deleted": 0,
           "files_failed": 0, "counts": {"copied": 1}, "bytes": {"copied": 1}}


@lan_patches
def test_lan_retry_re_measures_the_guards_first(*mocks):
    c = _Lan(mocks)
    c.sync.side_effect = [_lan_fail(files_deleted=3), dict(_LAN_OK)]
    flow._run_lan_pipeline(_cfg(), "run-l", now_iso())
    assert c.sync.call_count == 2
    assert c.pre.with_options.return_value.call_count == 2        # robocopy /L again
    assert c.guards.call_count == 2
    assert c.guards.call_args_list[1].kwargs["already_deleted"] == 3
    assert c.access.call_count == 2
    c.sleep.assert_called_once_with(600)
    assert [a["deleted"] for a in c.row["metrics"]["sync_attempts"]] == [3, 0]


@lan_patches
def test_lan_retry_blocked_by_the_re_measure_stops(*mocks):
    c = _Lan(mocks)
    c.sync.side_effect = [_lan_fail(), dict(_LAN_OK)]
    c.guards.side_effect = [{"results": []}, g.GuardBlocked("lan", {"results": []})]
    c.run()
    assert c.sync.call_count == 1
    assert c.row["status"] == "LAN_BLOCKED" and c.row["exit_code"] == 16


@lan_patches
def test_lan_unknown_deletions_are_not_retried(*mocks):
    c = _Lan(mocks)
    c.sync.side_effect = [_lan_fail(files_deleted=None), dict(_LAN_OK)]
    c.run()
    assert c.sync.call_count == 1
    assert c.row["status"] == "LAN_FAILED" and c.row["exit_code"] == 16


@lan_patches
def test_lan_suspect_and_timeout_are_not_retried(*mocks):
    c = _Lan(mocks)
    c.sync.side_effect = [_lan_fail(status="LAN_SUSPECT"), dict(_LAN_OK)]
    c.run()
    assert c.sync.call_count == 1 and c.row["status"] == "LAN_SUSPECT"


@lan_patches
def test_lan_timeout_is_not_retried(*mocks):
    c = _Lan(mocks)
    c.sync.side_effect = [_lan_fail(timed_out=True), dict(_LAN_OK)]
    c.run()
    assert c.sync.call_count == 1


def _lan_guard_cfg(tmp_path):
    cfg = _cfg()
    cfg.paths.source_drive = str(tmp_path)
    cfg.paths.lan_destination = "\\\\10.0.0.9\\backup\\FY26-27"
    cfg.guards = GuardsConfig(max_delete_files=500, max_delete_percent=5.0)
    cfg.approvals = ApprovalsConfig()
    return cfg


def test_lan_guard_counts_earlier_deletions_against_g3(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    db = MagicMock()
    db.guard_baseline.return_value = None
    counts = {"total": 1000, "copied": 0, "skipped": 1000, "mismatch": 0, "failed": 0,
              "extras": 25}
    dry = {"counts": counts, "planned_deletes": 25, "canary_present": True}
    with patch("flow.ManifestDB") as mdb, \
            patch("flow.guards.resolve_host", return_value={"10.0.0.9"}), \
            patch("flow.guards.local_addresses", return_value={"127.0.0.1"}):
        mdb.from_config.return_value = db
        with pytest.raises(g.GuardBlocked) as info:
            flow.lan_guards_task.fn(_lan_guard_cfg(tmp_path), "FY26-27", dry, 970,
                                    run_id="run-l", already_deleted=30)
    g3 = [r for r in info.value.record["results"] if r["guard"] == "G3_MASS_DELETE"][0]
    assert g3["metrics"]["planned_deletes"] == 55
    assert g3["metrics"]["destination_files"] == 1000
