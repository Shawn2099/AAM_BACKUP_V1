"""Prefect 3 deployment entry point for AAM Backup Automation V1.

Run from project root:  python serve.py

Registers four deployments with schedules read from config.yaml.
Edit the `schedule:` section in config.yaml to change cron expressions and timezone.
"""

from prefect import serve
from prefect.schedules import Cron

from flow import backup, monthly_report_flow, weekly_report_flow
from models.config import load_config

config = load_config("config.yaml")
tz = config.schedule.timezone

cloud_deployment = backup.to_deployment(
    name="backup-cloud",
    parameters={"config_path": "config.yaml", "mode": "cloud"},
    schedules=[Cron(config.schedule.cloud_cron, tz)],
    tags=["production", "cloud"],
    description="Daily cloud backup — rclone sync to GCS (asia-south1)",
)

lan_deployment = backup.to_deployment(
    name="backup-lan",
    parameters={"config_path": "config.yaml", "mode": "lan"},
    schedules=[Cron(config.schedule.lan_cron, tz)],
    tags=["production", "lan"],
    description="Daily LAN backup — robocopy /MIR, includes WoL and auto-shutdown",
)

report_deployment = weekly_report_flow.to_deployment(
    name="weekly-report",
    parameters={"config_path": "config.yaml"},
    schedules=[Cron(config.schedule.weekly_cron, tz)],
    tags=["reporting"],
    description="Weekly backup summary email",
)

monthly_deployment = monthly_report_flow.to_deployment(
    name="monthly-report",
    parameters={"config_path": "config.yaml"},
    schedules=[Cron(config.schedule.monthly_cron, tz)],
    tags=["reporting"],
    description="Monthly backup summary email (1st of month)",
)

if __name__ == "__main__":
    serve(
        cloud_deployment,
        lan_deployment,
        report_deployment,
        monthly_deployment,
        pause_on_shutdown=False,
    )
