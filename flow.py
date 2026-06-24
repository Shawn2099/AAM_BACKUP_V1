"""AAM Backup Automation V1 — Prefect 3 flow orchestrator.

Each backup pipeline is decomposed into granular @task functions.
Each task is independently tracked by Prefect — state, timing, logs, retries.
This provides full visibility into which step is running, which failed, and how long each took.

Two deployments from one codebase:
  - backup-cloud: daily 6 PM IST, rclone sync → GCS
  - backup-lan:   daily 1 AM IST, robocopy /MIR → LAN (WoL + shutdown)
  - backup-all:   manual, runs both sequentially (cloud first)
"""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

import pendulum

from exceptiongroup import ExceptionGroup
from loguru import logger
from prefect import flow, task
from prefect.concurrency.sync import concurrency

from core.backup_repository import record_run_history, record_sync_results
from core.process import write_lock
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
from core.rclone_config import temp_rclone_config
from core.report import send_failure_alert
from core.shutdown import shutdown_server
from core.time_utils import now_iso
from core.wol import ensure_server_online
from models.config import CONFIG_PATH, load_config


def _stable_run_id(mode: str) -> str:
    """Generate a run_id stable across Prefect task retries."""
    try:
        from prefect.context import FlowRunContext
        ctx = FlowRunContext.get()
        if ctx:
            return f"{ctx.flow_run.id}-{mode}"
    except Exception:
        pass
    return f"{uuid.uuid4()}-{mode}"


# ═══════════════════════════════════════════════════════════════
# Shared tasks
# ═══════════════════════════════════════════════════════════════

@task(name="health-check")
def health_check_task(config, mode: str):
    """Run pre-backup health checks. Fail fast — won't fix itself."""
    logger.info(f"Running health checks (mode={mode})")
    pre_backup_health(
        config.paths.source_drive,
        mode,
        config.paths.gcs_key_path,
        min_free_source_gb=config.health.min_free_source_gb,
        max_clock_skew_seconds=config.health.max_clock_skew_seconds,
        clock_check_timeout_seconds=config.health.clock_check_timeout_seconds,
    )


# ═══════════════════════════════════════════════════════════════
# Cloud pipeline tasks
# ═══════════════════════════════════════════════════════════════

@task(name="cloud-preflight")
def cloud_preflight_task(config, fy_prefix: str):
    """Two-probe preflight: source drive alive + GCS auth/bucket probe.

    Probe A (Python): confirms source drive is mounted and readable.
    Probe B (rclone lsjson --max-depth 0): validates GCS credentials,
    bucket existence, and network reachability in ~1-3 seconds.

    No HDD scan. No file comparison. Fails fast before committing to sync.
    """
    logger.info(f"Cloud preflight: source={config.paths.source_drive}, dest={config.cloud.bucket}/{fy_prefix}")
    result = run_cloud_dry_run(
        source=config.paths.source_drive,
        bucket=config.cloud.bucket,
        fy_prefix=fy_prefix,
        gcs_key_path=config.paths.gcs_key_path,
        project_number=config.cloud.project_number,
        storage_class=config.cloud.storage_class,
        location=config.cloud.location,
        timeout=config.cloud.preflight_timeout_seconds,
    )
    if not result["ok"]:
        raise RuntimeError(f"Cloud preflight failed: {result['error']}")
    return result


@task(name="cloud-sync")
def cloud_sync_task(config, fy_prefix: str):
    """Run rclone sync to mirror source → GCS."""
    logger.info(f"Cloud sync: {config.paths.source_drive} → {config.cloud.bucket}/{fy_prefix}")
    result = run_cloud_sync(
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
        buffer_size=config.cloud.buffer_size,
        timeout=config.cloud.subprocess_timeout_seconds,
    )
    if result["status"] == "CLOUD_FAILED":
        raise RuntimeError(result.get("error", "Cloud sync failed"))
    return result


