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
from contextlib import ExitStack, contextmanager

import pendulum
from loguru import logger
from prefect import flow, task
from prefect.concurrency.sync import concurrency

from core import approvals, faults, fy_rollover, guards, permissions, report, statuses
from core.rclone_errors import find_transport_failure
from core.backup_repository import (
    record_run_history,
    record_sync_results,
    start_run_row,
    sweep_abandoned_runs,
)
from core.cloud_preflight import run_cloud_dry_run
from core.cloud_reporter import (
    CloudReporterError,
    count_objects,
    get_cloud_diff,
    get_cloud_manifest,
    get_cloud_size,
)
from core.budget import BudgetExceeded, RunBudget, late_seconds as seconds_late
from core.cloud_sync import resolve_max_duration_seconds, run_cloud_sync
from core.cloud_verify import verify_cloud_integrity
from core.health import CLOCK_OK, check_clock, pre_backup_health
from core.lan_manifest import diff_snapshots, snapshot_to_dict, walk_lan_destination
from core.lan_preflight import run_lan_dry_run
from core.lan_sync import cleanup_orphaned_robocopy_logs, run_lan_sync
from core.logging import configure as configure_logging
from core.logging import configure_prefect_bridge
from core.manifest import ManifestDB
from core.notify import SENT, AlertEvent, notify, send_email
from core.process import acquire_lock, read_lock_alive, write_lock
from core.rclone_config import temp_rclone_config
from core.run_artefacts import RunArtefacts
from core.shutdown import FAILED as SHUTDOWN_FAILED
from core.shutdown import ShutdownResult, shutdown_from_config
from core.time_utils import get_fy_prefix, now_iso
from core.wol import ensure_server_online
from models.config import CONFIG_PATH, load_config


class PartialRun(RuntimeError):
    """A pipeline ended PARTIAL: transfer ran but work is incomplete.

    Raised (instead of returning normally) so a partial backup can never
    present as Prefect COMPLETED. The in-pipeline partial alert already
    fired; backup() skips the duplicate generic alert for this type but
    still fails the flow via ExceptionGroup.
    """


class SyncFailed(RuntimeError):
    """A sync attempt ended FAILED / SUSPECT; `result` is the tool's result.

    Carries the verdict (exit code, status, deletions) to the attempt loop
    and the run row - a bare RuntimeError lost it (the row said exit -1).
    """

    def __init__(self, message: str, result: dict):
        super().__init__(message, result)
        self.result = result

    def __str__(self) -> str:
        return str(self.args[0])


# Terminal-status policy: Prefect COMPLETED is allowed ONLY for these row
# statuses. Every other terminal status raises before the pipeline returns,
# so a non-success backup can never hide behind a successful orchestration
# result. COMPLETE means "transfer evidence says the scheduled work
# finished" — it does NOT mean independently/cryptographically verified
# (see the weekly integrity audit).
_CLOUD_OK_STATUSES = statuses.CLOUD_SUCCESS_STATUSES
_LAN_OK_STATUSES = statuses.LAN_SUCCESS_STATUSES


_FY_LEAF_PATTERN = re.compile(r"^FY\d{2}-\d{2}$", re.IGNORECASE)


def _effective_fy_prefix(config) -> str:
    """Cloud FY scope: configured source leaf when it is a valid FY dir.

    Normal days the leaf equals the date-derived prefix (no behavior
    change). Around FY rollover the configured leaf is authoritative: it
    matches the LAN destination leaf and the final-backup prefix, so a
    daily run can never sync old-FY data into the new-FY cloud prefix
    before the rollover executes. Non-FY sources fall back to the date.
    Enumeration scope is unchanged (source_drive is still the root).
    """
    return fy_rollover.effective_fy_prefix(config.paths.source_drive)


def _failure_telemetry(stage_seconds: dict, phase: str, verified: bool = False) -> str | None:
    """Build failure-path extended_metrics from stages actually measured.

    Supplementary evidence only: unexecuted stages stay absent (never
    fabricated); business state is decided elsewhere and untouched.
    """
    try:
        payload: dict = {"stage_seconds": dict(stage_seconds), "failure_phase": phase}
        if verified:
            payload["verified"] = True
        return json.dumps(payload)
    except Exception:
        return None


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


def _open_run_artefacts(config, run_id: str) -> RunArtefacts | None:
    """Create the run's full-log folder (plan WP2). Never blocks a backup:
    on failure the tools fall back to a temp folder and the reason is logged."""
    try:
        return RunArtefacts.for_config(config, run_id)
    except Exception as e:  # noqa: BLE001 - evidence storage must never block a backup
        logger.error(f"Run log folder could not be created ({e}) - full logs for "
                     "this run go to a temporary folder")
        return None


def _finalize_run_artefacts(config, artefacts: RunArtefacts | None) -> None:
    if artefacts is None:
        return
    try:
        artefacts.finalize(config.run_logs.compress_after_kb)
    except Exception as e:  # noqa: BLE001 - evidence handling must not fail a run
        logger.warning(f"Run log finalisation failed ({artefacts.dir}): {e}")


def _with_artefacts(extended_metrics: str | None, artefacts: RunArtefacts | None) -> str | None:
    """Add the run's log folder to extended_metrics (JSON) for the dashboard."""
    if artefacts is None:
        return extended_metrics
    try:
        payload = json.loads(extended_metrics) if extended_metrics else {}
    except ValueError:
        payload = {"unparsed_metrics": extended_metrics}
    payload["artefacts_dir"] = str(artefacts.dir)
    payload["artefacts_degraded"] = artefacts.degraded
    return json.dumps(payload)


def _with_guards(extended_metrics: str | None, guard_record: dict | None) -> str | None:
    """Add the run's guard record to extended_metrics (JSON).

    Kept on every row (pass or block): it is the G2/G4 baseline of the next
    run (ManifestDB.guard_baseline) and the evidence a BLOCKED row shows.
    """
    if not isinstance(guard_record, dict):
        return extended_metrics
    try:
        payload = json.loads(extended_metrics) if extended_metrics else {}
    except ValueError:
        payload = {"unparsed_metrics": extended_metrics}
    payload["guards"] = guard_record
    return json.dumps(payload)


@contextmanager
def _backup_slot(config):
    """P2-CONC: global serialization slot + watchdog lock, per pipeline.

    The slot lives HERE (inside each pipeline) instead of around the flow
    body so that EVERY entry path - production flow, manual runs, scripts,
    tests - serializes on the same "aam-backup" limit. The flow-level wrapper
    was removed in the same commit; keeping both would self-deadlock a
    limit=1 resource.

    The watchdog lock is written only while the slot is held, preserving the
    F13 invariant: lock holder == slot holder, so a second process can never
    overwrite an active run's PID and then delete the lock.
    """
    wait_seconds = int(
        getattr(
            getattr(config, "maintenance", None),
            "concurrency_wait_seconds",
            3600,
        )
    )
    lock_path = config.paths.backup_lock_path

    if os.environ.get("PREFECT_TEST_MODE") == "1":
        # Unit-test mode (tests/conftest forces this env): occupancy requires
        # an orchestration API which these tests deliberately do not provide.
        # Keep the watchdog-lock lifecycle so its semantics stay testable;
        # production never sets PREFECT_TEST_MODE.
        try:
            write_lock(lock_path)
            logger.info(f"Backup lock acquired (PID={os.getpid()}) - test mode")
        except OSError as e:
            logger.warning(f"Could not write backup lock file: {e}")
        try:
            yield
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        return

    with concurrency("aam-backup", occupy=1, timeout_seconds=wait_seconds):
        # Exclusive acquisition: a live lock owned by another pipeline aborts
        # this one BEFORE any transfer starts (slot is released by the raise).
        # The finally below only removes the lock if it is still ours, so a
        # successor's lock can never be deleted by a finishing predecessor.
        acquired = False
        try:
            acquired = acquire_lock(lock_path)
            if not acquired:
                raise RuntimeError(
                    "Backup lock is held by a live process - refusing to "
                    "overwrite it or run without it"
                )
            logger.info(
                f"Backup lock acquired (PID={os.getpid()}) - watchdog will defer restarts"
            )
        except OSError as e:
            logger.warning(f"Could not write backup lock file: {e}")
        try:
            yield
        finally:
            if acquired:
                try:
                    _, owner_pid = read_lock_alive(lock_path)
                    if owner_pid == os.getpid():
                        lock_path.unlink(missing_ok=True)
                        logger.info("Backup lock released")
                    else:
                        logger.warning(
                            "Backup lock changed during run - leaving it in place"
                        )
                except OSError:
                    pass


def _cloud_diff_count(manifest: list, before: dict) -> int:
    """Files whose bucket entry differs from the cloud leg's last recorded
    state (new, other size, mtime off by > 1.1 s, or baseline unknown).
    Metrics CROSS-CHECK only (plan WP6) - never the reported count."""
    if not isinstance(before, dict):     # no usable baseline: everything differs
        before = {}
    changed = 0
    for item in manifest:
        path = item.get("Path") if item.get("Path") is not None else item.get("path", "")
        size = item.get("Size") if item.get("Size") is not None else item.get("size", 0)
        mtime = item.get("ModTime") if item.get("ModTime") is not None else item.get("mtime", 0)
        old_size, old_mtime = before.get(path, (None, None))
        if old_size is None or old_mtime is None:
            changed += 1
            continue
        # 0.01-byte threshold: float representation noise in reported sizes.
        if abs(float(size) - float(old_size)) > 0.01:
            changed += 1
            continue
        try:
            if isinstance(mtime, (int, float)) and isinstance(old_mtime, (int, float)):
                t1, t2 = float(mtime), float(old_mtime)
            else:
                t1 = pendulum.parse(str(mtime)).timestamp()
                t2 = pendulum.parse(str(old_mtime)).timestamp()
            if abs(t1 - t2) > 1.1:
                changed += 1
        except Exception:  # noqa: BLE001 - unparsable = compare as text
            if str(mtime) != str(old_mtime):
                changed += 1
    return changed


def _send_run_alert(config, leg: str, status: str, message: str, exit_code,
                    occurred_at: str, *, run_id: str | None = None,
                    sync_result: dict | None = None, fault_code: str | None = None,
                    artefacts_dir: str | None = None) -> None:
    """THE failure alert of one run (plan WP8): one per failed run, at the
    terminal decision, through core.notify (delivery recorded, cooldown per
    leg+status, dashboard link). Never raises. The fault code is the one the
    pipeline classified, else derived from the status + tool evidence."""
    try:
        code = fault_code or faults.classify_run(leg, status, sync_result=sync_result)
    except Exception:  # noqa: BLE001 - the alert must still go out
        code = faults.UNKNOWN
    notify(config, AlertEvent(
        key=f"backup:{leg}:{status}", kind="FAILURE", mode=leg,
        title=f"{leg.upper()} backup {statuses.label(status).lower()}",
        message=message, status=status,
        exit_code=exit_code if type(exit_code) is int else None,
        occurred_at=occurred_at, fault_code=code, run_id=run_id,
        artefacts_dir=artefacts_dir,
    ))


def _failure_alert_text(leg: str, status: str, error: str | None, nas_note: str = "") -> str:
    name = "Cloud" if leg == "cloud" else "LAN"
    if status.endswith("_SUSPECT"):
        return (f"{name} backup SUSPECT: {error} The transfer did not terminate normally; "
                f"completion is unproven. {nas_note or 'The next scheduled run will re-sync.'}")
    return f"{name} backup {statuses.label(status)}: {error or status} {nas_note}".strip()


def _send_recovery_if_needed(config, leg: str, run_id: str) -> None:
    """RECOVERED email on the first success after a failure (plan WP8)."""
    try:
        db = ManifestDB.from_config(config)
        try:
            previous = db.previous_run_status(leg, run_id)
            previous_code = db.previous_run_fault_code(leg, run_id)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - never affects the run
        logger.warning(f"Recovery check skipped ({leg}): {e}")
        return
    if previous and statuses.category(previous) in (
            statuses.CATEGORY_FAILED, statuses.CATEGORY_PARTIAL, statuses.CATEGORY_BLOCKED):
        notify(config, AlertEvent(
            key=f"recovered:{leg}", kind="RECOVERED", mode=leg,
            title=f"{leg.upper()} backup recovered",
            message=f"The {leg.upper()} backup completed successfully again "
                    f"(previous run: {statuses.label(previous)}).",
            status="RECOVERED", run_id=run_id,
            resolved_code=previous_code or faults.classify_run(leg, previous)))


def _exception_kind(exc: BaseException) -> str:
    """Map a pipeline exception onto statuses.decide_terminal_status kinds."""
    if isinstance(exc, guards.GuardBlocked):
        return statuses.EXC_BLOCKED
    if isinstance(exc, PartialRun):
        return statuses.EXC_PARTIAL
    return statuses.EXC_ERROR


def _budget_metrics(budget: RunBudget, sync_result: dict) -> dict:
    """WP14 evidence on every run row: the budget and whether this run hit it."""
    return {"hours": round(budget.seconds / 3600, 2),
            "used_hours": round((budget.seconds - budget.remaining()) / 3600, 2),
            "stopped": bool(sync_result.get("budget_stopped"))}


def _merge_metrics(extended_metrics: str | None, **fields) -> str | None:
    """Add fields to the extended_metrics JSON (never drops existing data)."""
    try:
        payload = json.loads(extended_metrics) if extended_metrics else {}
    except ValueError:
        payload = {"unparsed_metrics": extended_metrics}
    payload.update(fields)
    return json.dumps(payload, default=str)


def _record_running(config, leg: str, run_id: str, started_at: str) -> None:
    """Write the *_RUNNING row BEFORE the slot is requested (plan WP5), so a
    run that never gets the slot - or dies - is still visible. First records
    earlier runs whose process died meanwhile as ABANDONED (a flow-run crash
    while the agent kept running is otherwise never swept). Never blocks
    the backup."""
    try:
        db = ManifestDB.from_config(config)
        try:
            try:
                sweep_abandoned_runs(config, db)
            except Exception as e:  # noqa: BLE001 - bookkeeping must not stop a backup
                logger.error(f"Could not check for abandoned runs before {leg} run {run_id}: {e}")
            start_run_row(db, run_id=run_id, mode=leg, started_at=started_at)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - bookkeeping must not stop a backup
        logger.error(f"Could not record the RUNNING row for {leg} run {run_id}: {e}")


def _scheduled_lateness_seconds() -> float:
    """Plan I2: how long after its SCHEDULED time this flow run started (a reboot or a busy
    server starts a missed run late). 0 for a manual run and outside a flow run: those count
    their run budget from the actual start."""
    try:
        from prefect.context import FlowRunContext
        ctx = FlowRunContext.get()
        run = ctx.flow_run if ctx else None
        if run is None or not getattr(run, "auto_scheduled", False) or run.expected_start_time is None:
            return 0.0
        return seconds_late(run.expected_start_time.timestamp(), time.time())
    except Exception as e:  # noqa: BLE001 - a lateness we cannot read means "on time", never a block
        logger.warning(f"Scheduled start time unknown ({e}); the run budget counts from now")
        return 0.0


