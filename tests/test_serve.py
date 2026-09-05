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
    mock_config.schedule.audit_cron = "0 3 * * SUN"
    return mock_config


def _patched_flows():
    return (
        patch("serve.backup"),
        patch("serve.weekly_report_flow"),
        patch("serve.monthly_report_flow"),
        patch("serve.rollover_check_flow"),
        patch("serve.integrity_audit_flow"),
    )


class TestDeployments:
    @patch("serve.load_config")
    def test_returns_six_deployments(self, mock_cfg):
        mock_cfg.return_value = _mock_config()

        patches = _patched_flows()
        with patches[0] as mock_backup, \
             patches[1] as mock_weekly, \
             patches[2] as mock_monthly, \
             patches[3] as mock_rollover, \
             patches[4] as mock_audit:

            for m in (mock_backup, mock_weekly, mock_monthly, mock_rollover, mock_audit):
                m.to_deployment.return_value = MagicMock()

            result = _deployments()
            assert len(result) == 6

    @patch("serve.load_config")
    def test_cloud_deployment_has_correct_params(self, mock_cfg):
        mock_cfg.return_value = _mock_config()

        patches = _patched_flows()
        with patches[0] as mock_backup, \
             patches[1] as mock_weekly, \
             patches[2] as mock_monthly, \
             patches[3] as mock_rollover, \
             patches[4] as mock_audit:

            for m in (mock_backup, mock_weekly, mock_monthly, mock_rollover, mock_audit):
                m.to_deployment.return_value = MagicMock()

            _deployments()

            # Check cloud deployment was created with mode=cloud
            cloud_call = mock_backup.to_deployment.call_args_list[0]
            assert cloud_call[1]["parameters"]["mode"] == "cloud"
            assert cloud_call[1]["name"] == "backup-cloud"

    @patch("serve.load_config")
    def test_lan_deployment_has_correct_params(self, mock_cfg):
        mock_cfg.return_value = _mock_config()

        patches = _patched_flows()
        with patches[0] as mock_backup, \
             patches[1] as mock_weekly, \
             patches[2] as mock_monthly, \
             patches[3] as mock_rollover, \
             patches[4] as mock_audit:

            for m in (mock_backup, mock_weekly, mock_monthly, mock_rollover, mock_audit):
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

        patches = _patched_flows()
        with patches[0] as mock_backup, \
             patches[1] as mock_weekly, \
             patches[2] as mock_monthly, \
             patches[3] as mock_rollover, \
             patches[4] as mock_audit:

            for m in (mock_backup, mock_weekly, mock_monthly, mock_rollover, mock_audit):
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

    @patch("serve.load_config")
    def test_audit_deployment_uses_audit_cron(self, mock_cfg):
        """Integrity audit: 6th deployment, weekly read-only schedule."""
        mock_cfg.return_value = _mock_config()

        patches = _patched_flows()
        with patches[0] as mock_backup, \
             patches[1] as mock_weekly, \
             patches[2] as mock_monthly, \
             patches[3] as mock_rollover, \
             patches[4] as mock_audit:

            for m in (mock_backup, mock_weekly, mock_monthly, mock_rollover, mock_audit):
                m.to_deployment.return_value = MagicMock()

            _deployments()

            mock_audit.to_deployment.assert_called_once()
            kwargs = mock_audit.to_deployment.call_args.kwargs
            assert kwargs["name"] == "integrity-audit"
            assert kwargs["parameters"]["mode"] == "all"
            schedules = kwargs["schedules"]
            assert len(schedules) == 1
            assert schedules[0].cron == "0 3 * * SUN"
            assert schedules[0].timezone == "Asia/Kolkata"
