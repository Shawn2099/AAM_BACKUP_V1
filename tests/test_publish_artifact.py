"""Prefect run-summary artifacts (GitNexus scan 2026-10-16).

Findings: with unknown tool counts (rclone final stats missing) the summary
task divided None and was never published - exactly on the degraded night
an operator most wants it; and a failure of the task call itself was
swallowed by a bare `except: pass` with nothing in the log.
"""

import json
from unittest.mock import MagicMock, patch

from loguru import logger

import flow


def _published_markdown(create):
    assert create.call_count == 1, "summary artifact was not published"
    return create.call_args.kwargs["markdown"]


@patch("prefect.artifacts.create_markdown_artifact")
def test_cloud_summary_published_when_counts_unknown(create):
    flow.cloud_publish_artifact_task.fn(
        {"verified": True, "size": {"count": 3, "bytes": 30}},
        {"status": "CLOUD_COMPLETE", "exit_code": 0},
        None, None,
    )
    md = _published_markdown(create)
    assert "unknown" in md


@patch("prefect.artifacts.create_markdown_artifact")
def test_lan_summary_published_when_counts_unknown(create):
    flow.lan_publish_artifact_task.fn(
        {"status": "LAN_COMPLETE", "exit_code": 1},
        {"added": ["a"], "modified": [], "removed": []},
        None, None, 5,
    )
    md = _published_markdown(create)
    assert "unknown" in md


@patch("prefect.artifacts.create_markdown_artifact")
def test_cloud_summary_still_shows_real_counts(create):
    flow.cloud_publish_artifact_task.fn(
        {"verified": True, "size": {"count": 3, "bytes": 30}},
        {"status": "CLOUD_COMPLETE", "exit_code": 0},
        2, 2 * 1024 * 1024,
    )
    md = _published_markdown(create)
    assert "`2` files" in md and "2.00 MB" in md


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
@patch("flow.cloud_publish_artifact_task", side_effect=RuntimeError("prefect api down"))
@patch("flow.cloud_record_task")
@patch("flow.cloud_verify_and_report_task")
@patch("flow.cloud_sync_task")
@patch("flow.cloud_access_task")
@patch("flow.cloud_guards_task")
@patch("flow.cloud_preflight_task")
@patch("flow.health_check_task")
@patch("flow.ManifestDB")
@patch("flow._record_run")
def test_cloud_publish_failure_is_logged_and_run_still_recorded(
    rec, mdb, _h, _pre, _g, _acc, sync, verify, _rt, _pub, _art, _running,
):
    mdb.from_config.return_value.get_cloud_synced_entries.return_value = {}
    sync.with_options.return_value.return_value = {
        "status": "CLOUD_COMPLETE", "exit_code": 0,
        "stats": {"transfers": 0, "bytes": 0, "deletes": 0, "errors": 0}}
    verify.with_options.return_value.return_value = {
        "verified": True, "verify_termination": "normal", "size": {"count": 0, "bytes": 0},
        "manifest": [], "diff": {"added": [], "removed": [], "modified": []}}
    warnings = []
    sink = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        flow._run_cloud_pipeline(_cfg(), "run-pub", "2026-10-16T02:00:00")
    finally:
        logger.remove(sink)
    assert rec.call_args.args[4] == "CLOUD_COMPLETE"
    assert any("prefect api down" in w for w in warnings), warnings


@patch("flow._record_running")
@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.lan_publish_artifact_task", side_effect=RuntimeError("prefect api down"))
@patch("flow.lan_record_task")
@patch("flow.lan_snapshot_after_task", return_value={"a": (1, 2.0)})
@patch("flow.lan_shutdown_task")
@patch("flow.lan_sync_task")
@patch("flow.lan_access_task")
@patch("flow.lan_guards_task")
@patch("flow.lan_snapshot_before_task", return_value={"a": (1, 1.0)})
@patch("flow.lan_preflight_task")
@patch("flow.wol_check_task")
@patch("flow.health_check_task")
@patch("flow._record_run")
def test_lan_publish_failure_is_logged_and_run_still_recorded(
    rec, _h, _w, _pre, _snap, _g, _acc, sync, _shut, _after, _rt, _pub, _art, _running,
):
    sync.with_options.return_value.return_value = {
        "status": "LAN_COMPLETE", "exit_code": 1,
        "counts": {"total": 1, "copied": 1, "skipped": 0, "mismatch": 0, "failed": 0, "extras": 0},
        "bytes": {"total": 1, "copied": 1, "skipped": 0, "mismatch": 0, "failed": 0, "extras": 0},
        "files_deleted": 0}
    warnings = []
    sink = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        flow._run_lan_pipeline(_cfg(), "run-pub-l", "2026-10-16T21:00:00")
    finally:
        logger.remove(sink)
    assert json.loads(rec.call_args.kwargs["extended_metrics"])["counts_source"] == "robocopy"
    assert any("prefect api down" in w for w in warnings), warnings
