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
import time
import uuid
from pathlib import Path

import pendulum

from exceptiongroup import ExceptionGroup
from loguru import logger
from prefect import flow, task
from prefect.concurrency.sync import concurrency

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
from core.rclone_config import temp_rclone_config
from core.report import send_failure_alert
from core.shutdown import shutdown_server
from core.time_utils import utcnow_iso
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
    pre_backup_health(config.paths.source_drive, mode, config.paths.gcs_key_path)


# ═══════════════════════════════════════════════════════════════
# Cloud pipeline tasks
# ═══════════════════════════════════════════════════════════════

@task(name="cloud-preflight")
def cloud_preflight_task(config, fy_prefix: str):
    """Run rclone check --one-way dry-run before sync."""
    logger.info(f"Cloud preflight: checking {config.paths.source_drive} ↔ {config.cloud.bucket}/{fy_prefix}")
    result = run_cloud_dry_run(
        source=config.paths.source_drive,
        bucket=config.cloud.bucket,
        fy_prefix=fy_prefix,
        gcs_key_path=config.paths.gcs_key_path,
        project_number=config.cloud.project_number,
        storage_class=config.cloud.storage_class,
        location=config.cloud.location,
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
        size = get_cloud_size(config.cloud.bucket, fy_prefix, rclone_cfg)
        manifest = get_cloud_manifest(config.cloud.bucket, fy_prefix, rclone_cfg)
        cloud_diff = get_cloud_diff(
            config.paths.source_drive,
            config.cloud.bucket,
            fy_prefix,
            rclone_cfg,
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
def cloud_record_task(db_path: str, verify_data: dict, sync_result: dict):
    """Record cloud sync results to ManifestDB."""
    db = ManifestDB(db_path)
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
def lan_record_task(db_path: str, sync_result: dict, before_dict: dict, after_dict: dict):
    """Compute diff from before/after snapshots, record to ManifestDB."""
    diff = diff_snapshots(before_dict, after_dict)

    db = ManifestDB(db_path)
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
def lan_publish_artifact_task(sync_result: dict, before_dict: dict, after_dict: dict, files_copied: int, bytes_copied: int):
    """Publish a beautiful Markdown summary of the LAN Backup to the Prefect Console."""
    try:
        from prefect.artifacts import create_markdown_artifact
        status = sync_result.get("status", "UNKNOWN")
        exit_code = sync_result.get("exit_code", -1)
        
        diff = diff_snapshots(before_dict, after_dict)
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
            f"* **Active Files:** `{len(after_dict)}` files\n"
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
    db = ManifestDB(db_path)
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
        cloud_record_task(db_path, verify_data, sync_result)

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
        _record_run(db_path, run_id, "cloud", started_at, status,
                     sync_result.get("exit_code", -1), error_msg,
                     files_copied, bytes_copied, extended_metrics)


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
        lan_record_task(db_path, sync_result, before_dict, after_dict)

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
            lan_publish_artifact_task(sync_result, before_dict, after_dict, files_copied, bytes_copied)
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
        _record_run(db_path, run_id, "lan", started_at, status,
                     sync_result.get("exit_code", -1), error_msg,
                     files_copied, bytes_copied, extended_metrics)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _record_run(db_path: str, run_id: str, mode: str, started_at: str,
                status: str, exit_code: int, error_msg: str | None,
                files_copied: int = 0, bytes_copied: int = 0,
                extended_metrics: str | None = None):
    """Record run history to ManifestDB."""
    ended_at = utcnow_iso()
    duration = time.time() - pendulum.parse(started_at).timestamp()
    db = ManifestDB(db_path)
    try:
        record_run_history(
            db,
            run_id=run_id, mode=mode,
            started_at=started_at, ended_at=ended_at,
            status=status, exit_code=exit_code,
            duration_seconds=duration, error_message=error_msg,
            files_copied=files_copied, bytes_copied=bytes_copied,
            extended_metrics=extended_metrics,
        )
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# Report flows
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
    configure_logging(config.paths.log_directory)
    try:
        configure_prefect_bridge()
    except Exception as e:
        logger.debug(f"configure_prefect_bridge skipped: {e} — Prefect UI may not show loguru logs")

    logger.info(f"AAM Backup starting — mode={mode}, firm={config.firm_name}")

    # ── Watchdog lock — signals that a backup is in progress ──
    # watchdog.py reads this file and defers any service restart until it
    # disappears. The file contains the PID of this process so the watchdog
    # can detect stale locks from a previous crash.
    _lock_path = Path(config.paths.database_path).parent / "backup.lock"
    try:
        _lock_path.parent.mkdir(parents=True, exist_ok=True)
        _lock_path.write_text(str(os.getpid()))
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
                    _run_cloud_pipeline(config, _stable_run_id("cloud"), utcnow_iso())
                except Exception as e:
                    excs.append(e)

            # ── LAN ──
            if mode in ("lan", "all") and config.lan.enabled:
                logger.info("Starting LAN backup pipeline")
                try:
                    _run_lan_pipeline(config, _stable_run_id("lan"), utcnow_iso())
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
            db = ManifestDB(config.paths.database_path)
            db.purge_old_runs(retention_days=90)
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
