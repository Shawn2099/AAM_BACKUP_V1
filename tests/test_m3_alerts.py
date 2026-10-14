"""M3 3.4 — exactly one alert per failed run, through core.notify (plan WP8).

Findings: duplicate alerts (SUSPECT x2, verify failure x2, three identical in
18 min) - the pipelines alerted and backup()'s tail alerted again; delivery
was never recorded. Now: one alert at the terminal decision (before any NAS
shutdown), BLOCKED keeps only its ACTION REQUIRED email, RECOVERED on the
first success after a failure.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import pytest

import flow
from core import guards as g
from core.manifest import ManifestDB
from core.time_utils import now_iso
from models.config import MaintenanceConfig


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


def _failures(notify):
    return [c.args[1] for c in notify.call_args_list if c.args[1].kind == "FAILURE"]


_CLOUD = ["flow._record_running", "flow._open_run_artefacts", "flow.cloud_publish_artifact_task",
          "flow.cloud_record_task", "flow.cloud_verify_and_report_task", "flow.cloud_sync_task",
          "flow.cloud_access_task", "flow.cloud_guards_task", "flow.cloud_preflight_task",
          "flow.health_check_task", "flow.ManifestDB", "flow._record_run",
          "flow._send_recovery_if_needed", "flow.notify"]


@contextmanager
def _cloud_rig():
    ps = {name: patch(name) for name in _CLOUD}
    ms = {name.split(".", 1)[1]: p.start() for name, p in ps.items()}
    ms["_open_run_artefacts"].return_value = None
    try:
        yield ms
    finally:
        for p in ps.values():
            p.stop()


def test_cloud_verify_failure_sends_exactly_one_alert_through_notify():
    with _cloud_rig() as m:
        m["cloud_sync_task"].with_options.return_value.return_value = {
            "status": "CLOUD_COMPLETE", "exit_code": 0, "stats": None}
        m["cloud_verify_and_report_task"].with_options.return_value.return_value = {
            "verified": False, "size": {}, "manifest": [], "diff": {"added": ["a"]}}
        with pytest.raises(RuntimeError):
            flow._run_cloud_pipeline(_cfg(), "run-v", now_iso())
        [event] = _failures(m["notify"])
        assert event.key == "backup:cloud:CLOUD_VERIFY_FAILED"
        assert event.status == "CLOUD_VERIFY_FAILED" and "missing-from-cloud=1" in event.message


def test_cloud_suspect_sends_exactly_one_alert():
    with _cloud_rig() as m:
        m["cloud_sync_task"].with_options.return_value.side_effect = RuntimeError("killed")
        with pytest.raises(RuntimeError):
            flow._run_cloud_pipeline(_cfg(), "run-s", now_iso())
        [event] = _failures(m["notify"])
        assert event.key == "backup:cloud:CLOUD_FAILED"


def test_cloud_partial_sends_exactly_one_alert():
    with _cloud_rig() as m:
        m["cloud_sync_task"].with_options.return_value.return_value = {
            "status": "CLOUD_PARTIAL", "exit_code": 5, "stats": None}
        m["cloud_verify_and_report_task"].with_options.return_value.return_value = {
            "verified": True, "verify_termination": "normal", "size": {"count": 1, "bytes": 1},
            "manifest": [], "diff": {"added": [], "removed": [], "modified": []}}
        with pytest.raises(flow.PartialRun):
            flow._run_cloud_pipeline(_cfg(), "run-p", now_iso())
        [event] = _failures(m["notify"])
        assert event.status == "CLOUD_PARTIAL" and "resumable" in event.message


def test_guard_block_sends_no_failure_alert():
    with _cloud_rig() as m, patch("flow._open_block_request") as block:
        m["cloud_guards_task"].side_effect = g.GuardBlocked("cloud", {"results": []})
        with pytest.raises(g.GuardBlocked):
            flow._run_cloud_pipeline(_cfg(), "run-b", now_iso())
        assert _failures(m["notify"]) == []
        block.assert_called_once()


@patch("flow._send_recovery_if_needed")
@patch("flow.notify")
@patch("flow._record_running")
@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.lan_publish_artifact_task")
@patch("flow.lan_record_task")
@patch("flow.lan_snapshot_after_task", return_value={"a": (1, 1.0)})
@patch("flow.lan_shutdown_task")
@patch("flow.lan_sync_task")
@patch("flow.lan_access_task")
@patch("flow.lan_guards_task")
@patch("flow.lan_snapshot_before_task", return_value={})
@patch("flow.lan_preflight_task")
@patch("flow.wol_check_task")
@patch("flow.health_check_task")
@patch("flow._record_run")
def test_lan_partial_alerts_once_and_before_the_nas_shutdown(
    _rec, _h, _w, _pre, _snap, _g, _acc, sync, shutdown, _after, _rt, _pub, _art, _running,
    notify, _recov,
):
    order = MagicMock()
    order.attach_mock(notify, "notify")
    order.attach_mock(shutdown, "shutdown")
    cfg = _cfg()
    cfg.lan.shutdown_on_all_retries_exhausted = True
    sync.with_options.return_value.return_value = {"status": "LAN_PARTIAL", "exit_code": 9,
                                                   "error": "1 file FAILED"}
    with pytest.raises(flow.PartialRun):
        flow._run_lan_pipeline(cfg, "run-lp", now_iso())
    [event] = _failures(notify)
    assert event.status == "LAN_PARTIAL"
    names = [c[0] for c in order.mock_calls]
    assert names.index("notify") < names.index("shutdown")


def test_backup_flow_adds_no_second_alert():
    cfg = MagicMock()
    cfg.cloud.enabled = False
    cfg.lan.enabled = True
    with patch("flow.load_config", return_value=cfg), patch("flow.configure_logging"), \
         patch("flow.configure_prefect_bridge"), patch("flow.cleanup_orphaned_robocopy_logs"), \
         patch("flow._run_lan_pipeline", side_effect=RuntimeError("lan broke")), \
         patch("flow.notify") as notify:
        with pytest.raises(ExceptionGroup):
            flow.backup.fn(config_path="x", mode="lan")
    notify.assert_not_called()


def test_slot_timeout_alerts(tmp_path):
    cfg = MagicMock()
    cfg.paths.database_path = str(tmp_path / "m.db")
    cfg.maintenance = MaintenanceConfig()

    @contextmanager
    def slot(config):
        raise TimeoutError("slot timed out")
        yield  # pragma: no cover

    with patch("flow._backup_slot", slot), patch("flow.notify") as notify, \
         pytest.raises(TimeoutError):
        with flow._pipeline_slot(cfg, "lan", "run-t", now_iso()):
            pass
    [event] = _failures(notify)
    assert event.status == "LAN_SLOT_TIMEOUT"


@pytest.mark.parametrize("previous,expected", [
    ("LAN_FAILED", 1), ("LAN_PARTIAL", 1), ("LAN_BLOCKED", 1), ("LAN_SLOT_TIMEOUT", 1),
    ("LAN_COMPLETE", 0), (None, 0),
])
def test_recovered_email_after_a_failure_only(tmp_path, previous, expected):
    db = ManifestDB(str(tmp_path / "m.db"))
    if previous:
        db.insert_run({"run_id": "old", "mode": "lan", "started_at": "2026-10-13T21:00:00",
                       "status": previous})
    db.insert_run({"run_id": "new", "mode": "lan", "started_at": "2026-10-14T21:00:00",
                   "status": "LAN_COMPLETE"})
    db.close()
    cfg = MagicMock()
    cfg.paths.database_path = str(tmp_path / "m.db")
    cfg.maintenance = MaintenanceConfig()
    with patch("flow.notify") as notify:
        flow._send_recovery_if_needed(cfg, "lan", "new")
    assert notify.call_count == expected
    if expected:
        assert notify.call_args.args[1].kind == "RECOVERED"


def test_flow_and_watchdog_no_longer_use_the_old_alert_function():
    import inspect

    import watchdog
    assert "send_failure_alert" not in inspect.getsource(flow)
    assert "send_failure_alert" not in inspect.getsource(watchdog)
