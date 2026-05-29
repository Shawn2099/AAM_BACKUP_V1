"""Tests for flow.py — decomposed tasks and pipeline orchestration."""

from unittest.mock import patch, MagicMock
import pytest

from flow import (
    backup, weekly_report_flow, monthly_report_flow,
    health_check_task, cloud_preflight_task, cloud_sync_task,
    cloud_verify_and_report_task, cloud_record_task,
    wol_check_task, lan_preflight_task, lan_snapshot_before_task,
    lan_sync_task, lan_record_task, lan_shutdown_task,
    _run_cloud_pipeline, _run_lan_pipeline, _record_run,
)


# ═══════════════════════════════════════════════════════════════
# Mode routing
# ═══════════════════════════════════════════════════════════════

class TestBackupModeRouting:
    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.configure_prefect_bridge")
    def test_invalid_mode_raises(self, mock_bridge, mock_log, mock_cfg):
        with pytest.raises(ValueError, match="Invalid mode"):
            backup.fn("config.yaml", "invalid")

    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.configure_prefect_bridge")
    def test_valid_modes_accepted(self, mock_bridge, mock_log, mock_cfg):
        mock_config = MagicMock()
        mock_config.cloud.enabled = False
        mock_config.lan.enabled = False
        mock_cfg.return_value = mock_config
        for mode in ("cloud", "lan", "all"):
            backup.fn("config.yaml", mode)

    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.configure_prefect_bridge")
    def test_mode_case_insensitive(self, mock_bridge, mock_log, mock_cfg):
        mock_config = MagicMock()
        mock_config.cloud.enabled = False
        mock_config.lan.enabled = False
        mock_cfg.return_value = mock_config
        backup.fn("config.yaml", "CLOUD")


class TestBackupDisabledPipelines:
    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.configure_prefect_bridge")
    def test_both_disabled_completes(self, mock_bridge, mock_log, mock_cfg):
        mock_config = MagicMock()
        mock_config.cloud.enabled = False
        mock_config.lan.enabled = False
        mock_cfg.return_value = mock_config
        backup.fn("config.yaml", "all")


# ═══════════════════════════════════════════════════════════════
# Individual tasks — health check
# ═══════════════════════════════════════════════════════════════

class TestHealthCheckTask:
    @patch("flow.pre_backup_health")
    def test_calls_health_check(self, mock_health):
        config = MagicMock()
        health_check_task.fn(config, "cloud")
        mock_health.assert_called_once()

    @patch("flow.pre_backup_health", side_effect=RuntimeError("source missing"))
    def test_raises_on_failure(self, mock_health):
        config = MagicMock()
        with pytest.raises(RuntimeError, match="source missing"):
            health_check_task.fn(config, "cloud")


# ═══════════════════════════════════════════════════════════════
# Individual tasks — cloud
# ═══════════════════════════════════════════════════════════════

class TestCloudPreflightTask:
    @patch("flow.run_cloud_dry_run", return_value={"ok": True, "matched": True, "exit_code": 0, "error": None})
    def test_success_returns_result(self, mock_dry):
        config = MagicMock()
        result = cloud_preflight_task.fn(config, "FY26-27")
        assert result["ok"] is True

    @patch("flow.run_cloud_dry_run", return_value={"ok": False, "matched": False, "exit_code": 2, "error": "auth failed"})
    def test_failure_raises(self, mock_dry):
        config = MagicMock()
        with pytest.raises(RuntimeError, match="Cloud preflight failed"):
            cloud_preflight_task.fn(config, "FY26-27")


class TestCloudSyncTask:
    @patch("flow.run_cloud_sync", return_value={"status": "CLOUD_COMPLETE", "exit_code": 0, "error": None})
    def test_success_returns_result(self, mock_sync):
        config = MagicMock()
        result = cloud_sync_task.fn(config, "FY26-27")
        assert result["status"] == "CLOUD_COMPLETE"

    @patch("flow.run_cloud_sync", return_value={"status": "CLOUD_FAILED", "exit_code": 7, "error": "auth error"})
    def test_failure_raises(self, mock_sync):
        config = MagicMock()
        with pytest.raises(RuntimeError, match="auth error"):
            cloud_sync_task.fn(config, "FY26-27")


class TestCloudRecordTask:
    @patch("flow.ManifestDB")
    @patch("flow.record_sync_results")
    def test_records_to_db(self, mock_record, mock_db_cls):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        verify_data = {
            "manifest": [{"Path": "a.txt", "Size": 100, "ModTime": 1.0}],
            "diff": {"removed": ["old.txt"]},
        }
        cloud_record_task.fn("/tmp/test.db", verify_data, {"status": "CLOUD_COMPLETE"})
        mock_record.assert_called_once()
        mock_db.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# Individual tasks — LAN
