"""Task retries come from config (owner rule: every tunable is a config key).

Deferred from M3 (SESSION_HANDOFF): flow.py hard-coded the preflight
(1 x 30 s) and verify (1 x 60 s) task retries. Defaults keep those values.
"""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

import flow
from models.config import CloudConfig, LanConfig


def test_defaults_keep_the_tested_values():
    lan = LanConfig.model_fields
    cloud = CloudConfig.model_fields
    assert lan["preflight_retries"].default == 1
    assert lan["preflight_retry_delay_seconds"].default == 30
    assert cloud["preflight_retries"].default == 1
    assert cloud["preflight_retry_delay_seconds"].default == 30
    assert cloud["verify_retries"].default == 1
    assert cloud["verify_retry_delay_seconds"].default == 60


def test_valid_values_are_accepted():
    # 0 retries is a legitimate choice (fail on the first error)
    c = CloudConfig(bucket="aam-test-bucket", project_number="123456", preflight_retries=0, verify_retries=5,
                    preflight_retry_delay_seconds=5, verify_retry_delay_seconds=3600)
    assert (c.preflight_retries, c.verify_retries) == (0, 5)
    assert LanConfig(preflight_retries=0, preflight_retry_delay_seconds=3600).preflight_retries == 0


@pytest.mark.parametrize("model,key,bad", [
    (CloudConfig, "preflight_retries", -1),
    (CloudConfig, "preflight_retries", 6),
    (CloudConfig, "verify_retries", 6),
    (CloudConfig, "preflight_retry_delay_seconds", 0),
    (CloudConfig, "verify_retry_delay_seconds", 3601),
])
def test_cloud_bounds_reject_unsafe_values(model, key, bad):
    with pytest.raises(ValidationError):
        model(bucket="aam-test-bucket", project_number="123456", **{key: bad})


@pytest.mark.parametrize("key,bad", [
    ("preflight_retries", -1), ("preflight_retries", 6),
    ("preflight_retry_delay_seconds", 0), ("preflight_retry_delay_seconds", 3601),
])
def test_lan_bounds_reject_unsafe_values(key, bad):
    with pytest.raises(ValidationError):
        LanConfig(**{key: bad})


def _cfg():
    cfg = MagicMock()
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:/DATA/FY26-27"
    cfg.cloud.preflight_retries = 3
    cfg.cloud.preflight_retry_delay_seconds = 45
    cfg.cloud.verify_retries = 2
    cfg.cloud.verify_retry_delay_seconds = 90
    cfg.cloud.max_attempts = 2
    cfg.cloud.retry_delay_seconds = 60
    cfg.lan.preflight_retries = 4
    cfg.lan.preflight_retry_delay_seconds = 15
    cfg.lan.max_attempts = 2
    cfg.lan.retry_delay_seconds = 600
    cfg.lan.shutdown_on_all_retries_exhausted = False
    return cfg


@patch("flow._record_running")
@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.cloud_publish_artifact_task", new=MagicMock())
@patch("flow.cloud_record_task", new=MagicMock())
@patch("flow.cloud_verify_and_report_task")
@patch("flow.cloud_sync_task")
@patch("flow.cloud_access_task", new=MagicMock())
@patch("flow.cloud_guards_task", new=MagicMock())
@patch("flow.cloud_preflight_task")
@patch("flow.health_check_task", new=MagicMock())
@patch("flow.ManifestDB", new=MagicMock())
@patch("flow._record_run", new=MagicMock())
def test_cloud_pipeline_takes_task_retries_from_config(pre, sync, verify, _art, _running):
    sync.with_options.return_value.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0,
                                                   "stats": None}
    verify.with_options.return_value.return_value = {
        "verified": True, "verify_termination": "normal", "size": {"count": 0, "bytes": 0},
        "manifest": [], "diff": {"added": [], "removed": [], "modified": []}}
    flow._run_cloud_pipeline(_cfg(), "run-r", "2026-10-16T02:00:00")
    pre.with_options.assert_called_once_with(retries=3, retry_delay_seconds=45)
    verify.with_options.assert_called_once_with(retries=2, retry_delay_seconds=90)


@patch("flow._record_running")
@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.lan_publish_artifact_task", new=MagicMock())
@patch("flow.lan_record_task", new=MagicMock())
@patch("flow.lan_snapshot_after_task", new=MagicMock(return_value={}))
@patch("flow.lan_shutdown_task", new=MagicMock())
@patch("flow.lan_sync_task")
@patch("flow.lan_access_task", new=MagicMock())
@patch("flow.lan_guards_task", new=MagicMock())
@patch("flow.lan_snapshot_before_task", new=MagicMock(return_value={}))
@patch("flow.lan_preflight_task")
@patch("flow.wol_check_task", new=MagicMock())
@patch("flow.health_check_task", new=MagicMock())
@patch("flow._record_run", new=MagicMock())
def test_lan_pipeline_takes_preflight_retries_from_config(pre, sync, _art, _running):
    sync.with_options.return_value.return_value = {
        "status": "LAN_COMPLETE", "exit_code": 0,
        "counts": {"total": 0, "copied": 0, "skipped": 0, "mismatch": 0, "failed": 0, "extras": 0},
        "bytes": {"total": 0, "copied": 0, "skipped": 0, "mismatch": 0, "failed": 0, "extras": 0},
        "files_deleted": 0}
    flow._run_lan_pipeline(_cfg(), "run-rl", "2026-10-16T21:00:00")
    pre.with_options.assert_called_once_with(retries=4, retry_delay_seconds=15)
