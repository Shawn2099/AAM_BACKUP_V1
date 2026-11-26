"""AF-14: reporting-only rclone size error must not fail cloud evidence_ok."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import flow


@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.cloud_publish_artifact_task")
@patch("flow.cloud_record_task")
@patch("flow.cloud_verify_and_report_task")
@patch("flow.cloud_sync_task")
@patch("flow.cloud_access_task", new=MagicMock())
@patch("flow.cloud_guards_task")
@patch("flow.cloud_preflight_task")
@patch("flow.health_check_task")
@patch("flow.ManifestDB")
@patch("flow._record_run")
def test_af14_size_error_does_not_fail_evidence_ok(
    rec, _db, _h, _pre, guards_task, sync, verify, _rt, _pub, _art,
):
    cfg = MagicMock()
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.cloud.max_attempts = 1
    cfg.cloud.retry_delay_seconds = 60
    cfg.cloud.verify_recheck_enabled = False
    cfg.budget.cloud_max_hours = 6
    cfg.budget.cloud_verify_reserve_minutes = 60

    guards_task.return_value = {"fy": "FY26-27", "source_file_count": 5,
                                "destination": "gs://bkt/FY26-27", "max_delete": 5, "results": []}
    sync.with_options.return_value.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0}
    verify.with_options.return_value.return_value = {
        "verified": True,
        "verify_termination": "normal",
        "verify_completion": True,
        "size": {"_error": "rclone size timed out"},
        "manifest": [],
        "diff": {"added": [], "removed": [], "modified": []},
    }
    flow._run_cloud_pipeline(cfg, "run-size-err", "2026-10-14T22:00:00")
    assert rec.call_args[0][4] == "CLOUD_COMPLETE"
