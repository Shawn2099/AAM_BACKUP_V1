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
import re
import time
import uuid
from datetime import datetime

import pendulum

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
from core.lan_manifest import (
    WalkIncompleteError,
    diff_snapshots,
    snapshot_to_dict,
    walk_lan_destination,
)
from core.lan_preflight import run_lan_dry_run
from core.lan_sync import cleanup_orphaned_robocopy_logs, run_lan_sync
from core.logging import configure as configure_logging
from core.logging import configure_prefect_bridge
from core.manifest import ManifestDB
from core.process import write_lock
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
        # F8: the dry-run /L walk scales with dataset size (1M files ~= 5-10 min
        # over SMB); the old hardcoded 300s failed at the scale this deployment
        # targets. Config-driven so the operator can tune per environment.
        timeout=config.lan.dry_run_timeout_seconds,
    )
    if not result["ok"]:
        raise RuntimeError(f"LAN preflight failed: {result['error']}")
    return result


@task(name="lan-snapshot-before")
def lan_snapshot_before_task(config):
    """Snapshot LAN destination before sync for diff comparison."""
    logger.info("Taking LAN snapshot (before sync)")
    try:
        before = snapshot_to_dict(walk_lan_destination(config.paths.lan_destination))
    except WalkIncompleteError as e:
        # Some destination directories were unreadable. An incomplete BEFORE
        # snapshot is safe to downgrade to empty: diff's "removed" is
        # before-set minus after-set, so nothing can be falsely reported as
        # removed (no false DB pruning); only "added" over-counts for this
        # run and self-heals next run. The sync itself still proceeds.
        logger.error(
            f"Pre-sync LAN snapshot incomplete ({e}) — proceeding with an "
            "empty before-snapshot; added/modified metrics for this run are "
            "under-counted for the unreadable subtree(s)."
        )
        before = {}
    logger.info(f"LAN snapshot: {len(before)} files before sync")
    return before


@task(name="lan-snapshot-after")
def lan_snapshot_after_task(config):
    """Snapshot LAN destination after sync for diff comparison.

    G14: returns None (instead of raising) when the walk fails. The sync
    itself already succeeded — a dropped SMB session or NAS hiccup while
    enumerating must not turn a completed backup into a failed run. The
    pipeline skips diff metrics + DB record for that run; the next run
    re-derives everything from a fresh walk.
    """
    logger.info("Taking LAN snapshot (after sync)")
    try:
        after_files = walk_lan_destination(config.paths.lan_destination)
        after = snapshot_to_dict(after_files)
    except Exception as e:
        logger.critical(
            f"Post-sync destination walk failed: {e} — sync result is "
            "UNAFFECTED; diff metrics and DB record are skipped for this run"
        )
        return None
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

# Plausible range for Unix timestamps this application handles (1970–2100).
_MIN_UNIX_TS = 0.0
_MAX_UNIX_TS = 4_102_444_800.0
# Numeric-string Unix timestamps: 9-11 integer digits (2001–2336) with an
# optional fractional part. Anchored so date-looking strings such as
# "20260818" (8 digits) are never misread as Unix seconds.
_NUMERIC_UNIX_RE = re.compile(r"^\d{9,11}(\.\d+)?$")