def _missed_if_no_budget(config, leg: str, run_id: str, started_at: str, budget: RunBudget,
                         reserve_s: float, late_s: float, monotonic_start: float | None) -> None:
    """Plan I2: a run that starts with no budget left (less than the time its last steps need)
    would run into office hours. Record <LEG>_MISSED and send ONE warning per leg (the stable
    key `missed:<leg>` keeps several missed nights at boot from sending one email each), then
    raise PartialRun so Prefect never shows this run as completed. Takes no backup slot."""
    if late_s <= 0 or budget.remaining() > reserve_s:
        return
    status = f"{leg.upper()}_MISSED"
    late_h = late_s / 3600
    error = (f"The {leg.upper()} backup was scheduled for {late_h:.1f} h before it could start "
             f"(the server was off or busy), so its {budget.describe()} run budget was already "
             "used up. Nothing was changed; the next scheduled run continues.")
    logger.warning(error)
    _record_run(config, run_id, leg, started_at, status, -1, error,
                monotonic_start=monotonic_start)
    notify(config, AlertEvent(
        key=f"missed:{leg}", kind="WARNING", mode=leg,
        title=f"{leg.upper()} backup night missed", message=error, status=status,
        occurred_at=started_at, fault_code="AAM-SYS-07", run_id=run_id))
    raise PartialRun(f"{leg.upper()} run ended {status} - {error}")


@contextmanager
def _pipeline_slot(config, leg: str, run_id: str, started_at: str,
                   monotonic_start: float | None = None):
    """RUNNING row, then the backup slot. If the slot (or the watchdog lock)
    cannot be obtained, the row becomes *_SLOT_TIMEOUT ("could not start")
    and the error is re-raised - previously no row was written at all."""
    _record_running(config, leg, run_id, started_at)
    with ExitStack() as stack:
        try:
            stack.enter_context(_backup_slot(config))
        except Exception as e:
            status = f"{leg.upper()}_SLOT_TIMEOUT"
            logger.error(f"{leg} run {run_id} could not start: {e}")
            try:
                _record_run(
                    config, run_id, leg, started_at, status, -1,
                    f"Backup could not start: {e}",
                    monotonic_start=monotonic_start,
                )
            except Exception as rec_err:  # noqa: BLE001
                logger.critical(f"Failed to record {status} for run {run_id}: {rec_err}")
            _send_run_alert(config, leg, status, f"The {leg.upper()} backup could not start: {e}",
                            None, started_at, run_id=run_id)
            raise
        yield


# ═══════════════════════════════════════════════════════════════
# Shared tasks
# ═══════════════════════════════════════════════════════════════

@task(name="health-check")
def health_check_task(config, mode: str, artefacts_dir: str | None = None):
    """Run pre-backup health checks. Fail fast — won't fix itself.

    LAN with /ZB (lan.use_backup_mode): SeBackupPrivilege is checked here,
    before the slow dry-run (plan WP4, DEC-8).
    """
    logger.info(f"Running health checks (mode={mode})")
    artefacts = RunArtefacts.open(artefacts_dir) if artefacts_dir else RunArtefacts.ephemeral("health")
    clock = pre_backup_health(
        config.paths.source_drive,
        mode,
        config.paths.gcs_key_path,
        min_free_source_gb=config.health.min_free_source_gb,
        max_clock_skew_seconds=config.health.max_clock_skew_seconds,
        clock_check_timeout_seconds=config.health.clock_check_timeout_seconds,
        clock_unverified_policy=config.health.clock_unverified_policy,
        ntp_server=config.health.ntp_server,
        w32tm_timeout_seconds=config.health.w32tm_timeout_seconds,
        artefacts=artefacts,
    )
    if mode == "lan" and config.lan.use_backup_mode:
        permissions.check_backup_privilege(
            artefacts, timeout_s=config.lan.privilege_check_timeout_seconds,
        )
    # WP12: the clock verdict goes on the run row (dashboard: "clock unverified")
    return clock.to_metrics() if clock is not None else None


# ═══════════════════════════════════════════════════════════════
# Cloud pipeline tasks
# ═══════════════════════════════════════════════════════════════

@task(name="cloud-preflight")
def cloud_preflight_task(config, fy_prefix: str, artefacts_dir: str | None = None):
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
        retries=config.cloud.probe_retries,
        retries_sleep_seconds=config.cloud.probe_retries_sleep_seconds,
        artefacts=RunArtefacts.open(artefacts_dir) if artefacts_dir else None,
    )
    if not result["ok"]:
        raise faults.coded(RuntimeError(f"Cloud preflight failed: {result['error']}"),
                           _cloud_preflight_fault(result))
    return result


def _cloud_preflight_fault(result: dict) -> str:
    """Fault code of a failed GCS probe: tool missing, transport cause, else CLD-10."""
    if result.get("error") == "rclone not found":
        return "AAM-SYS-04"
    cause = find_transport_failure(result.get("error") or "")
    return faults.TRANSPORT_CODES.get(cause.category, "AAM-CLD-10") if cause else "AAM-CLD-10"


@task(name="cloud-guards")
def cloud_guards_task(config, fy_prefix: str, artefacts_dir: str | None = None,
                      approval_id: str | None = None, run_id: str | None = None,
                      already_deleted: int = 0) -> dict:
    """Data-safety guards G1-G4 before rclone sync (plan WP3, M2 2.2).

    Measures with rclone itself: `rclone size` of source and destination,
    `rclone sync --dry-run` for planned deletions (same command as the
    real sync). A measurement that could not be taken FAILS the run
    (retried next night); a guard verdict BLOCKs it for a human. The
    record's "max_delete" is the --max-delete backstop for the real sync.
    already_deleted (XR-1): deletions of earlier attempts of this run - G3
    and the backstop cover the whole run, not each attempt.
    """
    source = config.paths.source_drive
    bucket = config.cloud.bucket
    remote = f"aam_gcs:{bucket}/{fy_prefix}"
    destination = f"gs://{bucket}/{fy_prefix}"
    limits = config.guards
    artefacts = RunArtefacts.open(artefacts_dir) if artefacts_dir else RunArtefacts.ephemeral("cloud-guards")

    first_file = guards.first_regular_file(source, guards.SOURCE_IGNORE_NAMES)
    try:
        with temp_rclone_config(
            config.paths.gcs_key_path, config.cloud.location,
            config.cloud.project_number, config.cloud.storage_class,
        ) as rclone_conf:
            source_files = count_objects(source, rclone_conf,
                                         config.cloud.dry_run_timeout_seconds, artefacts)
            destination_files = count_objects(remote, rclone_conf,
                                              config.cloud.cloud_size_timeout_seconds, artefacts)
    except CloudReporterError as e:
        raise RuntimeError(f"Cloud guard measurement failed: {e}") from e

    db = ManifestDB.from_config(config)
    try:
        baseline = db.guard_baseline("cloud", fy_prefix) or {}
    finally:
        db.close()

    results = [
        guards.g1_source_has_files(first_file, source),
        guards.g2_source_shrink(source_files, baseline.get("source_file_count"),
                                limits.max_source_shrink_percent),
        # Not a host path: the "is this machine" check does not apply.
        guards.g4_destination_identity(destination, baseline.get("destination"),
                                       None, set(), forbid_local=False),
    ]
    if first_file:
        # A source without files is already blocked by G1; its dry-run would
        # only plan the deletion of everything.
        dry = run_cloud_sync(
            source=source, bucket=bucket, fy_prefix=fy_prefix,
            gcs_key_path=config.paths.gcs_key_path, location=config.cloud.location,
            project_number=config.cloud.project_number,
            bwlimit=config.cloud.bandwidth_limit, retries=config.cloud.retry_count,
            storage_class=config.cloud.storage_class, transfers=config.cloud.transfers,
            checkers=config.cloud.checkers, buffer_size=config.cloud.buffer_size,
            modify_window_seconds=config.cloud.modify_window_seconds,
            retries_sleep_seconds=config.cloud.sync_retries_sleep_seconds,
            stats_interval_seconds=config.cloud.stats_interval_seconds,
            timeout=config.cloud.dry_run_timeout_seconds, max_duration_seconds=0,
            artefacts=artefacts, error_tail_lines=config.cloud.error_tail_lines,
            dry_run=True,
        )
        if dry.get("planned_deletes") is None:
            raise RuntimeError(
                f"Cloud guard measurement failed: rclone sync --dry-run did not complete "
                f"cleanly ({dry.get('status')}): {dry.get('error')}")
        results.append(guards.g3_mass_delete(
            dry["planned_deletes"], destination_files,
            limits.max_delete_files, limits.max_delete_percent,
            already_deleted=already_deleted))

    approved, approval_record = _apply_approval(config, "cloud", approval_id, run_id, results)
    if "G3_MASS_DELETE" in approved:
        # Approved mass deletion: the backstop is the re-measured count the
        # approval was just checked against - nothing beyond it.
        max_delete = dry["planned_deletes"]
    else:
        max_delete = guards.remaining_delete_allowance(
            destination_files, already_deleted,
            limits.max_delete_files, limits.max_delete_percent)
    return guards.enforce(
        "cloud", results, approved_guards=approved, fy=fy_prefix,
        source_file_count=source_files, destination=destination,
        destination_files=destination_files, max_delete=max_delete, **approval_record,
        **({"already_deleted": already_deleted} if already_deleted else {}),
    )


@task(name="cloud-access")
def cloud_access_task(config, run_id: str, artefacts_dir: str | None = None):
    """Prove the rights the sync uses (plan WP4, CL3/CL13): read a real source
    file; create + delete a probe object outside every FY prefix (DEC-5).
    Runs after the guards; a missing right FAILS the run."""
    permissions.probe_source_read(config.paths.source_drive)
    if not config.cloud.write_probe_enabled:
        logger.info("Cloud write probe disabled (cloud.write_probe_enabled: false)")
        return
    with temp_rclone_config(
        config.paths.gcs_key_path, config.cloud.location,
        config.cloud.project_number, config.cloud.storage_class,
    ) as rclone_conf:
        permissions.probe_cloud_write(
            config.cloud.bucket, run_id, rclone_conf, config.cloud.preflight_timeout_seconds,
            RunArtefacts.open(artefacts_dir) if artefacts_dir else RunArtefacts.ephemeral("cloud-access"),
            error_tail_lines=config.cloud.error_tail_lines,
        )


def _sync_with_retries(leg: str, *, run_once, remeasure, deletes_of, retryable,
                       max_attempts: int, delay_s: int, budget: RunBudget | None,
                       reserve_s: float, state: dict) -> dict:
    """XR-1 / XR-2 / XR-6: the ONE retry policy for a mirror (both legs).

    Replaces Prefect task retries, which re-ran the mirror with the same
    arguments: a fresh --max-delete each time (up to max_attempts x the G3
    limit deleted) and a robocopy /MIR repeat without re-checking anything.

    run_once() runs one attempt; it returns the result or
    raises SyncFailed (anything else is not retried). A failed attempt is
    retried only when
      * its deletions are known (deletes_of -> int; unknown = fail closed),
      * the tool did not stop at its delete backstop (-> BLOCKED, a human),
      * retryable(result) - a repeat can change it,
      * attempts are left and the delay fits the run budget,
      * and remeasure(already_deleted) - the same guards as before attempt 1,
        counting this run's deletions - passes (it raises GuardBlocked or
        RuntimeError otherwise).
    state["result"] always holds the latest attempt's result (the run row's
    exit code / status); state["attempts"] the per-attempt evidence.
    """
    attempts = state.setdefault("attempts", [])
    deleted = 0
    max_attempts = max(1, int(max_attempts))   # the mirror always runs once
    for attempt in range(1, max_attempts + 1):
        try:
            result, failure = run_once(), None
        except SyncFailed as e:
            result, failure = e.result, e
        state["result"] = result
        done = deletes_of(result)
        attempts.append({"attempt": attempt, "status": result.get("status"),
                         "exit_code": result.get("exit_code"), "deleted": done})
        if failure is None:
            return result
        label = (f"{leg.upper()} sync attempt {attempt}/{max_attempts} ended "
                 f"{result.get('status')} (exit {result.get('exit_code')})")
        if result.get("max_delete_reached"):
            deleted += done or 0
            logger.error(f"{label} at the delete backstop after {deleted} deletion(s) in "
                         "this run - not retried; re-measuring for the approval request")
            try:
                record = remeasure(deleted)
            except guards.GuardBlocked:
                raise
            except Exception as e:  # noqa: BLE001 - the stop must still reach a human
                logger.error(f"Re-measure after the delete backstop failed ({e}) - "
                             "blocking with the known deletions only")
                record = {"results": []}
            raise guards.backstop_blocked(leg, record, deleted)
        if done is None:
            logger.error(f"{label}; its deletions are unknown - not retried (a repeat "
                         "could not be bounded)")
            raise failure
        deleted += done
        if not retryable(result):
            logger.error(f"{label} - not a failure a repeat can change; not retried")
            raise failure
        if attempt == max_attempts:
            logger.error(f"{label} - no attempts left")
            raise failure
        if budget is not None and budget.remaining() <= delay_s + reserve_s:
            logger.error(f"{label} - the {delay_s}s retry delay does not fit the run "
                         f"budget ({budget.describe()}); not retried")
            raise failure
        logger.warning(f"{label}; retrying in {delay_s}s after re-checking the guards "
                       f"({deleted} file(s) deleted so far in this run)")
        time.sleep(delay_s)
        remeasure(deleted)
    raise AssertionError("unreachable: the loop returns or raises")


def _budget_stop_text(budget: RunBudget, what: str) -> str:
    return (f"Stopped at the run budget of {budget.describe()}: {what}. This is not a "
            "complete backup; the next run continues where this one stopped.")


