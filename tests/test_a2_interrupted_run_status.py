"""A2 (final audit 2026-10-20): an interrupted run is never recorded as a success.

The pipelines handled `except Exception` only. A BaseException (KeyboardInterrupt, SystemExit,
Prefect's TerminationSignal on cancel/stop) skipped the terminal-status decision, so the `finally`
recorded whatever status the run held: CLOUD_COMPLETE during verify/post (and a RECOVERED mail),
CLOUD_SKIPPED during pre/sync. The status decision stays in core.statuses.decide_terminal_status."""

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from prefect.exceptions import TerminationSignal

import flow
from core import faults, statuses

INTERRUPTS = [KeyboardInterrupt, SystemExit, pytest.param(TerminationSignal, id="TerminationSignal")]


def _raised(exc):
    """What a mock raises: TerminationSignal needs the signal number."""
    return exc(signal=15) if exc is TerminationSignal else exc()


def _make_config():
    cfg = MagicMock()
    cfg.firm_name = "TestCA"
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.paths.gcs_key_path = "/tmp/key.json"
    cfg.maintenance.sqlite_busy_timeout_ms = 30000
    cfg.maintenance.sqlite_vacuum_freelist_threshold = 10000
    cfg.cloud.max_attempts = 2
    cfg.cloud.retry_delay_seconds = 60
    cfg.lan.max_attempts = 2
    cfg.lan.retry_delay_seconds = 600
    return cfg


@contextlib.contextmanager
def _cloud_pipeline(interrupt_in, exc):
    verified = {"verified": True, "verify_termination": "normal",
                "diff": {"added": [], "modified": [], "removed": []},
                "size": {"count": 1, "bytes": 1}, "manifest": []}
    with contextlib.ExitStack() as st:
        p = lambda target, **kw: st.enter_context(patch(target, **kw))  # noqa: E731
        notify, run_alert = p("flow.notify"), p("flow._send_run_alert")
        record_run, recovery = p("flow._record_run"), p("flow._send_recovery_if_needed")
        record_task = p("flow.cloud_record_task")
        p("flow.cloud_publish_artifact_task")
        verify, sync = p("flow.cloud_verify_and_report_task"), p("flow.cloud_sync_task")
        health = p("flow.health_check_task")
        p("flow.cloud_preflight_task")
        p("flow.cloud_access_task", new=MagicMock())
        p("flow.cloud_guards_task", new=MagicMock(return_value={"max_delete": 10}))
        p("flow.get_fy_prefix", return_value="FY26-27")
        p("flow._missed_if_no_budget")
        db = p("flow.ManifestDB")
        db.return_value.get_cloud_synced_entries.return_value = {}
        db.from_config.return_value = db.return_value
        sync.with_options.return_value.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0,
                                                       "error": None}
        verify.with_options.return_value.return_value = verified
        {"pre": health, "post": record_task}.get(interrupt_in) and \
            setattr({"pre": health, "post": record_task}[interrupt_in], "side_effect", exc)
        if interrupt_in == "sync":
            sync.with_options.return_value.side_effect = exc
        if interrupt_in == "verify":
            verify.with_options.return_value.side_effect = exc
        yield record_run, recovery, run_alert


@pytest.mark.parametrize("exc", INTERRUPTS)
@pytest.mark.parametrize("phase,expected", [
    ("pre", "CLOUD_FAILED"), ("sync", "CLOUD_FAILED"),
    ("verify", "CLOUD_VERIFY_FAILED"), ("post", "CLOUD_FAILED"),
])
def test_cloud_interrupt_is_never_a_success(phase, expected, exc):
    with _cloud_pipeline(phase, _raised(exc)) as (record_run, recovery, _alert):
        with pytest.raises(exc):
            flow._run_cloud_pipeline(_make_config(), "run-a2", "2026-10-20T02:00:00")
    status = record_run.call_args.args[4]
    assert status == expected and not statuses.is_success(status)
    assert not recovery.called, "an interrupted run must not announce a recovery"
    metrics = json.loads(record_run.call_args.kwargs["extended_metrics"])
    assert metrics["fault_code"] == "AAM-SYS-02"


