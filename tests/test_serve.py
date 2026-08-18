"""Tests for serve.py — deployment creation."""

from unittest.mock import MagicMock, patch

from serve import _deployments


def _mock_config():
    mock_config = MagicMock()
    mock_config.schedule.timezone = "Asia/Kolkata"
    mock_config.schedule.cloud_cron = "0 18 * * *"
    mock_config.schedule.lan_cron = "0 1 * * *"
    mock_config.schedule.weekly_cron = "0 8 * * MON"
    mock_config.schedule.monthly_cron = "0 8 1 * *"
    mock_config.schedule.rollover_cron = "0 6 * * *"
    return mock_config


class TestDeployments:
    @patch("serve.load_config")
    def test_returns_five_deployments(self, mock_cfg):
        mock_cfg.return_value = _mock_config()

        with patch("serve.backup") as mock_backup, \
             patch("serve.weekly_report_flow") as mock_weekly, \
             patch("serve.monthly_report_flow") as mock_monthly, \
             patch("serve.rollover_check_flow") as mock_rollover:

            for m in (mock_backup, mock_weekly, mock_monthly, mock_rollover):
                m.to_deployment.return_value = MagicMock()

            result = _deployments()
            assert len(result) == 5

    @patch("serve.load_config")
    def test_cloud_deployment_has_correct_params(self, mock_cfg):
        mock_cfg.return_value = _mock_config()

        with patch("serve.backup") as mock_backup, \
             patch("serve.weekly_report_flow") as mock_weekly, \
             patch("serve.monthly_report_flow") as mock_monthly, \
             patch("serve.rollover_check_flow") as mock_rollover:

            for m in (mock_backup, mock_weekly, mock_monthly, mock_rollover):
                m.to_deployment.return_value = MagicMock()

            _deployments()

            # Check cloud deployment was created with mode=cloud
            cloud_call = mock_backup.to_deployment.call_args_list[0]
            assert cloud_call[1]["parameters"]["mode"] == "cloud"
            assert cloud_call[1]["name"] == "backup-cloud"

    @patch("serve.load_config")
    def test_lan_deployment_has_correct_params(self, mock_cfg):
        mock_cfg.return_value = _mock_config()

        with patch("serve.backup") as mock_backup, \
             patch("serve.weekly_report_flow") as mock_weekly, \
             patch("serve.monthly_report_flow") as mock_monthly, \
             patch("serve.rollover_check_flow") as mock_rollover:

            for m in (mock_backup, mock_weekly, mock_monthly, mock_rollover):
                m.to_deployment.return_value = MagicMock()

            _deployments()

            # Check LAN deployment was created with mode=lan
            lan_call = mock_backup.to_deployment.call_args_list[1]
            assert lan_call[1]["parameters"]["mode"] == "lan"
            assert lan_call[1]["name"] == "backup-lan"

    @patch("serve.load_config")
    def test_rollover_deployment_uses_rollover_cron(self, mock_cfg):
        """G10: the 5th deployment is the scheduled FY-rollover check."""
        mock_cfg.return_value = _mock_config()

        with patch("serve.backup") as mock_backup, \
             patch("serve.weekly_report_flow") as mock_weekly, \
             patch("serve.monthly_report_flow") as mock_monthly, \
             patch("serve.rollover_check_flow") as mock_rollover:

            for m in (mock_backup, mock_weekly, mock_monthly, mock_rollover):
                m.to_deployment.return_value = MagicMock()

            _deployments()

            mock_rollover.to_deployment.assert_called_once()
            kwargs = mock_rollover.to_deployment.call_args.kwargs
            assert kwargs["name"] == "rollover-check"
            assert list(kwargs["parameters"]) == ["config_path"]
            schedules = kwargs["schedules"]
            assert len(schedules) == 1
            assert schedules[0].cron == "0 6 * * *"
            assert schedules[0].timezone == "Asia/Kolkata"
