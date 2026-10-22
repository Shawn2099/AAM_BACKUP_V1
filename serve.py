"""Prefect 3 deployment entry point for AAM Backup Automation V1.

Run from project root:  python serve.py

Registers four deployments with schedules read from config.yaml.
Edit the `schedule:` section in config.yaml to change cron expressions and timezone.
"""

from prefect import serve
from prefect.schedules import Cron

from flow import backup, integrity_audit_flow, monthly_report_flow, rollover_check_flow, weekly_report_flow
from models.config import CONFIG_PATH, load_config


def deployments():
    """Public entry point — returns (cloud, lan, weekly, monthly, rollover) deployments."""
    return _deployments()


def _deployments():
    """Create deployments from config. Deferred so import doesn't trigger config load.

    P2-SCHED: only ENABLED legs are registered (lan.enabled / cloud.enabled).
    A leg disabled here must also be PAUSED server-side if a previous boot
    registered it - see launch._reconcile_disabled_legs(); otherwise the
    Prefect scheduler keeps firing the stale deployment forever (observed
    2026-08-23: backup-lan fired at 21:00 and completed doing nothing).
    """
    config = load_config(CONFIG_PATH)
    tz = config.schedule.timezone

    deployments_out = []

    if config.cloud.enabled:
        deployments_out.append(
            backup.to_deployment(
                name="backup-cloud",
                parameters={"config_path": CONFIG_PATH, "mode": "cloud"},
                schedules=[Cron(config.schedule.cloud_cron, tz)],
                tags=["production", "cloud"],
                description="Daily cloud backup - rclone sync to GCS (asia-south1)",
            )
        )

    if config.lan.enabled:
        deployments_out.append(
            backup.to_deployment(
                name="backup-lan",
                parameters={"config_path": CONFIG_PATH, "mode": "lan"},
                schedules=[Cron(config.schedule.lan_cron, tz)],
                tags=["production", "lan"],
                description="Daily LAN backup - robocopy /MIR, includes WoL and auto-shutdown",
            )
        )

    deployments_out.append(
        weekly_report_flow.to_deployment(
            name="weekly-report",
            parameters={"config_path": CONFIG_PATH},
            schedules=[Cron(config.schedule.weekly_cron, tz)],
            tags=["reporting"],
            description="Weekly backup summary email",
        )
    )

    deployments_out.append(
        monthly_report_flow.to_deployment(
            name="monthly-report",
            parameters={"config_path": CONFIG_PATH},
            schedules=[Cron(config.schedule.monthly_cron, tz)],
            tags=["reporting"],
            description="Monthly backup summary email (1st of month)",
        )
    )

    # G10 / WP11: scheduled FY-rollover check - the ONLY rollover entry point
    # (the boot-time check in launch.py was removed). Tagged "rollover" so the
    # dashboard can tell it is running before re-running an approved rollover.
    deployments_out.append(
        rollover_check_flow.to_deployment(
            name="rollover-check",
            parameters={"config_path": CONFIG_PATH},
            schedules=[Cron(config.schedule.rollover_cron, tz)],
            tags=["maintenance", "rollover"],
            description="Daily FY rollover check - idempotent; runs the real rollover on April 1",
        )
    )

    # Integrity audit: weekly READ-ONLY deep verification (source↔backup
    # content comparison). Report-only: records integrity_audits rows and
    # alerts on divergence; never modifies data or rewrites backup history.
    deployments_out.append(
        integrity_audit_flow.to_deployment(
            name="integrity-audit",
            parameters={"config_path": CONFIG_PATH, "mode": "all"},
            schedules=[Cron(config.schedule.audit_cron, tz)],
            tags=["maintenance", "integrity"],
            description="Weekly read-only integrity audit - content verification, single checker",
        )
    )

    _record_schedule_in_effect(config)
    return tuple(deployments_out)


def _record_schedule_in_effect(config) -> None:
    """Remember which schedules THIS process registers. The dashboard and the watchdog judge
    "overdue" against these: a schedule saved in Settings but not applied yet (it takes effect
    when the agent restarts) must not change what counts as late. Written just before serve()
    starts, from the same config object the deployments were built from."""
    try:
        from core.manifest import ManifestDB

        db = ManifestDB.from_config(config)
        try:
            db.set_schedule_in_effect({"cloud_cron": config.schedule.cloud_cron,
                                       "lan_cron": config.schedule.lan_cron,
                                       "timezone": config.schedule.timezone})
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - never stops the scheduler; the config is the fallback
        print(f"[serve] could not record the schedule in effect: {e}")


if __name__ == "__main__":
    d = _deployments()
    serve(*d, pause_on_shutdown=False)