@contextlib.contextmanager
def _lan_pipeline(interrupt_in, exc):
    with contextlib.ExitStack() as st:
        p = lambda target, **kw: st.enter_context(patch(target, **kw))  # noqa: E731
        p("flow.notify"), p("flow._send_run_alert")
        record_run, recovery = p("flow._record_run"), p("flow._send_recovery_if_needed")
        p("flow.lan_shutdown_task"), p("flow.lan_publish_artifact_task"), p("flow.lan_record_task")
        after = p("flow.lan_snapshot_after_task", return_value={"a.txt": (100, 1.0)})
        p("flow.lan_access_task", new=MagicMock()), p("flow.lan_guards_task", new=MagicMock())
        p("flow.lan_snapshot_before_task", return_value={})
        sync = p("flow.lan_sync_task")
        p("flow.lan_preflight_task"), p("flow.wol_check_task")
        health = p("flow.health_check_task")
        p("flow._missed_if_no_budget")
        sync.with_options.return_value.return_value = {
            "status": "LAN_COMPLETE", "exit_code": 1, "files_failed": 0,
            "counts": {"copied": 1}, "bytes": {"copied": 100}, "files_deleted": 0}
        if interrupt_in == "pre":
            health.side_effect = exc
        if interrupt_in == "sync":
            sync.with_options.return_value.side_effect = exc
        if interrupt_in == "post":
            after.side_effect = exc
        yield record_run, recovery


@pytest.mark.parametrize("exc", INTERRUPTS)
@pytest.mark.parametrize("phase", ["pre", "sync", "post"])
def test_lan_interrupt_is_never_a_success(phase, exc):
    with _lan_pipeline(phase, _raised(exc)) as (record_run, recovery):
        with pytest.raises(exc):
            flow._run_lan_pipeline(_make_config(), "run-a2l", "2026-10-20T21:00:00")
    status = record_run.call_args.args[4]
    assert status == "LAN_FAILED" and not statuses.is_success(status)
    assert not recovery.called


@pytest.mark.parametrize("exc", INTERRUPTS)
def test_interrupt_text_is_readable_and_never_powers_the_nas_off(exc):
    cfg = _make_config()
    cfg.lan.shutdown_on_all_retries_exhausted = True
    with _lan_pipeline("sync", _raised(exc)) as (record_run, _recovery):
        with patch("flow.lan_shutdown_task") as shutdown:
            with pytest.raises(exc):
                flow._run_lan_pipeline(cfg, "run-a2s", "2026-10-20T21:00:00", allow_shutdown=True)
    assert "interrupted" in record_run.call_args.args[6].lower()
    assert not shutdown.called, "a stop or cancel must not power the NAS off"


def test_interrupt_is_its_own_terminal_kind():
    assert flow._exception_kind(KeyboardInterrupt()) == statuses.EXC_INTERRUPTED
    assert flow._exception_kind(TerminationSignal(signal=15)) == statuses.EXC_INTERRUPTED
    assert flow._exception_kind(RuntimeError("x")) == statuses.EXC_ERROR
    for leg, phase, current, want in [
        ("cloud", "verify", "CLOUD_COMPLETE", "CLOUD_VERIFY_FAILED"),
        ("cloud", "post", "CLOUD_COMPLETE", "CLOUD_FAILED"),
        ("cloud", "sync", "CLOUD_SKIPPED", "CLOUD_FAILED"),
        ("lan", "post", "LAN_COMPLETE", "LAN_FAILED"),
        ("lan", "pre", "LAN_SKIPPED", "LAN_FAILED"),
    ]:
        assert statuses.decide_terminal_status(leg, phase, current, statuses.EXC_INTERRUPTED) == want


def test_interrupt_has_a_fault_code():
    code = faults.classify_run("cloud", "CLOUD_FAILED", exc=KeyboardInterrupt())
    assert code == "AAM-SYS-02"
    assert faults.classify_run("cloud", "CLOUD_FAILED", exc=RuntimeError("x")) != "AAM-SYS-02"


# ── record first, alert second (W23 follow-up) ─────────────────────────────────────────────────────
# Prefect allows a cancelled flow-run process 30 s before it escalates to a forced kill. The failure
# email goes through SMTP with retries (minutes when the mail server is down), so for an interrupt the
# run row must be written BEFORE the alert, or a forced kill during the send leaves the run RUNNING.

def _order_of_record_and_alert(pipeline_ctx, run):
    order = []
    with pipeline_ctx as (record_run, *_rest):
        record_run.side_effect = lambda *a, **k: order.append("record")
        with patch("flow._send_run_alert", side_effect=lambda *a, **k: order.append("alert")):
            with pytest.raises(KeyboardInterrupt):
                run()
    return order


def test_cloud_interrupt_records_the_run_before_it_alerts():
    order = _order_of_record_and_alert(
        _cloud_pipeline("sync", KeyboardInterrupt()),
        lambda: flow._run_cloud_pipeline(_make_config(), "run-a2o", "2026-10-20T02:00:00"))
    assert order == ["record", "alert"], order


def test_lan_interrupt_records_the_run_before_it_alerts():
    order = _order_of_record_and_alert(
        _lan_pipeline("sync", KeyboardInterrupt()),
        lambda: flow._run_lan_pipeline(_make_config(), "run-a2p", "2026-10-20T21:00:00"))
    assert order == ["record", "alert"], order
