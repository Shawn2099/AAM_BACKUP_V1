"""Tests for flow.py — decomposed tasks, pipeline orchestration, and failure alerting."""

from unittest.mock import patch, MagicMock
import pytest

from exceptiongroup import ExceptionGroup

from flow import (
    backup, weekly_report_flow, monthly_report_flow,
    health_check_task, cloud_preflight_task, cloud_sync_task,
    cloud_verify_and_report_task, cloud_record_task,
    wol_check_task, lan_preflight_task, lan_snapshot_before_task,
    lan_snapshot_after_task,
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
    @patch("flow.run_cloud_dry_run", return_value={"ok": True, "exit_code": 0, "error": None})
    def test_success_returns_result(self, mock_dry):
        config = MagicMock()
        result = cloud_preflight_task.fn(config, "FY26-27")
        assert result["ok"] is True

    @patch("flow.run_cloud_dry_run", return_value={"ok": False, "exit_code": 2, "error": "auth failed"})
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


class TestLanSnapshotTasks:
    @patch("flow.walk_lan_destination", return_value=[])
    @patch("flow.snapshot_to_dict", return_value={"a.txt": (100, 1.0)})
    def test_snapshot_before_returns_dict(self, mock_snap, mock_walk):
        config = MagicMock()
        result = lan_snapshot_before_task.fn(config)
        assert isinstance(result, dict)
        assert "a.txt" in result

    @patch("flow.walk_lan_destination", return_value=[])
    @patch("flow.snapshot_to_dict", return_value={"b.txt": (200, 2.0)})
    def test_snapshot_after_returns_dict(self, mock_snap, mock_walk):
        config = MagicMock()
        result = lan_snapshot_after_task.fn(config)
        assert isinstance(result, dict)
        assert "b.txt" in result


class TestLanRecordTask:
    @patch("flow.ManifestDB")
    @patch("flow.record_sync_results")
    @patch("flow.diff_snapshots", return_value={"added": {}, "removed": {}, "modified": {}})
    def test_records_with_after_dict(self, mock_diff, mock_record, mock_db_cls):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        # snapshot_to_dict returns {path: (size, mtime)} tuples
        before = {"old.txt": (100, 1.0)}
        after = {"new.txt": (200, 2.0)}
        lan_record_task.fn("/tmp/test.db", {"status": "LAN_COMPLETE"}, before, after)
        mock_record.assert_called_once()
        mock_db.close.assert_called_once()


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
    @patch("flow.logger")
    @patch("flow.record_run_history", return_value=False)
    def test_logs_critical_when_run_history_not_persisted(self, mock_record, mock_logger, mock_db_cls):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        _record_run("/tmp/test.db", "run-1", "cloud", "2026-01-01T00:00:00Z",
                    "CLOUD_COMPLETE", 0, None)
        mock_logger.critical.assert_called_once()
        mock_db.close.assert_called_once()


class TestBackupMaintenance:
    @patch("flow.ManifestDB")
    @patch("flow.concurrency")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    def test_maintenance_uses_configured_sqlite_tuning(
        self,
        mock_load_config,
        mock_log,
        mock_bridge,
        mock_concurrency,
        mock_db_cls,
    ):
        config = MagicMock()
        config.firm_name = "Test Firm"
        config.cloud.enabled = False
        config.lan.enabled = False
        config.paths.database_path = "/tmp/test.db"
        config.paths.log_directory = "/tmp/logs"
        config.maintenance.db_retention_days = 30
        config.maintenance.log_retention_days = 7
        config.maintenance.sqlite_busy_timeout_ms = 45000
        config.maintenance.sqlite_vacuum_freelist_threshold = 12345
        mock_load_config.return_value = config

        concurrency_cm = MagicMock()
        concurrency_cm.__enter__.return_value = None
        concurrency_cm.__exit__.return_value = False
        mock_concurrency.return_value = concurrency_cm

        backup.fn("config.yaml", "all")

        mock_db_cls.assert_called_with(
            "/tmp/test.db",
            busy_timeout_ms=45000,
            vacuum_freelist_threshold=12345,
        )


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


# ═══════════════════════════════════════════════════════════════
# Failure alert path — backup() error collection → send_failure_alert
# ═══════════════════════════════════════════════════════════════

class TestFailureAlertPath:
    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.send_failure_alert")
    @patch("flow._run_cloud_pipeline", side_effect=RuntimeError("cloud: auth failed"))
    def test_single_failure_sends_alert(self, mock_pipeline, mock_alert, mock_bridge, mock_log, mock_cfg):
        mock_config = MagicMock()
        mock_config.cloud.enabled = True
        mock_config.lan.enabled = False
        mock_config.firm_name = "Test Firm"
        mock_config.notifications = MagicMock()
        mock_cfg.return_value = mock_config

        with pytest.raises(ExceptionGroup, match="Backup completed with errors"):
            backup.fn("config.yaml", "cloud")

        mock_alert.assert_called_once()
        call_args = mock_alert.call_args
        assert call_args[0][2] == "cloud: auth failed"
        assert call_args[0][3]["mode"] == "cloud"

    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.send_failure_alert")
    @patch("flow._run_cloud_pipeline", side_effect=RuntimeError("cloud error"))
    @patch("flow._run_lan_pipeline", side_effect=RuntimeError("lan error"))
    def test_both_failures_join_messages(self, mock_lan, mock_cloud, mock_alert, mock_bridge, mock_log, mock_cfg):
        mock_config = MagicMock()
        mock_config.cloud.enabled = True
        mock_config.lan.enabled = True
        mock_config.firm_name = "Test Firm"
        mock_config.notifications = MagicMock()
        mock_cfg.return_value = mock_config

        with pytest.raises(ExceptionGroup, match="Backup completed with errors"):
            backup.fn("config.yaml", "all")

        call_args = mock_alert.call_args
        error_msg = call_args[0][2]
        assert "cloud error" in error_msg
        assert "lan error" in error_msg
        assert "; " in error_msg

    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.send_failure_alert", side_effect=RuntimeError("SMTP down"))
    @patch("flow._run_cloud_pipeline", side_effect=RuntimeError("cloud error"))
    def test_alert_failure_does_not_suppress_backup_exception(self, mock_pipeline, mock_alert, mock_bridge, mock_log, mock_cfg):
        mock_config = MagicMock()
        mock_config.cloud.enabled = True
        mock_config.lan.enabled = False
        mock_config.firm_name = "Test Firm"
        mock_config.notifications = MagicMock()
        mock_cfg.return_value = mock_config

        with pytest.raises(ExceptionGroup, match="Backup completed with errors"):
            backup.fn("config.yaml", "cloud")
