"""M3 3.1 — truthful terminal status (plan WP5).

Findings: a verify-phase exception left CLOUD_NO_CHANGES_COMPLETE (probe P1,
counted as the last success); a post-phase exception left CLOUD_COMPLETE /
LAN_COMPLETE (probe P2, audit M-2); a slot-acquire timeout raised outside the
try, so NO row was written; a crashed run stayed invisible.
"""

import json
from unittest.mock import MagicMock, patch

import pendulum
import pytest

import flow
from core import guards as g
from core import statuses as st
from core.manifest import ManifestDB
from core.time_utils import IST, now_iso
from models.config import MaintenanceConfig

ERROR, BLOCKED, PARTIAL = st.EXC_ERROR, st.EXC_BLOCKED, st.EXC_PARTIAL


# ── decide_terminal_status: every (phase x exception x status) ────────────

@pytest.mark.parametrize("leg,phase,current,exc,expected", [
    # no exception: the pipeline's own verdict stands
    ("cloud", "post", "CLOUD_COMPLETE", None, "CLOUD_COMPLETE"),
    ("lan", "post", "LAN_COMPLETE", None, "LAN_COMPLETE"),
    # guard verdicts and partial runs keep their meaning
    ("lan", "pre", "LAN_SKIPPED", BLOCKED, "LAN_BLOCKED"),
    ("cloud", "pre", "CLOUD_SKIPPED", BLOCKED, "CLOUD_BLOCKED"),
    ("lan", "post", "LAN_PARTIAL", PARTIAL, "LAN_PARTIAL"),
    ("cloud", "post", "CLOUD_PARTIAL", PARTIAL, "CLOUD_PARTIAL"),
    # pre: nothing transferred
    ("cloud", "pre", "CLOUD_SKIPPED", ERROR, "CLOUD_FAILED"),
    ("lan", "pre", "LAN_SKIPPED", ERROR, "LAN_FAILED"),
    # sync: the sync's own failure verdict is kept, anything else is FAILED
    ("cloud", "sync", "CLOUD_SUSPECT", ERROR, "CLOUD_SUSPECT"),
    ("cloud", "sync", "CLOUD_FAILED", ERROR, "CLOUD_FAILED"),
    ("cloud", "sync", "CLOUD_SKIPPED", ERROR, "CLOUD_FAILED"),
    ("lan", "sync", "LAN_SUSPECT", ERROR, "LAN_SUSPECT"),
    ("lan", "sync", "LAN_SKIPPED", ERROR, "LAN_FAILED"),
    # verify (cloud): never a success (P1)
    ("cloud", "verify", "CLOUD_COMPLETE", ERROR, "CLOUD_VERIFY_FAILED"),
    ("cloud", "verify", "CLOUD_NO_CHANGES_COMPLETE", ERROR, "CLOUD_VERIFY_FAILED"),
    ("cloud", "verify", "CLOUD_VERIFY_FAILED", ERROR, "CLOUD_VERIFY_FAILED"),
    # post: a success-looking status never survives an exception (P2)
    ("cloud", "post", "CLOUD_COMPLETE", ERROR, "CLOUD_FAILED"),
    ("cloud", "post", "CLOUD_NO_CHANGES_COMPLETE", ERROR, "CLOUD_FAILED"),
    ("lan", "post", "LAN_COMPLETE", ERROR, "LAN_FAILED"),
    ("lan", "post", "LAN_PARTIAL", ERROR, "LAN_PARTIAL"),
])
def test_decide_terminal_status(leg, phase, current, exc, expected):
    assert st.decide_terminal_status(leg, phase, current, exc) == expected


@pytest.mark.parametrize("phase", ["pre", "sync", "verify", "post"])
@pytest.mark.parametrize("current", sorted(st.SUCCESS_STATUSES - {"SUCCESS", "LAN_NO_CHANGES_COMPLETE",
                                                                  "ROLLOVER_COMPLETE"}))
@pytest.mark.parametrize("exc", [ERROR, BLOCKED, PARTIAL])
def test_no_exception_ever_ends_in_a_success_status(phase, current, exc):
    leg = "cloud" if current.startswith("CLOUD") else "lan"
    assert not st.is_success(st.decide_terminal_status(leg, phase, current, exc))


def test_exception_kind_mapping():
    assert flow._exception_kind(g.GuardBlocked("lan", {"results": []})) == BLOCKED
    assert flow._exception_kind(flow.PartialRun("x")) == PARTIAL
    assert flow._exception_kind(RuntimeError("x")) == ERROR


# ── P1 / P2 through the real pipelines ────────────────────────────────────

def _cfg():
    cfg = MagicMock()
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.cloud.max_attempts = 2
    cfg.cloud.retry_delay_seconds = 60
    cfg.lan.max_attempts = 2
    cfg.lan.retry_delay_seconds = 600
    cfg.lan.shutdown_on_all_retries_exhausted = False
    return cfg


@patch("flow._record_running")
@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.notify")
@patch("flow.cloud_verify_and_report_task")
@patch("flow.cloud_sync_task")
@patch("flow.cloud_access_task")
@patch("flow.cloud_guards_task")
@patch("flow.cloud_preflight_task")
@patch("flow.health_check_task")
@patch("flow.ManifestDB")
@patch("flow._record_run")
def test_p1_verify_exception_after_no_changes_is_not_a_success(
    rec, _db, _h, _pre, _g, _acc, sync, verify, _alert, _art, _running,
):
    sync.with_options.return_value.return_value = {"status": "CLOUD_NO_CHANGES_COMPLETE",
                                                   "exit_code": 9}
    verify.with_options.return_value.side_effect = RuntimeError("rclone check crashed")
    with pytest.raises(RuntimeError):
        flow._run_cloud_pipeline(_cfg(), "run-p1", now_iso())
    args, kwargs = rec.call_args
    assert args[4] == "CLOUD_VERIFY_FAILED"
    assert json.loads(kwargs["extended_metrics"])["sync_status"] == "CLOUD_NO_CHANGES_COMPLETE"


