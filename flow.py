"""AAM Backup Automation V1 — Prefect 3 flow orchestrator.

Two deployments from one codebase:
  - backup-cloud: daily 6 PM IST, rclone sync → GCS
  - backup-lan:   daily 1 AM IST, robocopy /MIR → LAN (WoL + shutdown)
  - backup-all:   manual, runs both sequentially (cloud first)

Each invocation is independent — opens ManifestDB, does its work, closes.
Reports pull from run_history across both.
"""

import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from prefect import flow, task
from prefect.concurrency.sync import concurrency
from prefect.logging import get_run_logger

from core.backup_repository import record_run_history, record_sync_results
from core.cloud_preflight import run_cloud_dry_run
from core.cloud_reporter import get_cloud_diff, get_cloud_manifest, get_cloud_size
from core.cloud_sync import run_cloud_sync
from core.cloud_verify import verify_cloud_integrity
from core.fy_router import get_fy_prefix
from core.health import pre_backup_health
from core.lan_manifest import diff_snapshots, snapshot_to_dict, walk_lan_destination
from core.lan_preflight import run_lan_dry_run
from core.lan_sync import run_lan_sync
from core.logging import configure as configure_logging
from core.logging import configure_prefect_bridge
from core.manifest import ManifestDB
from core.rclone_config import write_temp_config
from core.report import send_failure_alert
from core.shutdown import shutdown_server
from core.wol import ensure_server_online
from models.config import CONFIG_PATH, load_config


def _stable_run_id(mode: str) -> str:
    """Generate a run_id stable across Prefect task retries.

    Within a flow run, the flow_run.id is constant. Task retries create new
    task runs but reuse the same flow run. Combining flow_run_id + mode
    gives a key that's unique per backup mode per scheduled run, but stable
    across retries — so ON CONFLICT in insert_run deduplicates correctly.
    """
    try:
        from prefect.context import FlowRunContext
        ctx = FlowRunContext.get()
        if ctx:
            return f"{ctx.flow_run.id}-{mode}"
    except Exception:
        pass
    return f"{uuid.uuid4()}-{mode}"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _failure_alert_hook(config):
    """Return an on_failure hook that sends a failure alert email."""
    def _hook(task_or_flow, run, state):
        try:
            plogger = get_run_logger()
            plogger.error(f"{run.name} failed: {state.message}")
        except Exception:
            pass
        try:
            send_failure_alert(
                config.notifications,
                config.firm_name,
                str(state.message),
                {"run_id": str(run.id)},
            )
        except Exception:
            pass
    return _hook


# ═══════════════════════════════════════════════════════════════
# Cloud backup task
# ═══════════════════════════════════════════════════════════════