# ═══════════════════════════════════════════════════════════════

class TestWolCheckTask:
    def test_skips_when_disabled(self):
        config = MagicMock()
        config.wol.enabled = False
        wol_check_task.fn(config)  # Should not raise

    @patch("flow.ensure_server_online")
    def test_wakes_when_enabled(self, mock_wol):
        config = MagicMock()
        config.wol.enabled = True
        wol_check_task.fn(config)
        mock_wol.assert_called_once()


class TestLanPreflightTask:
    @patch("flow.run_lan_dry_run", return_value={"ok": True, "exit_code": 0, "error": None})
    def test_success_returns_result(self, mock_dry):
        config = MagicMock()
        result = lan_preflight_task.fn(config)
        assert result["ok"] is True

    @patch("flow.run_lan_dry_run", return_value={"ok": False, "exit_code": 16, "error": "path not found"})
    def test_failure_raises(self, mock_dry):
        config = MagicMock()
        with pytest.raises(RuntimeError, match="LAN preflight failed"):
            lan_preflight_task.fn(config)


class TestLanSyncTask:
    @patch("flow.run_lan_sync", return_value={"status": "LAN_COMPLETE", "exit_code": 0, "error": None})
    def test_success_returns_result(self, mock_sync):
        config = MagicMock()
        result = lan_sync_task.fn(config)
        assert result["status"] == "LAN_COMPLETE"

    @patch("flow.run_lan_sync", return_value={"status": "LAN_FAILED", "exit_code": 16, "error": "fatal"})
    def test_failure_raises(self, mock_sync):
        config = MagicMock()
        with pytest.raises(RuntimeError, match="fatal"):
            lan_sync_task.fn(config)


class TestLanShutdownTask:
    def test_skips_when_shutdown_disabled(self):
        config = MagicMock()
        config.lan.shutdown_after_backup = False
        config.wol.enabled = True
        lan_shutdown_task.fn(config)  # Should not raise

    def test_skips_when_wol_disabled(self):
        config = MagicMock()
        config.lan.shutdown_after_backup = True
        config.wol.enabled = False
        lan_shutdown_task.fn(config)  # Should not raise

    @patch("flow.shutdown_server")
    def test_shutdowns_when_enabled(self, mock_shutdown):
        config = MagicMock()
        config.lan.shutdown_after_backup = True
        config.wol.enabled = True
        config.wol.server_ip = "192.168.10.10"
        lan_shutdown_task.fn(config)
        mock_shutdown.assert_called_once_with("192.168.10.10")


# ═══════════════════════════════════════════════════════════════
# Pipeline orchestrators
# ═══════════════════════════════════════════════════════════════

class TestRecordRun:
    @patch("flow.ManifestDB")
    @patch("flow.record_run_history")
    def test_records_run(self, mock_record, mock_db_cls):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        _record_run("/tmp/test.db", "run-1", "cloud", "2026-01-01T00:00:00Z",
                     "CLOUD_COMPLETE", 0, None)
        mock_record.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("flow.ManifestDB")
    @patch("flow.record_run_history", side_effect=Exception("db error"))
    def test_handles_db_error(self, mock_record, mock_db_cls):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        # Should not raise — error is logged
        _record_run("/tmp/test.db", "run-1", "cloud", "2026-01-01T00:00:00Z",
                     "CLOUD_COMPLETE", 0, None)


# ═══════════════════════════════════════════════════════════════
# Report flows
# ═══════════════════════════════════════════════════════════════

class TestWeeklyReportFlow:
    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.ManifestDB")
    def test_calls_send_weekly_report(self, mock_db_cls, mock_log, mock_cfg):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        mock_config = MagicMock()
        mock_cfg.return_value = mock_config
        with patch("core.report.send_weekly_report") as mock_send:
            weekly_report_flow.fn("config.yaml")
            mock_send.assert_called_once()
            mock_db.close.assert_called_once()


class TestMonthlyReportFlow:
    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.ManifestDB")
    def test_calls_send_monthly_report(self, mock_db_cls, mock_log, mock_cfg):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        mock_config = MagicMock()
        mock_cfg.return_value = mock_config
        with patch("core.report.send_monthly_report") as mock_send:
            monthly_report_flow.fn("config.yaml")
            mock_send.assert_called_once()
            mock_db.close.assert_called_once()