@patch("flow._record_running")
@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.lan_shutdown_task")
@patch("flow.lan_record_task", side_effect=OSError("database is locked"))
@patch("flow.lan_snapshot_after_task", return_value={"a": (1, 1.0)})
@patch("flow.lan_sync_task")
@patch("flow.lan_access_task")
@patch("flow.lan_guards_task")
@patch("flow.lan_snapshot_before_task", return_value={})
@patch("flow.lan_preflight_task")
@patch("flow.wol_check_task")
@patch("flow.health_check_task")
@patch("flow._record_run")
def test_p2_post_exception_after_complete_is_not_a_success(
    rec, _h, _w, _pre, _snap, _g, _acc, sync, _after, _rt, shutdown, _art, _running,
):
    sync.with_options.return_value.return_value = {"status": "LAN_COMPLETE", "exit_code": 1}
    with pytest.raises(OSError):
        flow._run_lan_pipeline(_cfg(), "run-p2", now_iso())
    args, kwargs = rec.call_args
    assert args[4] == "LAN_FAILED"
    assert json.loads(kwargs["extended_metrics"])["sync_status"] == "LAN_COMPLETE"
    shutdown.assert_not_called()          # a run that did not complete never powers the NAS off


# ── RUNNING row before the slot; SLOT_TIMEOUT when the slot is not obtained ──

def test_running_row_is_written_before_the_slot(tmp_path):
    db_path = str(tmp_path / "m.db")
    cfg = MagicMock()
    cfg.paths.database_path = db_path
    cfg.maintenance = MaintenanceConfig()
    seen = {}

    from contextlib import contextmanager

    @contextmanager
    def slot(config):
        db = ManifestDB(db_path)
        seen["status_at_slot"] = db.last_run("lan")["status"]
        db.close()
        yield

    with patch("flow._backup_slot", slot):
        with flow._pipeline_slot(cfg, "lan", "run-r", now_iso()):
            pass
    assert seen["status_at_slot"] == "LAN_RUNNING"


def test_slot_not_obtained_records_slot_timeout_and_reraises(tmp_path):
    db_path = str(tmp_path / "m.db")
    cfg = MagicMock()
    cfg.paths.database_path = db_path
    cfg.maintenance = MaintenanceConfig()

    from contextlib import contextmanager

    @contextmanager
    def slot(config):
        raise TimeoutError("Attempt to acquire concurrency slots timed out")
        yield  # pragma: no cover

    body_ran = []
    with patch("flow._backup_slot", slot), pytest.raises(TimeoutError):
        with flow._pipeline_slot(cfg, "cloud", "run-t", now_iso()):
            body_ran.append(True)
    db = ManifestDB(db_path)
    try:
        row = db.last_run("cloud")
    finally:
        db.close()
    assert body_ran == []
    assert row["status"] == "CLOUD_SLOT_TIMEOUT" and "timed out" in row["error_message"]
    assert row["ended_at"]


def test_running_row_failure_never_blocks_the_backup():
    cfg = MagicMock()
    with patch("flow.ManifestDB") as mdb, patch("flow._backup_slot") as slot:
        mdb.from_config.side_effect = OSError("db locked")
        with flow._pipeline_slot(cfg, "lan", "run-x", now_iso()):
            pass
    slot.assert_called_once()


# ── ABANDONED at agent start ──────────────────────────────────────────────

def test_stale_running_rows_become_abandoned(tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        old = pendulum.now(IST).subtract(hours=30).isoformat()
        fresh = pendulum.now(IST).subtract(hours=1).isoformat()
        for run_id, mode, started, status in (
            ("a", "lan", old, "LAN_RUNNING"), ("b", "cloud", old, "CLOUD_RUNNING"),
            ("c", "lan", fresh, "LAN_RUNNING"), ("d", "lan", old, "LAN_COMPLETE"),
        ):
            db.insert_run({"run_id": run_id, "mode": mode, "started_at": started,
                           "status": status})
        assert db.mark_abandoned_runs(older_than_hours=24) == 2
        rows = {r["run_id"]: r for r in db.get_recent_runs(10)}
        assert rows["a"]["status"] == "LAN_ABANDONED" and rows["a"]["error_message"]
        assert rows["b"]["status"] == "CLOUD_ABANDONED"
        assert rows["c"]["status"] == "LAN_RUNNING"
        assert rows["d"]["status"] == "LAN_COMPLETE"
    finally:
        db.close()


def test_abandon_threshold_is_config():
    f = MaintenanceConfig.model_fields["abandon_running_after_hours"]
    assert f.default == 24


def test_launch_marks_abandoned_runs_at_start():
    import inspect

    import launch
    assert "_mark_abandoned_runs" in inspect.getsource(launch.main)
    cfg = MagicMock()
    cfg.maintenance.abandon_running_after_hours = 24
    with patch("models.config.load_config", return_value=cfg), \
         patch("core.manifest.ManifestDB.from_config") as mdb:
        mdb.return_value.mark_abandoned_runs.return_value = 3
        launch._mark_abandoned_runs()
    mdb.return_value.mark_abandoned_runs.assert_called_once_with(older_than_hours=24)