@task(name="cloud-backup")
def cloud_backup_task(config):
    """Cloud backup pipeline: preflight → sync → verify → report → DB.

    Uses Prefect-native retries via with_options() from config values.
    A global concurrency limit ensures only one backup runs at a time.
    """
    run_id = _stable_run_id("cloud")
    started_at = _utcnow()
    db = ManifestDB(config.paths.database_path)
    status = "CLOUD_SKIPPED"
    sync_result = {"exit_code": -1}
    error_msg = None

    with concurrency("aam-backup", occupy=1, timeout_seconds=3600):
        try:
            plogger = get_run_logger()
            plogger.info("Starting cloud backup pipeline")

            pre_backup_health(config.paths.source_drive, "cloud", config.paths.gcs_key_path)

            fy_prefix = get_fy_prefix()

            dry_run = run_cloud_dry_run(
                source=config.paths.source_drive,
                bucket=config.cloud.bucket,
                fy_prefix=fy_prefix,
                gcs_key_path=config.paths.gcs_key_path,
                project_number=config.cloud.project_number,
                storage_class=config.cloud.storage_class,
                location=config.cloud.location,
            )
            if not dry_run["ok"]:
                raise RuntimeError(f"Cloud preflight failed: {dry_run['error']}")

            sync_result = run_cloud_sync(
                source=config.paths.source_drive,
                bucket=config.cloud.bucket,
                fy_prefix=fy_prefix,
                gcs_key_path=config.paths.gcs_key_path,
                location=config.cloud.location,
                project_number=config.cloud.project_number,
                bwlimit=config.cloud.bandwidth_limit,
                retries=config.cloud.retry_count,
                storage_class=config.cloud.storage_class,
                transfers=config.cloud.transfers,
                checkers=config.cloud.checkers,
                timeout=config.cloud.subprocess_timeout_seconds,
            )
            status = sync_result["status"]

            if status == "CLOUD_FAILED":
                raise RuntimeError(sync_result.get("error", "Cloud sync failed"))

            rclone_cfg = write_temp_config(
                config.paths.gcs_key_path,
                config.cloud.location,
                config.cloud.project_number,
                config.cloud.storage_class,
            )
            try:
                verify_result = verify_cloud_integrity(
                    source=config.paths.source_drive,
                    bucket=config.cloud.bucket,
                    fy_prefix=fy_prefix,
                    config_path=rclone_cfg,
                    timeout=config.cloud.verify_timeout_seconds,
                )

                size = get_cloud_size(config.cloud.bucket, fy_prefix, rclone_cfg)
                manifest = get_cloud_manifest(config.cloud.bucket, fy_prefix, rclone_cfg)
                cloud_diff = get_cloud_diff(
                    config.paths.source_drive,
                    config.cloud.bucket,
                    fy_prefix,
                    rclone_cfg,
                )
            finally:
                try:
                    Path(rclone_cfg).unlink()
                except OSError:
                    pass

            record_sync_results(db, "cloud", manifest, cloud_diff.get("removed"))

            plogger.info(
                f"Cloud run complete: {size['count']} files, "
                f"{size['bytes']} bytes, verified={verify_result['verified']}"
            )

            return {"status": status, "exit_code": sync_result.get("exit_code", 0)}

        except Exception as e:
            error_msg = str(e)
            raise
        finally:
            try:
                ended_at = _utcnow()
                duration = time.time() - datetime.fromisoformat(started_at).timestamp()
                record_run_history(
                    db,
                    run_id=run_id, mode="cloud",
                    started_at=started_at, ended_at=ended_at,
                    status=status, exit_code=sync_result.get("exit_code", -1),
                    duration_seconds=duration, error_message=error_msg,
                )
            finally:
                db.close()


# ═══════════════════════════════════════════════════════════════
# LAN backup task
# ═══════════════════════════════════════════════════════════════

@task(name="lan-backup")
def lan_backup_task(config):
    """LAN backup pipeline: WoL → preflight → sync → manifest → shutdown.

    Uses Prefect-native retries via with_options() from config values.
    A global concurrency limit ensures only one backup runs at a time.
    """
    run_id = _stable_run_id("lan")
    started_at = _utcnow()
    db = ManifestDB(config.paths.database_path)
    status = "LAN_SKIPPED"
    sync_result = {"exit_code": -1}
    error_msg = None

    with concurrency("aam-backup", occupy=1, timeout_seconds=3600):
        try:
            plogger = get_run_logger()
            plogger.info("Starting LAN backup pipeline")

            pre_backup_health(config.paths.source_drive, "lan")

            if config.wol.enabled:
                ensure_server_online(config)

            dry_run = run_lan_dry_run(
                source=config.paths.source_drive,
                dest=config.paths.lan_destination,
            )
            if not dry_run["ok"]:
                raise RuntimeError(f"LAN preflight failed: {dry_run['error']}")

            lan_before = snapshot_to_dict(
                walk_lan_destination(config.paths.lan_destination)
            )

            sync_result = run_lan_sync(
                source=config.paths.source_drive,
                dest=config.paths.lan_destination,
                lan_config=config.lan,
            )
            status = sync_result["status"]

            if status == "LAN_FAILED":
                raise RuntimeError(sync_result.get("error", "LAN sync failed"))

            lan_after_files = walk_lan_destination(config.paths.lan_destination)
            after_dict = snapshot_to_dict(lan_after_files)

            diff = diff_snapshots(lan_before, after_dict)
            record_sync_results(db, "lan", lan_after_files, diff.get("removed"))

            plogger.info(
                f"LAN run complete: {len(lan_after_files)} files on destination, "
                f"+{len(diff['added'])} -{len(diff['removed'])} "
                f"*{len(diff['modified'])} changed"
            )

            return {"status": status, "exit_code": sync_result.get("exit_code", 0)}

        except Exception as e:
            error_msg = str(e)
            raise
        finally:
            try:
                ended_at = _utcnow()
                duration = time.time() - datetime.fromisoformat(started_at).timestamp()
                record_run_history(
                    db,
                    run_id=run_id, mode="lan",
                    started_at=started_at, ended_at=ended_at,
                    status=status, exit_code=sync_result.get("exit_code", -1),
                    duration_seconds=duration, error_message=error_msg,
                )
            finally:
                db.close()

            if error_msg is None and config.lan.shutdown_after_backup and config.wol.enabled:
                try:
                    shutdown_server(config.wol.server_ip)
                except Exception as e:
                    logger.warning(f"Server shutdown failed (non-critical): {e}")