@task(name="cloud-sync")
def cloud_sync_task(config, fy_prefix: str, artefacts_dir: str | None = None,
                    max_delete: int | None = None, budget: RunBudget | None = None):
    """Run rclone sync to mirror source → GCS (max_delete: G3 backstop).

    WP14: with a run budget the upload gets what is left of it (minus the
    verify reserve) as rclone's own --max-duration; stopping there is
    CLOUD_PARTIAL with `budget_stopped` - resumable, never a retry.
    """
    timeout = config.cloud.subprocess_timeout_seconds
    max_duration = config.cloud.max_duration_seconds
    limited = False
    if budget is not None:
        reserve = config.budget.cloud_verify_reserve_minutes * 60
        try:
            limited = budget.binding(timeout, reserve)
            timeout = int(budget.cap(timeout, reserve))
        except BudgetExceeded as e:
            logger.error(str(e))
            return {"status": "CLOUD_PARTIAL", "exit_code": -1, "budget_stopped": True,
                    "stats": None, "error": _budget_stop_text(
                        budget, "no time was left for the upload")}
        if limited:
            # rclone stops itself (exit 10) before the hard kill at `timeout`
            max_duration = resolve_max_duration_seconds(timeout, None)
            logger.info(f"Cloud upload limited by the run budget: --max-duration "
                        f"{max_duration}s, hard stop {timeout}s")
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
        modify_window_seconds=config.cloud.modify_window_seconds,
        retries_sleep_seconds=config.cloud.sync_retries_sleep_seconds,
        stats_interval_seconds=config.cloud.stats_interval_seconds,
        timeout=timeout,
        max_duration_seconds=max_duration,
        artefacts=RunArtefacts.open(artefacts_dir) if artefacts_dir else None,
        error_tail_lines=config.cloud.error_tail_lines,
        max_delete=max_delete,
    )
    stopped = result.get("exit_code") == 10 or (limited and result.get("timed_out"))
    if stopped:
        stats = result.get("stats") or {}
        done = (f"{stats.get('transfers', 'unknown')} file(s), "
                f"{(stats.get('bytes') or 0) / 1024 ** 3:.2f} GB uploaded in this run"
                if stats else "upload progress unknown (no rclone stats)")
        if limited:
            logger.warning(f"Cloud upload stopped at the run budget - {done}")
            return {**result, "status": "CLOUD_PARTIAL", "budget_stopped": True,
                    "error": _budget_stop_text(budget, done)}
        # XR-5: rclone's own --max-duration (cloud.max_duration_seconds, or the
        # subprocess timeout minus its margin) - the same knowingly incomplete
        # bucket as a budget stop, so the same truthful PARTIAL, never a verify.
        cap_s = resolve_max_duration_seconds(timeout, max_duration)
        logger.warning(f"Cloud upload stopped at rclone's --max-duration of {cap_s}s - {done}")
        return {**result, "status": "CLOUD_PARTIAL", "time_limit_stopped": True, "error": (
            f"Stopped at rclone's time limit (--max-duration {cap_s}s; config "
            f"cloud.max_duration_seconds / cloud.subprocess_timeout_seconds): {done}. This is "
            "not a complete backup; the next run continues where this one stopped.")}
    if result["status"] in ("CLOUD_FAILED", "CLOUD_SUSPECT"):
        raise SyncFailed(result.get("error") or "Cloud sync failed", result)
    return result


@task(name="cloud-verify-and-report")
def cloud_verify_and_report_task(config, fy_prefix: str, budget: RunBudget | None = None,
                                 artefacts_dir: str | None = None):
    """Verify integrity + gather size/manifest/diff for reporting.

    WP14: each check's timeout is capped by what is left of the run budget.
    """
    def limit(timeout_s):
        return int(budget.cap(timeout_s)) if budget is not None else timeout_s

    artefacts = (RunArtefacts.open(artefacts_dir) if artefacts_dir
                 else RunArtefacts.ephemeral("cloud-verify-report"))

    with temp_rclone_config(
        config.paths.gcs_key_path,
        config.cloud.location,
        config.cloud.project_number,
        config.cloud.storage_class,
    ) as rclone_cfg:
        logger.info("Verifying cloud integrity")
        _t_verify = time.monotonic()
        verify_result = verify_cloud_integrity(
            source=config.paths.source_drive,
            bucket=config.cloud.bucket,
            fy_prefix=fy_prefix,
            config_path=rclone_cfg,
            timeout=limit(config.cloud.verify_timeout_seconds),
            checkers=config.cloud.checkers,
            modify_window_seconds=config.cloud.modify_window_seconds,
            artefacts=artefacts,
        )
        verify_s = time.monotonic() - _t_verify

        logger.info("Gathering cloud report data")
        _t_size = time.monotonic()
        size = get_cloud_size(
            config.cloud.bucket, fy_prefix, rclone_cfg,
            timeout=limit(config.cloud.cloud_size_timeout_seconds),
            artefacts=artefacts,
        )
        size_s = time.monotonic() - _t_size
        # M6: a failed manifest query must never look like an empty bucket.
        # Catch and carry the failure as data; the pipeline decides policy.
        manifest_error = None
        _t_manifest = time.monotonic()
        try:
            manifest = get_cloud_manifest(
                config.cloud.bucket, fy_prefix, rclone_cfg,
                timeout=limit(config.cloud.manifest_timeout_seconds),
                artefacts=artefacts,
            )
        except Exception as e:
            logger.warning(
                f"Cloud manifest query failed - metrics degraded this run: {e}"
            )
            manifest, manifest_error = [], str(e)
        manifest_s = time.monotonic() - _t_manifest
        _t_diff = time.monotonic()
        cloud_diff = get_cloud_diff(
            config.paths.source_drive,
            config.cloud.bucket,
            fy_prefix,
            rclone_cfg,
            timeout=limit(config.cloud.diff_timeout_seconds),
            checkers=config.cloud.checkers,
            modify_window_seconds=config.cloud.modify_window_seconds,
            retries=config.cloud.retry_count,
            retries_sleep_seconds=config.cloud.retries_sleep_seconds,
            artefacts=artefacts,
        )
        diff_s = time.monotonic() - _t_diff
        stage_seconds = {
            "verify_s": round(verify_s, 1),
            "size_s": round(size_s, 1),
            "manifest_s": round(manifest_s, 1),
            "diff_s": round(diff_s, 1),
        }
        logger.info(f"Cloud stage timings (s): {stage_seconds}")

        if "_error" in size:
            logger.warning(f"Cloud size query degraded this run: {size['_error']}")

        logger.info(
            f"Cloud verify complete: {size['count']} files, "
            f"{size['bytes']} bytes, verified={verify_result['verified']}"
        )

        return {
            "verified": verify_result["verified"],
            # F-08 liveness: the check process's own completion evidence,
            # NOT its (killable) exit code. Missing keys default to
            # abnormal/incomplete so older mocked results fail closed.
            "verify_termination": verify_result.get("termination", "abnormal"),
            "verify_completion": verify_result.get("completion", False),
            "verify_differences": verify_result.get("differences"),
            "verify_reason": verify_result.get("reason"),
            # C-DK-001 liveness: carried so the pipeline can distinguish a
            # genuine check pass (exit 0) from a killed-verify lucky pass
            # flagged via the evidence gate.
            "verify_exit_code": verify_result.get("exit_code", -1),
            "size": size,
            "manifest": manifest,
            "diff": cloud_diff,
            "manifest_error": manifest_error,
            "stage_seconds": stage_seconds,
        }


@task(name="cloud-record")
def cloud_record_task(
    config,
    verify_data: dict,
    sync_result: dict,
):
    """Record cloud sync results to ManifestDB."""
    db = ManifestDB.from_config(config)
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
def lan_preflight_task(config, artefacts_dir: str | None = None):
    """Run robocopy /L dry-run before real sync (full listing kept in run_logs)."""
    logger.info(f"LAN preflight: validating {config.paths.source_drive} → {config.paths.lan_destination}")
    result = run_lan_dry_run(
        source=config.paths.source_drive,
        dest=config.paths.lan_destination,
        # F8: the dry-run /L walk scales with dataset size (1M files ~= 5-10 min
        # over SMB); the old hardcoded 300s failed at the scale this deployment
        # targets. Config-driven so the operator can tune per environment.
        timeout=config.lan.dry_run_timeout_seconds,
        smb_attempts=config.lan.preflight_smb_attempts,
        smb_wait_seconds=config.lan.preflight_smb_wait_seconds,
        smb_connect_timeout_seconds=config.wol.smb_connect_timeout_seconds,
        artefacts=RunArtefacts.open(artefacts_dir) if artefacts_dir else None,
        log_tail_kb=config.lan.log_tail_kb,
    )
    if not result["ok"]:
        raise faults.coded(RuntimeError(f"LAN preflight failed: {result['error']}"),
                           "AAM-LAN-01")
    return result


@task(name="lan-snapshot-before")
def lan_snapshot_before_task(config):
    """Snapshot LAN destination before sync for diff comparison.

    Returns None when the walk fails: the destination file count is then
    UNKNOWN, and the guards fail the run (M2 2.2, G3 needs it). An empty
    dict would be indistinguishable from a genuinely empty destination.
    """
    logger.info("Taking LAN snapshot (before sync)")
    try:
        before = snapshot_to_dict(walk_lan_destination(config.paths.lan_destination))
        logger.info(f"LAN snapshot: {len(before)} files before sync")
        return before
    except Exception as e:
        logger.error(f"Pre-sync destination walk failed: {e} - destination file count unknown")
        return None


def _apply_approval(config, leg: str, approval_id: str | None, run_id: str | None,
                    results: list) -> tuple[frozenset, dict]:
    """Which blocked guards a human approved for THIS run (plan §6.2).

    Returns (approved guard names, record additions). The approval is
    consumed atomically here - before any transfer - so it can never cover
    a second run. No approval_id (every scheduled run) -> nothing approved.
    XR-1/XR-2: a retry of the SAME run re-checks the approval it already used
    (same numbers rule, cumulative deletions) without consuming it again.
    """
    blocked = [r for r in results if not r.passed]
    if not blocked or not approval_id:
        return frozenset(), {}
    db = ManifestDB.from_config(config)
    try:
        request = db.get_approval(approval_id)
        if request is not None and request.get("leg") != leg:
            logger.info(f"Approval {approval_id} is for the {request.get('leg')} leg - "
                        f"not applied to {leg}")
            return frozenset(), {}
        own_retry = (request is not None and run_id is not None
                     and request.get("used_by_run_id") == run_id)
        decision = approvals.evaluate_request(
            {**request, "used_at": None} if own_retry else request,
            leg=leg, blocked=blocked, now_iso=now_iso(),
            tolerance_percent=config.approvals.tolerance_percent)
        if decision.allowed and not own_retry and not db.mark_approval_used(
                approval_id, run_id=run_id, used_at=now_iso()):
            decision = approvals.ApprovalDecision(
                False, "approval already used by another run")
    finally:
        db.close()
    if not decision.allowed:
        logger.error(f"Approval {approval_id} not applied ({leg}): {decision.reason}")
        return frozenset(), {"approval_id": approval_id, "approval_rejected": decision.reason}
    logger.warning(f"Approval {approval_id} used by run {run_id} ({leg}): "
                   f"{', '.join(r.guard for r in blocked)} overridden once")
    return frozenset(r.guard for r in blocked), {"approval_id": approval_id}


def _open_block_request(config, leg: str, run_id: str, guard_record: dict,
                        occurred_at: str, artefacts_dir: str | None = None) -> None:
    """BLOCKED run -> one approval request + ONE attention email (DEC-3).

    Never raises: the run row is already written, and the human must be
    told even when the request could not be stored.
    """
    blocked = [r for r in guard_record.get("results", [])
               if r.get("verdict") == guards.BLOCK
               and r.get("guard") not in guard_record.get("approved_guards", [])]
    approval_id = None
    try:
        db = ManifestDB.from_config(config)
        try:
            approval_id = db.open_approval_request(
                leg=leg, blocked_run_id=run_id, fy=guard_record.get("fy"),
                guards=[{k: r.get(k) for k in ("guard", "reason", "metrics")} for r in blocked],
                phrase=approvals.request_phrase(blocked), created_at=now_iso())
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - the alert below must still go out
        logger.error(f"Could not record the approval request for blocked run {run_id}: {e}")
    rejected = guard_record.get("approval_rejected")
    message = "\n".join(r.get("reason", "") for r in blocked)
    if rejected:
        message += f"\n\nThe approval supplied for this run was not applied: {rejected}"
    message += ("\n\nNothing was copied or deleted. Open the dashboard to review the "
                "numbers and either approve this backup once or dismiss it.")
    notify(config, AlertEvent(
        key=f"blocked:{leg}", kind="ACTION_REQUIRED", mode=leg,
        title=f"{leg.upper()} backup blocked - approval needed",
        message=message, status=f"{leg.upper()}_BLOCKED",
        fault_code=faults.blocked_guard_code(guard_record) or faults.UNKNOWN, run_id=run_id,
        artefacts_dir=artefacts_dir,
        details={"Blocked by": ", ".join(r.get("guard", "?") for r in blocked),
                 "Run": run_id,
                 "Approval request": approval_id or "could not be recorded - see the logs"},
        occurred_at=occurred_at,
    ))


@task(name="lan-guards")
def lan_guards_task(config, fy_prefix: str, dry_run: dict, destination_files: int | None,
                    approval_id: str | None = None, run_id: str | None = None,
                    already_deleted: int = 0) -> dict:
    """Data-safety guards G1-G4 before robocopy /MIR (plan WP3, M2 2.2).

    approval_id: dashboard re-run of a blocked run (plan §6.2); used up here
    if it validly covers every guard that blocks again. An approved G5
    (missing mount marker) creates the marker before the mirror runs.

    A measurement that could not be taken FAILS the run (retried next
    night); a guard verdict BLOCKs it for a human (GuardBlocked). No Prefect
    retries: re-measuring the same state gives the same verdict.
    already_deleted (XR-2): robocopy deletions of earlier attempts of this
    run - G3 covers the whole run, not each attempt.
    """
    source = config.paths.source_drive
    dest = config.paths.lan_destination
    counts = (dry_run or {}).get("counts")
    planned_deletes = (dry_run or {}).get("planned_deletes")
    if not counts or planned_deletes is None:
        raise RuntimeError(
            "LAN guard measurement failed: the robocopy /L job summary is missing or "
            "unreadable, so planned deletions cannot be counted - refusing to mirror")
    if planned_deletes > counts["extras"]:
        # Every listed *EXTRA File is also in the Extras column; more lines
        # than the column means the listing cannot be trusted.
        raise RuntimeError(
            f"LAN guard measurement failed: robocopy /L listing is inconsistent "
            f"({planned_deletes} '*EXTRA File' lines but Extras column = "
            f"{counts['extras']}) - refusing to mirror")
    if destination_files is None:
        raise RuntimeError(
            f"LAN guard measurement failed: the destination could not be listed ({dest}), "
            "so the share of files a mirror would delete is unknown - refusing to mirror")

    db = ManifestDB.from_config(config)
    try:
        baseline = db.guard_baseline("lan", fy_prefix) or {}
    finally:
        db.close()

    limits = config.guards
    results = [
        guards.g1_source_has_files(
            guards.first_regular_file(source, guards.SOURCE_IGNORE_NAMES), source),
        guards.g2_source_shrink(counts["total"], baseline.get("source_file_count"),
                                limits.max_source_shrink_percent),
        guards.g4_destination_identity(
            dest, baseline.get("destination"),
            guards.resolve_host(guards.unc_host(dest)), guards.local_addresses(),
            limits.forbid_local_destination),
        guards.g3_mass_delete(planned_deletes, destination_files,
                              limits.max_delete_files, limits.max_delete_percent,
                              already_deleted=already_deleted),
        guards.g5_canary(dry_run.get("canary_present"), destination_files > 0, dest),
    ]
    approved, approval_record = _apply_approval(config, "lan", approval_id, run_id, results)
    record = guards.enforce(
        "lan", results, approved_guards=approved, fy=fy_prefix,
        source_file_count=counts["total"], destination=dest,
        destination_files=destination_files, **approval_record,
        **({"already_deleted": already_deleted} if already_deleted else {}),
    )
    if "G5_CANARY" in approved:
        marker = os.path.join(dest, guards.MOUNT_MARKER)
        try:
            with open(marker, "a", encoding="utf-8"):
                pass
        except OSError as e:
            raise RuntimeError(
                f"Approved, but the mount marker {marker} could not be created: {e} - "
                "nothing was copied or deleted") from e
        logger.warning(f"Mount marker {marker} created on human approval {approval_id}")
        record["marker_created"] = True
    return record


