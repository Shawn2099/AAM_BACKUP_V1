"""AF-12: cloud verify one-pass recheck on transient diff before CLOUD_VERIFY_FAILED."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import flow
from models.config import CloudConfig


def _cfg():
    cfg = MagicMock()
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.cloud.max_attempts = 1
    cfg.cloud.retry_delay_seconds = 60
    cfg.cloud.verify_recheck_enabled = True
    cfg.budget.cloud_max_hours = 6
    cfg.budget.cloud_verify_reserve_minutes = 60
    return cfg


def test_af12_config_has_verify_recheck_enabled_default_true():
    cfg = CloudConfig(bucket="test-bucket", project_number="123456")
    assert cfg.verify_recheck_enabled is True


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
def test_af12_verify_recheck_clears_transient_diff(
    rec, _db, _h, _pre, guards_task, sync, verify, _rt, _pub, _art,
):
    guards_task.return_value = {"fy": "FY26-27", "source_file_count": 1,
                                "destination": "gs://bkt/FY26-27", "max_delete": 5, "results": []}
    sync.with_options.return_value.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0}
    first = {
        "verified": False, "verify_termination": "normal", "verify_completion": True,
        "size": {"count": 1, "bytes": 10}, "manifest": [],
        "diff": {"added": ["transient.txt"], "removed": [], "modified": []},
    }
    second = {
        "verified": True, "verify_termination": "normal", "verify_completion": True,
        "size": {"count": 1, "bytes": 10}, "manifest": [],
        "diff": {"added": [], "removed": [], "modified": []},
    }
    verify.with_options.return_value.side_effect = [first, second]
    flow._run_cloud_pipeline(_cfg(), "run-recheck", "2026-10-14T22:00:00")
    assert verify.with_options.return_value.call_count == 2
    assert rec.call_args[0][4] == "CLOUD_COMPLETE"


@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.notify")
@patch("flow.cloud_verify_and_report_task")
@patch("flow.cloud_sync_task")
@patch("flow.cloud_access_task", new=MagicMock())
@patch("flow.cloud_guards_task")
@patch("flow.cloud_preflight_task")
@patch("flow.health_check_task")
@patch("flow.ManifestDB")
@patch("flow._record_run")
def test_af12_verify_recheck_still_fails_when_diff_persists(
    rec, _db, _h, _pre, guards_task, sync, verify, _notify, _art,
):
    guards_task.return_value = {"fy": "FY26-27", "source_file_count": 1,
                                "destination": "gs://bkt/FY26-27", "max_delete": 5, "results": []}
    sync.with_options.return_value.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0}
    bad = {
        "verified": False, "verify_termination": "normal", "verify_completion": True,
        "size": {"count": 1, "bytes": 10}, "manifest": [],
        "diff": {"added": ["still_missing.txt"], "removed": [], "modified": []},
    }
    verify.with_options.return_value.side_effect = [bad, bad]
    with pytest.raises(RuntimeError, match="Cloud integrity verification FAILED"):
        flow._run_cloud_pipeline(_cfg(), "run-persist", "2026-10-14T22:00:00")
    assert verify.with_options.return_value.call_count == 2
    assert rec.call_args[0][4] == "CLOUD_VERIFY_FAILED"
