"""Tests for flow.py orchestration — mode routing, error aggregation, config handling."""

from unittest.mock import patch, MagicMock
import pytest

from flow import backup, weekly_report_flow, monthly_report_flow


class TestBackupModeRouting:
    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.configure_prefect_bridge")
    def test_invalid_mode_raises(self, mock_bridge, mock_log, mock_cfg):
        with pytest.raises(ValueError, match="Invalid mode"):
            # Call the underlying function directly (not the Prefect-decorated version)
            backup.fn("config.yaml", "invalid")

    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.configure_prefect_bridge")
    def test_valid_modes_accepted(self, mock_bridge, mock_log, mock_cfg):
        mock_config = MagicMock()
        mock_config.cloud.enabled = False
        mock_config.lan.enabled = False
        mock_cfg.return_value = mock_config
        # Should not raise for valid modes
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
        backup.fn("config.yaml", "CLOUD")  # Should not raise


class TestBackupDisabledPipelines:
    @patch("flow.load_config")
    @patch("flow.configure_logging")
    @patch("flow.configure_prefect_bridge")
    def test_cloud_disabled_skips_cloud(self, mock_bridge, mock_log, mock_cfg):
        mock_config = MagicMock()
        mock_config.cloud.enabled = False
        mock_config.lan.enabled = False
        mock_cfg.return_value = mock_config
        # Should complete without error even with both disabled
        backup.fn("config.yaml", "all")


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
