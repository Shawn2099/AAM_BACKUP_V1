"""Prefect 3 deployment entry point for AAM Backup Automation V1.

Run from project root:  python serve.py

Registers four deployments:
  - backup-cloud:     Daily at 6:00 PM IST
  - backup-lan:       Daily at 1:00 AM IST
  - weekly-report:    Every Monday at 8:00 AM IST
  - monthly-report:   1st of every month at 8:00 AM IST
"""

from prefect import serve
from prefect.schedules import Cron

from flow import backup, weekly_report_flow, monthly_report_flow

cloud_deployment = backup.to_deployment(
    name="backup-cloud",
    parameters={"config_path": "config.yaml", "mode": "cloud"},
    schedules=[Cron("0 18 * * *", "Asia/Kolkata")],
    tags=["production", "cloud"],
    description="Daily cloud backup — rclone sync to GCS (asia-south1)",
)

lan_deployment = backup.to_deployment(
    name="backup-lan",
    parameters={"config_path": "config.yaml", "mode": "lan"},
    schedules=[Cron("0 1 * * *", "Asia/Kolkata")],
    tags=["production", "lan"],
    description="Daily LAN backup — robocopy /MIR, includes WoL and auto-shutdown",
)

report_deployment = weekly_report_flow.to_deployment(
    name="weekly-report",
    parameters={"config_path": "config.yaml"},
    schedules=[Cron("0 8 * * MON", "Asia/Kolkata")],
    tags=["reporting"],
    description="Weekly backup summary email",
)

monthly_deployment = monthly_report_flow.to_deployment(
    name="monthly-report",
    parameters={"config_path": "config.yaml"},
    schedules=[Cron("0 8 1 * *", "Asia/Kolkata")],
    tags=["reporting"],
    description="Monthly backup summary email (1st of month)",
)

if __name__ == "__main__":
    serve(cloud_deployment, lan_deployment, report_deployment, monthly_deployment)