@task(name="lan-access")
def lan_access_task(config, dry_run: dict, run_id: str):
    """Prove the rights the mirror uses (plan WP4, CL3/LA7/LA9): read a real
    source file; create/read/delete a probe at the destination; NAS free
    space for the bytes /L says will be copied. Runs AFTER the guards, so
    nothing is ever written to a destination whose mount marker is not
    confirmed. A missing right FAILS the run."""
    dest = config.paths.lan_destination
    permissions.probe_source_read(config.paths.source_drive)
    permissions.probe_destination_write(dest, run_id)
    permissions.check_destination_free_space(
        dest, bytes_needed=((dry_run or {}).get("bytes") or {}).get("copied"),
        min_free_gb=config.lan.min_free_dest_gb)


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
            f"Post-sync destination walk failed: {e} - sync result is "
            "UNAFFECTED; diff metrics and DB record are skipped for this run"
        )
        return None
    logger.info(f"LAN snapshot: {len(after)} files after sync")
    return after


@task(name="lan-sync")
def lan_sync_task(config, artefacts_dir: str | None = None, budget: RunBudget | None = None):
    """Run robocopy /MIR mirror sync.

    WP14: with a run budget robocopy gets what is left of it (minus the
    post-sync reserve); being stopped there is LAN_PARTIAL with
    `budget_stopped` - the next run continues the mirror, never a retry.
    """
    logger.info(f"LAN sync: {config.paths.source_drive} → {config.paths.lan_destination}")
    timeout = config.lan.subprocess_timeout_seconds
    limited = False
    if budget is not None:
        reserve = config.budget.lan_post_reserve_minutes * 60
        try:
            limited = budget.binding(timeout, reserve)
            timeout = int(budget.cap(timeout, reserve))
        except BudgetExceeded as e:
            logger.error(str(e))
            return {"status": "LAN_PARTIAL", "exit_code": -1, "budget_stopped": True,
                    "files_failed": 0, "counts": None, "bytes": None, "files_deleted": None,
                    "error": _budget_stop_text(budget, "no time was left for the mirror")}
    result = run_lan_sync(
        source=config.paths.source_drive,
        dest=config.paths.lan_destination,
        lan_config=config.lan,
        artefacts=RunArtefacts.open(artefacts_dir) if artefacts_dir else None,
        timeout_s=timeout,
    )
    if limited and result.get("timed_out"):
        logger.warning("LAN mirror stopped at the run budget")
        return {**result, "status": "LAN_PARTIAL", "budget_stopped": True,
                "error": _budget_stop_text(budget, "robocopy was stopped before the mirror "
                                                   "finished (full log kept)")}
    if result["status"] in ("LAN_FAILED", "LAN_SUSPECT"):
        raise SyncFailed(result.get("error") or "LAN sync failed", result)
    return result


@task(name="lan-record")
def lan_record_task(
    config,
    sync_result: dict,
    before_dict: dict,
    after_dict: dict,
):
    """Compute diff from before/after snapshots, record to ManifestDB."""
    diff = diff_snapshots(before_dict, after_dict)

    db = ManifestDB.from_config(config)
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
def lan_shutdown_task(config, artefacts_dir: str | None = None) -> dict | None:
    """Shut the NAS down and verify it went down (WP10).

    Returns the result as metrics (None when shutdown is disabled). Never
    raises: the backup's own status stays truthful and separate; a shutdown
    that is not verified is a WARNING email + a dashboard flag.
    """
    if not config.lan.shutdown_after_backup or not config.wol.enabled:
        logger.info("LAN shutdown disabled, skipping")
        return None
    server_ip = config.wol.server_ip
    logger.info(f"Shutting down backup server {server_ip}")
    try:
        artefacts = (RunArtefacts.open(artefacts_dir) if artefacts_dir
                     else RunArtefacts.ephemeral("nas-shutdown"))
        result = shutdown_from_config(config, artefacts)
    except Exception as e:  # noqa: BLE001 - recorded, never raised
        logger.error(f"NAS shutdown could not be carried out: {e}")
        result = ShutdownResult(SHUTDOWN_FAILED, server_ip, None,
                                f"shutdown could not be carried out: {e}", 0.0)
    if not result.verified:
        notify(config, AlertEvent(
            key=f"nas_shutdown:{server_ip}", kind="WARNING", mode="lan",
            title=f"NAS shutdown {result.outcome.replace('_', ' ').lower()}",
            message=(f"The NAS {server_ip} was not confirmed powered off after the backup: "
                     f"{result.detail}. The backup result itself is unaffected."),
            status=f"NAS_SHUTDOWN_{result.outcome}", exit_code=result.exit_code,
            fault_code="AAM-NAS-06",
        ))
    return result.to_metrics()


def _summary_counts(files_copied: int | None, bytes_copied: int | None) -> tuple[str, str]:
    """Run-summary text for tool counts; None (counts unavailable) reads 'unknown'."""
    files = "unknown" if files_copied is None else str(files_copied)
    if bytes_copied is None:
        return files, "unknown"
    return files, f"{bytes_copied / (1024 * 1024):.2f} MB` (`{bytes_copied}` bytes)"


@task(name="cloud-publish-artifact")
def cloud_publish_artifact_task(verify_data: dict, sync_result: dict,
                                files_copied: int | None, bytes_copied: int | None):
    """Publish a beautiful Markdown summary of the Cloud Backup to the Prefect Console."""
    try:
        from prefect.artifacts import create_markdown_artifact
        status = sync_result.get("status", "UNKNOWN")
        exit_code = sync_result.get("exit_code", -1)
        files_text, volume_text = _summary_counts(files_copied, bytes_copied)
        total_files = verify_data.get('size', {}).get('count', 0)
        total_space_gb = verify_data.get('size', {}).get('bytes', 0) / (1024 * 1024 * 1024)
        size_check_str = "✅ Passed" if verify_data.get("verified") else "❌ Failed/Skipped"
        
        markdown_content = (
            f"# ☁️ AAM Cloud Backup Run Summary\n\n"
            f"## 📊 Performance & Execution Metrics\n"
            f"* **Status:** `{status}` (Exit Code: `{exit_code}`)\n"
            f"* **Files Transferred:** `{files_text}` files\n"
            f"* **Volume Transferred:** `{volume_text}`\n\n"
            f"## 📁 Storage Metrics (GCS Bucket)\n"
            f"* **Total Tracked Files:** `{total_files}` files\n"
            f"* **Total Space Consumed:** `{total_space_gb:.3f} GB`\n\n"
            f"## 📏 Size check\n"
            f"* **Size check (file sizes only, not content):** {size_check_str}\n"
            f"* Content is compared by the weekly integrity audit.\n"
        )
        create_markdown_artifact(
            markdown=markdown_content,
            key="cloud-backup-summary"
        )
        logger.info("Published Cloud Backup Markdown Artifact to Prefect Console UI")
    except Exception as e:
        logger.warning(f"Could not publish cloud backup artifact: {e}", exc_info=True)


