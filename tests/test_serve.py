"""Tests for serve.py — deployment creation."""

from unittest.mock import patch, MagicMock

from serve import _deployments


class TestDeployments:
    @patch("serve.load_config")
    def test_returns_four_deployments(self, mock_cfg):
        mock_config = MagicMock()
        mock_config.schedule.timezone = "Asia/Kolkata"
        mock_config.schedule.cloud_cron = "0 18 * * *"
        mock_config.schedule.lan_cron = "0 1 * * *"
        mock_config.schedule.weekly_cron = "0 8 * * MON"
        mock_config.schedule.monthly_cron = "0 8 1 * *"
        mock_cfg.return_value = mock_config
        
        with patch("serve.backup") as mock_backup, \
             patch("serve.weekly_report_flow") as mock_weekly, \
             patch("serve.monthly_report_flow") as mock_monthly:
            
            mock_backup.to_deployment.return_value = MagicMock()
            mock_weekly.to_deployment.return_value = MagicMock()
            mock_monthly.to_deployment.return_value = MagicMock()
            
            result = _deployments()
            assert len(result) == 4

    @patch("serve.load_config")
    def test_cloud_deployment_has_correct_params(self, mock_cfg):
        mock_config = MagicMock()
        mock_config.schedule.timezone = "Asia/Kolkata"
        mock_config.schedule.cloud_cron = "0 18 * * *"
        mock_config.schedule.lan_cron = "0 1 * * *"
        mock_config.schedule.weekly_cron = "0 8 * * MON"
        mock_config.schedule.monthly_cron = "0 8 1 * *"
        mock_cfg.return_value = mock_config
        
        with patch("serve.backup") as mock_backup, \
             patch("serve.weekly_report_flow") as mock_weekly, \
             patch("serve.monthly_report_flow") as mock_monthly:
            
            mock_backup.to_deployment.return_value = MagicMock()
            mock_weekly.to_deployment.return_value = MagicMock()
            mock_monthly.to_deployment.return_value = MagicMock()
            
            _deployments()
            
            # Check cloud deployment was created with mode=cloud
            cloud_call = mock_backup.to_deployment.call_args_list[0]
            assert cloud_call[1]["parameters"]["mode"] == "cloud"
            assert cloud_call[1]["name"] == "backup-cloud"

    @patch("serve.load_config")
    def test_lan_deployment_has_correct_params(self, mock_cfg):
        mock_config = MagicMock()
        mock_config.schedule.timezone = "Asia/Kolkata"
        mock_config.schedule.cloud_cron = "0 18 * * *"
        mock_config.schedule.lan_cron = "0 1 * * *"
        mock_config.schedule.weekly_cron = "0 8 * * MON"
        mock_config.schedule.monthly_cron = "0 8 1 * *"
        mock_cfg.return_value = mock_config
        
        with patch("serve.backup") as mock_backup, \
             patch("serve.weekly_report_flow") as mock_weekly, \
             patch("serve.monthly_report_flow") as mock_monthly:
            
            mock_backup.to_deployment.return_value = MagicMock()
            mock_weekly.to_deployment.return_value = MagicMock()
            mock_monthly.to_deployment.return_value = MagicMock()
            
            _deployments()
            
            # Check LAN deployment was created with mode=lan
            lan_call = mock_backup.to_deployment.call_args_list[1]
            assert lan_call[1]["parameters"]["mode"] == "lan"
            assert lan_call[1]["name"] == "backup-lan"
