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
from datetime import datetime, timezone

from loguru import logger
from prefect import flow, task

from core.cloud_preflight import run_cloud_dry_run
from core.cloud_reporter import get_cloud_diff, get_cloud_manifest, get_cloud_size
from core.cloud_sync import run_cloud_sync
from core.cloud_verify import verify_cloud_integrity
from core.fy_router import get_fy_prefix
from core.health import pre_backup_health, check_clock_skew, check_gcs_key, HealthError
from core.lan_manifest import diff_snapshots, snapshot_to_dict, walk_lan_destination
from core.lan_preflight import run_lan_dry_run
from core.lan_sync import run_lan_sync
from core.logging import configure as configure_logging
from core.manifest import ManifestDB
from core.report import send_failure_alert
from core.shutdown import shutdown_server
from core.wol import ensure_server_online
from models.config import load_config


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════
# Cloud backup task
# ═══════════════════════════════════════════════════════════════

@task(name="cloud-backup", retries=2, retry_delay_seconds=300)
def cloud_backup_task(config):
    """Cloud backup pipeline: preflight → sync → verify → report → DB."""
    run_id = str(uuid.uuid4())
    started_at = _utcnow()
    db = ManifestDB(config.paths.database_path)
    error_msg = None
    status = "CLOUD_SKIPPED"
    sync_result = {"exit_code": -1}

    try:
        fy_prefix = get_fy_prefix()

        # ── Clock skew check (JWT auth requires <10 min skew) ──
        ok, reason = check_clock_skew()
        if not ok:
            raise HealthError(reason)

        # ── GCS key check ──
        ok, reason = check_gcs_key(config.paths.gcs_key_path)
        if not ok:
            raise HealthError(reason)

        # ── Preflight ──
        dry_run = run_cloud_dry_run(
            source=config.paths.source_drive,
            bucket=config.cloud.bucket,
            fy_prefix=fy_prefix,
            gcs_key_path=config.paths.gcs_key_path,
            location=config.cloud.location,
        )
        if not dry_run["ok"]:
            raise RuntimeError(f"Cloud preflight failed: {dry_run['error']}")

        # ── Sync ──
        sync_result = run_cloud_sync(
            source=config.paths.source_drive,
            bucket=config.cloud.bucket,
            fy_prefix=fy_prefix,
            gcs_key_path=config.paths.gcs_key_path,
            location=config.cloud.location,
            project_number=config.cloud.project_number,
            bwlimit=config.cloud.bandwidth_limit,
            retries=config.cloud.retry_count,
            timeout=config.cloud.subprocess_timeout_seconds,
        )
        status = sync_result["status"]

        if status == "CLOUD_FAILED":
            error_msg = sync_result.get("error", "Cloud sync failed")
            raise RuntimeError(error_msg)

        # ── Verify ──
        from core.cloud_preflight import _write_temp_config
        verify_config = _write_temp_config(
            config.paths.gcs_key_path,
            config.cloud.location,
            config.cloud.project_number,
        )
        try:
            verify_result = verify_cloud_integrity(
                source=config.paths.source_drive,
                bucket=config.cloud.bucket,
                fy_prefix=fy_prefix,
                config_path=verify_config,
            )
        finally:
            from pathlib import Path
            try:
                Path(verify_config).unlink()
            except OSError:
                pass

        # ── Report ──
        from core.cloud_preflight import _write_temp_config as _wtc
        report_config = _wtc(
            config.paths.gcs_key_path,
            config.cloud.location,
            config.cloud.project_number,
        )
        try:
            size = get_cloud_size(config.cloud.bucket, fy_prefix, report_config)
            manifest = get_cloud_manifest(config.cloud.bucket, fy_prefix, report_config)
            cloud_diff = get_cloud_diff(
                config.paths.source_drive,
                config.cloud.bucket,
                fy_prefix,
                report_config,
            )
        finally:
            try:
                Path(report_config).unlink()
            except OSError:
                pass

        # ── Update DB ──
        if manifest:
            paths = [f["Path"] for f in manifest]
            for f in manifest:
                db.upsert_file_entry(
                    relative_path=f["Path"],
                    file_size=f.get("Size", 0),
                    mtime=f.get("ModTime", 0),
                    cloud_status="synced",
                )
            db.mark_cloud_synced(paths)

        if cloud_diff.get("removed"):
            db.delete_entries(cloud_diff["removed"])

        logger.info(
            f"Cloud run complete: {size['count']} files, "
            f"{size['bytes']} bytes, verified={verify_result['verified']}"
        )

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Cloud backup failed: {error_msg}")
        raise

    finally:
        ended_at = _utcnow()
        duration = time.time() - datetime.fromisoformat(started_at).timestamp()

        db.insert_run({
            "run_id": run_id,
            "mode": "cloud",
            "started_at": started_at,
            "ended_at": ended_at,
            "status": status if status != "CLOUD_SKIPPED" else "CLOUD_FAILED",
            "exit_code": sync_result.get("exit_code", -1),
            "duration_seconds": duration,
            "error_message": error_msg,
        })

        if error_msg:
            send_failure_alert(
                config.notifications,
                config.firm_name,
                error_msg,
                {"mode": "cloud", "run_id": run_id, "status": status},
            )

        db.wal_checkpoint()
        db.close()