# ═══════════════════════════════════════════════════════════════
# Weekly report flow
# ═══════════════════════════════════════════════════════════════

@flow(name="weekly-report", log_prints=True)
def weekly_report_flow(config_path: str = CONFIG_PATH):
    """Send weekly backup summary report."""
    config = load_config(config_path)
    configure_logging(config.paths.log_directory)
    db = ManifestDB(config.paths.database_path)
    try:
        from core.report import send_weekly_report
        send_weekly_report(db, config.notifications, config.firm_name)
    finally:
        db.close()


@flow(name="monthly-report", log_prints=True)
def monthly_report_flow(config_path: str = CONFIG_PATH):
    """Send monthly backup summary report."""
    config = load_config(config_path)
    configure_logging(config.paths.log_directory)
    db = ManifestDB(config.paths.database_path)
    try:
        from core.report import send_monthly_report
        send_monthly_report(db, config.notifications, config.firm_name)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# Main backup flow — entry point for all modes
# ═══════════════════════════════════════════════════════════════

@flow(name="aam-backup", log_prints=True)
def backup(config_path: str = CONFIG_PATH, mode: str = "all"):
    """AAM Backup Automation — nightly backup orchestrator.

    Modes:
        cloud — Run only cloud backup (rclone sync → GCS)
        lan   — Run only LAN backup (robocopy /MIR, includes WoL + shutdown)
        all   — Run both sequentially (cloud first, then LAN)
    """
    valid_modes = {"cloud", "lan", "all"}
    mode = mode.lower()
    if mode not in valid_modes:
        raise ValueError(f"Invalid mode '{mode}'. Must be one of: {sorted(valid_modes)}")

    config = load_config(config_path)
    configure_logging(config.paths.log_directory)
    try:
        configure_prefect_bridge()
    except Exception:
        pass

    logger.info(f"AAM Backup starting — mode={mode}, firm={config.firm_name}")

    # Apply config-driven retry settings via Prefect-native with_options
    cloud_task = cloud_backup_task.with_options(
        retries=config.cloud.max_attempts - 1,
        retry_delay_seconds=config.cloud.retry_delay_seconds,
        on_failure=[_failure_alert_hook(config)],
    )
    lan_task = lan_backup_task.with_options(
        retries=config.lan.max_attempts - 1,
        retry_delay_seconds=config.lan.retry_delay_seconds,
        on_failure=[_failure_alert_hook(config)],
    )

    excs = []

    # ── Cloud ──
    if mode in ("cloud", "all") and config.cloud.enabled:
        logger.info("Starting cloud backup")
        try:
            cloud_task(config)
        except Exception as e:
            excs.append(e)

    # ── LAN ──
    if mode in ("lan", "all") and config.lan.enabled:
        logger.info("Starting LAN backup")
        try:
            lan_task(config)
        except Exception as e:
            excs.append(e)

    # ── Summary ──
    if excs:
        logger.error(f"Backup completed with {len(excs)} error(s): {'; '.join(str(e) for e in excs)}")
        raise ExceptionGroup("Backup completed with errors", excs)

    # ── Maintenance ──
    try:
        db = ManifestDB(config.paths.database_path)
        db.purge_old_runs(retention_days=90)
        db.close()
    except Exception as e:
        logger.warning(f"DB maintenance failed (non-critical): {e}")

    logger.info("AAM Backup completed successfully")