@task(name="cloud-verify-and-report")
def cloud_verify_and_report_task(config, fy_prefix: str):
    """Verify integrity + gather size/manifest/diff for reporting."""
    with temp_rclone_config(
        config.paths.gcs_key_path,
        config.cloud.location,
        config.cloud.project_number,
        config.cloud.storage_class,
    ) as rclone_cfg:
        logger.info("Verifying cloud integrity")
        verify_result = verify_cloud_integrity(
            source=config.paths.source_drive,
            bucket=config.cloud.bucket,
            fy_prefix=fy_prefix,
            config_path=rclone_cfg,
            timeout=config.cloud.verify_timeout_seconds,
        )

        logger.info("Gathering cloud report data")
        size = get_cloud_size(
            config.cloud.bucket, fy_prefix, rclone_cfg,
            timeout=config.cloud.cloud_size_timeout_seconds,
        )
        manifest = get_cloud_manifest(
            config.cloud.bucket, fy_prefix, rclone_cfg,
            timeout=config.cloud.manifest_timeout_seconds,
        )
        cloud_diff = get_cloud_diff(
            config.paths.source_drive,
            config.cloud.bucket,
            fy_prefix,
            rclone_cfg,
            timeout=config.cloud.diff_timeout_seconds,
        )

        logger.info(
            f"Cloud verify complete: {size['count']} files, "
            f"{size['bytes']} bytes, verified={verify_result['verified']}"
        )

        return {
            "verified": verify_result["verified"],
            "size": size,
            "manifest": manifest,
            "diff": cloud_diff,
        }


@task(name="cloud-record")
def cloud_record_task(
    db_path: str,
    verify_data: dict,
    sync_result: dict,
    busy_timeout_ms: int = 30000,
    vacuum_freelist_threshold: int = 10000,
):
    """Record cloud sync results to ManifestDB."""
    db = ManifestDB(
        db_path,
        busy_timeout_ms=busy_timeout_ms,
        vacuum_freelist_threshold=vacuum_freelist_threshold,
    )
    try:
        manifest = verify_data.get("manifest", [])
        removed = verify_data.get("diff", {}).get("removed", [])
        record_sync_results(db, "cloud", manifest, removed)
        logger.info(f"Recorded {len(manifest)} cloud entries to database")
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# LAN pipeline tasks
# ═══════════════════════════════════════════════════════════════

@task(name="wol-check")
def wol_check_task(config):
    """Wake backup server if WoL is enabled."""
    if not config.wol.enabled:
        logger.info("WoL disabled, skipping")
        return
    logger.info(f"Waking backup server {config.wol.server_ip}")
    ensure_server_online(config)


@task(name="lan-preflight")
def lan_preflight_task(config):
    """Run robocopy /L dry-run before real sync."""
    logger.info(f"LAN preflight: validating {config.paths.source_drive} → {config.paths.lan_destination}")
    result = run_lan_dry_run(
        source=config.paths.source_drive,
        dest=config.paths.lan_destination,
    )
    if not result["ok"]:
        raise RuntimeError(f"LAN preflight failed: {result['error']}")
    return result


@task(name="lan-snapshot-before")
def lan_snapshot_before_task(config):
    """Snapshot LAN destination before sync for diff comparison."""
    logger.info("Taking LAN snapshot (before sync)")
    before = snapshot_to_dict(walk_lan_destination(config.paths.lan_destination))
    logger.info(f"LAN snapshot: {len(before)} files before sync")
    return before


@task(name="lan-snapshot-after")
def lan_snapshot_after_task(config):
    """Snapshot LAN destination after sync for diff comparison."""
    logger.info("Taking LAN snapshot (after sync)")
    after_files = walk_lan_destination(config.paths.lan_destination)
    after = snapshot_to_dict(after_files)
    logger.info(f"LAN snapshot: {len(after)} files after sync")
    return after


@task(name="lan-sync")
def lan_sync_task(config):
    """Run robocopy /MIR mirror sync."""
    logger.info(f"LAN sync: {config.paths.source_drive} → {config.paths.lan_destination}")
    result = run_lan_sync(
        source=config.paths.source_drive,
        dest=config.paths.lan_destination,
        lan_config=config.lan,
    )
    if result["status"] == "LAN_FAILED":
        raise RuntimeError(result.get("error", "LAN sync failed"))
    return result