def _mtime_to_unix(value) -> float | None:
    """Normalize any mtime representation this application stores to Unix seconds.

    Accepted forms (all produced by this application):
      * int/float — Unix seconds (LAN records write os.stat mtime floats);
      * numeric string — Unix seconds as text, e.g. "1783677447.705671";
      * ISO-8601 / RFC-3339 string — e.g. "2026-08-18T19:58:48.071149100+05:30"
        or "2026-07-10T10:27:27.705671Z" (cloud records / rclone ModTime);
      * datetime / pendulum instances.
    Returns None when the value cannot be interpreted as a point in time.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts if _MIN_UNIX_TS < ts < _MAX_UNIX_TS else None
    if isinstance(value, datetime):  # pendulum is a subclass of datetime
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if _NUMERIC_UNIX_RE.match(s):
            return float(s)
        try:
            return pendulum.parse(s).timestamp()
        except Exception:
            return None
    return None


def compute_copied_files(
    manifest: list[dict],
    before_dict: dict[str, tuple],
    mtime_threshold_seconds: float = 1.1,
) -> list[tuple[str, float]]:
    """Return [(path, size)] of manifest entries representing an actual transfer.

    An entry counts as copied when it is new (absent from before_dict), its
    size changed beyond rclone float-reporting noise (0.01 B), or its mtime
    moved by more than `mtime_threshold_seconds`. before_dict maps
    relative_path -> (file_size, mtime) as stored in the database; the mtime
    may be a numeric Unix float (LAN records) or an ISO-8601 string (cloud
    records), while manifest entries carry rclone ModTime strings. Both sides
    are normalized to Unix seconds via _mtime_to_unix BEFORE comparison, so
    equivalent timestamps in different representations never produce a false
    "copied". Only a value no representation can interpret falls back to the
    conservative raw-string last resort.
    """
    copied_files_list = []
    for item in manifest:
        path = item.get("Path") if item.get("Path") is not None else item.get("path", "")
        size = item.get("Size") if item.get("Size") is not None else item.get("size", 0)
        mtime = item.get("ModTime") if item.get("ModTime") is not None else item.get("mtime", 0)

        if path not in before_dict:
            copied_files_list.append((path, size))
            continue

        old_size, old_mtime = before_dict[path]
        # 0.01-byte threshold: guards against float representation noise
        # in rclone's size reporting. Accurate for all real file sizes
        # since actual byte counts are always whole numbers.
        if abs(float(size) - float(old_size)) > 0.01:
            copied_files_list.append((path, size))
            continue

        new_ts = _mtime_to_unix(mtime)
        old_ts = _mtime_to_unix(old_mtime)
        if new_ts is None or old_ts is None:
            # Uninterpretable representation on at least one side: keep the
            # legacy conservative last resort (assume changed when the raw
            # representations differ) instead of letting a parse failure
            # classify every file as copied.
            if str(mtime) != str(old_mtime):
                copied_files_list.append((path, size))
        elif abs(new_ts - old_ts) > mtime_threshold_seconds:
            copied_files_list.append((path, size))

    return copied_files_list


def _run_cloud_pipeline(config, run_id: str, started_at: str, monotonic_start: float | None = None):
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
    phase = "pre"

    try:
        health_check_task(config, "cloud")
        preflight(config, fy_prefix)
        phase = "sync"
        sync_result = sync(config, fy_prefix)
        status = sync_result["status"]
        phase = "verify"
        verify_data = verify_report(config, fy_prefix)
        cloud_record_task(
            db_path, verify_data, sync_result,
            busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
            vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
        )

        # F1: verification is part of the backup contract. A failed check must
        # NOT be recorded as COMPLETE — record a distinct, non-skip status
        # and fail the run so it is visible in the Prefect console.
        #
        # M2/S2-02: this pipeline does NOT send the failure alert itself —
        # backup()'s flow-level summary is the single alert point for
        # pipeline failures (it carries the same verify_err text and
        # performs the [ALERT_NOT_DELIVERED] bookkeeping via the since_iso
        # annotation). Pre-fix, the pipeline AND the summary each emailed
        # the operator: one verify failure = 2 emails (observed live in
        # session-1 T6, where "emailed 2 alerts" was a PASSING outcome).
        if not verify_data.get("verified"):
            diff = verify_data.get("diff") or {}
            # rclone check SOURCE DEST --combined semantics (verified live,
            # 2026-08-20): '+' = present in source, ABSENT from cloud (added to
            # dest), '-' = present in cloud only (unexpected/extra in cloud),
            # '*' = size mismatch. The labels below MUST match that direction —
            # a swapped label tells the operator the opposite of what is wrong.
            verify_err = (
                "Cloud integrity verification FAILED after sync: rclone check found "
                f"differences vs source (missing-from-cloud={len(diff.get('added', []))}, "
                f"unexpected-in-cloud={len(diff.get('removed', []))}, "
                f"size-changed={len(diff.get('modified', []))}). The cloud copy may be "
                "incomplete or out of sync. rclone sync is resumable — the next "
                "scheduled run will re-sync the differences."
            )
            status = "CLOUD_VERIFY_FAILED"
            error_msg = verify_err
            logger.error(verify_err)
            # The raised RuntimeError carries verify_err into the flow-level
            # summary, which alerts ONCE for it.
            raise RuntimeError(verify_err)

        phase = "post"

        # Calculate files and bytes copied by comparing old database state with new live GCS manifest
        copied_files_list = compute_copied_files(verify_data.get("manifest", []), before_dict)
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
        # F2: record the TRUE terminal status. Previously any exception after
        # initialization left the run recorded as CLOUD_SKIPPED (initial value),
        # so real failures were invisible in reports and the dashboard.
        if phase == "sync":
            status = "CLOUD_FAILED"
        elif phase == "verify" and status == "CLOUD_COMPLETE":
            status = "CLOUD_VERIFY_FAILED"
        elif phase == "pre":
            status = "CLOUD_SKIPPED"
        # phase "post" (record/artifact after a successful sync+verify): the
        # data is safe on GCS — keep the sync status; the bookkeeping error
        # stays visible in the run record's error_message.
        raise
    finally:
        _record_run(
            db_path, run_id, "cloud", started_at, status,
            sync_result.get("exit_code", -1), error_msg,
            files_copied=files_copied,
            bytes_copied=bytes_copied,
            files_failed=0,
            extended_metrics=extended_metrics,
            busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
            vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
            monotonic_start=monotonic_start,
        )


# ═══════════════════════════════════════════════════════════════
# LAN pipeline orchestrator
# ═══════════════════════════════════════════════════════════════

def _run_lan_pipeline(config, run_id: str, started_at: str, monotonic_start: float | None = None):
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
    files_failed = 0
    extended_metrics = None
    phase = "pre"

    try:
        health_check_task(config, "lan")
        wol_check_task(config)
        preflight(config)
        before_dict = lan_snapshot_before_task(config)
        phase = "sync"
        sync_result = sync(config)
        status = sync_result["status"]
        phase = "post"
        # F12: robocopy per-file failure count (0 on clean/anomaly runs)
        files_failed = int(sync_result.get("files_failed", 0))
        after_dict = lan_snapshot_after_task(config)
        if after_dict is not None:
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
        else:
            # G14: metrics unavailable this run — record the sync outcome only
            logger.error(
                "Post-sync snapshot unavailable — diff metrics and file-level "
                "DB record skipped for this run; the sync outcome is recorded "
                "as-is and the next run re-derives the metrics."
            )

        logger.info(f"LAN pipeline completed with status {status}")
        # F3/M1: shut the NAS down ONLY after a mirror in which NO file
        # failed to copy (robocopy exit 0-3, or anomaly-only exit 4-7).
        #
        # M1/S2-01 (session-2 finding): core/lan_sync.classify_exit_code
        # maps exit 4-7 (bit 2 only) to LAN_PARTIAL with error=None and
        # files_failed=0 — "mismatched/extra attributes only; the mirror
        # itself completed". The pre-fix branch treated EVERY LAN_PARTIAL
        # as "some files were not copied": it emailed a false failure alert
        # and skipped the NAS shutdown, so a healthy night produced a scary
        # email plus a NAS powered on all night — training operators to
        # ignore the alert that matters (exit 8-15, identical wording).
        #
        # Semantics now, per the core contract:
        #   exit 0-3  (LAN_COMPLETE)          → shut down the NAS
        #   exit 4-7  (anomaly-only PARTIAL)  → NO alert; shut down the NAS
        #   exit 8-15 (copy-error PARTIAL)    → failure alert; keep NAS on
        if status == "LAN_COMPLETE":
            lan_shutdown_task(config)
        elif status == "LAN_PARTIAL":
            exit_code = int(sync_result.get("exit_code", 0) or 0)
            if exit_code & 8:
                # Copy-error PARTIAL (exit 8-15): files really did not land.
                # Alert and keep the NAS on — the next run re-syncs, and the
                # operator may need to inspect the destination now.
                logger.error(
                    f"LAN backup PARTIAL (robocopy exit {exit_code}): COPY "
                    "ERRORS — some files were not copied. NAS shutdown SKIPPED "
                    "— the next run will re-sync the missing files."
                )
                alert_ok = True
                try:
                    alert_ok = send_failure_alert(
                        config.notifications, config.firm_name,
                        (
                            f"LAN backup PARTIAL: robocopy exit code "
                            f"{exit_code} — some files were not copied "
                            f"(files_failed={sync_result.get('files_failed', 0)}). "
                            "The NAS was NOT shut down; the next scheduled "
                            "run will re-sync. "
                            f"{sync_result.get('error') or ''}"
                        ).strip(),
                        {"mode": "lan", "status": status,
                         "exit_code": exit_code},
                        started_at,
                    )
                except Exception as alert_err:
                    alert_ok = False
                    logger.warning(f"Could not send partial-backup alert: {alert_err}")
                if not alert_ok:
                    logger.critical(
                        "ALERT DELIVERY FAILED — the PARTIAL-backup alert was NOT "
                        "delivered; the operator was NOT notified that some files "
                        "were not copied. Check SMTP connectivity/credentials."
                    )
                    _mark_run_alert_not_delivered(db_path, run_id)
            else:
                # Anomaly-only PARTIAL (exit 4-7): every file was copied —
                # robocopy detected only mismatched/extra attributes
                # (timestamps/attributes it will not copy). Per the core
                # contract this is a COMPLETE backup: warn, do NOT alert,
                # and shut the NAS down as after a full mirror.
                logger.warning(
                    f"LAN backup anomaly-only PARTIAL (robocopy exit {exit_code}): "
                    "mismatched/extra attributes only — no file failed to copy. "
                    "Treating the backup as COMPLETE; shutting down the NAS."
                )
                lan_shutdown_task(config)
        else:
            logger.info(f"LAN shutdown skipped — status is {status}")
        return {"status": status, "exit_code": sync_result.get("exit_code", 0)}

    except Exception as e:
        error_msg = str(e)
        # F2: record the TRUE terminal status (previously left as LAN_SKIPPED).
        # phase "post" = sync already succeeded; keep its status, the
        # bookkeeping error stays visible in the run record's error_message.
        if phase == "sync":
            status = "LAN_FAILED"
        elif phase == "pre":
            status = "LAN_SKIPPED"
        raise
    finally:
        _record_run(
            db_path, run_id, "lan", started_at, status,
            sync_result.get("exit_code", -1), error_msg,
            files_copied=files_copied,
            bytes_copied=bytes_copied,
            files_failed=files_failed,
            extended_metrics=extended_metrics,
            busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
            vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
            monotonic_start=monotonic_start,
        )


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _mark_run_alert_not_delivered(db_path: str, run_id: str) -> None:
    """Annotate one run's record with [ALERT_NOT_DELIVERED] (best-effort).

    Used by pipeline-level alert sites (PARTIAL, verify-failed) where the
    run_id is known. Never raises — an annotation failure must not mask the
    original backup failure.
    """
    try:
        db = ManifestDB(db_path)
        try:
            db.mark_alert_not_delivered(run_id=run_id)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Could not annotate run {run_id}: {e}")


def _handle_concurrency_slot_timeout(
    config,
    mode: str,
    flow_start_iso: str,
    exc: TimeoutError,
) -> None:
    """M3/S2-03: make a concurrency-SLOT timeout visible everywhere a real
    failure is visible.

    When the 'aam-backup' slot cannot be acquired within its 1 h timeout
    (a stuck run is holding it — possible given the 3×6 h cloud retry
    budget), no pipeline runs at all. Pre-fix this failed silently: Prefect
    console showed a failed run, but NO email was sent, NO run_history row
    was written, and the dashboard showed nothing. Now:

    1. Record a *_FAILED run row for EVERY pipeline this mode would have
       run — the night shows up in reports, the dashboard, and the weekly
       summary as a failure instead of as if it never happened.
    2. Send ONE failure alert explaining that nothing ran and why.
    3. If the alert itself fails: CRITICAL log + [ALERT_NOT_DELIVERED]
       annotation on the just-recorded rows (same A1 double-failure
       bookkeeping as the flow summary).

    Never raises — the caller re-raises the original TimeoutError so the
    Prefect flow run still fails in the console.
    """
    error = (
        "Backup did not run: the 'aam-backup' concurrency slot could not be "
        f"acquired within 3600 s ({exc}). Another backup run is holding the "
        "lock — possibly a stuck or abnormally long pipeline. No pipelines "
        "were executed for this flow run. Check for orphaned robocopy/rclone "
        "processes and restart the AamBackupAgent service if none are found."
    )
    started_at = now_iso()
    if mode in ("cloud", "all") and config.cloud.enabled:
        _record_run(
            config.paths.database_path,
            _stable_run_id("cloud"),
            "cloud",
            started_at,
            "CLOUD_FAILED",
            -1,
            error,
            busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
            vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
        )
    if mode in ("lan", "all") and config.lan.enabled:
        _record_run(
            config.paths.database_path,
            _stable_run_id("lan"),
            "lan",
            started_at,
            "LAN_FAILED",
            -1,
            error,
            busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
            vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
        )

    alert_ok = True
    try:
        alert_ok = send_failure_alert(
            config.notifications,
            config.firm_name,
            error,
            {"mode": mode, "status": "LOCK_TIMEOUT", "exit_code": None},
            timestamp=now_iso(),
        )
    except Exception as alert_err:
        alert_ok = False
        logger.warning(f"Could not send slot-timeout alert: {alert_err}")

    if not alert_ok:
        logger.critical(
            "ALERT DELIVERY FAILED — the concurrency-slot-timeout alert was "
            "NOT delivered; the operator was NOT notified that no backups "
            "ran this cycle. Check SMTP connectivity, credentials, and "
            "network policy."
        )
        try:
            _ann_db = ManifestDB(config.paths.database_path)
            try:
                _n = _ann_db.mark_alert_not_delivered(since_iso=flow_start_iso)
                logger.critical(f"Annotated {_n} run record(s) with [ALERT_NOT_DELIVERED]")
            finally:
                _ann_db.close()
        except Exception as ann_err:
            logger.warning(f"Could not annotate run record(s): {ann_err}")


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
    files_failed: int = 0,
    extended_metrics: str | None = None,
    busy_timeout_ms: int = 30000,
    vacuum_freelist_threshold: int = 10000,
    monotonic_start: float | None = None,
):
    """Record run history to ManifestDB."""
    ended_at = now_iso()
    # F16: duration must come from a monotonic clock. The old wall-clock
    # subtraction (time.time() - parse(started_at)) goes negative or wildly
    # wrong if NTP adjusts the clock mid-run — exactly the condition this
    # deployment is exposed to (GCS auth depends on NTP being applied).
    if monotonic_start is not None:
        duration = time.monotonic() - monotonic_start
    else:
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
            files_failed=files_failed,
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
        had_runs = bool(db.get_runs_since(7))
        sent = send_weekly_report(db, config.notifications, config.firm_name)
    finally:
        db.close()
    # send_*_report returns False for BOTH "no runs (normal skip)" and
    # "email not delivered" — previously the flow ignored the result either
    # way, so a weekly report silently not arriving left no trace in Prefect.
    if not sent and had_runs:
        logger.error(
            "Weekly report email was NOT delivered (runs exist for the period) "
            "— check SMTP connectivity/credentials."
        )
        raise RuntimeError("Weekly report email failed to send")
    if not sent:
        logger.info("No runs in the last 7 days — weekly report skipped (normal)")


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
        had_runs = bool(db.get_runs_since(30))
        sent = send_monthly_report(db, config.notifications, config.firm_name)
    finally:
        db.close()
    # Same double-meaning False as the weekly flow — see comment there.
    if not sent and had_runs:
        logger.error(
            "Monthly report email was NOT delivered (runs exist for the period) "
            "— check SMTP connectivity/credentials."
        )
        raise RuntimeError("Monthly report email failed to send")
    if not sent:
        logger.info("No runs in the last 30 days — monthly report skipped (normal)")


# ═══════════════════════════════════════════════════════════════
# FY rollover check — scheduled (G10 fix)
# ═══════════════════════════════════════════════════════════════

@flow(name="rollover-check", log_prints=False)
def rollover_check_flow(config_path: str = CONFIG_PATH):
    """Daily FY-rollover check (G10 fix).

    The rollover call in launch.py runs only when the agent process starts.
    On a 24x7 server that is rarely rebooted, the April 1 rollover would
    silently never happen. This scheduled deployment runs the SAME idempotent
    rollover() every day: a no-op all year, and the real rollover on the
    boundary (idempotency was verified with crash-injection probes).

    Returns:
        "ROLLOVER_COMPLETED" | "NO_ROLLOVER_NEEDED"
    Raises:
        RolloverError — rollover is BLOCKED (e.g. source not mounted). The
        flow run fails in the Prefect console and an alert email is sent, so
        a blocked rollover is visible daily until resolved.
    """
    from core.fy_rollover import RolloverError, rollover

    try:
        if rollover(config_path=config_path):
            logger.info("FY rollover completed (scheduled check) — config updated for new FY")
            return "ROLLOVER_COMPLETED"
        return "NO_ROLLOVER_NEEDED"
    except RolloverError as e:
        logger.error(f"FY rollover BLOCKED: {e}")
        alert_ok = True
        try:
            cfg = load_config(config_path)
            alert_ok = send_failure_alert(
                cfg.notifications, cfg.firm_name,
                (
                    f"FY Rollover BLOCKED: {e} The fiscal-year transition "
                    "(new FY folders + config update) did not happen. Until "
                    "this is resolved, new-FY data may not be backed up. "
                    "The scheduled check retries daily."
                ),
                {"mode": "rollover", "status": "ROLLOVER_BLOCKED", "exit_code": None},
                now_iso(),
            )
        except Exception as alert_err:
            alert_ok = False
            logger.warning(f"Could not send rollover-blocked alert: {alert_err}")
        if not alert_ok:
            logger.critical(
                "ALERT DELIVERY FAILED — the rollover-BLOCKED alert was NOT "
                "delivered; the operator was NOT notified that the fiscal-year "
                "transition is blocked. Check SMTP connectivity/credentials."
            )
        raise


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

    # G8: clean up orphaned robocopy temp logs left behind by hard-killed
    # processes (SCM stop, timeout kill, crash). Normal runs delete their own
    # log in a finally block; only killed runs strand them in %TEMP%. The 24h
    # age gate protects a concurrently running pipeline's active log.
    try:
        cleanup_orphaned_robocopy_logs(max_age_hours=24)
    except Exception as e:
        logger.warning(f"Orphaned robocopy log cleanup failed (non-fatal): {e}")

    # ── Watchdog lock — signals that a backup is in progress ──
    # Written INSIDE the concurrency block so only the flow that actually
    # holds the concurrency slot writes the lock.  This prevents a second
    # flow from overwriting the first flow's PID, losing the slot, and then
    # deleting the lock in its finally block — which would leave the active
    # backup invisible to the watchdog.
    # Format: "PID:create_time" — the process creation timestamp makes
    # PID-reuse detection mathematically exact (see core/process.py).
    _lock_path = config.paths.backup_lock_path

    excs = []
    # Timestamp of this flow run — used to scope the [ALERT_NOT_DELIVERED]
    # annotation to THIS run's run_history rows if the summary alert fails.
    _flow_start_iso = now_iso()

    # M3/S2-03: whether the slot was actually acquired. Prefect's
    # concurrency() raises TimeoutError ON ENTRY (during __enter__) if the
    # slot cannot be acquired within timeout_seconds; a TimeoutError raised
    # later (inside the block) means the slot WAS held. The flag lets the
    # handler below distinguish the two.
    _slot_acquired = False

    try:
        with concurrency("aam-backup", occupy=1, timeout_seconds=3600):
            _slot_acquired = True
            # ── Acquire watchdog lock now that we hold the concurrency slot ──
            try:
                write_lock(_lock_path)
                logger.info(f"Backup lock acquired (PID={os.getpid()}) — watchdog will defer restarts")
            except OSError as e:
                logger.warning(f"Could not write backup lock file: {e}")

            try:
                # ── Cloud ──
                if mode in ("cloud", "all") and config.cloud.enabled:
                    logger.info("Starting cloud backup pipeline")
                    try:
                        _run_cloud_pipeline(config, _stable_run_id("cloud"), now_iso(), time.monotonic())
                    except Exception as e:
                        excs.append(e)

                # ── LAN ──
                if mode in ("lan", "all") and config.lan.enabled:
                    logger.info("Starting LAN backup pipeline")
                    try:
                        _run_lan_pipeline(config, _stable_run_id("lan"), now_iso(), time.monotonic())
                    except Exception as e:
                        excs.append(e)

            finally:
                # Release the watchdog lock as soon as pipelines are done —
                # inside the concurrency block so the lock lifetime is
                # strictly bounded to the time we hold the concurrency slot.
                try:
                    _lock_path.unlink(missing_ok=True)
                    logger.info("Backup lock released")
                except OSError:
                    pass

        # ── Summary ──
        if excs:
            error_summary = '; '.join(str(e) for e in excs)
            logger.error(f"Backup completed with {len(excs)} error(s): {error_summary}")
            alert_ok = True
            try:
                alert_ok = send_failure_alert(
                    config.notifications,
                    config.firm_name,
                    error_summary,
                    {"mode": mode},
                    timestamp=now_iso(),
                )
            except Exception as alert_err:
                alert_ok = False
                logger.warning(f"Could not send failure alert: {alert_err}")
            if not alert_ok:
                # DOUBLE FAILURE: the backup failed AND the operator was not
                # notified. Make it visible at every layer: a CRITICAL log
                # line (log file + Prefect console via the bridge) and a
                # [ALERT_NOT_DELIVERED] annotation on this run's record(s) so
                # the dashboard/reports surface the notification gap. Before
                # this fix the alert failure was swallowed by `except: pass`
                # — during the 2026-07-25→08-13 blackout (backup + SMTP both
                # network-blocked) 19 nights of failures produced no
                # notification and no persistent trace of the gap.
                logger.critical(
                    "ALERT DELIVERY FAILED — the failure alert email was NOT "
                    "delivered; the operator was NOT notified of this backup "
                    "failure. Check SMTP connectivity, credentials, and "
                    "network policy."
                )
                try:
                    _ann_db = ManifestDB(config.paths.database_path)
                    try:
                        _n = _ann_db.mark_alert_not_delivered(since_iso=_flow_start_iso)
                        logger.critical(f"Annotated {_n} run record(s) with [ALERT_NOT_DELIVERED]")
                    finally:
                        _ann_db.close()
                except Exception as ann_err:
                    logger.warning(f"Could not annotate run record(s): {ann_err}")
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

    except TimeoutError as e:
        if not _slot_acquired:
            # M3/S2-03: the SLOT ACQUISITION itself timed out — a stuck run
            # held 'aam-backup' for >1 h (possible: the cloud retry budget is
            # 3×6 h). Pre-fix this path was the system's one fully-silent
            # failure mode: the flow failed in the Prefect console but NO
            # email was sent, NO run_history row was written, and the
            # dashboard showed nothing — an entire night of backups could
            # vanish with no trace anywhere.
            _handle_concurrency_slot_timeout(config, mode, _flow_start_iso, e)
        raise
    except Exception:
        raise
