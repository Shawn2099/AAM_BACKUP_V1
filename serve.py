"""Prefect 3 deployment entry point for AAM Backup Automation V1.

Run from project root:  python serve.py

Registers four deployments with schedules read from config.yaml.
Edit the `schedule:` section in config.yaml to change cron expressions and timezone.
"""

from prefect import serve
from prefect.schedules import Cron

from flow import backup, monthly_report_flow, weekly_report_flow
from models.config import CONFIG_PATH, load_config


def deployments():
    """Public entry point — returns (cloud, lan, weekly, monthly) deployments."""
    return _deployments()


def _deployments():
    """Create deployments from config. Deferred so import doesn't trigger config load."""
    config = load_config(CONFIG_PATH)
    tz = config.schedule.timezone

    cloud_deployment = backup.to_deployment(
        name="backup-cloud",
        parameters={"config_path": CONFIG_PATH, "mode": "cloud"},
        schedules=[Cron(config.schedule.cloud_cron, tz)],
        tags=["production", "cloud"],
        description="Daily cloud backup — rclone sync to GCS (asia-south1)",
    )

    lan_deployment = backup.to_deployment(
        name="backup-lan",
        parameters={"config_path": CONFIG_PATH, "mode": "lan"},
        schedules=[Cron(config.schedule.lan_cron, tz)],
        tags=["production", "lan"],
        description="Daily LAN backup — robocopy /MIR, includes WoL and auto-shutdown",
    )

    report_deployment = weekly_report_flow.to_deployment(
        name="weekly-report",
        parameters={"config_path": CONFIG_PATH},
        schedules=[Cron(config.schedule.weekly_cron, tz)],
        tags=["reporting"],
        description="Weekly backup summary email",
    )

    monthly_deployment = monthly_report_flow.to_deployment(
        name="monthly-report",
        parameters={"config_path": CONFIG_PATH},
        schedules=[Cron(config.schedule.monthly_cron, tz)],
        tags=["reporting"],
        description="Monthly backup summary email (1st of month)",
    )

    return cloud_deployment, lan_deployment, report_deployment, monthly_deployment


if __name__ == "__main__":
    d = _deployments()
    serve(*d, pause_on_shutdown=False)