@task(name="lan-record")
def lan_record_task(
    db_path: str,
    sync_result: dict,
    before_dict: dict,
    after_dict: dict,
    busy_timeout_ms: int = 30000,
    vacuum_freelist_threshold: int = 10000,
):
    """Compute diff from before/after snapshots, record to ManifestDB."""
    diff = diff_snapshots(before_dict, after_dict)

    db = ManifestDB(
        db_path,
        busy_timeout_ms=busy_timeout_ms,
        vacuum_freelist_threshold=vacuum_freelist_threshold,
    )
    try:
        # snapshot_to_dict returns {path: (size, mtime)} tuples
        files_list = [{"path": k, "size": v[0], "mtime": v[1]}
                      for k, v in after_dict.items()]
        record_sync_results(db, "lan", files_list, diff.get("removed"))
        logger.info(
            f"LAN recorded: {len(after_dict)} files, "
            f"+{len(diff['added'])} -{len(diff['removed'])} "
            f"*{len(diff['modified'])} changed"
        )
    finally:
        db.close()


@task(name="lan-shutdown")
def lan_shutdown_task(config):
    """Shut down backup server after successful LAN sync."""
    if not config.lan.shutdown_after_backup or not config.wol.enabled:
        logger.info("LAN shutdown disabled, skipping")
        return
    logger.info(f"Shutting down backup server {config.wol.server_ip}")
    try:
        shutdown_server(config.wol.server_ip)
    except Exception as e:
        logger.warning(f"Server shutdown failed (non-critical): {e}")


@task(name="cloud-publish-artifact")
def cloud_publish_artifact_task(verify_data: dict, sync_result: dict, files_copied: int, bytes_copied: int):
    """Publish a beautiful Markdown summary of the Cloud Backup to the Prefect Console."""
    try:
        from prefect.artifacts import create_markdown_artifact
        status = sync_result.get("status", "UNKNOWN")
        exit_code = sync_result.get("exit_code", -1)
        size_mb = bytes_copied / (1024 * 1024)
        total_files = verify_data.get('size', {}).get('count', 0)
        total_space_gb = verify_data.get('size', {}).get('bytes', 0) / (1024 * 1024 * 1024)
        verified_str = "✅ Passed" if verify_data.get("verified") else "❌ Failed/Skipped"
        
        markdown_content = (
            f"# ☁️ AAM Cloud Backup Run Summary\n\n"
            f"## 📊 Performance & Execution Metrics\n"
            f"* **Status:** `{status}` (Exit Code: `{exit_code}`)\n"
            f"* **Files Transferred:** `{files_copied}` files\n"
            f"* **Volume Transferred:** `{size_mb:.2f} MB` (`{bytes_copied}` bytes)\n\n"
            f"## 📁 Storage Metrics (GCS Bucket)\n"
            f"* **Total Tracked Files:** `{total_files}` files\n"
            f"* **Total Space Consumed:** `{total_space_gb:.3f} GB`\n\n"
            f"## 🔒 Integrity Verification\n"
            f"* **Cryptographic Checks:** {verified_str}\n"
        )
        create_markdown_artifact(
            markdown=markdown_content,
            key="cloud-backup-summary"
        )
        logger.info("Published Cloud Backup Markdown Artifact to Prefect Console UI")
    except Exception as e:
        logger.warning(f"Could not publish cloud backup artifact: {e}", exc_info=True)


