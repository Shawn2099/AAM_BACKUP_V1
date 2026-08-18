"""Regression tests for F1/F2/F3/G7/G10 fixes (fix log 2026-08-18).

Covers the pipeline status semantics that no prior test touched:
- F1: cloud verify failure must NOT be recorded as CLOUD_COMPLETE
- F2: a failed pipeline must be recorded with its true terminal status
- F3: LAN_PARTIAL must alert and must NOT shut the NAS down
- G7: cloud timeout message states resumability
- G10: scheduled rollover-check flow (no-op / completed / blocked)
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import flow


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# F1 — cloud verify failure must fail the run with a distinct status
# ═══════════════════════════════════════════════════════════════

class TestCloudVerifyFailure:
    @patch("flow.send_failure_alert", return_value=True)
    @patch("flow.cloud_publish_artifact_task")
    @patch("flow.cloud_record_task")
    @patch("flow.cloud_verify_and_report_task")
    @patch("flow.cloud_sync_task")
    @patch("flow.cloud_preflight_task")
    @patch("flow.health_check_task")
    @patch("flow.get_fy_prefix", return_value="FY26-27")
    @patch("flow.ManifestDB")
    @patch("flow._record_run")
    def test_verify_failure_records_verify_failed_and_alerts(
        self, mock_record, mock_db, mock_fy, mock_health, mock_preflight,
        mock_sync, mock_verify, mock_record_task, mock_artifact, mock_alert,
    ):
        mock_sync.with_options.return_value.return_value = {
            "status": "CLOUD_COMPLETE", "exit_code": 0, "error": None,
        }
        mock_verify.with_options.return_value.return_value = {
            "verified": False,
            "size": {"count": 0, "bytes": 0},
            "manifest": [],
            "diff": {"added": ["a.txt"], "removed": ["b.txt", "c.txt"],
                     "modified": ["d.txt"], "unchanged": []},
        }
        mock_db.return_value.get_cloud_synced_entries.return_value = {}
        cfg = _make_config()

        with pytest.raises(RuntimeError, match="verification FAILED"):
            flow._run_cloud_pipeline(cfg, "run-1", "2026-08-18T18:00:00")

        # True terminal status recorded (F1+F2)
        args = mock_record.call_args.args
        assert args[4] == "CLOUD_VERIFY_FAILED"
        assert "missing-from-cloud=2" in args[6]
        # Alert fired with the verify details
        mock_alert.assert_called_once()
        alert_err = mock_alert.call_args.args[2]
        assert "missing-from-cloud=2" in alert_err
        assert "size-changed=1" in alert_err
        # Observed cloud state still recorded to the DB before failing
        mock_record_task.assert_called_once()
        # No artifact on failure
        mock_artifact.assert_not_called()

    @patch("flow.send_failure_alert", return_value=True)
    @patch("flow.cloud_publish_artifact_task")
    @patch("flow.cloud_record_task")
    @patch("flow.cloud_verify_and_report_task")
    @patch("flow.cloud_sync_task")
    @patch("flow.cloud_preflight_task")
    @patch("flow.health_check_task")
    @patch("flow.get_fy_prefix", return_value="FY26-27")
    @patch("flow.ManifestDB")
    @patch("flow._record_run")
    def test_verify_ok_records_complete_no_alert(
        self, mock_record, mock_db, mock_fy, mock_health, mock_preflight,
        mock_sync, mock_verify, mock_record_task, mock_artifact, mock_alert,
    ):
        mock_sync.with_options.return_value.return_value = {
            "status": "CLOUD_COMPLETE", "exit_code": 0, "error": None,
        }
        mock_verify.with_options.return_value.return_value = {
            "verified": True,
            "size": {"count": 5, "bytes": 5000},
            "manifest": [{"Path": "a.txt", "Size": 100, "ModTime": "2026-08-18T10:00:00Z"}],
            "diff": {"added": [], "removed": [], "modified": [], "unchanged": ["a.txt"]},
        }
        mock_db.return_value.get_cloud_synced_entries.return_value = {}
        cfg = _make_config()

        result = flow._run_cloud_pipeline(cfg, "run-2", "2026-08-18T18:00:00")

        assert result["status"] == "CLOUD_COMPLETE"
        args = mock_record.call_args.args
        assert args[4] == "CLOUD_COMPLETE"
        assert args[6] is None
        mock_alert.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# F2 — true terminal status on failure
# ═══════════════════════════════════════════════════════════════

class TestCloudFailureStatus:
    @patch("flow.cloud_publish_artifact_task")
    @patch("flow.cloud_record_task")
    @patch("flow.cloud_verify_and_report_task")
    @patch("flow.cloud_sync_task")
    @patch("flow.cloud_preflight_task")
    @patch("flow.health_check_task")
    @patch("flow.get_fy_prefix", return_value="FY26-27")
    @patch("flow.ManifestDB")
    @patch("flow._record_run")
    def test_sync_failure_records_cloud_failed_not_skipped(
        self, mock_record, mock_db, mock_fy, mock_health, mock_preflight,
        mock_sync, mock_verify, mock_record_task, mock_artifact,
    ):
        mock_sync.with_options.return_value.side_effect = RuntimeError("Cloud sync failed")
        mock_db.return_value.get_cloud_synced_entries.return_value = {}
        cfg = _make_config()

        with pytest.raises(RuntimeError):
            flow._run_cloud_pipeline(cfg, "run-3", "2026-08-18T18:00:00")

        args = mock_record.call_args.args
        assert args[4] == "CLOUD_FAILED"  # was CLOUD_SKIPPED before the fix
        assert args[6] == "Cloud sync failed"

    @patch("flow.cloud_publish_artifact_task")
    @patch("flow.cloud_record_task")
    @patch("flow.cloud_verify_and_report_task")
    @patch("flow.cloud_sync_task")
    @patch("flow.cloud_preflight_task")
    @patch("flow.health_check_task", side_effect=RuntimeError("source missing"))
    @patch("flow.get_fy_prefix", return_value="FY26-27")
    @patch("flow.ManifestDB")
    @patch("flow._record_run")
    def test_preflight_failure_records_skipped(
        self, mock_record, mock_db, mock_fy, mock_health, mock_preflight,
        mock_sync, mock_verify, mock_record_task, mock_artifact,
    ):
        mock_db.return_value.get_cloud_synced_entries.return_value = {}
        cfg = _make_config()

        with pytest.raises(RuntimeError, match="source missing"):
            flow._run_cloud_pipeline(cfg, "run-4", "2026-08-18T18:00:00")

        args = mock_record.call_args.args
        assert args[4] == "CLOUD_SKIPPED"  # genuinely never ran — SKIPPED is honest


class TestLanFailureStatus:
    @patch("flow.lan_shutdown_task")
    @patch("flow.lan_publish_artifact_task")
    @patch("flow.lan_record_task")
    @patch("flow.lan_snapshot_after_task")
    @patch("flow.lan_snapshot_before_task", return_value={})
    @patch("flow.lan_sync_task")
    @patch("flow.lan_preflight_task")
    @patch("flow.wol_check_task")
    @patch("flow.health_check_task")
    @patch("flow._record_run")
    def test_lan_sync_failure_records_failed_not_skipped(
        self, mock_record, mock_health, mock_wol, mock_preflight, mock_sync,
        mock_before, mock_after, mock_record_task, mock_artifact, mock_shutdown,
    ):
        mock_sync.with_options.return_value.side_effect = RuntimeError("LAN sync failed")
        cfg = _make_config()

        with pytest.raises(RuntimeError):
            flow._run_lan_pipeline(cfg, "run-5", "2026-08-18T01:00:00")

        args = mock_record.call_args.args
        assert args[4] == "LAN_FAILED"  # was LAN_SKIPPED before the fix

    @patch("flow.lan_shutdown_task")
    @patch("flow.lan_publish_artifact_task")
    @patch("flow.lan_record_task")
    @patch("flow.lan_snapshot_after_task")
    @patch("flow.lan_snapshot_before_task")
    @patch("flow.lan_sync_task")
    @patch("flow.lan_preflight_task")
    @patch("flow.wol_check_task")
    @patch("flow.health_check_task", side_effect=RuntimeError("NAS unreachable"))
    @patch("flow._record_run")
    def test_lan_preflight_failure_records_skipped(
        self, mock_record, mock_health, mock_wol, mock_preflight, mock_sync,
        mock_before, mock_after, mock_record_task, mock_artifact, mock_shutdown,
    ):
        cfg = _make_config()

        with pytest.raises(RuntimeError, match="NAS unreachable"):
            flow._run_lan_pipeline(cfg, "run-6", "2026-08-18T01:00:00")

        args = mock_record.call_args.args
        assert args[4] == "LAN_SKIPPED"


# ═══════════════════════════════════════════════════════════════
# F3 — PARTIAL: alert, no NAS shutdown; COMPLETE: shutdown
# ═══════════════════════════════════════════════════════════════

class TestLanPartialShutdown:
    @patch("flow.send_failure_alert", return_value=True)
    @patch("flow.lan_shutdown_task")
    @patch("flow.lan_publish_artifact_task")
    @patch("flow.lan_record_task")
    @patch("flow.lan_snapshot_after_task", return_value={"a.txt": (100, 1.0)})
    @patch("flow.lan_snapshot_before_task", return_value={})
    @patch("flow.lan_sync_task")
    @patch("flow.lan_preflight_task")
    @patch("flow.wol_check_task")
    @patch("flow.health_check_task")
    @patch("flow._record_run")
    def test_partial_alerts_and_skips_shutdown(
        self, mock_record, mock_health, mock_wol, mock_preflight, mock_sync,
        mock_before, mock_after, mock_record_task, mock_artifact, mock_shutdown, mock_alert,
    ):
        mock_sync.with_options.return_value.return_value = {
            "status": "LAN_PARTIAL", "exit_code": 9,
            "error": "robocopy tail: 3 files FAILED",
        }
        cfg = _make_config()

        result = flow._run_lan_pipeline(cfg, "run-7", "2026-08-18T01:00:00")

        assert result["status"] == "LAN_PARTIAL"
        mock_shutdown.assert_not_called()  # THE F3 BUG: used to shut the NAS down
        mock_alert.assert_called_once()
        alert_msg = mock_alert.call_args.args[2]
        assert "PARTIAL" in alert_msg and "9" in alert_msg
        args = mock_record.call_args.args
        assert args[4] == "LAN_PARTIAL"

    @patch("flow.send_failure_alert", return_value=True)
    @patch("flow.lan_shutdown_task")
    @patch("flow.lan_publish_artifact_task")
    @patch("flow.lan_record_task")
    @patch("flow.lan_snapshot_after_task", return_value={"a.txt": (100, 1.0)})
    @patch("flow.lan_snapshot_before_task", return_value={})
    @patch("flow.lan_sync_task")
    @patch("flow.lan_preflight_task")
    @patch("flow.wol_check_task")
    @patch("flow.health_check_task")
    @patch("flow._record_run")
    def test_complete_shuts_down_and_no_alert(
        self, mock_record, mock_health, mock_wol, mock_preflight, mock_sync,
        mock_before, mock_after, mock_record_task, mock_artifact, mock_shutdown, mock_alert,
    ):
        mock_sync.with_options.return_value.return_value = {
            "status": "LAN_COMPLETE", "exit_code": 1, "error": None,
        }
        cfg = _make_config()

        result = flow._run_lan_pipeline(cfg, "run-8", "2026-08-18T01:00:00")

        assert result["status"] == "LAN_COMPLETE"
        mock_shutdown.assert_called_once()  # full mirror OK — power off the NAS
        mock_alert.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# G7 — timeout message states resumability
# ═══════════════════════════════════════════════════════════════

class TestCloudTimeoutMessage:
    @patch("core.cloud_sync.temp_rclone_config")
    def test_timeout_error_is_resumable(self, mock_temp_cfg):
        from contextlib import contextmanager

        @contextmanager
        def fake_cfg(*a, **k):
            yield "/tmp/rclone-test.conf"

        mock_temp_cfg.side_effect = fake_cfg

        with patch("core.cloud_sync.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="rclone", timeout=300)):
            from core.cloud_sync import run_cloud_sync
            result = run_cloud_sync(
                source="E:\\DATA", bucket="b", fy_prefix="FY26-27",
                gcs_key_path="/tmp/k.json", project_number="1", storage_class="STANDARD",
                timeout=300,
            )

        assert result["status"] == "CLOUD_FAILED"
        assert result["exit_code"] == -1
        assert "resumable" in result["error"]
        assert "next run continues" in result["error"]


# ═══════════════════════════════════════════════════════════════
# G10 — scheduled rollover-check flow
# ═══════════════════════════════════════════════════════════════

class TestRolloverCheckFlow:
    @patch("core.fy_rollover.rollover", return_value=False)
    def test_no_rollover_needed(self, mock_rollover):
        assert flow.rollover_check_flow.fn("config.yaml") == "NO_ROLLOVER_NEEDED"
        mock_rollover.assert_called_once_with(config_path="config.yaml")

    @patch("core.fy_rollover.rollover", return_value=True)
    def test_rollover_completed(self, mock_rollover):
        assert flow.rollover_check_flow.fn("config.yaml") == "ROLLOVER_COMPLETED"

    @patch("flow.load_config")
    @patch("flow.send_failure_alert", return_value=True)
    @patch("core.fy_rollover.rollover")
    def test_blocked_alerts_and_raises(self, mock_rollover, mock_alert, mock_cfg):
        from core.fy_rollover import RolloverError
        mock_rollover.side_effect = RolloverError("source drive E: not mounted")
        mock_cfg.return_value = _make_config()
        with pytest.raises(RolloverError, match="not mounted"):
            flow.rollover_check_flow.fn("config.yaml")
        mock_alert.assert_called_once()
        assert "BLOCKED" in mock_alert.call_args.args[2]