@task(name="lan-publish-artifact")
def lan_publish_artifact_task(sync_result: dict, diff: dict, files_copied: int | None,
                              bytes_copied: int | None, total_files: int):
    """Publish a beautiful Markdown summary of the LAN Backup to the Prefect Console."""
    try:
        from prefect.artifacts import create_markdown_artifact
        status = sync_result.get("status", "UNKNOWN")
        exit_code = sync_result.get("exit_code", -1)
        
        added = len(diff.get("added", []))
        modified = len(diff.get("modified", []))
        removed = len(diff.get("removed", []))
        files_text, volume_text = _summary_counts(files_copied, bytes_copied)
        
        markdown_content = (
            f"# 🖥️ AAM LAN Backup Run Summary\n\n"
            f"## 📊 Robocopy Differential Metrics\n"
            f"* **Status:** `{status}` (Exit Code: `{exit_code}`)\n"
            f"* **Total Differential Changes:** `{files_text}` files\n"
            f"* **Volume Transferred:** `{volume_text}`\n\n"
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

def _audit_settings(config) -> dict:
    """rclone check tuning for the integrity audit (config.audit, WP7)."""
    a = config.audit
    return {"checkers": a.checkers, "retries": a.retries,
            "retries_sleep_seconds": a.retries_sleep_seconds,
            "modify_window_seconds": a.modify_window_seconds,
            "detail_max_paths": a.detail_max_paths,
            "network_error_patterns": tuple(a.network_error_patterns)}


def _audit_outcome_text(leg: str, result: dict) -> str:
    """Truthful audit outcome: 'could not run' is never called divergence."""
    if result.get("status") == "AUDIT_INCOMPLETE":
        return (f"{leg} integrity audit COULD NOT RUN (no verdict - this is not a data "
                f"difference): {result.get('detail')}")
    return f"{leg} integrity audit FOUND DIFFERENCES: {result.get('detail')}"


def _nullable_audit_count(value):
    """Pass through a populated audit counter; preserve unknown as NULL.

    F-T4-3: a genuinely unknown count must not become a false zero in
    the manifest. Negative sentinels (legacy -1) still clamp to 0 so no
    negative ever reaches the DB (BUG-08 intent preserved).
    """
    if value is None:
        return None
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _cloud_gate_failure_message(verify_data: dict, diff: dict, size: dict) -> str:
    """Explain WHY the cloud evidence gate failed, accurately.

    F-T3-2: the old fixed template blamed "differences vs source" even
    when the diff was clean and the real cause was an abnormally
    terminated check, a missing completion summary, or incomplete
    manifest/size evidence. The fail-closed DECISION is unchanged here —
    only the primary-cause attribution. Categories, in precedence order:
      1. abnormal termination / missing completion summary (process never
         proved anything — data diffs are not the cause);
      2. content mismatch (dirty diff: missing/modified/partial);
      3. manifest/size evidence error (cannot prove the cloud state).
    The checker's own reason string is always appended verbatim.
    """
    verify_data = verify_data or {}
    diff = diff or {}
    size = size or {}
    reason = verify_data.get("verify_reason")
    termination = verify_data.get("verify_termination", "abnormal")
    completion = verify_data.get("verify_completion", False)
    added = len(diff.get("added", []))
    modified = len(diff.get("modified", []))
    removed = len(diff.get("removed", []))
    reason_suffix = f" Verify detail: {reason}." if reason else ""
    if termination != "normal" or not completion:
        primary = (
            "verification did not complete normally "
            f"(termination={termination}, completion={bool(completion)}"
            f"{f', differences={verify_data.get('verify_differences')}' if verify_data.get('verify_differences') is not None else ''})"
            f". Diff state at gate: missing-from-cloud={added}, "
            f"unexpected-in-cloud={removed}, size-changed={modified} — "
            "these counts are not the cause; the check process itself "
            "never reported a verdict (killed/crashed/truncated, F-08)."
        )
    elif added or modified or diff.get("_partial"):
        primary = (
            "rclone check found differences vs source "
            f"(missing-from-cloud={added}, "
            f"unexpected-in-cloud={removed}, "
            f"size-changed={modified}). The cloud copy may be "
            "incomplete or out of sync."
        )
    else:
        manifest_error = verify_data.get("manifest_error")
        size_error = size.get("_error") if isinstance(size, dict) else None
        primary = (
            "manifest/evidence error "
            f"(manifest_error={manifest_error!r}, size_error={size_error!r}). "
            "The cloud state could not be proven to match source."
        )
    return (
        "Cloud integrity verification FAILED after sync: "
        f"{primary}.{reason_suffix} rclone sync is resumable — the next "
        "scheduled run will re-sync the differences."
    )


def _run_cloud_pipeline(config, run_id: str, started_at: str, monotonic_start: float | None = None,
                        budget_hours: float | None = None,
                        approval_id: str | None = None, late_seconds: float = 0.0):
    """Execute cloud backup tasks sequentially. Each task is independently tracked.

    late_seconds: how long after its scheduled time the run started (plan I2); the budget
    counts from the scheduled time, so a late start gets only what is left of it."""
    # WP14: the run budget counts from the job's start (I2: its scheduled start), including
    # the slot wait
    budget = RunBudget(
        "cloud",
        (budget_hours if budget_hours is not None else config.budget.cloud_max_hours) * 3600,
        (monotonic_start if monotonic_start is not None else time.monotonic()) - late_seconds)
    _missed_if_no_budget(config, "cloud", run_id, started_at, budget,
                         config.budget.cloud_verify_reserve_minutes * 60, late_seconds,
                         monotonic_start)
    with _pipeline_slot(config, "cloud", run_id, started_at, monotonic_start):
        fy_prefix = _effective_fy_prefix(config)

        # Apply config-driven retries to tasks that benefit from retrying
        preflight = cloud_preflight_task.with_options(
            retries=config.cloud.preflight_retries,
            retry_delay_seconds=config.cloud.preflight_retry_delay_seconds,
        )
        # XR-1/XR-6: no Prefect retries for the mirror - _sync_with_retries
        # decides (cloud.max_attempts / retry_delay_seconds), re-measuring the
        # guards and lowering --max-delete by what earlier attempts deleted.
        sync = cloud_sync_task.with_options(retries=0)
        verify_report = cloud_verify_and_report_task.with_options(
            retries=config.cloud.verify_retries,
            retry_delay_seconds=config.cloud.verify_retry_delay_seconds,
        )

        # Fetch database state before sync to calculate differential transfers
        db = ManifestDB.from_config(config)
        before_dict = {}
        try:
            before_dict = db.get_cloud_synced_entries()
        except Exception as e:
            logger.warning(f"Could not fetch database state before cloud sync: {e}")
        finally:
            db.close()

        status = "CLOUD_SKIPPED"
        sync_result = {"exit_code": -1}
        sync_state: dict = {}   # XR: the latest attempt's result + per-attempt evidence
        error_msg = None
        fault_code: str | None = None   # core.faults code of a non-success run
        files_copied = 0
        bytes_copied = 0
        extended_metrics = None
        artefacts = _open_run_artefacts(config, run_id)
        artefacts_dir = str(artefacts.dir) if artefacts else None
        # Failure-path telemetry: accumulate per-stage durations as stages
        # complete so a failed run still records what was measured. Stages
        # that never execute stay absent (never fabricated).
        stage_seconds: dict = {}
        sync_seconds: float | None = None
        verify_report_seconds: float | None = None
        phase = "pre"
        guard_record = None
        alert_sent = False   # WP8: exactly one alert per failed run
        clock = None         # WP12: clock verdict from the health check

        try:
            clock = health_check_task(config, "cloud", artefacts_dir)
            preflight(config, fy_prefix, artefacts_dir)
            guard_record = cloud_guards_task(config, fy_prefix, artefacts_dir,
                                             approval_id=approval_id, run_id=run_id)
            cloud_access_task(config, run_id, artefacts_dir)
            phase = "sync"
            _t_sync = time.monotonic()
            caps = {"max_delete": guard_record["max_delete"]}

            def remeasure(already_deleted: int) -> dict:
                nonlocal guard_record
                guard_record = cloud_guards_task(
                    config, fy_prefix, artefacts_dir, approval_id=approval_id,
                    run_id=run_id, already_deleted=already_deleted)
                cloud_access_task(config, run_id, artefacts_dir)
                caps["max_delete"] = guard_record["max_delete"]
                return guard_record

            def deletes_of(result: dict) -> int | None:
                stats = result.get("stats")
                if isinstance(stats, dict) and type(stats.get("deletes")) is int:
                    return stats["deletes"]
                # rclone deletes exactly --max-delete files before it stops (F2)
                return caps["max_delete"] if result.get("max_delete_reached") else None

            sync_result = _sync_with_retries(
                "cloud",
                run_once=lambda: sync(config, fy_prefix, artefacts_dir,
                                      max_delete=caps["max_delete"], budget=budget),
                remeasure=remeasure, deletes_of=deletes_of,
                retryable=lambda r: r.get("retryable") is True,
                max_attempts=config.cloud.max_attempts,
                delay_s=config.cloud.retry_delay_seconds, budget=budget,
                reserve_s=config.budget.cloud_verify_reserve_minutes * 60, state=sync_state)
            sync_seconds = round(time.monotonic() - _t_sync, 1)
            stage_seconds["sync_s"] = sync_seconds
            status = sync_result["status"]
            if sync_result.get("budget_stopped") or sync_result.get("time_limit_stopped"):
                # WP14 / XR-5: the upload stopped at the run budget or at rclone's
                # own --max-duration. Not verified (the bucket is knowingly
                # incomplete) and not recorded as bucket state; the next run
                # continues. One PARTIAL alert.
                phase = "post"
                error_msg = sync_result["error"]
                stats = sync_result.get("stats")
                if isinstance(stats, dict):
                    files_copied, bytes_copied = stats.get("transfers"), stats.get("bytes")
                else:
                    files_copied = bytes_copied = None
                which = "run budget" if sync_result.get("budget_stopped") else "rclone time limit"
                extended_metrics = json.dumps({
                    "stage_seconds": dict(stage_seconds),
                    "verify": f"skipped - the upload stopped at the {which}",
                    "counts_source": "rclone" if isinstance(stats, dict) else "unavailable",
                    "tool_counts": {k: stats.get(k) for k in ("transfers", "bytes", "deletes",
                                                              "errors", "checks")}
                    if isinstance(stats, dict) else None,
                })
                _send_run_alert(config, "cloud", status, error_msg,
                                sync_result.get("exit_code"), started_at,
                                run_id=run_id, sync_result=sync_result,
                                artefacts_dir=artefacts_dir)
                alert_sent = True
                raise PartialRun(f"Cloud run ended {status} - {error_msg}")
            phase = "verify"
            _t_verify_report = time.monotonic()
            verify_data = verify_report(config, fy_prefix, budget=budget,
                                        artefacts_dir=artefacts_dir)
            verify_report_seconds = round(time.monotonic() - _t_verify_report, 1)
            stage_seconds.update(verify_data.get("stage_seconds") or {})
            stage_seconds["verify_report_s"] = verify_report_seconds
            diff = verify_data.get("diff") or {}
            size = verify_data.get("size") or {}
            evidence_ok = (
                verify_data.get("verified") is True
                and verify_data.get("verify_termination", "abnormal") == "normal"
                and len(diff.get("added", [])) == 0
                and len(diff.get("modified", [])) == 0
                and not diff.get("_partial")
                and verify_data.get("manifest_error") is None
                and "_error" not in size
            )
            if not evidence_ok:
                # NOTE: rclone `+` (diff["added"]) = missing from cloud,
                # `-` (diff["removed"]) = extra in cloud (C-F1INV-002 fix).
                # F-T3-2: the message attributes the PRIMARY failure
                # category (abnormal termination vs mismatch vs evidence
                # error); the fail-closed decision itself is unchanged.
                verify_err = _cloud_gate_failure_message(verify_data, diff, size)
                status = "CLOUD_VERIFY_FAILED"
                error_msg = verify_err
                logger.error(verify_err)
                _send_run_alert(config, "cloud", status, verify_err,
                                sync_result.get("exit_code"), started_at,
                                run_id=run_id, sync_result=sync_result,
                                artefacts_dir=artefacts_dir)
                alert_sent = True
                raise RuntimeError(verify_err)

            phase = "post"

            # BUG-01: ManifestDB record MUST only happen after evidence_ok passes.
            # M6: a failed manifest query is NEVER recorded as bucket state.
            manifest_error = verify_data.get("manifest_error")
            if manifest_error:
                logger.error(
                    f"Cloud manifest query failed ({manifest_error}) - file-level "
                    "DB record and transfer metrics are SKIPPED this run; the "
                    "integrity verification above is unaffected."
                )
            else:
                cloud_record_task(
                    config, verify_data, sync_result,
                )

            # WP6: the numbers are rclone's OWN accounting (final stats line).
            # The bucket-vs-DB diff is only a cross-check (it re-derived "579
            # files copied" on no-change nights when the baseline was wrong).
            diff_copied = _cloud_diff_count(verify_data.get("manifest", []), before_dict)
            stats = sync_result.get("stats")
            if isinstance(stats, dict):
                files_copied, bytes_copied = stats.get("transfers"), stats.get("bytes")
                counts_source = "rclone"
            else:
                files_copied = bytes_copied = None
                counts_source = "unavailable"
                logger.warning("rclone final stats missing - files/bytes copied recorded as unknown")
            metrics_consistent = files_copied is not None and diff_copied == files_copied
            if files_copied is not None and not metrics_consistent:
                logger.warning(f"Cloud metrics cross-check: rclone uploaded {files_copied} file(s), "
                               f"the bucket-vs-DB diff shows {diff_copied} - the tool count is "
                               "recorded; the DB baseline was stale")

            extended_metrics = json.dumps({
                "stage_seconds": dict(stage_seconds),
                "verified": verify_data.get("verified", False),     # old key, kept for readers
                # WP7: the daily check compares sizes only - named for what it is
                "size_check_passed": verify_data.get("verified", False),
                # F-08: liveness is the check process's own completion
                # evidence, never the killable exit code. A killed check
                # (TerminateProcess with exit 0) reports abnormal
                # termination, so verify_liveness is false and — via the
                # gate above — the run itself fails closed.
                "verify_liveness": bool(verify_data.get("verified", False))
                and verify_data.get("verify_termination", "abnormal") == "normal",
                "total_files": verify_data.get("size", {}).get("count", 0),
                "total_size_gb": verify_data.get("size", {}).get("bytes", 0) / (1024 * 1024 * 1024),
                # M6: record WHY numbers may be zero on a degraded run
                "manifest_ok": manifest_error is None,
                "size_ok": "_error" not in (verify_data.get("size") or {}),
                "diff_partial": bool((verify_data.get("diff") or {}).get("_partial")),
                "counts_source": counts_source,
                "tool_counts": {k: stats.get(k) for k in ("transfers", "bytes", "deletes",
                                                          "errors", "checks")}
                if isinstance(stats, dict) else None,
                "diff_copied": diff_copied,
                "metrics_consistent": metrics_consistent,
            })
            try:
                cloud_publish_artifact_task(verify_data, sync_result, files_copied, bytes_copied)
            except Exception as e:
                # The Prefect summary is cosmetic; the run row is the record.
                logger.warning(f"Cloud run summary not published to Prefect: {e}")

            logger.info(f"Cloud pipeline completed successfully: {files_copied} files, {bytes_copied} bytes copied")
            if status == "CLOUD_PARTIAL":
                # Transient rclone outcome (rc 4/5/10) over a destination the
                # evidence gate just approved: data-true, but incomplete work
                # remains. Alert specifically, then fail via the terminal
                # policy below.
                _send_run_alert(
                    config, "cloud", status,
                    f"Cloud backup PARTIAL: rclone exit {sync_result.get('exit_code')} - some "
                    "work did not complete. rclone sync is resumable; the next scheduled run "
                    "continues from where this one left off.",
                    sync_result.get("exit_code"), started_at,
                    run_id=run_id, sync_result=sync_result,
                                artefacts_dir=artefacts_dir)
                alert_sent = True
            # Terminal-state policy (single authoritative decision point):
            # a non-success status must never return normally (which the
            # engine would record as Prefect COMPLETED).
            if status not in _CLOUD_OK_STATUSES:
                raise PartialRun(
                    f"Cloud run ended {status} - incomplete/interrupted, "
                    "not a successful backup"
                )
            return {"status": status, "exit_code": sync_result.get("exit_code", 0)}

        except Exception as e:
            error_msg = str(e)
            # XR: a failed attempt's own verdict (exit code, FAILED / SUSPECT)
            sync_result = sync_state.get("result", sync_result)
            current = sync_result.get("status", status) if phase == "sync" else status
            status = statuses.decide_terminal_status("cloud", phase, current, _exception_kind(e))
            fault_code = faults.classify_run(
                "cloud", status, exc=e, sync_result=sync_result,
                guard_record=e.record if isinstance(e, guards.GuardBlocked) else guard_record)
            if isinstance(e, guards.GuardBlocked):
                guard_record = e.record   # its ACTION REQUIRED email goes out below
            elif not alert_sent:
                _send_run_alert(config, "cloud", status,
                                _failure_alert_text("cloud", status, error_msg),
                                sync_result.get("exit_code"), started_at,
                                run_id=run_id, fault_code=fault_code,
                                artefacts_dir=artefacts_dir)
                alert_sent = True
            if extended_metrics is None:
                # Failure-path telemetry: persist whatever stages were
                # actually measured (possibly none). Business state above
                # is unchanged; this only preserves evidence for tuning.
                extended_metrics = _failure_telemetry(stage_seconds, phase)
            if phase in ("verify", "post"):
                # WP5: the transfer's own verdict survives a later failure.
                extended_metrics = _merge_metrics(extended_metrics,
                                                  sync_status=sync_result.get("status"))
            raise
        finally:
            _finalize_run_artefacts(config, artefacts)
            extended_metrics = _merge_metrics(extended_metrics, budget=_budget_metrics(
                budget, sync_result))
            if sync_state.get("attempts"):
                extended_metrics = _merge_metrics(extended_metrics,
                                                  sync_attempts=sync_state["attempts"])
            if clock is not None:
                extended_metrics = _merge_metrics(extended_metrics, clock=clock)
            extended_metrics = _with_guards(extended_metrics, guard_record)
            extended_metrics = _with_artefacts(extended_metrics, artefacts)
            if fault_code:
                extended_metrics = _merge_metrics(extended_metrics, fault_code=fault_code)
            try:
                _record_run(
                    config, run_id, "cloud", started_at, status,
                    sync_result.get("exit_code", -1), error_msg,
                    files_copied=files_copied,
                    bytes_copied=bytes_copied,
                    files_failed=0,
                    extended_metrics=extended_metrics,
                    monotonic_start=monotonic_start,
                )
            except Exception as rec_err:
                logger.critical(f"Failed to record cloud run {run_id}: {rec_err}")
            if status == "CLOUD_BLOCKED" and guard_record:
                _open_block_request(config, "cloud", run_id, guard_record, started_at,
                                    artefacts_dir)
            elif statuses.is_success(status):
                _send_recovery_if_needed(config, "cloud", run_id)


# ═══════════════════════════════════════════════════════════════
# LAN pipeline orchestrator
# ═══════════════════════════════════════════════════════════════

def _complete_pending_rollover_nas_folder(config) -> None:
    """Plan A7 (+ F-9): finish a rollover's `nas_folder` step from the journal.

    Only when the config switch is done, `paths.lan_destination` IS the journal's new FY
    folder and the share root exists (create_nas_folder refuses otherwise). Never raises and
    never blocks the backup: on failure the step stays failed and G5 behaves as before.
    """
    from core import rollover_journal as rj

    try:
        db = ManifestDB.from_config(config)
        try:
            journal = db.rollover_incomplete()
            if journal is None:
                return
            steps = journal["steps"]
            if (rj.is_finished(steps[rj.NAS_FOLDER])
                    or steps[rj.CONFIG_SWITCH]["state"] != rj.DONE
                    or fy_rollover._fy_name(config.paths.lan_destination) != journal["to_fy"]):
                return
            from_fy, to_fy = journal["from_fy"], journal["to_fy"]
            db.rollover_step_start(from_fy, to_fy, rj.NAS_FOLDER, now_iso())
            try:
                fy_rollover.create_nas_folder(
                    fy_rollover._parent_path(config.paths.lan_destination), to_fy)
            except OSError as e:
                logger.warning(f"FY rollover: NAS folder for {to_fy} still not possible: {e}")
                db.rollover_step_failed(from_fy, to_fy, rj.NAS_FOLDER, now_iso(), error=str(e))
                return
            db.rollover_step_done(from_fy, to_fy, rj.NAS_FOLDER, now_iso())
            if not rj.is_finished(steps[rj.NAS_SHUTDOWN]):
                db.rollover_step_skipped(from_fy, to_fy, rj.NAS_SHUTDOWN, now_iso(),
                                         reason="this LAN run's own shutdown follows")
            logger.info(f"FY rollover: NAS folder for {to_fy} completed by the LAN run")
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - bookkeeping must never stop a backup
        logger.error(f"FY rollover: pending NAS folder step not completed: {e}")


def _run_lan_pipeline(config, run_id: str, started_at: str, monotonic_start: float | None = None,
                       budget_hours: float | None = None,
                      approval_id: str | None = None, allow_shutdown: bool = True,
                      late_seconds: float = 0.0):
    """Execute LAN backup tasks sequentially. Each task is independently tracked.

    allow_shutdown=False (the final backup of an FY rollover, plan A4): the NAS is NOT powered
    off at any call site - the new NAS FY folder is created after this run.
    late_seconds: see _run_cloud_pipeline (plan I2)."""
    # WP14: the run budget counts from the job's start (I2: its scheduled start), including
    # the slot wait
    budget = RunBudget(
        "lan",
        (budget_hours if budget_hours is not None else config.budget.lan_max_hours) * 3600,
        (monotonic_start if monotonic_start is not None else time.monotonic()) - late_seconds)
    _missed_if_no_budget(config, "lan", run_id, started_at, budget,
                         config.budget.lan_post_reserve_minutes * 60, late_seconds,
                         monotonic_start)
    with _pipeline_slot(config, "lan", run_id, started_at, monotonic_start):

        # Apply config-driven retries to tasks that benefit from retrying
        preflight = lan_preflight_task.with_options(
            retries=config.lan.preflight_retries,
            retry_delay_seconds=config.lan.preflight_retry_delay_seconds,
        )
        # XR-2/XR-6: no Prefect retries for the mirror - _sync_with_retries
        # decides (lan.max_attempts / retry_delay_seconds) and re-runs the
        # robocopy /L measurement + guards before any repeat of /MIR.
        sync = lan_sync_task.with_options(retries=0)

        status = "LAN_SKIPPED"
        sync_result = {"exit_code": -1}
        sync_state: dict = {}   # XR: the latest attempt's result + per-attempt evidence
        error_msg = None
        fault_code: str | None = None   # core.faults code of a non-success run
        files_copied = 0
        bytes_copied = 0
        files_failed = 0
        extended_metrics = None
        artefacts = _open_run_artefacts(config, run_id)
        artefacts_dir = str(artefacts.dir) if artefacts else None
        # Failure-path telemetry (same contract as cloud): measured stages
        # persist even when the run fails; unexecuted stages stay absent.
        stage_seconds: dict = {}
        phase = "pre"
        guard_record = None
        alert_sent = False   # WP8: exactly one alert per failed run
        nas_shutdown = None  # WP10: verified shutdown result, if one was attempted

        try:
            health_check_task(config, "lan", artefacts_dir)
            wol_check_task(config)
            # plan A7: a rollover whose NAS folder could not be made (NAS was off) is
            # completed now that the NAS is awake - before G5 looks for the mount marker
            _complete_pending_rollover_nas_folder(config)
            dry_run = preflight(config, artefacts_dir)
            before_dict = lan_snapshot_before_task(config)
            guard_record = lan_guards_task(
                config, _effective_fy_prefix(config), dry_run,
                None if before_dict is None else guards.count_backup_files(before_dict),
                approval_id=approval_id, run_id=run_id,
            )
            lan_access_task(config, dry_run, run_id)
            phase = "sync"
            _t_sync = time.monotonic()

            def remeasure(already_deleted: int) -> dict:
                nonlocal guard_record
                fresh = preflight(config, artefacts_dir)
                listing = lan_snapshot_before_task(config)
                guard_record = lan_guards_task(
                    config, _effective_fy_prefix(config), fresh,
                    None if listing is None else guards.count_backup_files(listing),
                    approval_id=approval_id, run_id=run_id, already_deleted=already_deleted,
                )
                lan_access_task(config, fresh, run_id)
                return guard_record

            sync_result = _sync_with_retries(
                "lan",
                run_once=lambda: sync(config, artefacts_dir, budget=budget),
                remeasure=remeasure,
                deletes_of=lambda r: (r.get("files_deleted")
                                      if type(r.get("files_deleted")) is int else None),
                retryable=lambda r: (r.get("status") == "LAN_FAILED"
                                     and not r.get("timed_out")),
                max_attempts=config.lan.max_attempts,
                delay_s=config.lan.retry_delay_seconds, budget=budget,
                reserve_s=config.budget.lan_post_reserve_minutes * 60, state=sync_state)
            sync_seconds = round(time.monotonic() - _t_sync, 1)
            stage_seconds["sync_s"] = sync_seconds
            status = sync_result["status"]
            phase = "post"
            # F12: robocopy per-file failure count (0 on clean/anomaly runs)
            files_failed = int(sync_result.get("files_failed", 0))
            # WP6: the numbers are robocopy's OWN summary (Files / Bytes
            # "Copied"); unknown when the summary is missing.
            tool_files = (sync_result.get("counts") or {}).get("copied")
            tool_bytes = (sync_result.get("bytes") or {}).get("copied")
            files_copied, bytes_copied = tool_files, tool_bytes
            tool_metrics = {
                "counts_source": "robocopy" if tool_files is not None else "unavailable",
                "files_deleted": sync_result.get("files_deleted"),
            }
            after_dict = lan_snapshot_after_task(config)
            if after_dict is not None:
                lan_record_task(
                    config, sync_result, before_dict, after_dict,
                )

                # Cross-check only: the before/after walk diff vs the tool count.
                diff = diff_snapshots(before_dict, after_dict)
                diff_copied = len(diff.get("added", [])) + len(diff.get("modified", []))
                metrics_consistent = tool_files is not None and diff_copied == tool_files
                if tool_files is not None and not metrics_consistent:
                    logger.warning(f"LAN metrics cross-check: robocopy copied {tool_files} file(s), "
                                   f"the destination walk diff shows {diff_copied} - the tool "
                                   "count is recorded")
                extended_metrics = json.dumps({
                    "added": len(diff.get("added", [])),
                    "modified": len(diff.get("modified", [])),
                    "removed": len(diff.get("removed", [])),
                    "total_files": len(after_dict),
                    "stage_seconds": dict(stage_seconds),
                    **tool_metrics,
                    "diff_copied": diff_copied,
                    "metrics_consistent": metrics_consistent,
                })
                try:
                    lan_publish_artifact_task(sync_result, diff, files_copied, bytes_copied, len(after_dict))
                except Exception as e:
                    # The Prefect summary is cosmetic; the run row is the record.
                    logger.warning(f"LAN run summary not published to Prefect: {e}")
            else:
                # G14: walk unavailable - the tool counts are still recorded.
                extended_metrics = json.dumps({"stage_seconds": dict(stage_seconds),
                                               **tool_metrics})
                logger.error(
                    "Post-sync snapshot unavailable - diff metrics and file-level "
                    "DB record skipped for this run; the sync outcome is recorded "
                    "as-is and the next run re-derives the metrics."
                )

            logger.info(f"LAN pipeline completed with status {status}")
            # F3: shut the NAS down ONLY after a fully complete mirror (robocopy
            # exit 0-3). A PARTIAL run (exit 4-15) means some files did not land —
            # powering off the NAS now strands them until the next WoL, and the
            # partial state must be reported, not silent.
            if status == "LAN_COMPLETE":
                if allow_shutdown:
                    nas_shutdown = lan_shutdown_task(config, artefacts_dir)
            elif status == "LAN_PARTIAL":
                shutdown_on_fail = (allow_shutdown and getattr(
                    config.lan, "shutdown_on_all_retries_exhausted", False) is True)
                nas_msg = (
                    "The NAS is scheduled for shutdown as shutdown_on_all_retries_exhausted is enabled."
                    if shutdown_on_fail
                    else "The NAS was NOT shut down; the next scheduled run will re-sync."
                )
                logger.error(
                    f"LAN backup PARTIAL (robocopy exit {sync_result.get('exit_code')}): "
                    f"some files were not copied. {nas_msg}"
                )
                # The alert goes out BEFORE any NAS shutdown below.
                partial_text = (
                    f"LAN backup PARTIAL: {sync_result['error']} {nas_msg}"
                    if sync_result.get("budget_stopped") else
                    f"LAN backup PARTIAL: robocopy exit code {sync_result.get('exit_code')} - "
                    f"some files were not copied. {nas_msg} {sync_result.get('error') or ''}"
                ).strip()
                if sync_result.get("budget_stopped"):
                    error_msg = sync_result["error"]
                _send_run_alert(config, "lan", status, partial_text,
                                sync_result.get("exit_code"), started_at,
                                run_id=run_id, sync_result=sync_result,
                                artefacts_dir=artefacts_dir)
                alert_sent = True
                if shutdown_on_fail:
                    logger.info("shutdown_on_all_retries_exhausted enabled: powering down NAS after partial backup alert")
                    nas_shutdown = lan_shutdown_task(config, artefacts_dir)
            else:
                logger.info(f"LAN shutdown skipped - status is {status}")
            # Terminal-state policy (single authoritative decision point):
            # a non-success status must never return normally (which the
            # engine would record as Prefect COMPLETED).
            if status not in _LAN_OK_STATUSES:
                raise PartialRun(
                    f"LAN run ended {status} - incomplete/interrupted, "
                    "not a successful backup"
                )
            return {"status": status, "exit_code": sync_result.get("exit_code", 0)}

        except Exception as e:
            error_msg = str(e)
            # XR: a failed attempt's own verdict (exit code, FAILED / SUSPECT)
            sync_result = sync_state.get("result", sync_result)
            current = sync_result.get("status", status) if phase == "sync" else status
            status = statuses.decide_terminal_status("lan", phase, current, _exception_kind(e))
            if isinstance(e, guards.GuardBlocked):
                guard_record = e.record
            fault_code = faults.classify_run("lan", status, exc=e, guard_record=guard_record,
                                             sync_result=sync_result)
            shutdown_on_fail = (allow_shutdown and getattr(
                config.lan, "shutdown_on_all_retries_exhausted", False) is True)
            if not alert_sent and not isinstance(e, guards.GuardBlocked):
                # The one alert of this run, sent BEFORE any NAS shutdown below.
                nas_msg = (
                    "The NAS is scheduled for shutdown as shutdown_on_all_retries_exhausted is enabled."
                    if shutdown_on_fail
                    else "The NAS was NOT shut down; the next scheduled run will re-sync."
                )
                _send_run_alert(config, "lan", status,
                                _failure_alert_text("lan", status, error_msg, nas_msg),
                                sync_result.get("exit_code"), started_at,
                                run_id=run_id, fault_code=fault_code,
                                artefacts_dir=artefacts_dir)
                alert_sent = True
            if shutdown_on_fail and not isinstance(e, PartialRun):
                logger.info(
                    f"shutdown_on_all_retries_exhausted enabled: powering down NAS after {status} failure alert"
                )
                nas_shutdown = lan_shutdown_task(config, artefacts_dir)
            if extended_metrics is None:
                extended_metrics = _failure_telemetry(stage_seconds, phase)
            if phase == "post":
                # WP5: the transfer's own verdict survives a later failure.
                extended_metrics = _merge_metrics(extended_metrics,
                                                  sync_status=sync_result.get("status"))
            raise
        finally:
            _finalize_run_artefacts(config, artefacts)
            extended_metrics = _merge_metrics(extended_metrics, budget=_budget_metrics(
                budget, sync_result))
            if sync_state.get("attempts"):
                extended_metrics = _merge_metrics(extended_metrics,
                                                  sync_attempts=sync_state["attempts"])
            if nas_shutdown is not None:
                extended_metrics = _merge_metrics(extended_metrics, nas_shutdown=nas_shutdown)
            extended_metrics = _with_guards(extended_metrics, guard_record)
            extended_metrics = _with_artefacts(extended_metrics, artefacts)
            if fault_code:
                extended_metrics = _merge_metrics(extended_metrics, fault_code=fault_code)
            try:
                _record_run(
                    config, run_id, "lan", started_at, status,
                    sync_result.get("exit_code", -1), error_msg,
                    files_copied=files_copied,
                    bytes_copied=bytes_copied,
                    files_failed=files_failed,
                    extended_metrics=extended_metrics,
                    monotonic_start=monotonic_start,
                )
            except Exception as rec_err:
                logger.critical(f"Failed to record lan run {run_id}: {rec_err}")
            if status == "LAN_BLOCKED" and guard_record:
                _open_block_request(config, "lan", run_id, guard_record, started_at,
                                    artefacts_dir)
            elif statuses.is_success(status):
                _send_recovery_if_needed(config, "lan", run_id)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _record_run(
    config,
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
    db = ManifestDB.from_config(config)
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
            logger.warning(f"Run {run_id} ({mode}) was not recorded to database - check logs above for details")
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# Report flows
# ═══════════════════════════════════════════════════════════════

def _enabled_legs(config) -> tuple[str, ...]:
    return tuple(leg for leg in ("cloud", "lan") if getattr(config, leg).enabled)


def _send_summary_report(config, days: int, period: str) -> None:
    """Send a summary report truthfully (plan WP8): delivery is recorded in
    the notifications table and a failed delivery FAILS the flow; a period
    with no runs at all sends a STALE warning instead of skipping silently."""
    db = ManifestDB.from_config(config)
    try:
        email = report.build_summary_email(db, config.firm_name, days, period,
                                           legs=_enabled_legs(config))
        if email is None:
            _, label = report.period_runs(db, days, period)
            msg = (f"No backup runs were recorded in {label} - the {period} report "
                   "has nothing to report. Check that the backup service and its schedules are "
                   "running.")
            notify(config, AlertEvent(
                key=f"stale:{period.lower()}", kind="ACTION_REQUIRED", mode="report",
                title=f"No backups in {label}", message=msg, status="STALE",
                fault_code="AAM-RPT-01"), db=db)
            raise RuntimeError(msg)
        subject, body, attachments = email
        result = send_email(config.notifications, subject, body, attachments)
        try:
            db.record_notification({
                "event_key": f"report:{period.lower()}", "kind": "REPORT", "mode": "report",
                "subject": subject, "status": result.status, "error": result.error,
                "attempts": result.attempts, "created_at": now_iso()})
        except Exception as e:  # noqa: BLE001 - the delivery verdict below still stands
            logger.error(f"Could not record {period} report delivery: {e}")
        if result.status != SENT:
            raise RuntimeError(f"{period} report email not delivered ({result.status}): "
                               f"{result.error}")
        logger.info(f"{period} report delivered")
    finally:
        db.close()


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
        logger.info("Weekly backup report email is disabled in configuration - skipping")
        return
    _send_summary_report(config, days=7, period="Weekly")


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
        logger.info("Monthly backup report email is disabled in configuration - skipping")
        return
    _send_summary_report(config, days=30, period="Monthly")


# ═══════════════════════════════════════════════════════════════
# FY rollover check — scheduled (G10 fix)
# ═══════════════════════════════════════════════════════════════

# NSSM-rotated service logs live in the log directory next to the app logs.
_SERVICE_LOG_GLOBS = ("agent_svc-*.log", "prefect_svc-*.log", "watchdog_svc-*.log")


@task(name="log-retention")
def log_retention_task(config) -> dict:
    """Delete retained logs past their configured retention (plan WP2).

    * run artefacts (full rclone/robocopy logs): run_logs.retention_days
    * NSSM-rotated service logs: run_logs.service_log_retention_days
    App logs (backup_*.log) are rotated/retained by loguru
    (maintenance.log_retention_days). Never raises.
    """
    from core.run_artefacts import purge_older_than

    removed = {"run_logs": 0, "service_logs": 0}
    try:
        removed["run_logs"] = purge_older_than(
            config.run_logs_dir, config.run_logs.retention_days)
        for pattern in _SERVICE_LOG_GLOBS:
            removed["service_logs"] += purge_older_than(
                config.paths.log_directory,
                config.run_logs.service_log_retention_days, glob=pattern)
    except Exception as e:  # noqa: BLE001 - housekeeping must never fail the flow
        logger.warning(f"Log retention task failed (non-fatal): {e}")
    logger.info(f"Log retention: removed {removed}")
    return removed


@flow(name="rollover-check", log_prints=False)
def rollover_check_flow(config_path: str = CONFIG_PATH, approval_id: str | None = None):
    """Daily FY-rollover check (plan WP11, owner DEC-4). The single entry
    point: the boot-time rollover in launch.py is gone.

    * Forward only: today's FY must be exactly the next FY. A clock that is
      BEHIND never rolls back (probe P4) and is not approvable - fix the
      clock. More than one FY ahead needs a human (G6).
    * The final backup of the closing FY runs through the NORMAL pipelines
      (backup slot, guards, permission checks, run rows, alerts). Every
      enabled leg must end COMPLETE, otherwise G6 blocks and asks a human.
    * The rollover is a persisted step journal (`rollover_journal`, plan A): final backups ->
      source folder + placeholder -> NAS folder + mount marker -> config switch
      (compare-and-set) -> archive transition -> NAS shutdown -> INFO email. Each step is
      idempotent; this daily check is the resumer and continues at the first unfinished step
      (a failed NAS step never stops the config switch; a failed switch is ROLLOVER_FAILED +
      AAM-ROL-04, retried every morning with a repeated final backup, owner D-17).
    * Everything after the final backups runs inside one backup-slot hold.
    * One run_history row (mode "rollover") per run.

    Returns "ROLLOVER_COMPLETED" | "NO_ROLLOVER_NEEDED".
    Raises RolloverError when blocked (the Prefect run fails; the attention
    email points to the dashboard; the check repeats daily).
    """
    # Daily housekeeping rides on this daily deployment (no extra schedule).
    # Runs first so a blocked rollover can never stop log retention.
    try:
        log_retention_task(load_config(config_path))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Log retention skipped (config load failed): {e}")

    config = load_config(config_path)
    db = ManifestDB.from_config(config)
    try:
        return _rollover_resume(config, config_path, approval_id, db)
    finally:
        db.close()


def _rollover_step(db, from_fy: str, to_fy: str, step: str, run_id: str, fn):
    """Run one journal step and persist its outcome (plan A, saga).

    fn() returns None (done) or ("skipped", reason). Any exception marks the step failed
    and is returned to the caller - except a guard block, which is a human decision, not a
    failure: the step is marked failed with that text and the block is re-raised.
    Returns (finished, error).
    """
    db.rollover_step_start(from_fy, to_fy, step, now_iso(), run_id=run_id)
    try:
        outcome = fn()
    except guards.GuardBlocked as e:
        db.rollover_step_failed(from_fy, to_fy, step, now_iso(), error=f"blocked: {e}")
        raise
    except Exception as e:  # noqa: BLE001 - recorded on the journal; the caller decides
        logger.error(f"FY rollover {from_fy} -> {to_fy}: step {step} failed: {e}")
        db.rollover_step_failed(from_fy, to_fy, step, now_iso(), error=str(e))
        return False, e
    if isinstance(outcome, tuple) and outcome[0] == "skipped":
        db.rollover_step_skipped(from_fy, to_fy, step, now_iso(), reason=outcome[1])
    else:
        db.rollover_step_done(from_fy, to_fy, step, now_iso())
    return True, None


def _rollover_resume(config, config_path: str, approval_id: str | None, db):
    """The journal-driven rollover (plan A): continue the open journal at its first
    unfinished step, or open a new one when a rollover is due. See rollover_check_flow."""
    from core import rollover_journal as rj

    journal = db.rollover_incomplete()
    source, lan_dest = config.paths.source_drive, config.paths.lan_destination
    if journal is not None:
        configured = fy_rollover.configured_fy(source, lan_dest)
        switch_done = rj.is_finished(journal["steps"][rj.CONFIG_SWITCH])
        if not switch_done and configured == journal["to_fy"]:
            # R3: the config was written but the process died before the journal saw it.
            # The switch is done; a "final backup" now would back up the NEW folders.
            logger.warning(f"FY rollover {journal['from_fy']} -> {journal['to_fy']}: "
                           "config.yaml already points at the new FY - switch step closed")
            db.rollover_step_done(journal["from_fy"], journal["to_fy"], rj.CONFIG_SWITCH,
                                  now_iso())
            journal = db.rollover_get(journal["from_fy"], journal["to_fy"])
            switch_done = True
        if (switch_done and configured is not None and fy_rollover.classify_rollover(
                configured, get_fy_prefix()) in (fy_rollover.FORWARD, fy_rollover.JUMP)):
            # a NEW rollover is due: an old journal stuck on a follow-up step (archive, NAS
            # folder) must not hold the next year back
            for step in rj.attempt_order(journal):
                db.rollover_step_skipped(
                    journal["from_fy"], journal["to_fy"], step, now_iso(),
                    reason=f"superseded by the rollover to {get_fy_prefix()}")
            logger.warning(f"FY rollover {journal['from_fy']} -> {journal['to_fy']}: open "
                           "follow-up steps closed because the next rollover is due")
            journal = None
    resumed = journal is not None
    if resumed:
        from_fy, to_fy = journal["from_fy"], journal["to_fy"]
        switch_pending = not rj.is_finished(journal["steps"][rj.CONFIG_SWITCH])
        # the daily check only needs the date when the one-way switch is still to do
        kind = (fy_rollover.classify_rollover(from_fy, get_fy_prefix())
                if switch_pending else fy_rollover.FORWARD)
    else:
        from_fy = fy_rollover.configured_fy(source, lan_dest)
        if from_fy is None:
            fy_rollover.detect_rollover(source, lan_dest)   # logs the opt-out warning
            return "NO_ROLLOVER_NEEDED"
        to_fy = get_fy_prefix()
        kind = fy_rollover.classify_rollover(from_fy, to_fy)
        if kind == fy_rollover.NONE:
            return "NO_ROLLOVER_NEEDED"
        switch_pending = True

    run_id = _stable_run_id("rollover")
    started_at = now_iso()
    monotonic_start = time.monotonic()
    metrics = {"from_fy": from_fy, "to_fy": to_fy, "kind": kind, "resumed": resumed}

    def record(status: str, error: str | None, extra: dict) -> None:
        try:
            _record_run(config, run_id, "rollover", started_at, status,
                        0 if status == "ROLLOVER_COMPLETE" else -1, error,
                        extended_metrics=json.dumps({**metrics, **extra}, default=str),
                        monotonic_start=monotonic_start)
        except Exception as e:  # noqa: BLE001 - the decision itself stands
            logger.critical(f"Failed to record rollover run {run_id}: {e}")

    if switch_pending and kind == fy_rollover.CLOCK_BEHIND:
        msg = (f"FY rollover refused: the computer clock says the current FY is "
               f"{get_fy_prefix()}, which is EARLIER than the configured {from_fy}. The clock "
               "is wrong; the backups keep using " + from_fy + ". Correct the server date/time "
               "(w32tm /resync). Nothing was changed.")
        logger.error(msg)
        record("ROLLOVER_BLOCKED", msg, {})
        notify(config, AlertEvent(
            key="rollover:clock_behind", kind="ACTION_REQUIRED", mode="rollover",
            title="Server clock is behind - FY rollover refused", message=msg,
            status="ROLLOVER_BLOCKED", occurred_at=started_at,
            fault_code="AAM-CLK-03", run_id=run_id))
        raise fy_rollover.RolloverError(msg)

    # The FY switch is one-way, so the date it acts on must be proven against
    # Google / NTP first: a clock one FY ahead would otherwise roll over on its
    # own and leave the real FY unprotected. Not approvable (like CLOCK_BEHIND).
    # Only a pending switch needs it; the steps after the switch resume without.
    if switch_pending:
        clock = check_clock(
            max_skew_seconds=config.health.max_clock_skew_seconds,
            connection_timeout=config.health.clock_check_timeout_seconds,
            ntp_server=config.health.ntp_server,
            w32tm_timeout_s=config.health.w32tm_timeout_seconds,
            artefacts=RunArtefacts.ephemeral("rollover-clock"),
        )
        metrics["clock"] = clock.to_metrics()
        if clock.state != CLOCK_OK:
            msg = (f"FY rollover {from_fy} -> {to_fy} refused: the server clock could not be "
                   f"proven correct ({clock.state}: {clock.reason}). The backups keep using "
                   f"{from_fy}; nothing was changed. Correct the date/time (w32tm /resync) or "
                   "restore internet/NTP access - the check repeats every morning.")
            logger.error(msg)
            record("ROLLOVER_BLOCKED", msg, {})
            notify(config, AlertEvent(
                key=f"rollover:clock_{clock.state.lower()}", kind="ACTION_REQUIRED",
                mode="rollover", title="Server clock not proven - FY rollover refused",
                message=msg, status="ROLLOVER_BLOCKED", occurred_at=started_at,
                fault_code="AAM-ROL-03", run_id=run_id))
            raise fy_rollover.RolloverError(msg)

    journal = db.rollover_open(from_fy, to_fy, started_at)
    _record_running(config, "rollover", run_id, started_at)   # a crash is swept as ABANDONED
    for step in rj.reset_at_start(journal):                    # owner D-17
        db.rollover_step_reset(from_fy, to_fy, step)
    journal = db.rollover_get(from_fy, to_fy)
    logger.info(f"FY rollover {from_fy} -> {to_fy} ({kind}), "
                f"{'resuming' if resumed else 'starting'}: steps still to do: "
                f"{', '.join(rj.attempt_order(journal))}")

    src_root = fy_rollover._parent_path(source)
    lan_root = fy_rollover._parent_path(lan_dest)
    final_status: dict = {}
    guard_record = None
    blocking_error: tuple[str, Exception] | None = None
    config_backup = None
    archive_ok = None

    def step_final_backups():
        nonlocal final_status, guard_record
        logger.info(f"FY rollover {from_fy} -> {to_fy} ({kind}): final backup of {from_fy}")
        final_status, final_errors = _run_final_backups(config, run_id)
        g6 = guards.g6_fy_rollover(from_fy, to_fy, kind, final_status)
        approved, approval_record = _apply_approval(config, "rollover", approval_id, run_id, [g6])
        try:
            guard_record = guards.enforce("rollover", [g6], approved_guards=approved,
                                          **{k: v for k, v in metrics.items()
                                             if k in ("from_fy", "to_fy", "kind", "clock")},
                                          **approval_record)
        except guards.GuardBlocked as e:
            record("ROLLOVER_BLOCKED", str(e),
                   {"guards": e.record, "final_errors": final_errors})
            _open_block_request(config, "rollover", run_id, e.record, started_at)
            raise

    def step_source_folder():
        fy_rollover.create_source_folder(
            src_root, to_fy, placeholder_name=config.rollover.source_placeholder_name)

    def step_nas_folder():
        if not config.lan.enabled:
            return ("skipped", "LAN backup is disabled")
        if config.wol.enabled:
            ensure_server_online(config)     # wake + wait, exactly as the LAN leg does
        fy_rollover.create_nas_folder(lan_root, to_fy)

    def step_config_switch():
        nonlocal config_backup
        config_backup = fy_rollover.update_config_yaml(
            config_path, src_root, lan_root, to_fy, expect_fy=from_fy)

    def step_archive():
        nonlocal archive_ok
        if not config.cloud.enabled:
            return ("skipped", "cloud backup is disabled")
        archive_ok = fy_rollover.run_archive_transition(
            bucket=config.cloud.bucket, old_fy=from_fy,
            gcs_key_path=config.paths.gcs_key_path,
            auth_timeout=config.health.rollover_auth_timeout_seconds,
            archive_timeout=config.health.rollover_archive_timeout_seconds,
        )
        if not archive_ok:
            raise RuntimeError(f"Archive transition of {from_fy} failed - retried every morning")

    def step_nas_shutdown():
        if not (config.lan.enabled and config.lan.shutdown_after_backup
                and config.wol.enabled):
            return ("skipped", "NAS shutdown is not configured")
        lan_shutdown_task(config)

    def step_announce():
        pending = [s for s in rj.STEPS if s != rj.ANNOUNCE
                   and not rj.is_finished(db.rollover_get(from_fy, to_fy)["steps"][s])]
        todo = ("" if not pending else
                " A few follow-up steps are still being retried automatically "
                f"({', '.join(p.replace('_', ' ') for p in pending)}); your data is safe.")
        notify(config, AlertEvent(
            key=f"rollover:done:{to_fy}", kind="INFO", mode="rollover",
            title=f"New financial year {to_fy} started",
            message=(f"From today your backups use the new folder {to_fy}. The {from_fy} "
                     f"backups are kept unchanged.{todo or ' Nothing to do.'}"),
            status="ROLLOVER_COMPLETE", run_id=run_id, occurred_at=started_at))

    steps = {
        rj.FINAL_BACKUPS: step_final_backups, rj.SOURCE_FOLDER: step_source_folder,
        rj.NAS_FOLDER: step_nas_folder, rj.CONFIG_SWITCH: step_config_switch,
        rj.ARCHIVE: step_archive, rj.NAS_SHUTDOWN: step_nas_shutdown,
        rj.ANNOUNCE: step_announce,
    }

    def run_steps():
        nonlocal blocking_error
        for step in rj.attempt_order(journal):
            current = db.rollover_get(from_fy, to_fy)
            if not rj.prerequisites_met(current, step):
                skipped_req = rj.skipped_prerequisite(current, step)
                if skipped_req is not None:   # not needed either (F1): the journal can complete
                    db.rollover_step_skipped(from_fy, to_fy, step, now_iso(),
                                             reason=f"not needed: {skipped_req} was skipped")
                continue                      # e.g. NAS shutdown while the NAS was never reached
            finished, error = _rollover_step(db, from_fy, to_fy, step, run_id, steps[step])
            if not finished and step in rj.BLOCKING:
                blocking_error = (step, error)
                return
            if not finished and step == rj.ARCHIVE:
                _rollover_archive_alert(config, db, from_fy, to_fy, error, run_id, started_at)

    def fail(step: str, error) -> None:
        msg = (f"FY rollover {from_fy} -> {to_fy} stopped at step '{step.replace('_', ' ')}': "
               f"{error}. Your data is safe; the rollover continues where it stopped every "
               "morning.")
        record("ROLLOVER_FAILED", msg, {"failed_step": step})
        notify(config, AlertEvent(
            key=f"rollover:{to_fy}:{step}", kind="ACTION_REQUIRED", mode="rollover",
            title="Financial-year switch could not be finished", message=msg,
            status="ROLLOVER_FAILED", occurred_at=started_at,
            fault_code="AAM-ROL-04", run_id=run_id))
        raise fy_rollover.RolloverError(msg)

    try:
        if not rj.is_finished(journal["steps"][rj.FINAL_BACKUPS]):
            # the final backups run through the normal pipelines, which take the backup slot
            # themselves (limit 1) - they must stay OUTSIDE the slot held below
            finished, error = _rollover_step(db, from_fy, to_fy, rj.FINAL_BACKUPS, run_id,
                                             step_final_backups)
            if not finished:
                blocking_error = (rj.FINAL_BACKUPS, error)
            journal = db.rollover_get(from_fy, to_fy)
        if blocking_error is None:
            with _backup_slot(config):
                run_steps()
    except guards.GuardBlocked as e:
        raise fy_rollover.RolloverError(str(e)) from e
    except fy_rollover.RolloverError:
        raise
    except Exception as e:  # noqa: BLE001 - never leave a RUNNING row and no alert behind
        fail("unexpected", e)

    final = db.rollover_get(from_fy, to_fy)
    metrics["steps"] = {name: final["steps"][name]["state"] for name in rj.STEPS}
    if blocking_error is not None:
        fail(*blocking_error)

    record("ROLLOVER_COMPLETE", None, {"guards": guard_record, "final_status": final_status,
                                       "archive_ok": archive_ok,
                                       "config_backup": str(config_backup)})
    logger.info(f"FY rollover {from_fy} -> {to_fy}: switched | archive_ok={archive_ok} | "
                f"steps={metrics['steps']}")
    return "ROLLOVER_COMPLETED"


def _rollover_archive_alert(config, db, from_fy: str, to_fy: str, error, run_id: str,
                            started_at: str) -> None:
    """AAM-ROL-05: the archive move failed. WARNING at first; ACTION_REQUIRED once it has
    been failing for rollover.archive_alert_after_days. One stable incident per rollover."""
    step = db.rollover_get(from_fy, to_fy)["steps"]["archive"]
    first = step.get("first_failed_at") or started_at
    days = (pendulum.parse(started_at) - pendulum.parse(first)).total_days()
    escalate = days >= config.rollover.archive_alert_after_days
    notify(config, AlertEvent(
        key=f"rollover:{to_fy}:archive",
        kind="ACTION_REQUIRED" if escalate else "WARNING", mode="rollover",
        title="Old year not moved to cheaper storage yet",
        message=(f"Moving the {from_fy} online copy to the ARCHIVE storage class failed"
                 f"{f' for {int(days)} day(s)' if days >= 1 else ''}: {error}. Your data is "
                 "safe; it is tried again every morning."),
        status="ROLLOVER_COMPLETE", occurred_at=started_at, fault_code="AAM-ROL-05",
        run_id=run_id))


def _run_final_backups(config, run_id: str) -> tuple[dict, dict]:
    """Final backup of the closing FY on every enabled leg, through the normal
    pipelines (config still points at the closing FY). Returns
    ({leg: terminal status}, {leg: error})."""
    status: dict = {}
    errors: dict = {}
    for leg, enabled, pipeline in (("cloud", config.cloud.enabled, _run_cloud_pipeline),
                                   ("lan", config.lan.enabled, _run_lan_pipeline)):
        if not enabled:
            continue
        try:
            # plan A4: the LAN final backup leaves the NAS on - its new FY folder follows
            extra = {"allow_shutdown": False} if leg == "lan" else {}
            result = pipeline(config, f"{run_id}-final-{leg}", now_iso(), time.monotonic(),
                              **extra)
            status[leg] = result.get("status", "UNKNOWN")
        except Exception as e:  # noqa: BLE001 - recorded; G6 blocks on it
            status[leg] = f"{leg.upper()}_BLOCKED" if isinstance(e, guards.GuardBlocked) \
                else f"{leg.upper()}_NOT_COMPLETE"
            errors[leg] = str(e)
            logger.error(f"FY rollover: final {leg} backup did not complete: {e}")
    return status, errors


# ═══════════════════════════════════════════════════════════════
# Weekly independent integrity audit (read-only deep verification)
# ═══════════════════════════════════════════════════════════════

def _audit_budget_incomplete(exc: BudgetExceeded) -> dict:
    """An audit leg with no time left in the run budget could not run (WP7/WP14)."""
    logger.error(f"Integrity audit leg skipped: {exc}")
    return {"status": "AUDIT_INCOMPLETE", "scope": "full", "files_checked": None,
            "bytes_checked": None, "mismatches": 0,
            "detail": f"could not run - {exc}. Raise budget.audit_max_hours or move the audit."}


def _audit_alert_kind(outcomes: list[str]) -> str:
    """WARNING only when every unverified leg merely could not run; any
    difference or unknown result needs a person (never switchable)."""
    if outcomes and all(o.endswith("=AUDIT_INCOMPLETE") for o in outcomes):
        return "WARNING"
    return "ACTION_REQUIRED"


@flow(name="integrity-audit", log_prints=True)
def integrity_audit_flow(config_path: str = CONFIG_PATH, mode: str = "all"):
    """Read-only weekly integrity audit: source↔backup content verification.

    This flow NEVER modifies source or destination and NEVER rewrites
    historical backup rows. It records one `integrity_audits` manifest row
    per enabled leg (VERIFIED | VERIFICATION_FAILED) and alerts + fails on
    divergence. Absence of an audit row means NOT_VERIFIED.

    Modes: cloud | lan | all (both sequentially, cloud first).
    """
    from core.integrity import audit_cloud, audit_lan, new_audit_id

    valid_modes = {"cloud", "lan", "all"}
    mode = mode.lower()
    if mode not in valid_modes:
        raise ValueError(f"Invalid mode '{mode}'. Must be one of: {sorted(valid_modes)}")

    config = load_config(config_path)
    configure_logging(config.paths.log_directory, log_retention_days=config.maintenance.log_retention_days)
    try:
        configure_prefect_bridge()
    except Exception as e:
        logger.debug(f"configure_prefect_bridge skipped: {e}")

    logger.info(f"AAM integrity audit starting - mode={mode}, firm={config.firm_name}")
    # WP14: the audit's run budget counts from here, including the slot wait
    budget = RunBudget("audit", config.budget.audit_max_hours * 3600, time.monotonic())
    db = ManifestDB.from_config(config)
    legs = [leg for leg in ("cloud", "lan")
            if mode in (leg, "all") and getattr(config, leg).enabled]
    recorded: set[str] = set()
    excs = []
    outcomes: list[str] = []   # "<leg>=<status>" of each non-VERIFIED leg: part of the alert key

    def _record(leg: str, audit_id: str, started_at: str, result: dict) -> None:
        # A VERIFIED verdict is durable only once persisted: a failed DB
        # write must fail the audit loudly (alert + FAILED flow), never
        # silently drop a passing result or leave NOT_VERIFIED behind a
        # COMPLETED run.
        name = "Cloud" if leg == "cloud" else "LAN"
        recorded.add(leg)
        try:
            db.record_audit({
                "audit_id": audit_id, "mode": leg, "scope": result["scope"],
                "started_at": started_at, "ended_at": now_iso(),
                "status": result["status"],
                "files_checked": _nullable_audit_count(result.get("files_checked")),
                "bytes_checked": _nullable_audit_count(result.get("bytes_checked")),
                "mismatches": max(result["mismatches"], 0),
                "detail": result["detail"], "created_at": now_iso(),
            })
        except Exception as e:
            excs.append(RuntimeError(f"{name} audit persistence failed: {e}"))
            outcomes.append(f"{leg}=NOT_RECORDED")
        if result["status"] != "VERIFIED":
            excs.append(RuntimeError(_audit_outcome_text(name, result)))
            outcomes.append(f"{leg}={result['status']}")

    def _audit_cloud_leg(audit_id: str) -> dict:
        try:
            cloud_timeout = int(budget.cap(config.cloud.verify_timeout_seconds))
        except BudgetExceeded as e:
            return _audit_budget_incomplete(e)
        audit_artefacts = _open_run_artefacts(config, audit_id)
        try:
            with temp_rclone_config(
                config.paths.gcs_key_path,
                config.cloud.location,
                config.cloud.project_number,
                config.cloud.storage_class,
            ) as rclone_cfg:
                return audit_cloud(
                    config.paths.source_drive, config.cloud.bucket,
                    _effective_fy_prefix(config), rclone_cfg,
                    timeout=cloud_timeout, artefacts=audit_artefacts,
                    **_audit_settings(config),
                )
        finally:
            _finalize_run_artefacts(config, audit_artefacts)

    def _audit_lan_leg(audit_id: str) -> dict:
        try:
            lan_timeout = int(budget.cap(config.lan.subprocess_timeout_seconds,
                                         config.budget.lan_post_reserve_minutes * 60))
        except BudgetExceeded as e:
            return _audit_budget_incomplete(e)   # NAS not woken: nothing to do
        wol_check_task(config)
        audit_artefacts = _open_run_artefacts(config, audit_id)
        try:
            return audit_lan(
                config.paths.source_drive, config.paths.lan_destination,
                timeout=lan_timeout, artefacts=audit_artefacts, **_audit_settings(config),
            )
        finally:
            # Only after a successful wake; a failed shutdown never replaces the verdict.
            if config.lan.shutdown_after_backup and config.wol.enabled:
                try:
                    lan_shutdown_task(config, str(audit_artefacts.dir)
                                      if audit_artefacts else None)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"NAS shutdown after the LAN audit failed: {e}")
            _finalize_run_artefacts(config, audit_artefacts)

    try:
        try:
            with _backup_slot(config):
                for leg, run_leg in (("cloud", _audit_cloud_leg), ("lan", _audit_lan_leg)):
                    if leg not in legs:
                        continue
                    audit_id, started_at = new_audit_id(leg, "full"), now_iso()
                    try:
                        result = run_leg(audit_id)
                    except Exception as e:  # noqa: BLE001 - recorded + alerted, never silent
                        logger.error(f"Integrity audit ({leg}) could not run: {e}")
                        result = {"status": "AUDIT_INCOMPLETE", "scope": "full",
                                  "files_checked": None, "bytes_checked": None,
                                  "mismatches": 0,
                                  "detail": f"could not run - {type(e).__name__}: {e}"}
                    _record(leg, audit_id, started_at, result)
        except Exception as e:  # noqa: BLE001 - backup slot / lock not obtained
            logger.error(f"Integrity audit could not run: {e}")
            for leg in legs:
                if leg not in recorded:
                    _record(leg, new_audit_id(leg, "full"), now_iso(), {
                        "status": "AUDIT_INCOMPLETE", "scope": "full", "files_checked": None,
                        "bytes_checked": None, "mismatches": 0,
                        "detail": f"could not run - backup slot not obtained: {e}"})

        if excs:
            error_summary = '; '.join(str(e) for e in excs)
            logger.error(f"Integrity audit failed: {error_summary}")
            # The key carries each leg's outcome: a "could not run" alert must
            # never put a real mismatch inside its cooldown (code review 2026-10-16).
            notify(config, AlertEvent(
                key=f"audit:{mode}:{','.join(sorted(outcomes))}",
                kind=_audit_alert_kind(outcomes), mode="audit",
                fault_code=("AAM-AUD-02" if _audit_alert_kind(outcomes) == "WARNING"
                            else "AAM-AUD-01"),
                title="Integrity audit did not verify", message=error_summary,
                status="NOT_VERIFIED"))
            raise ExceptionGroup("Integrity audit did not verify", excs)
        logger.info("AAM integrity audit VERIFIED - all enabled legs match source")
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# Main backup flow — entry point for all modes
# ═══════════════════════════════════════════════════════════════

@flow(name="aam-backup", log_prints=True)
def backup(config_path: str = CONFIG_PATH, mode: str = "all", approval_id: str | None = None,
           budget_hours: float | None = None):
    """AAM Backup Automation — nightly backup orchestrator.

    Each pipeline step is a separate Prefect task — visible in the Prefect UI
    with individual state, timing, logs, and retries.

    Modes:
        cloud — Run only cloud backup (rclone sync → GCS)
        lan   — Run only LAN backup (robocopy /MIR, includes WoL + shutdown)
        all   — Run both sequentially (cloud first, then LAN)

    approval_id: set only by the dashboard when a human approved a BLOCKED
    run once (plan §6.2); scheduled runs never pass one.

    budget_hours: manual runs only (e.g. the first full upload over a
    weekend): replaces budget.<leg>_max_hours for this run. Scheduled runs
    never pass it, so the schedule check (WP14) stays valid.
    """
    if budget_hours is not None and not 0 < budget_hours <= 168:
        raise ValueError(f"budget_hours must be between 0 and 168, got {budget_hours}")
    valid_modes = {"cloud", "lan", "all"}
    mode = mode.lower()
    if mode not in valid_modes:
        raise ValueError(f"Invalid mode '{mode}'. Must be one of: {sorted(valid_modes)}")

    config = load_config(config_path)
    configure_logging(config.paths.log_directory, log_retention_days=config.maintenance.log_retention_days)
    try:
        configure_prefect_bridge()
    except Exception as e:
        logger.debug(f"configure_prefect_bridge skipped: {e} - Prefect UI may not show loguru logs")

    logger.info(f"AAM Backup starting - mode={mode}, firm={config.firm_name}")

    # Disabled-leg guard: an explicit single-leg request for a disabled leg
    # must fail loudly (Prefect FAILED, no run_history row, no success
    # appearance) rather than no-op into a successful run. mode="all" still
    # runs whichever legs are enabled (config validation forbids both off).
    if mode == "cloud" and not config.cloud.enabled:
        raise ValueError("Cloud backup leg is disabled in configuration; refusing to run.")
    if mode == "lan" and not config.lan.enabled:
        raise ValueError("LAN backup leg is disabled in configuration; refusing to run.")

    # G8: clean up orphaned robocopy temp logs left behind by hard-killed
    # processes (SCM stop, timeout kill, crash). Normal runs delete their own
    # log in a finally block; only killed runs strand them in %TEMP%. The age
    # gate protects a concurrently running pipeline's active log.
    try:
        cleanup_orphaned_robocopy_logs(
            max_age_hours=config.maintenance.orphan_robocopy_log_max_age_hours)
    except Exception as e:
        logger.warning(f"Orphaned robocopy log cleanup failed (non-fatal): {e}")

    # ── Watchdog lock + serialization now live INSIDE each pipeline ──
    # P2-CONC: the concurrency slot and the lock moved into _backup_slot()
    # (acquired per pipeline) so direct callers - scripts, manual runs,
    # tests - serialize on the same limit as production flows. The old
    # flow-level wrapper is intentionally GONE in this same commit; keeping
    # both would self-deadlock the limit=1 resource.
    excs = []
    # plan I2: only the FIRST leg of this flow run is late; a second leg starts when it starts
    late_s = 0.0 if budget_hours is not None else _scheduled_lateness_seconds()

    try:
        # ── Cloud ──
        if mode in ("cloud", "all") and config.cloud.enabled:
            logger.info("Starting cloud backup pipeline")
            try:
                _run_cloud_pipeline(config, _stable_run_id("cloud"), now_iso(), time.monotonic(),
                                    approval_id=approval_id, budget_hours=budget_hours,
                                    late_seconds=late_s)
            except Exception as e:
                excs.append(e)
            late_s = 0.0

        # ── LAN ──
        if mode in ("lan", "all") and config.lan.enabled:
            logger.info("Starting LAN backup pipeline")
            try:
                _run_lan_pipeline(config, _stable_run_id("lan"), now_iso(), time.monotonic(),
                                  approval_id=approval_id, budget_hours=budget_hours,
                                  late_seconds=late_s)
            except Exception as e:
                excs.append(e)

        # ── Summary ──
        if excs:
            error_summary = '; '.join(str(e) for e in excs)
            logger.error(f"Backup completed with {len(excs)} error(s): {error_summary}")
            # WP8: no generic alert here - every failed leg already sent its
            # ONE alert at its terminal decision (or its ACTION REQUIRED
            # email when blocked); a second email per failure was the
            # duplicate-alert finding.
            raise ExceptionGroup("Backup completed with errors", excs)

        # ── Maintenance ──
        try:
            db = ManifestDB.from_config(config)
            db.purge_old_runs(retention_days=config.maintenance.db_retention_days)
            db.close()
        except Exception as e:
            logger.warning(f"DB maintenance failed (non-critical): {e}")

        logger.info("AAM Backup completed successfully")

    except Exception:
        raise