@task(name="lan-publish-artifact")
def lan_publish_artifact_task(sync_result: dict, diff: dict, files_copied: int, bytes_copied: int, total_files: int):
    """Publish a beautiful Markdown summary of the LAN Backup to the Prefect Console."""
    try:
        from prefect.artifacts import create_markdown_artifact
        status = sync_result.get("status", "UNKNOWN")
        exit_code = sync_result.get("exit_code", -1)
        
        added = len(diff.get("added", []))
        modified = len(diff.get("modified", []))
        removed = len(diff.get("removed", []))
        size_mb = bytes_copied / (1024 * 1024)
        
        markdown_content = (
            f"# 🖥️ AAM LAN Backup Run Summary\n\n"
            f"## 📊 Robocopy Differential Metrics\n"
            f"* **Status:** `{status}` (Exit Code: `{exit_code}`)\n"
            f"* **Total Differential Changes:** `{files_copied}` files\n"
            f"* **Volume Transferred:** `{size_mb:.2f} MB` (`{bytes_copied}` bytes)\n\n"
            f"## 📁 File Alterations Detail\n"
            f"* **➕ Files Added:** `{added}` files\n"
            f"* **✏️ Files Modified:** `{modified}` files\n"
            f"* **🗑️ Files Pruned (Mirror):** `{removed}` files\n\n"
            f"## 📦 Destination Volume Inventory\n"
            f"* **Active Files:** `{total_files}` files\n"
        )
        create_markdown_artifact(
            markdown=markdown_content,
            key="lan-backup-summary"
        )
        logger.info("Published LAN Backup Markdown Artifact to Prefect Console UI")
    except Exception as e:
        logger.warning(f"Could not publish lan backup artifact: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════
# Cloud pipeline orchestrator
# ═══════════════════════════════════════════════════════════════

def _run_cloud_pipeline(config, run_id: str, started_at: str):
    """Execute cloud backup tasks sequentially. Each task is independently tracked."""
    db_path = config.paths.database_path
    fy_prefix = get_fy_prefix()

    # Apply config-driven retries to tasks that benefit from retrying
    preflight = cloud_preflight_task.with_options(
        retries=1, retry_delay_seconds=30,
    )
    sync = cloud_sync_task.with_options(
        retries=config.cloud.max_attempts - 1,
        retry_delay_seconds=config.cloud.retry_delay_seconds,
    )
    verify_report = cloud_verify_and_report_task.with_options(
        retries=1, retry_delay_seconds=60,
    )

    # Fetch database state before sync to calculate differential transfers
    db = ManifestDB(
        db_path,
        busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
        vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
    )
    before_dict = {}
    try:
        before_dict = db.get_cloud_synced_entries()
    except Exception as e:
        logger.warning(f"Could not fetch database state before cloud sync: {e}")
    finally:
        db.close()

    status = "CLOUD_SKIPPED"
    sync_result = {"exit_code": -1}
    error_msg = None
    files_copied = 0
    bytes_copied = 0
    extended_metrics = None

    try:
        health_check_task(config, "cloud")
        preflight(config, fy_prefix)
        sync_result = sync(config, fy_prefix)
        status = sync_result["status"]
        verify_data = verify_report(config, fy_prefix)
        cloud_record_task(
            db_path, verify_data, sync_result,
            busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
            vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
        )

        # Calculate files and bytes copied by comparing old database state with new live GCS manifest
        manifest = verify_data.get("manifest", [])
        copied_files_list = []
        for item in manifest:
            path = item.get("Path") if item.get("Path") is not None else item.get("path", "")
            size = item.get("Size") if item.get("Size") is not None else item.get("size", 0)
            mtime = item.get("ModTime") if item.get("ModTime") is not None else item.get("mtime", 0)
            
            if path not in before_dict:
                copied_files_list.append((path, size))
            else:
                old_size, old_mtime = before_dict[path]
                # 0.01-byte threshold: guards against float representation noise
                # in rclone's size reporting. Accurate for all real file sizes
                # since actual byte counts are always whole numbers.
                if abs(float(size) - float(old_size)) > 0.01:
                    copied_files_list.append((path, size))
                else:
                    try:
                        if isinstance(mtime, (int, float)) and isinstance(old_mtime, (int, float)):
                            # Numeric Unix timestamps — compare directly
                            t1, t2 = float(mtime), float(old_mtime)
                        else:
                            t1 = pendulum.parse(str(mtime)).timestamp()
                            t2 = pendulum.parse(str(old_mtime)).timestamp()
                        if abs(t1 - t2) > 1.1:
                            copied_files_list.append((path, size))
                    except Exception:
                        if str(mtime) != str(old_mtime):
                            copied_files_list.append((path, size))

        files_copied = len(copied_files_list)
        bytes_copied = sum(round(float(size)) for _, size in copied_files_list)

        extended_metrics = json.dumps({
            "verified": verify_data.get("verified", False),
            "total_files": verify_data.get("size", {}).get("count", 0),
            "total_size_gb": verify_data.get("size", {}).get("bytes", 0) / (1024 * 1024 * 1024)
        })
        try:
            cloud_publish_artifact_task(verify_data, sync_result, files_copied, bytes_copied)
        except Exception:
            pass

        logger.info(f"Cloud pipeline completed successfully: {files_copied} files, {bytes_copied} bytes copied")
        return {"status": status, "exit_code": sync_result.get("exit_code", 0)}

    except Exception as e:
        error_msg = str(e)
        raise
    finally:
        _record_run(
            db_path, run_id, "cloud", started_at, status,
            sync_result.get("exit_code", -1), error_msg,
            files_copied, bytes_copied, extended_metrics,
            busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
            vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
        )


# ═══════════════════════════════════════════════════════════════
# LAN pipeline orchestrator
# ═══════════════════════════════════════════════════════════════

def _run_lan_pipeline(config, run_id: str, started_at: str):
    """Execute LAN backup tasks sequentially. Each task is independently tracked."""
    db_path = config.paths.database_path

    # Apply config-driven retries to tasks that benefit from retrying
    preflight = lan_preflight_task.with_options(
        retries=1, retry_delay_seconds=30,
    )
    sync = lan_sync_task.with_options(
        retries=config.lan.max_attempts - 1,
        retry_delay_seconds=config.lan.retry_delay_seconds,
    )

    status = "LAN_SKIPPED"
    sync_result = {"exit_code": -1}
    error_msg = None
    files_copied = 0
    bytes_copied = 0
    extended_metrics = None

    try:
        health_check_task(config, "lan")
        wol_check_task(config)
        preflight(config)
        before_dict = lan_snapshot_before_task(config)
        sync_result = sync(config)
        status = sync_result["status"]
        after_dict = lan_snapshot_after_task(config)
        lan_record_task(
            db_path, sync_result, before_dict, after_dict,
            busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
            vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
        )

        # Calculate files and bytes copied
        diff = diff_snapshots(before_dict, after_dict)
        copied_paths = diff.get("added", []) + diff.get("modified", [])
        files_copied = len(copied_paths)
        bytes_copied = sum(after_dict[path][0] for path in copied_paths if path in after_dict)

        extended_metrics = json.dumps({
            "added": len(diff.get("added", [])),
            "modified": len(diff.get("modified", [])),
            "removed": len(diff.get("removed", [])),
            "total_files": len(after_dict)
        })
        try:
            lan_publish_artifact_task(sync_result, diff, files_copied, bytes_copied, len(after_dict))
        except Exception:
            pass

        logger.info("LAN pipeline completed successfully")
        # Shut down the backup server after a successful sync.
        # NOTE: this is intentionally inside the try block, not in an else clause.
        # Python's try/else does NOT execute if try exits via return — so lan_shutdown
        # was previously dead code. Placing it here before the return is correct.
        lan_shutdown_task(config)
        return {"status": status, "exit_code": sync_result.get("exit_code", 0)}

    except Exception as e:
        error_msg = str(e)
        raise
    finally:
        _record_run(
            db_path, run_id, "lan", started_at, status,
            sync_result.get("exit_code", -1), error_msg,
            files_copied, bytes_copied, extended_metrics,
            busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
            vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
        )


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _record_run(
    db_path: str,
    run_id: str,
    mode: str,
    started_at: str,
    status: str,
    exit_code: int,
    error_msg: str | None,
    files_copied: int = 0,
    bytes_copied: int = 0,
    extended_metrics: str | None = None,
    busy_timeout_ms: int = 30000,
    vacuum_freelist_threshold: int = 10000,
):
    """Record run history to ManifestDB."""
    ended_at = now_iso()
    duration = time.time() - pendulum.parse(started_at).timestamp()
    db = ManifestDB(
        db_path,
        busy_timeout_ms=busy_timeout_ms,
        vacuum_freelist_threshold=vacuum_freelist_threshold,
    )
    try:
        if not record_run_history(
            db,
            run_id=run_id, mode=mode,
            started_at=started_at, ended_at=ended_at,
            status=status, exit_code=exit_code,
            duration_seconds=duration, error_message=error_msg,
            files_copied=files_copied, bytes_copied=bytes_copied,
            extended_metrics=extended_metrics,
        ):
            logger.critical(
                f"Run history persistence failed for run_id={run_id} mode={mode} "
                f"status={status} exit_code={exit_code}"
            )
            logger.warning(f"Run {run_id} ({mode}) was not recorded to database — check logs above for details")
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# Report flows
# ═══════════════════════════════════════════════════════════════

@flow(name="weekly-report", log_prints=True)
def weekly_report_flow(config_path: str = CONFIG_PATH):
    """Send weekly backup summary report."""
    config = load_config(config_path)
    configure_logging(config.paths.log_directory, log_retention_days=config.maintenance.log_retention_days)
    try:
        configure_prefect_bridge()
    except Exception:
        pass
    if not config.notifications.weekly_enabled:
        logger.info("Weekly backup report email is disabled in configuration — skipping")
        return
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
    configure_logging(config.paths.log_directory, log_retention_days=config.maintenance.log_retention_days)
    try:
        configure_prefect_bridge()
    except Exception:
        pass
    if not config.notifications.monthly_enabled:
        logger.info("Monthly backup report email is disabled in configuration — skipping")
        return
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

    Each pipeline step is a separate Prefect task — visible in the Prefect UI
    with individual state, timing, logs, and retries.

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
    configure_logging(config.paths.log_directory, log_retention_days=config.maintenance.log_retention_days)
    try:
        configure_prefect_bridge()
    except Exception as e:
        logger.debug(f"configure_prefect_bridge skipped: {e} — Prefect UI may not show loguru logs")

    logger.info(f"AAM Backup starting — mode={mode}, firm={config.firm_name}")

    # ── Watchdog lock — signals that a backup is in progress ──
    # watchdog.py and launch.py read this file and defer any service restart
    # until it disappears.  Format: "PID:create_time" — the process creation
    # timestamp makes PID-reuse detection mathematically exact (see core/process.py).
    _lock_path = config.paths.backup_lock_path
    try:
        write_lock(_lock_path)
        logger.info(f"Backup lock acquired (PID={os.getpid()}) — watchdog will defer restarts")
    except OSError as e:
        logger.warning(f"Could not write backup lock file: {e}")

    excs = []

    try:
        with concurrency("aam-backup", occupy=1, timeout_seconds=3600):
            # ── Cloud ──
            if mode in ("cloud", "all") and config.cloud.enabled:
                logger.info("Starting cloud backup pipeline")
                try:
                    _run_cloud_pipeline(config, _stable_run_id("cloud"), now_iso())
                except Exception as e:
                    excs.append(e)

            # ── LAN ──
            if mode in ("lan", "all") and config.lan.enabled:
                logger.info("Starting LAN backup pipeline")
                try:
                    _run_lan_pipeline(config, _stable_run_id("lan"), now_iso())
                except Exception as e:
                    excs.append(e)

        # ── Summary ──
        if excs:
            error_summary = '; '.join(str(e) for e in excs)
            logger.error(f"Backup completed with {len(excs)} error(s): {error_summary}")
            try:
                send_failure_alert(
                    config.notifications,
                    config.firm_name,
                    error_summary,
                    {"mode": mode},
                )
            except Exception:
                pass
            raise ExceptionGroup("Backup completed with errors", excs)

        # ── Maintenance ──
        try:
            db = ManifestDB(
                config.paths.database_path,
                busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
                vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
            )
            db.purge_old_runs(retention_days=config.maintenance.db_retention_days)
            db.close()
        except Exception as e:
            logger.warning(f"DB maintenance failed (non-critical): {e}")

        logger.info("AAM Backup completed successfully")

    finally:
        # Always release the watchdog lock — even on crash or ExceptionGroup raise
        try:
            _lock_path.unlink(missing_ok=True)
            logger.info("Backup lock released")
        except OSError:
            pass