# ═══════════════════════════════════════════════════════════════
# LAN backup task
# ═══════════════════════════════════════════════════════════════

@task(name="lan-backup", retries=1, retry_delay_seconds=600)
def lan_backup_task(config):
    """LAN backup pipeline: WoL → preflight → sync → manifest → shutdown."""
    run_id = str(uuid.uuid4())
    started_at = _utcnow()
    db = ManifestDB(config.paths.database_path)
    error_msg = None
    status = "LAN_SKIPPED"
    sync_result = {"exit_code": -1}

    try:
        # ── WoL ──
        if config.wol.enabled:
            ensure_server_online(config)

        # ── Preflight ──
        dry_run = run_lan_dry_run(
            source=config.paths.source_drive,
            dest=config.paths.lan_destination,
        )
        if not dry_run["ok"]:
            raise RuntimeError(f"LAN preflight failed: {dry_run['error']}")

        # ── Before snapshot (optional, for diff) ──
        lan_before = snapshot_to_dict(
            walk_lan_destination(config.paths.lan_destination)
        )

        # ── Sync ──
        sync_result = run_lan_sync(
            source=config.paths.source_drive,
            dest=config.paths.lan_destination,
            lan_config=config.lan,
        )
        status = sync_result["status"]

        if status == "LAN_FAILED":
            error_msg = sync_result.get("error", "LAN sync failed")
            raise RuntimeError(error_msg)

        # ── Manifest + diff ──
        lan_after_files = walk_lan_destination(config.paths.lan_destination)
        after_dict = snapshot_to_dict(lan_after_files)

        # Feed DB
        paths = [f["path"] for f in lan_after_files]
        for f in lan_after_files:
            db.upsert_file_entry(
                relative_path=f["path"],
                file_size=f["size"],
                mtime=f["mtime"],
                lan_status="synced",
            )
        db.mark_lan_synced(paths)

        # Diff + purge deletions
        diff = diff_snapshots(lan_before, after_dict)
        if diff["removed"]:
            db.delete_entries(diff["removed"])

        logger.info(
            f"LAN run complete: {len(lan_after_files)} files on destination, "
            f"+{len(diff['added'])} -{len(diff['removed'])} "
            f"*{len(diff['modified'])} changed"
        )

    except Exception as e:
        error_msg = str(e)
        logger.error(f"LAN backup failed: {error_msg}")
        raise

    finally:
        ended_at = _utcnow()
        duration = time.time() - datetime.fromisoformat(started_at).timestamp()

        db.insert_run({
            "run_id": run_id,
            "mode": "lan",
            "started_at": started_at,
            "ended_at": ended_at,
            "status": status if status != "LAN_SKIPPED" else "LAN_FAILED",
            "exit_code": sync_result.get("exit_code", -1),
            "duration_seconds": duration,
            "error_message": error_msg,
        })

        if error_msg:
            send_failure_alert(
                config.notifications,
                config.firm_name,
                error_msg,
                {"mode": "lan", "run_id": run_id, "status": status},
            )

        db.wal_checkpoint()
        db.close()

        # ── Shutdown (always, regardless of success/failure) ──
        if config.lan.shutdown_after_backup and config.wol.enabled:
            try:
                shutdown_server(config.wol.server_ip)
            except Exception as e:
                logger.warning(f"Server shutdown failed (non-critical): {e}")


# ═══════════════════════════════════════════════════════════════
# Weekly report flow
# ═══════════════════════════════════════════════════════════════

@flow(name="weekly-report", log_prints=True)
def weekly_report_flow(config_path: str = "config.yaml"):
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
def monthly_report_flow(config_path: str = "config.yaml"):
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
def backup(config_path: str = "config.yaml", mode: str = "all"):
    """AAM Backup Automation — nightly backup orchestrator.

    Modes:
        cloud — Run only cloud backup (rclone sync → GCS)
        lan   — Run only LAN backup (robocopy /MIR, includes WoL + shutdown)
        all   — Run both sequentially (cloud first, then LAN)
    """
    config = load_config(config_path)
    configure_logging(config.paths.log_directory)

    logger.info(f"AAM Backup starting — mode={mode}, firm={config.firm_name}")

    # Health check
    pre_backup_health(config.paths.source_drive, mode)

    errors = []

    # ── Cloud ──
    if mode in ("cloud", "all") and config.cloud.enabled:
        logger.info("Starting cloud backup")
        try:
            cloud_backup_task(config)
        except Exception as e:
            errors.append(f"Cloud: {e}")

    # ── LAN ──
    if mode in ("lan", "all") and config.lan.enabled:
        logger.info("Starting LAN backup")
        try:
            lan_backup_task(config)
        except Exception as e:
            errors.append(f"LAN: {e}")

    # ── Summary ──
    if errors:
        logger.error(f"Backup completed with errors: {'; '.join(errors)}")
        raise RuntimeError(f"Backup completed with errors: {'; '.join(errors)}")

    # ── Maintenance ──
    try:
        db = ManifestDB(config.paths.database_path)
        db.purge_old_runs(retention_days=90)
        db.close()
    except Exception as e:
        logger.warning(f"DB maintenance failed (non-critical): {e}")

    logger.info("AAM Backup completed successfully")