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

from core.cloud_preflight import run_cloud_dry_run
from core.cloud_reporter import get_cloud_diff, get_cloud_manifest, get_cloud_size
from core.cloud_sync import run_cloud_sync
from core.cloud_verify import verify_cloud_integrity
from core.fy_router import get_fy_prefix
from core.health import HealthError, check_clock_skew, check_gcs_key, pre_backup_health
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
    return datetime.now(UTC).isoformat()


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive (cross-platform check)."""
    import os
    import sys
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return f"{pid}" in result.stdout
        except Exception:
            return False
    return False


def acquire_global_backup_lock(config, mode: str, timeout_seconds: int = 18000) -> bool:
    """Acquire a global file-lock. If held, poll and wait until it is released, enforcing sequential execution."""
    import os
    import json
    lock_dir = Path(config.paths.temp_directory)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / "global_backup.lock"
    
    start_time = time.time()
    pid = os.getpid()
    
    while True:
        if lock_file.exists():
            try:
                data = json.loads(lock_file.read_text())
                active_pid = data.get("pid")
                active_mode = data.get("mode")
                
                if active_pid == pid:
                    logger.debug(f"Current process already holds global lock for {active_mode}")
                    return True
                
                if _pid_alive(active_pid):
                    logger.info(f"Another backup ({active_mode}, PID={active_pid}) is currently running. Waiting sequentially...")
                else:
                    logger.warning(f"Found stale backup lock from PID {active_pid} ({active_mode}). Cleaning up.")
                    lock_file.unlink(missing_ok=True)
            except Exception:
                try:
                    lock_file.unlink(missing_ok=True)
                except OSError:
                    pass
        
        if not lock_file.exists():
            try:
                lock_file.write_text(json.dumps({
                    "pid": pid,
                    "mode": mode,
                    "started_at": datetime.now(UTC).isoformat()
                }))
                logger.info(f"Acquired global backup lock for {mode} (PID={pid})")
                return True
            except OSError:
                pass
            
        if time.time() - start_time > timeout_seconds:
            logger.error(f"Timed out waiting for global backup lock after {timeout_seconds}s")
            return False
            
        time.sleep(10)


def release_global_backup_lock(config, mode: str):
    """Release the global backup file-lock."""
    import os
    import json
    lock_file = Path(config.paths.temp_directory) / "global_backup.lock"
    if lock_file.exists():
        try:
            data = json.loads(lock_file.read_text())
            if data.get("pid") == os.getpid() and data.get("mode") == mode:
                lock_file.unlink(missing_ok=True)
                logger.info(f"Released global backup lock for {mode}")
        except Exception:
            try:
                lock_file.unlink(missing_ok=True)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════
# Cloud backup task
# ═══════════════════════════════════════════════════════════════

@task(name="cloud-backup")
def cloud_backup_task(config):
    """Cloud backup pipeline: preflight → sync → verify → report → DB.

    Retried up to 2 additional times internally (3 total attempts) with 300s
    delay between attempts. Only the final outcome is recorded in run_history.
    """
    run_id = str(uuid.uuid4())
    started_at = _utcnow()
    db = ManifestDB(config.paths.database_path)
    error_msg = None
    status = "CLOUD_SKIPPED"
    sync_result = {"exit_code": -1}
    max_attempts = config.cloud.max_attempts
    retry_delay = config.cloud.retry_delay_seconds

    if not acquire_global_backup_lock(config, "cloud"):
        error_msg = "Failed to acquire global backup lock (another backup run is currently active)"
        raise RuntimeError(error_msg)

    try:
        for attempt in range(max_attempts):
            try:
                from core.health import pre_backup_health
                pre_backup_health(config.paths.source_drive, "cloud")

                fy_prefix = get_fy_prefix()

                ok, reason = check_clock_skew()
                if not ok:
                    raise HealthError(reason)

                ok, reason = check_gcs_key(config.paths.gcs_key_path)
                if not ok:
                    raise HealthError(reason)

                dry_run = run_cloud_dry_run(
                    source=config.paths.source_drive,
                    bucket=config.cloud.bucket,
                    fy_prefix=fy_prefix,
                    gcs_key_path=config.paths.gcs_key_path,
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
                    error_msg = sync_result.get("error", "Cloud sync failed")
                    raise RuntimeError(error_msg)

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
                        timeout=config.cloud.verify_timeout_seconds,
                    )
                finally:
                    try:
                        Path(verify_config).unlink()
                    except OSError:
                        pass

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
                error_msg = None
                break

            except Exception as e:
                error_msg = str(e)
                if attempt < max_attempts - 1:
                    logger.warning(
                        f"Cloud backup attempt {attempt + 1}/{max_attempts} failed: {error_msg}. "
                        f"Retrying in {retry_delay}s..."
                    )
                    time.sleep(retry_delay)
                else:
                    logger.error(f"Cloud backup failed after {max_attempts} attempts: {error_msg}")
                    raise

    finally:
        try:
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
        except Exception as e:
            logger.error(f"Failed to record run history / send alert: {e}")
        finally:
            db.close()
            release_global_backup_lock(config, "cloud")


# ═══════════════════════════════════════════════════════════════
# LAN backup task
# ═══════════════════════════════════════════════════════════════

@task(name="lan-backup")
def lan_backup_task(config):
    """LAN backup pipeline: WoL → preflight → sync → manifest → shutdown.

    Retried up to 1 additional time internally (2 total attempts) with 600s
    delay. Only the final outcome is recorded in run_history.
    """
    run_id = str(uuid.uuid4())
    started_at = _utcnow()
    db = ManifestDB(config.paths.database_path)
    error_msg = None
    status = "LAN_SKIPPED"
    sync_result = {"exit_code": -1}
    max_attempts = config.lan.max_attempts
    retry_delay = config.lan.retry_delay_seconds

    if not acquire_global_backup_lock(config, "lan"):
        error_msg = "Failed to acquire global backup lock (another backup run is currently active)"
        raise RuntimeError(error_msg)

    try:
        for attempt in range(max_attempts):
            try:
                from core.health import pre_backup_health
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
                    error_msg = sync_result.get("error", "LAN sync failed")
                    raise RuntimeError(error_msg)

                lan_after_files = walk_lan_destination(config.paths.lan_destination)
                after_dict = snapshot_to_dict(lan_after_files)

                paths = [f["path"] for f in lan_after_files]
                for f in lan_after_files:
                    db.upsert_file_entry(
                        relative_path=f["path"],
                        file_size=f["size"],
                        mtime=f["mtime"],
                        lan_status="synced",
                    )
                db.mark_lan_synced(paths)

                diff = diff_snapshots(lan_before, after_dict)
                if diff["removed"]:
                    db.delete_entries(diff["removed"])

                logger.info(
                    f"LAN run complete: {len(lan_after_files)} files on destination, "
                    f"+{len(diff['added'])} -{len(diff['removed'])} "
                    f"*{len(diff['modified'])} changed"
                )
                error_msg = None
                break

            except Exception as e:
                error_msg = str(e)
                if attempt < max_attempts - 1:
                    logger.warning(
                        f"LAN backup attempt {attempt + 1}/{max_attempts} failed: {error_msg}. "
                        f"Retrying in {retry_delay}s..."
                    )
                    time.sleep(retry_delay)
                else:
                    logger.error(f"LAN backup failed after {max_attempts} attempts: {error_msg}")
                    raise

    finally:
        try:
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
        except Exception as e:
            logger.error(f"Failed to record run history / send alert: {e}")
        finally:
            db.close()
            release_global_backup_lock(config, "lan")

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
    valid_modes = {"cloud", "lan", "all"}
    mode = mode.lower()
    if mode not in valid_modes:
        raise ValueError(f"Invalid mode '{mode}'. Must be one of: {sorted(valid_modes)}")

    config = load_config(config_path)
    configure_logging(config.paths.log_directory)
    try:
        from core.logging import configure_prefect_bridge
        configure_prefect_bridge()
    except Exception:
        pass

    logger.info(f"AAM Backup starting — mode={mode}, firm={config.firm_name}")

    excs = []

    # ── Cloud ──
    if mode in ("cloud", "all") and config.cloud.enabled:
        logger.info("Starting cloud backup")
        try:
            cloud_backup_task(config)
        except Exception as e:
            excs.append(e)

    # ── LAN ──
    if mode in ("lan", "all") and config.lan.enabled:
        logger.info("Starting LAN backup")
        try:
            lan_backup_task(config)
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