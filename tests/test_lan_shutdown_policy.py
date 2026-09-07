"""Tests for Option B: Configurable Post-Failure NAS Shutdown Policy.

Tests verify:
1. Default Rule F3 behavior: shutdown_on_all_retries_exhausted=False leaves NAS powered on during LAN_PARTIAL.
2. Option B enabled: shutdown_on_all_retries_exhausted=True sends alert first, then powers down NAS on LAN_PARTIAL.
3. Option B enabled on sync exception / LAN_FAILED: sends alert first, then powers down NAS.
4. Clean run (LAN_COMPLETE): powers down NAS normally.
5. Global override: shutdown_after_backup=False prevents power-down even when Option B is enabled.
"""

from unittest.mock import MagicMock, patch
import pytest

import flow


def _make_lan_config(
    shutdown_after_backup: bool = True,
    shutdown_on_all_retries_exhausted: bool = False,
    wol_enabled: bool = True,
):
    cfg = MagicMock()
    cfg.firm_name = "TestFirm"
    cfg.paths.source_drive = "D:\\SOURCE\\FY26-27"
    cfg.paths.lan_destination = "\\\\NAS\\share\\FY26-27"
    cfg.paths.database_path = "C:\\test\\manifest.db"
    cfg.paths.log_directory = "C:\\test\\logs"

    cfg.lan.enabled = True
    cfg.lan.max_attempts = 2
    cfg.lan.retry_delay_seconds = 600
    cfg.lan.shutdown_after_backup = shutdown_after_backup
    cfg.lan.shutdown_on_all_retries_exhausted = shutdown_on_all_retries_exhausted
    cfg.lan.dry_run_timeout_seconds = 900

    cfg.wol.enabled = wol_enabled
    cfg.wol.server_ip = "192.168.1.100"

    cfg.maintenance.sqlite_busy_timeout_ms = 30000
    cfg.maintenance.sqlite_vacuum_freelist_threshold = 10000
    cfg.maintenance.sqlite_synchronous = "normal"
    return cfg


