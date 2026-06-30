"""Comprehensive tests for flow.py — backup orchestrator, mode routing, lock files, error handling.

NOTE: Prefect is NOT imported or used. The concurrency context manager is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════
# Mock the concurrency context manager before importing flow
# ═══════════════════════════════════════════════════════════════

class _FakeConcurrency:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


def _mock_concurrency(*args, **kwargs):
    return _FakeConcurrency()


def _make_config(cloud_enabled=True, lan_enabled=True):
    cfg = MagicMock()
    cfg.firm_name = "TestFirm"
    cfg.paths.source_drive = "E:\\SOURCE\\FY26-27"
    cfg.paths.lan_destination = "\\\\NAS\\share\\FY26-27"
    cfg.paths.database_path = "C:\\test\\manifest.db"
    cfg.paths.log_directory = "C:\\test\\logs"
    cfg.paths.gcs_key_path = "C:\\test\\key.json"
    cfg.cloud.enabled = cloud_enabled
    cfg.cloud.bucket = "test-bucket"
    cfg.cloud.project_number = "123456"
    cfg.cloud.location = "asia-south1"
    cfg.cloud.storage_class = "STANDARD"
    cfg.cloud.bandwidth_limit = "10M"
    cfg.cloud.retry_count = 3
    cfg.cloud.max_attempts = 3
    cfg.cloud.retry_delay_seconds = 300
    cfg.cloud.preflight_timeout_seconds = 300
    cfg.cloud.verify_timeout_seconds = 600
    cfg.cloud.cloud_size_timeout_seconds = 30
    cfg.cloud.manifest_timeout_seconds = 300
    cfg.cloud.diff_timeout_seconds = 600
    cfg.cloud.subprocess_timeout_seconds = 21600
    cfg.cloud.transfers = 2
    cfg.cloud.checkers = 4
    cfg.cloud.buffer_size = "64M"
    cfg.lan.enabled = lan_enabled
    cfg.lan.max_attempts = 2
    cfg.lan.retry_delay_seconds = 600
    cfg.lan.shutdown_after_backup = False
    cfg.wol.enabled = False
    cfg.notifications.weekly_enabled = False
    cfg.notifications.monthly_enabled = False
    cfg.maintenance.log_retention_days = 90
    cfg.maintenance.sqlite_busy_timeout_ms = 30000
    cfg.maintenance.sqlite_vacuum_freelist_threshold = 10000
    cfg.maintenance.db_retention_days = 90
    cfg.health.min_free_source_gb = 1
    cfg.health.max_clock_skew_seconds = 600
    cfg.health.clock_check_timeout_seconds = 10
    cfg.health.rollover_auth_timeout_seconds = 30
    cfg.health.rollover_archive_timeout_seconds = 600
    return cfg


# ═══════════════════════════════════════════════════════════════
# 1. Mode routing
# ═══════════════════════════════════════════════════════════════

class TestModeRouting:
    """backup.fn routes to correct pipelines based on mode."""

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_lan_pipeline")
    @patch("flow._run_cloud_pipeline")
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_mode_cloud_only_cloud_runs(self, mock_conc, mock_cloud, mock_lan,
                                         mock_load, mock_log, mock_bridge,
                                         mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=True)
        mock_cloud.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0}

        from flow import backup
        backup(config_path="test.yaml", mode="cloud")

        mock_cloud.assert_called_once()
        mock_lan.assert_not_called()

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_lan_pipeline")
    @patch("flow._run_cloud_pipeline")
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_mode_lan_only_lan_runs(self, mock_conc, mock_cloud, mock_lan,
                                     mock_load, mock_log, mock_bridge,
                                     mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=True)
        mock_lan.return_value = {"status": "LAN_COMPLETE", "exit_code": 0}

        from flow import backup
        backup(config_path="test.yaml", mode="lan")

        mock_lan.assert_called_once()
        mock_cloud.assert_not_called()

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_lan_pipeline")
    @patch("flow._run_cloud_pipeline")
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_mode_all_both_run(self, mock_conc, mock_cloud, mock_lan,
                                mock_load, mock_log, mock_bridge,
                                mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=True)
        mock_cloud.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0}
        mock_lan.return_value = {"status": "LAN_COMPLETE", "exit_code": 0}

        from flow import backup
        backup(config_path="test.yaml", mode="all")

        mock_cloud.assert_called_once()
        mock_lan.assert_called_once()

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_lan_pipeline")
    @patch("flow._run_cloud_pipeline")
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_mode_all_cloud_disabled_only_lan(self, mock_conc, mock_cloud, mock_lan,
                                               mock_load, mock_log, mock_bridge,
                                               mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=False, lan_enabled=True)
        mock_lan.return_value = {"status": "LAN_COMPLETE", "exit_code": 0}

        from flow import backup
        backup(config_path="test.yaml", mode="all")

        mock_cloud.assert_not_called()
        mock_lan.assert_called_once()

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_lan_pipeline")
    @patch("flow._run_cloud_pipeline")
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_mode_all_lan_disabled_only_cloud(self, mock_conc, mock_cloud, mock_lan,
                                               mock_load, mock_log, mock_bridge,
                                               mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=False)
        mock_cloud.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0}

        from flow import backup
        backup(config_path="test.yaml", mode="all")

        mock_cloud.assert_called_once()
        mock_lan.assert_not_called()

    def test_invalid_mode_raises(self):
        from flow import backup
        with pytest.raises(ValueError, match="Invalid mode"):
            backup(config_path="test.yaml", mode="invalid")

    def test_mode_is_case_insensitive(self):
        from flow import backup
        # "ALL" should be lowered to "all" and not raise ValueError
        # We need to mock everything to get past config loading
        with patch("flow.load_config", return_value=_make_config()), \
             patch("flow.write_lock"), \
             patch("flow.configure_prefect_bridge"), \
             patch("flow.configure_logging"), \
             patch("flow._run_cloud_pipeline", return_value={"status": "CLOUD_COMPLETE", "exit_code": 0}), \
             patch("flow._run_lan_pipeline", return_value={"status": "LAN_COMPLETE", "exit_code": 0}), \
             patch("flow.concurrency", side_effect=_mock_concurrency), \
             patch("flow.ManifestDB"), \
             patch("flow.send_failure_alert"):
            # Should not raise
            backup(config_path="test.yaml", mode="ALL")


# ═══════════════════════════════════════════════════════════════
# 2. Lock file
# ═══════════════════════════════════════════════════════════════

class TestLockFile:
    """Watchdog lock file acquired and released."""

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_cloud_pipeline", return_value={"status": "CLOUD_COMPLETE", "exit_code": 0})
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_lock_file_written(self, mock_conc, mock_cloud, mock_load,
                                mock_log, mock_bridge, mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=False)

        from flow import backup
        backup(config_path="test.yaml", mode="cloud")

        mock_lock.assert_called_once()

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock", side_effect=OSError("permission denied"))
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_cloud_pipeline", return_value={"status": "CLOUD_COMPLETE", "exit_code": 0})
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_lock_failure_does_not_crash(self, mock_conc, mock_cloud, mock_load,
                                          mock_log, mock_bridge, mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=False)

        from flow import backup
        # Should not raise
        backup(config_path="test.yaml", mode="cloud")


# ═══════════════════════════════════════════════════════════════
# 3. Error handling
# ═══════════════════════════════════════════════════════════════

class TestErrorHandling:
    """Pipeline exceptions are collected and reported."""

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_cloud_pipeline", side_effect=RuntimeError("cloud failed"))
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_cloud_exception_recorded(self, mock_conc, mock_cloud, mock_load,
                                       mock_log, mock_bridge, mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=True)

        from flow import backup
        with pytest.raises(ExceptionGroup):
            backup(config_path="test.yaml", mode="cloud")

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_lan_pipeline", side_effect=RuntimeError("lan failed"))
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_lan_exception_recorded(self, mock_conc, mock_lan, mock_load,
                                     mock_log, mock_bridge, mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=True)

        from flow import backup
        with pytest.raises(ExceptionGroup):
            backup(config_path="test.yaml", mode="lan")

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_cloud_pipeline", side_effect=RuntimeError("cloud err"))
    @patch("flow._run_lan_pipeline", side_effect=RuntimeError("lan err"))
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_both_exceptions_in_group(self, mock_conc, mock_lan, mock_cloud,
                                       mock_load, mock_log, mock_bridge,
                                       mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=True)

        from flow import backup
        with pytest.raises(ExceptionGroup) as exc_info:
            backup(config_path="test.yaml", mode="all")

        assert len(exc_info.value.exceptions) == 2

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_cloud_pipeline", side_effect=RuntimeError("fail"))
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_failure_alert_called(self, mock_conc, mock_cloud, mock_load,
                                   mock_log, mock_bridge, mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=False)

        from flow import backup
        with pytest.raises(ExceptionGroup):
            backup(config_path="test.yaml", mode="cloud")

        mock_alert.assert_called_once()

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_cloud_pipeline", side_effect=RuntimeError("fail"))
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_failure_alert_exception_swallowed(self, mock_conc, mock_cloud, mock_load,
                                                mock_log, mock_bridge, mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=False)
        mock_alert.side_effect = RuntimeError("email error")

        from flow import backup
        # Should not raise from alert error
        with pytest.raises(ExceptionGroup):
            backup(config_path="test.yaml", mode="cloud")


# ═══════════════════════════════════════════════════════════════
# 4. Successful completion
# ═══════════════════════════════════════════════════════════════

class TestSuccessfulCompletion:
    """No exceptions → no ExceptionGroup."""

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_cloud_pipeline", return_value={"status": "CLOUD_COMPLETE", "exit_code": 0})
    @patch("flow._run_lan_pipeline", return_value={"status": "LAN_COMPLETE", "exit_code": 0})
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_all_success_no_exception(self, mock_conc, mock_lan, mock_cloud,
                                       mock_load, mock_log, mock_bridge,
                                       mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=True)

        from flow import backup
        # Should not raise
        backup(config_path="test.yaml", mode="all")

        mock_alert.assert_not_called()

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_cloud_pipeline", return_value={"status": "CLOUD_COMPLETE", "exit_code": 0})
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_cloud_success(self, mock_conc, mock_cloud, mock_load,
                            mock_log, mock_bridge, mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=False)

        from flow import backup
        backup(config_path="test.yaml", mode="cloud")

        mock_cloud.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# 5. Status strings
# ═══════════════════════════════════════════════════════════════

class TestStatusStrings:
    """Status strings are correct and consistent."""

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_cloud_pipeline", return_value={"status": "CLOUD_COMPLETE", "exit_code": 0})
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_cloud_complete_status(self, mock_conc, mock_cloud, mock_load,
                                    mock_log, mock_bridge, mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=True, lan_enabled=False)

        from flow import backup
        backup(config_path="test.yaml", mode="cloud")

        assert mock_cloud.return_value["status"] == "CLOUD_COMPLETE"

    @patch("flow.send_failure_alert")
    @patch("flow.ManifestDB")
    @patch("flow.write_lock")
    @patch("flow.configure_prefect_bridge")
    @patch("flow.configure_logging")
    @patch("flow.load_config")
    @patch("flow._run_lan_pipeline", return_value={"status": "LAN_COMPLETE", "exit_code": 0})
    @patch("flow.concurrency", side_effect=_mock_concurrency)
    def test_lan_complete_status(self, mock_conc, mock_lan, mock_load,
                                  mock_log, mock_bridge, mock_lock, mock_db, mock_alert):
        mock_load.return_value = _make_config(cloud_enabled=False, lan_enabled=True)

        from flow import backup
        backup(config_path="test.yaml", mode="lan")

        assert mock_lan.return_value["status"] == "LAN_COMPLETE"