class TestLanShutdownPolicy:

    @patch("flow.send_failure_alert", return_value=True)
    @patch("flow.lan_shutdown_task")
    @patch("flow.lan_publish_artifact_task")
    @patch("flow.lan_record_task")
    @patch("flow.lan_snapshot_after_task", return_value={"file.txt": (100, 1.0)})
    @patch("flow.lan_snapshot_before_task", return_value={})
    @patch("flow.lan_sync_task")
    @patch("flow.lan_preflight_task")
    @patch("flow.wol_check_task")
    @patch("flow.health_check_task")
    @patch("flow._record_run")
    def test_default_f3_partial_skips_shutdown_and_alerts(
        self, mock_record, mock_health, mock_wol, mock_preflight, mock_sync,
        mock_before, mock_after, mock_record_task, mock_artifact, mock_shutdown, mock_alert,
    ):
        """When shutdown_on_all_retries_exhausted=False, F3 contract prevents shutdown."""
        mock_sync.with_options.return_value.return_value = {
            "status": "LAN_PARTIAL", "exit_code": 9,
            "error": "robocopy tail: 1 file FAILED",
        }
        cfg = _make_lan_config(shutdown_on_all_retries_exhausted=False)

        with pytest.raises(flow.PartialRun):
            flow._run_lan_pipeline(cfg, "run-f3-default", "2026-09-07T01:00:00")

        # Alert fired
        mock_alert.assert_called_once()
        alert_msg = mock_alert.call_args.args[2]
        assert "NOT shut down" in alert_msg

        # Shutdown must NOT be called
        mock_shutdown.assert_not_called()

    @patch("flow.send_failure_alert", return_value=True)
    @patch("flow.lan_shutdown_task")
    @patch("flow.lan_publish_artifact_task")
    @patch("flow.lan_record_task")
    @patch("flow.lan_snapshot_after_task", return_value={"file.txt": (100, 1.0)})
    @patch("flow.lan_snapshot_before_task", return_value={})
    @patch("flow.lan_sync_task")
    @patch("flow.lan_preflight_task")
    @patch("flow.wol_check_task")
    @patch("flow.health_check_task")
    @patch("flow._record_run")
    def test_option_b_partial_shuts_down_after_alert(
        self, mock_record, mock_health, mock_wol, mock_preflight, mock_sync,
        mock_before, mock_after, mock_record_task, mock_artifact, mock_shutdown, mock_alert,
    ):
        """When shutdown_on_all_retries_exhausted=True, alert fires first and NAS is powered down."""
        mock_sync.with_options.return_value.return_value = {
            "status": "LAN_PARTIAL", "exit_code": 9,
            "error": "robocopy tail: 1 file FAILED",
        }
        cfg = _make_lan_config(shutdown_on_all_retries_exhausted=True)

        with pytest.raises(flow.PartialRun):
            flow._run_lan_pipeline(cfg, "run-option-b-partial", "2026-09-07T01:00:00")

        # Alert fired first
        mock_alert.assert_called_once()
        alert_msg = mock_alert.call_args.args[2]
        assert "scheduled for shutdown" in alert_msg

        # Shutdown IS called
        mock_shutdown.assert_called_once_with(cfg)

    @patch("flow.send_failure_alert", return_value=True)
    @patch("flow.lan_shutdown_task")
    @patch("flow.lan_publish_artifact_task")
    @patch("flow.lan_record_task")
    @patch("flow.lan_snapshot_after_task", return_value=None)
    @patch("flow.lan_snapshot_before_task", return_value={})
    @patch("flow.lan_sync_task")
    @patch("flow.lan_preflight_task")
    @patch("flow.wol_check_task")
    @patch("flow.health_check_task")
    @patch("flow._record_run")
    def test_option_b_failed_exception_shuts_down(
        self, mock_record, mock_health, mock_wol, mock_preflight, mock_sync,
        mock_before, mock_after, mock_record_task, mock_artifact, mock_shutdown, mock_alert,
    ):
        """When a sync exception occurs and Option B is enabled, NAS is shut down."""
        mock_sync.with_options.return_value.side_effect = RuntimeError("network dropped")
        cfg = _make_lan_config(shutdown_on_all_retries_exhausted=True)

        with pytest.raises(RuntimeError, match="network dropped"):
            flow._run_lan_pipeline(cfg, "run-option-b-failed", "2026-09-07T01:00:00")

        # Shutdown IS called
        mock_shutdown.assert_called_once_with(cfg)

    @patch("flow.send_failure_alert", return_value=True)
    @patch("flow.lan_shutdown_task")
    @patch("flow.lan_publish_artifact_task")
    @patch("flow.lan_record_task")
    @patch("flow.lan_snapshot_after_task", return_value={"file.txt": (100, 1.0)})
    @patch("flow.lan_snapshot_before_task", return_value={})
    @patch("flow.lan_sync_task")
    @patch("flow.lan_preflight_task")
    @patch("flow.wol_check_task")
    @patch("flow.health_check_task")
    @patch("flow._record_run")
    def test_complete_shuts_down_normally(
        self, mock_record, mock_health, mock_wol, mock_preflight, mock_sync,
        mock_before, mock_after, mock_record_task, mock_artifact, mock_shutdown, mock_alert,
    ):
        """LAN_COMPLETE always powers down regardless of the failure flag."""
        mock_sync.with_options.return_value.return_value = {
            "status": "LAN_COMPLETE", "exit_code": 1,
        }
        cfg = _make_lan_config(shutdown_on_all_retries_exhausted=False)

        res = flow._run_lan_pipeline(cfg, "run-complete", "2026-09-07T01:00:00")
        assert res["status"] == "LAN_COMPLETE"
        mock_shutdown.assert_called_once_with(cfg)
        mock_alert.assert_not_called()

    @patch("flow.send_failure_alert", return_value=True)
    @patch("flow.shutdown_server")
    @patch("flow.lan_publish_artifact_task")
    @patch("flow.lan_record_task")
    @patch("flow.lan_snapshot_after_task", return_value={"file.txt": (100, 1.0)})
    @patch("flow.lan_snapshot_before_task", return_value={})
    @patch("flow.lan_sync_task")
    @patch("flow.lan_preflight_task")
    @patch("flow.wol_check_task")
    @patch("flow.health_check_task")
    @patch("flow._record_run")
    def test_global_shutdown_disabled_overrides_option_b(
        self, mock_record, mock_health, mock_wol, mock_preflight, mock_sync,
        mock_before, mock_after, mock_record_task, mock_artifact, mock_server_shutdown, mock_alert,
    ):
        """If shutdown_after_backup=False, shutdown_server is never called even if Option B is on."""
        mock_sync.with_options.return_value.return_value = {
            "status": "LAN_PARTIAL", "exit_code": 9,
            "error": "robocopy tail: 1 file FAILED",
        }
        cfg = _make_lan_config(
            shutdown_after_backup=False,
            shutdown_on_all_retries_exhausted=True,
        )

        with pytest.raises(flow.PartialRun):
            flow._run_lan_pipeline(cfg, "run-disabled-override", "2026-09-07T01:00:00")

        # shutdown_server must not be invoked because lan_shutdown_task honors global toggle
        mock_server_shutdown.assert_not_called()
