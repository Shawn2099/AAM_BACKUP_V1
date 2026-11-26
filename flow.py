"""AAM Backup Automation V1 — Prefect 3 flow orchestrator.

Each backup pipeline is decomposed into granular @task functions.
Each task is independently tracked by Prefect — state, timing, logs, retries.
This provides full visibility into which step is running, which failed, and how long each took.

Two deployments from one codebase:
  - backup-cloud: daily 6 PM IST, rclone sync → GCS
  - backup-lan:   daily 1 AM IST, rclone mirror → LAN (WoL + shutdown)
  - backup-all:   manual, runs both sequentially (cloud first)
"""

import json
import os
import re
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pendulum
from loguru import logger
from prefect import flow, task
from prefect.concurrency.sync import concurrency

from core import alert_class, approvals, faults, fy_overlap, fy_rollover, guards, health, permissions, report, statuses
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
from core import cloud_sync
from core.cloud_sync import measure_planned_deletes, resolve_max_duration_seconds, run_cloud_sync
from core.cloud_verify import verify_cloud_and_diff, verify_cloud_integrity
from core.health import CLOCK_OK, check_clock, pre_backup_health
from core.lan_preflight import check_lan_destination
from core import lan_sync
from core.lan_sync import plan_lan_mirror
from core.logging import configure as configure_logging
from core.logging import configure_prefect_bridge
from core.manifest import ManifestDB
from core.notify import (AUDIENCE_REPORTS, SENT, AlertEvent, attachment_ladder, notify,
                         send_email)
from core.process import acquire_lock, read_lock_alive
from core.rclone_config import temp_rclone_config
from core.run_artefacts import RunArtefacts
from core.shutdown import FAILED as SHUTDOWN_FAILED
from core.shutdown import ShutdownResult, shutdown_from_config, validate_shutdown_target
from core.report_period import ReportPeriod
from core.time_utils import IST, get_fy_prefix, now_iso
from core.wol import ensure_server_online, ensure_server_online_status
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


def _open_run_artefacts(config, run_id: str, started_at: str | None = None) -> RunArtefacts | None:
    """Create the run's full-log folder (plan WP2); its name carries the run's start time (IST)
    and leg (plan F2). Never blocks a backup: on failure the tools fall back to a temp folder and
    the reason is logged."""
    try:
        started = pendulum.parse(started_at).in_timezone(IST) if started_at else None
        return RunArtefacts.for_config(config, run_id, started=started)
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
    if os.environ.get("PREFECT_TEST_MODE") == "1":
        # Unit-test mode (tests/conftest forces this env): occupancy requires
        # an orchestration API which these tests deliberately do not provide, and
        # their mock configs have no real lock path (so a lock that cannot be
        # written is tolerated here, and only here). The lock lifecycle is the
        # SAME shared helper as in production; production never sets
        # PREFECT_TEST_MODE.
        with _hold_backup_lock(config, required=False):
            yield
        return

    with concurrency("aam-backup", occupy=1, timeout_seconds=wait_seconds):
        with _hold_backup_lock(config, required=True):
            yield


@contextmanager
def _hold_backup_lock(config, *, required: bool):
    """The watchdog lock of one pipeline (A7), shared by the production and the test branch.

    Exclusive: a live lock of another process aborts this pipeline BEFORE any transfer.
    A lock holding THIS process's own live PID is the leftover of an earlier pipeline of the same
    flow process (a release that failed) and is taken over - the slot guarantees pipelines of one
    process never overlap. `required`: a lock that cannot be written stops the run (the watchdog
    could not see it and could restart the agent mid-transfer); never run without it."""
    lock_path = config.paths.backup_lock_path
    held = False
    try:
        held = acquire_lock(lock_path)
        if not held:
            alive, owner_pid = read_lock_alive(lock_path)
            if alive and owner_pid == os.getpid():
                held = True
                logger.warning("Backup lock of an earlier pipeline of this process taken over")
            else:
                raise RuntimeError(
                    "Backup lock is held by a live process - refusing to "
                    "overwrite it or run without it"
                )
        logger.info(f"Backup lock acquired (PID={os.getpid()}) - watchdog will defer restarts")
    except OSError as e:
        if required:
            raise RuntimeError(
                f"Backup lock could not be written ({e}) - refusing to run without it: the "
                "watchdog could not see this backup") from e
        logger.warning(f"Could not write backup lock file: {e}")
    try:
        yield
    finally:
        if held:
            _release_backup_lock(
                lock_path, int(config.maintenance.lock_release_retries),
                float(config.maintenance.lock_release_delay_ms) / 1000)


def _release_backup_lock(lock_path, retries: int, delay_s: float) -> None:
    """Remove the lock if it is still ours, retrying while the file is busy (an antivirus scan or
    the watchdog reading it makes the delete fail on Windows). A lock of another owner is left."""
    for attempt in range(retries + 1):
        _, owner_pid = read_lock_alive(lock_path)
        if owner_pid is None and not lock_path.exists():
            return                                     # already gone
        if owner_pid == os.getpid():
            try:
                lock_path.unlink(missing_ok=True)
                logger.info("Backup lock released")
                return
            except OSError:
                pass
        elif owner_pid is not None and owner_pid != -1:
            logger.warning("Backup lock changed during run - leaving it in place")
            return
        if attempt < retries:                          # busy (-1) / unreadable / delete failed
            time.sleep(delay_s)
    logger.error(f"Backup lock {lock_path} could not be released after {retries + 1} attempts; "
                 "the next pipeline of this process takes it over, a later process sees a dead PID")


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
                    artefacts_dir: str | None = None, fy: str | None = None,
                    title_fy: str | None = None) -> None:
    """THE failure alert of one run (plan WP8): one per failed run, at the
    terminal decision, through core.notify (delivery recorded, cooldown per
    leg+status, dashboard link). Never raises. The fault code is the one the
    pipeline classified, else derived from the status + tool evidence.
    fy / title_fy: the overlap scope of the pass (plan 2.3, see _alert_scope)."""
    try:
        code = fault_code or faults.classify_run(leg, status, sync_result=sync_result)
    except Exception:  # noqa: BLE001 - the alert must still go out
        code = faults.UNKNOWN
    notify(config, AlertEvent(
        key=f"backup:{leg}{_fy_key_part(fy)}:{status}",
        kind="FAILURE", mode=leg,
        title=f"{_leg_title(leg, title_fy or fy)} backup {statuses.label(status).lower()}",
        message=message, status=status,
        exit_code=exit_code if type(exit_code) is int else None,
        occurred_at=occurred_at, fault_code=code, run_id=run_id,
        artefacts_dir=artefacts_dir, fy=fy,
    ))


def _failure_alert_text(leg: str, status: str, error: str | None, nas_note: str = "") -> str:
    name = "Cloud" if leg == "cloud" else "LAN"
    if status.endswith("_SUSPECT"):
        return (f"{name} backup SUSPECT: {error} The transfer did not terminate normally; "
                f"completion is unproven. {nas_note or 'The next scheduled run will re-sync.'}")
    return f"{name} backup {statuses.label(status)}: {error or status} {nas_note}".strip()


def _send_recovery_if_needed(config, leg: str, run_id: str, *, fy: str | None = None,
                             title_fy: str | None = None) -> None:
    """RECOVERED email on the first success after a failure (plan WP8). fy: the pass's own FY (plan 2.3)."""
    try:
        db = ManifestDB.from_config(config)
        try:
            scope = {"fy": fy} if fy else {}
            previous = db.previous_run_status(leg, run_id, **scope)
            previous_code = db.previous_run_fault_code(leg, run_id, **scope)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - never affects the run
        logger.warning(f"Recovery check skipped ({leg}): {e}")
        return
    if previous and statuses.category(previous) in (
            statuses.CATEGORY_FAILED, statuses.CATEGORY_PARTIAL, statuses.CATEGORY_BLOCKED):
        notify(config, AlertEvent(
            key=f"recovered:{leg}{_fy_key_part(fy)}", kind="RECOVERED", mode=leg,
            title=f"{_leg_title(leg, title_fy or fy)} backup recovered",
            message=f"The {leg.upper()} backup completed successfully again "
                    f"(previous run: {statuses.label(previous)}).",
            status="RECOVERED", run_id=run_id,
            resolved_code=previous_code or faults.classify_run(leg, previous), fy=fy))


def _exception_kind(exc: BaseException) -> str:
    """Map a pipeline exception onto statuses.decide_terminal_status kinds."""
    if isinstance(exc, guards.GuardBlocked):
        return statuses.EXC_BLOCKED
    if isinstance(exc, PartialRun):
        return statuses.EXC_PARTIAL
    if not isinstance(exc, Exception):   # KeyboardInterrupt, SystemExit, Prefect's TerminationSignal
        return statuses.EXC_INTERRUPTED
    return statuses.EXC_ERROR


def _error_text(exc: BaseException) -> str:
    """The run row's error text: an interrupt has no message of its own."""
    if isinstance(exc, Exception):
        return str(exc)
    return (f"The run was interrupted ({type(exc).__name__}: service stop, cancel or Ctrl+C) "
            "before it finished.")


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


# ── FY overlap passes (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, section 2.3) ──────────────────
# fy_pass None = no overlap pass: every helper below returns the baseline value (no extra kwargs,
# no FY metrics, the baseline key and title).

def _budget_origin(budget_start: float | None, monotonic_start: float | None) -> float:
    """Where the leg's run budget starts: the leg's first pass sets it, the next pass shares it."""
    if budget_start is not None:
        return budget_start
    return monotonic_start if monotonic_start is not None else time.monotonic()


def _pass_run_id(run_id: str, fy_pass) -> str:
    """The next-FY pass's run id carries its FY: `<flow-run>-cloud-FY27-28` (plan D-4)."""
    if fy_pass is None or not fy_pass.extra or fy_overlap.fy_of_id(run_id):
        return run_id
    return fy_overlap.pass_run_id(run_id, fy_pass.fy)


def _writes_entries(fy_pass) -> bool:
    """file_entries and the cloud bucket-vs-DB cross-check belong to the configured FY (plan D-9)."""
    return fy_pass is None or not fy_pass.extra


def _alert_scope(fy_pass) -> dict:
    """Alert keys, groups and titles of a pass (plan 2.3). `fy` keys the alert of the next-FY pass only;
    `title_fy` names the FY in the title of BOTH passes while the overlap is active. {} = baseline."""
    if fy_pass is None:
        return {}
    return {"fy": fy_pass.fy if fy_pass.extra else None, "title_fy": fy_pass.fy}


def _other_fy(fy_pass) -> str | None:
    """The other active FY: a pass's approval request supersedes only the requests of its own FY (plan 2.4)."""
    if fy_pass is None:
        return None
    return fy_overlap.prev_fy(fy_pass.fy) if fy_pass.extra else fy_overlap.next_fy(fy_pass.fy)


def _pass_metrics(fy_pass, *, cloud: bool = False) -> dict:
    """extended_metrics of a pass: its FY; the next-FY pass also says so and, on the cloud leg, that the
    cross-check was skipped (D-9)."""
    if fy_pass is None:
        return {}
    if not fy_pass.extra:
        return {"fy": fy_pass.fy}
    metrics = {"fy": fy_pass.fy, "overlap": True}
    if cloud:
        metrics["cross_check"] = "skipped (overlap pass)"
    return metrics


def _pass_row_metrics(fy_pass, leg: str) -> str | None:
    """extended_metrics JSON of a row that never reached the pipeline body (MISSED, SLOT_TIMEOUT), None for
    the baseline call: the row still says which FY it belongs to (review F5)."""
    if fy_pass is None:
        return None
    return _merge_metrics(None, **_pass_metrics(fy_pass, cloud=leg == "cloud"))


def _artifact_kwargs(fy_pass, base: str) -> dict:
    """The Prefect summary key: only the next-FY pass has its own (plan 2.3); otherwise the task's default."""
    if fy_pass is not None and fy_pass.extra:
        return {"key": fy_overlap.artifact_key(base, fy_pass.fy)}
    return {}


def _approval_of_pass(config, approval_id: str | None, fy_pass) -> str | None:
    """An approval made for the other FY is not offered to this pass (plan 2.3). It is not consumed here,
    so it stays open for its own FY. No pass (baseline): the id is returned unchanged, no database read."""
    if fy_pass is None or not approval_id:
        return approval_id
    db = ManifestDB.from_config(config)
    try:
        request = db.get_approval(approval_id)
    finally:
        db.close()
    if request is not None and request.get("fy") and request.get("fy") != fy_pass.fy:
        logger.info(f"Approval {approval_id} is for {request.get('fy')} - not applied to the "
                    f"{fy_pass.fy} pass")
        return None
    return approval_id


def _leg_title(leg: str, title_fy: str | None) -> str:
    return f"{leg.upper()} ({title_fy})" if title_fy else leg.upper()


def _fy_key_part(fy: str | None) -> str:
    """`:FY27-28` in an alert key of the next-FY pass, '' for the baseline (keeps the key literal, see
    tests/test_alert_class.py which reads the key's literal prefix)."""
    return f":{fy}" if fy else ""


def _ensure_pass_nas_folder(config, fy: str) -> None:
    """The next-FY LAN pass's NAS folder, after WoL (plan 2.3). A refused destination or an unreachable
    share is LAN_FAILED with AAM-LAN-01: that pass fails, the other pass still runs."""
    try:
        fy_overlap.ensure_pass_nas_folder(config, fy)
    except (OSError, ValueError) as e:
        raise faults.coded(RuntimeError(f"The NAS folder for {fy} could not be prepared: {e}"),
                           "AAM-LAN-01") from e


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


def _auto_scheduled_run() -> bool:
    """True when this flow run was started by its schedule (Prefect's `auto_scheduled` flag). A run started by
    hand, or outside a flow run, is not: the weekly audit then covers every enabled leg (plan 2.5, section 5)."""
    try:
        from prefect.context import FlowRunContext
        ctx = FlowRunContext.get()
        run = ctx.flow_run if ctx else None
        return bool(run is not None and getattr(run, "auto_scheduled", False))
    except Exception as e:  # noqa: BLE001 - unknown means "started by hand": the audit covers every enabled leg
        logger.warning(f"Could not tell whether this run was scheduled ({e}); the audit covers every enabled leg")
        return False


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
                         reserve_s: float, late_s: float, monotonic_start: float | None, *,
                         fy: str | None = None, title_fy: str | None = None, fy_pass=None) -> None:
    """Plan I2: a run that starts with no budget left (less than the time its last steps need)
    would run into office hours. Record <LEG>_MISSED and send ONE warning per leg (the stable
    key `missed:<leg>` keeps several missed nights at boot from sending one email each), then
    raise PartialRun so Prefect never shows this run as completed. Takes no backup slot.
    A next-FY overlap pass (fy set) has no lateness of its own: it is missed when the leg's
    budget is used up by the earlier pass (plan D-7)."""
    if (late_s <= 0 and fy is None) or budget.remaining() > reserve_s:
        return
    status = f"{leg.upper()}_MISSED"
    if late_s > 0:
        late_h = late_s / 3600
        error = (f"The {leg.upper()} backup was scheduled for {late_h:.1f} h before it could start "
                 f"(the server was off or busy), so its {budget.describe()} run budget was already "
                 "used up. Nothing was changed; the next scheduled run continues.")
    else:
        error = (f"The {leg.upper()} backup for {fy} could not start: the {budget.describe()} run budget "
                 "of this night was used up by the earlier pass. Nothing was changed; the next "
                 "scheduled run continues.")
    logger.warning(error)
    _record_run(config, run_id, leg, started_at, status, -1, error,
                extended_metrics=_pass_row_metrics(fy_pass, leg), monotonic_start=monotonic_start)
    notify(config, AlertEvent(
        key=f"missed:{leg}{_fy_key_part(fy)}", kind="WARNING", mode=leg,
        title=f"{_leg_title(leg, title_fy or fy)} backup night missed", message=error, status=status,
        occurred_at=started_at, fault_code="AAM-SYS-07", run_id=run_id, fy=fy))
    raise PartialRun(f"{leg.upper()} run ended {status} - {error}")


STUCK_SLOT_NOTE = (" - the backup slot is taken but no backup is running (an earlier backup was "
                   "cut off, e.g. the backup service was restarted while it ran); support: reset the "
                   "'aam-backup' concurrency limit in Prefect or restart AamPrefectServer")


def _slot_stuck(config, exc: BaseException) -> bool:
    """AAM-SYS-08: the slot wait ran out while no live process holds the backup lock.

    A real holder writes backup.lock right after taking the slot and keeps it until it releases
    both, so a timed-out wait with a dead or absent lock means the slot belongs to nobody. Unsure
    (lock unreadable, any error) = the ordinary AAM-SYS-01 text; this never changes what is done."""
    if not isinstance(exc, TimeoutError):
        return False
    try:
        alive, _pid = read_lock_alive(config.paths.backup_lock_path)
    except Exception:  # noqa: BLE001 - only picks the wording of the message
        return False
    return alive is False


@contextmanager
def _pipeline_slot(config, leg: str, run_id: str, started_at: str,
                   monotonic_start: float | None = None, fy_pass=None, **scope):
    """RUNNING row, then the backup slot. If the slot (or the watchdog lock)
    cannot be obtained, the row becomes *_SLOT_TIMEOUT ("could not start")
    and the error is re-raised - previously no row was written at all.
    scope: the pass's alert scope (fy, title_fy; plan 2.3), {} for the baseline."""
    _record_running(config, leg, run_id, started_at)
    with ExitStack() as stack:
        try:
            stack.enter_context(_backup_slot(config))
        except Exception as e:
            status = f"{leg.upper()}_SLOT_TIMEOUT"
            stuck = _slot_stuck(config, e)
            detail = f"{e}{STUCK_SLOT_NOTE}" if stuck else str(e)
            logger.error(f"{leg} run {run_id} could not start: {detail}")
            try:
                _record_run(
                    config, run_id, leg, started_at, status, -1,
                    f"Backup could not start: {detail}",
                    extended_metrics=_pass_row_metrics(fy_pass, leg),
                    monotonic_start=monotonic_start,
                )
            except Exception as rec_err:  # noqa: BLE001
                logger.critical(f"Failed to record {status} for run {run_id}: {rec_err}")
            _send_run_alert(config, leg, status,
                            f"The {leg.upper()} backup could not start: {detail}",
                            None, started_at, run_id=run_id,
                            fault_code="AAM-SYS-08" if stuck else None, **scope)
            raise
        yield


# ═══════════════════════════════════════════════════════════════
# Shared tasks
# ═══════════════════════════════════════════════════════════════

@task(name="health-check")
def health_check_task(config, mode: str, artefacts_dir: str | None = None):
    """Run pre-backup health checks. Fail fast — won't fix itself.

    LAN needs the rclone binary (plan P1 section 2 step 1); no privilege probe any more.
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
    if mode in ("cloud", "lan", "all"):
        ver_timeout = getattr(config.tools, "rclone_version_timeout_seconds", 30)
        if not isinstance(ver_timeout, int) or ver_timeout < 1:
            ver_timeout = 30
        ver_fault = health.rclone_version_fault(
            config.tools.rclone_expected_version,
            timeout_s=ver_timeout,
            artefacts=artefacts,
        )
        if ver_fault:
            if config.tools.rclone_version_policy == "block":
                raise faults.coded(health.HealthError(ver_fault), "AAM-SYS-04")
            logger.warning(
                f"rclone version mismatch - continuing "
                f"(tools.rclone_version_policy: warn): {ver_fault}"
            )
            notify(config, AlertEvent(
                key="rclone_version",
                kind="WARNING",
                mode=mode,
                title="rclone version mismatch",
                message=ver_fault,
                status="WARNING",
                fault_code="AAM-SYS-04",
                occurred_at=now_iso(),
                artefacts_dir=artefacts_dir,
                cooldown_hours=24,
            ))
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
    `measure_planned_deletes` (rclone sync --dry-run --track-renames, shared
    with the LAN leg) for planned deletions. A measurement that could not be taken FAILS the run
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

    db = ManifestDB.from_config(config)
    try:
        baseline = db.guard_baseline("cloud", fy_prefix) or {}
    finally:
        db.close()

    results = [
        guards.g1_source_has_files(first_file, source),
        # Not a host path: the "is this machine" check does not apply.
        guards.g4_destination_identity(destination, baseline.get("destination"),
                                       None, set(), forbid_local=False),
    ]
    source_files = 0
    destination_files = 0
    dry = None
    if first_file:
        # A source without files is already blocked by G1; its dry-run would
        # only plan the deletion of everything.
        with temp_rclone_config(
            config.paths.gcs_key_path, config.cloud.location,
            config.cloud.project_number, config.cloud.storage_class,
        ) as rclone_conf:
            dry = measure_planned_deletes(
                source, remote, config_path=rclone_conf, fast_list=True,
                track_renames=config.cloud.track_renames,
                modify_window_seconds=config.cloud.modify_window_seconds,
                retries=config.cloud.retry_count,
                retries_sleep_seconds=config.cloud.sync_retries_sleep_seconds,
                timeout=config.cloud.dry_run_timeout_seconds,
                artefacts=artefacts, error_tail_lines=config.cloud.error_tail_lines)
        if dry.get("planned_deletes") is None:
            raise RuntimeError(
                f"Cloud guard measurement failed: rclone sync --dry-run did not complete "
                f"cleanly: {dry.get('error')}")
        source_files = dry.get("source_files") or 0
        destination_files = dry.get("dest_files") or 0
        results.append(guards.g3_mass_delete(
            dry["planned_deletes"], destination_files,
            limits.max_delete_files, limits.max_delete_percent,
            already_deleted=already_deleted,
            min_floor_files=limits.min_delete_floor_files))

    approved, approval_record = _apply_approval(config, "cloud", approval_id, run_id, results)
    if "G3_MASS_DELETE" in approved and dry is not None:
        # Approved mass deletion: the backstop is the re-measured count the
        # approval was just checked against - nothing beyond it.
        max_delete = dry["planned_deletes"]
    else:
        max_delete = guards.remaining_delete_allowance(
            destination_files, already_deleted,
            limits.max_delete_files, limits.max_delete_percent,
            min_floor_files=limits.min_delete_floor_files)
    return guards.enforce(
        "cloud", results, approved_guards=approved, fy=fy_prefix,
        source_file_count=source_files, destination=destination,
        destination_files=destination_files, max_delete=max_delete, **approval_record,
        **({"already_deleted": already_deleted} if already_deleted else {}),
    )


@task(name="cloud-access")
def cloud_access_task(config, run_id: str, artefacts_dir: str | None = None):
    """Prove the rights the sync uses (plan WP4, CL3/CL13): read a real source file."""
    permissions.probe_source_read(config.paths.source_drive)


def _sync_with_retries(leg: str, *, run_once, remeasure, deletes_of, retryable,
                       max_attempts: int, delay_s: int, budget: RunBudget | None,
                       reserve_s: float, state: dict) -> dict:
    """XR-1 / XR-2 / XR-6: the ONE retry policy for a mirror (both legs).

    Replaces Prefect task retries, which re-ran the mirror with the same
    arguments: a fresh --max-delete each time (up to max_attempts x the G3
    limit deleted) and a mirror repeat without re-checking anything.

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
                    max_delete: int | None = None, budget: RunBudget | None = None,
                    mode: str = "sync"):
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
        track_renames=config.cloud.track_renames,
        mode=mode,
    )
    if limited and result.get("timed_out") and max_duration is not None:
        # C8 (F2 / R6): rclone had --max-duration with --cutoff-mode SOFT and still
        # did not stop within the 300s margin — killed_after_cutoff, not budget_stopped.
        stats = result.get("stats") or {}
        done = (f"{stats.get('transfers', 'unknown')} file(s), "
                f"{(stats.get('bytes') or 0) / 1024 ** 3:.2f} GB uploaded in this run"
                if stats else "upload progress unknown (no rclone stats)")
        logger.error("Cloud sync: rclone did not stop after its time limit and was killed")
        return {**result, "status": "CLOUD_PARTIAL", "killed_after_cutoff": True,
                "budget_stopped": False, "fault_code": "AAM-CLD-08",
                "error": (f"rclone did not stop within {cloud_sync.MAX_DURATION_MARGIN_S} s after its time "
                          f"limit (--max-duration {max_duration}s, --cutoff-mode SOFT) and was killed after "
                          f"{timeout}s - a large file may still have been uploading, or network I/O may be "
                          f"hung. {done}")}
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
        logger.info("Verifying cloud integrity and diff")
        _t_verify = time.monotonic()
        verify_result = verify_cloud_and_diff(
            source=config.paths.source_drive,
            bucket=config.cloud.bucket,
            fy_prefix=fy_prefix,
            config_path=rclone_cfg,
            timeout=limit(config.cloud.verify_timeout_seconds),
            checkers=config.cloud.checkers,
            modify_window_seconds=config.cloud.modify_window_seconds,
            retries=config.cloud.retry_count,
            retries_sleep_seconds=config.cloud.retries_sleep_seconds,
            artefacts=artefacts,
        )
        verify_s = time.monotonic() - _t_verify

        logger.info("Gathering cloud size")
        _t_size = time.monotonic()
        size = get_cloud_size(
            config.cloud.bucket, fy_prefix, rclone_cfg,
            timeout=limit(config.cloud.cloud_size_timeout_seconds),
            artefacts=artefacts,
        )
        size_s = time.monotonic() - _t_size
        manifest = verify_result.get("manifest", [])
        manifest_error = verify_result.get("manifest_error")
        cloud_diff = verify_result.get("diff", {"added": [], "removed": [], "modified": [], "unchanged": []})
        stage_seconds = {
            "verify_s": round(verify_s, 1),
            "size_s": round(size_s, 1),
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
    """Wake backup server if WoL is enabled; returns 'disabled', 'already_up', or 'woken'."""
    if not config.wol.enabled:
        logger.info("WoL disabled, skipping")
        return "disabled"
    logger.info(f"Waking backup server {config.wol.server_ip}")
    return ensure_server_online_status(config)


def _auto_backup_dir(config, artefacts_dir: str | None = None) -> str | None:
    """Resolve per-run, per-FY `--backup-dir` when versions are enabled on a real config."""
    lan = getattr(config, "lan", None)
    if not lan or getattr(lan, "versions_enabled", False) is not True:
        return None
    vf = getattr(lan, "versions_folder", None)
    dst = getattr(getattr(config, "paths", None), "lan_destination", None)
    if not isinstance(vf, str) or not isinstance(dst, str):
        raise ValueError(
            "lan.versions_enabled is True but lan.versions_folder or paths.lan_destination is invalid"
        )
    fy = _effective_fy_prefix(config)
    tag = Path(artefacts_dir).name[-6:] if artefacts_dir else ""
    return lan_sync.backup_dir_for_run(dst, vf, fy, run_tag=tag)


@task(name="lan-preflight")
def lan_preflight_task(config, artefacts_dir: str, backup_dir: str | None = None):
    """SMB probe, mount marker, G1 first file, then ONE rclone dry-run (plan P1 section 2 step 3).

    Returns the plan dict (lan_sync.plan_lan_mirror) plus canary_present, or
    {"source_empty": True, "canary_present": ...} without a dry-run so the
    guards task gives G1's own message. A dry-run that is not ok is a FAIL
    (AAM-LAN-01), never a BLOCK.
    """
    logger.info(f"LAN preflight: validating {config.paths.source_drive} -> {config.paths.lan_destination}")
    canary_present = check_lan_destination(
        config.paths.lan_destination,
        smb_attempts=config.lan.preflight_smb_attempts,
        smb_wait_seconds=config.lan.preflight_smb_wait_seconds,
        smb_connect_timeout_seconds=config.wol.smb_connect_timeout_seconds,
    )
    try:
        source_empty = guards.first_regular_file(
            config.paths.source_drive, guards.SOURCE_IGNORE_NAMES) is None
    except OSError:
        source_empty = False   # unreadable source: the dry-run / G1 report it
    if source_empty:
        return {"source_empty": True, "canary_present": canary_present}
    plan = plan_lan_mirror(config.lan, config.paths.source_drive,
                           config.paths.lan_destination, artefacts_dir,
                           backup_dir=backup_dir or _auto_backup_dir(config, artefacts_dir))
    if not plan["ok"]:
        raise faults.coded(RuntimeError(
            f"LAN guard measurement failed: rclone sync --dry-run did not complete "
            f"cleanly: {plan['error']}"), "AAM-LAN-01")
    plan["canary_present"] = canary_present
    return plan


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
                        occurred_at: str, artefacts_dir: str | None = None,
                        sync_attempted: bool = False, *, fy: str | None = None,
                        title_fy: str | None = None, keep_fy: str | None = None) -> None:
    """BLOCKED run -> one approval request + ONE attention email (DEC-3).

    sync_attempted (A3): a sync attempt ran before the block (delete backstop, re-measure
    between retries), so the text must not say nothing was changed.
    fy / title_fy / keep_fy (plan 2.3, 2.4): the pass's alert scope and the other active FY,
    whose pending requests this request does not supersede. None = the baseline call.

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
                phrase=approvals.request_phrase(blocked), created_at=now_iso(),
                **({"keep_fy": keep_fy} if keep_fy else {}))
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - the alert below must still go out
        logger.error(f"Could not record the approval request for blocked run {run_id}: {e}")
    rejected = guard_record.get("approval_rejected")
    message = "\n".join(r.get("reason", "") for r in blocked)
    if rejected:
        message += f"\n\nThe approval supplied for this run was not applied: {rejected}"
    sync_attempted = sync_attempted or bool((guard_record.get("copy_only") or {}).get("attempted"))
    changed = (statuses.BLOCKED_AFTER_CHANGES_NOTE if sync_attempted
               else "Nothing was copied or deleted.")
    message += (f"\n\n{changed} Open the dashboard to review the "
                "numbers and either approve this backup once or dismiss it.")
    co_note = statuses.copy_only_note(guard_record.get("copy_only"))
    if co_note:
        message += f"\n\n{co_note}"
    notify(config, AlertEvent(
        key=f"blocked:{leg}{_fy_key_part(fy)}", kind="ACTION_REQUIRED", mode=leg,
        title=f"{_leg_title(leg, title_fy or fy)} backup blocked - approval needed",
        message=message, status=f"{leg.upper()}_BLOCKED", fy=fy,
        fault_code=faults.blocked_guard_code(guard_record) or faults.UNKNOWN, run_id=run_id,
        artefacts_dir=artefacts_dir,
        details={"Blocked by": ", ".join(r.get("guard", "?") for r in blocked),
                 "Run": run_id,
                 "Approval request": approval_id or "could not be recorded - see the logs"},
        occurred_at=occurred_at,
    ))


@task(name="lan-guards")
def lan_guards_task(config, fy_prefix: str, plan: dict,
                    approval_id: str | None = None, run_id: str | None = None,
                    already_deleted: int = 0) -> dict:
    """Data-safety guards G1-G5 on the one rclone dry-run `plan` (plan P1 section 2 step 4).

    approval_id: dashboard re-run of a blocked run (plan 6.2); used up here
    if it validly covers every guard that blocks again. An approved G5
    (missing mount marker) creates the marker before the mirror runs.

    A measurement that could not be taken FAILS the run (retried next
    night); a guard verdict BLOCKs it for a human (GuardBlocked).
    already_deleted (XR-2): deletions of earlier attempts of this run - G3
    covers the whole run, not each attempt. The record's max_delete is the
    --max-delete backstop for the real sync.
    """
    source = config.paths.source_drive
    dest = config.paths.lan_destination
    limits = config.guards
    plan = plan or {}
    if plan.get("source_empty"):
        # empty/missing source: G1 speaks first (it blocks); anything else is a FAIL
        g1 = guards.g1_source_has_files(
            guards.first_regular_file(source, guards.SOURCE_IGNORE_NAMES), source)
        approved, approval_record = _apply_approval(config, "lan", approval_id, run_id, [g1])
        guards.enforce("lan", [g1], approved_guards=approved, fy=fy_prefix, **approval_record)
        raise faults.coded(RuntimeError(
            "LAN guard measurement skipped: the source looked empty at preflight"), "AAM-LAN-01")
    source_files = plan.get("source_files")
    destination_files = plan.get("dest_files")
    planned_deletes = plan.get("planned_deletes")
    if source_files is None or destination_files is None or planned_deletes is None:
        raise faults.coded(RuntimeError(
            "LAN guard measurement failed: the rclone dry-run counts are missing, so planned "
            "deletions cannot be counted - refusing to mirror"), "AAM-LAN-01")

    destination_ips = guards.resolve_host(guards.unc_host(dest))
    if limits.forbid_local_destination and destination_ips is None:
        raise faults.coded(RuntimeError(
            f"Could not resolve the destination host of {dest} to check it is not this machine"),
            "AAM-NAS-01")

    db = ManifestDB.from_config(config)
    try:
        baseline = db.guard_baseline("lan", fy_prefix) or {}
    finally:
        db.close()

    results = [
        guards.g1_source_has_files(
            guards.first_regular_file(source, guards.SOURCE_IGNORE_NAMES), source),
        guards.g2_source_shrink(source_files, baseline.get("source_file_count"),
                                limits.max_source_shrink_percent),
        guards.g4_destination_identity(
            dest, baseline.get("destination"), destination_ips, guards.local_addresses(),
            limits.forbid_local_destination),
        guards.g3_mass_delete(planned_deletes, destination_files,
                              limits.max_delete_files, limits.max_delete_percent,
                              already_deleted=already_deleted,
                              min_floor_files=limits.min_delete_floor_files),
        guards.g5_canary(plan.get("canary_present"), destination_files > 0, dest),
    ]
    approved, approval_record = _apply_approval(config, "lan", approval_id, run_id, results)
    if "G3_MASS_DELETE" in approved:
        # Approved mass deletion: the backstop is the count the approval was checked against.
        max_delete = planned_deletes
    else:
        max_delete = guards.remaining_delete_allowance(
            destination_files, already_deleted,
            limits.max_delete_files, limits.max_delete_percent,
            min_floor_files=limits.min_delete_floor_files)
    record = guards.enforce(
        "lan", results, approved_guards=approved, fy=fy_prefix,
        source_file_count=source_files, destination=dest,
        destination_files=destination_files, max_delete=max_delete, **approval_record,
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
def lan_access_task(config, plan: dict, run_id: str, copy_only: bool = False):
    """Prove the rights the mirror uses (plan WP4, CL3/LA7/LA9): read a real
    source file; create/read/delete a probe at the destination; NAS free
    space for the bytes the rclone dry-run says will be copied (plus planned
    move bytes in copy-only mode, RF-4). Runs AFTER the guards, so
    nothing is ever written to a destination whose mount marker is not
    confirmed. A missing right FAILS the run."""
    dest = config.paths.lan_destination
    permissions.probe_source_read(config.paths.source_drive)
    permissions.probe_destination_write(dest, run_id)
    bytes_needed = (plan or {}).get("planned_bytes")
    if copy_only and bytes_needed is not None:
        move_bytes = (plan or {}).get("planned_move_bytes")
        bytes_needed = None if move_bytes is None else bytes_needed + move_bytes
    if bytes_needed is not None and getattr(config.lan, "versions_enabled", False):
        log_path = (plan or {}).get("log_path")
        if log_path:
            try:
                log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
                bytes_needed += cloud_sync.planned_versions_bytes(log_text)
            except OSError:
                pass
    permissions.check_destination_free_space(
        dest, bytes_needed=bytes_needed,
        min_free_gb=config.lan.min_free_dest_gb)


@task(name="lan-sync")
def lan_sync_task(config, artefacts_dir: str, max_delete: int,
                  budget: RunBudget | None = None, mode: str = "sync",
                  backup_dir: str | None = None):
    """Run the real LAN `rclone sync` (plan P1 section 2 step 6; max_delete: G3 backstop).

    With a run budget rclone gets what is left of it (minus the post reserve) as its own
    --max-duration with --cutoff-mode SOFT; stopping there (exit 10) is LAN_PARTIAL with
    `budget_stopped` - resumable, never a retry, never an arrival check.
    """
    timeout = config.lan.subprocess_timeout_seconds
    max_duration = None
    limited = False
    if budget is not None:
        reserve = config.budget.lan_post_reserve_minutes * 60
        try:
            limited = budget.binding(timeout, reserve)
            timeout = int(budget.cap(timeout, reserve))
        except BudgetExceeded as e:
            logger.error(str(e))
            return {"status": "LAN_PARTIAL", "exit_code": -1, "budget_stopped": True,
                    "stats": None, "files_failed": 0, "failed_paths": [], "deletes_held": False,
                    "error": _budget_stop_text(budget, "no time was left for the mirror")}
        if limited:
            max_duration = resolve_max_duration_seconds(timeout, None)
            logger.info(f"LAN mirror limited by the run budget: --max-duration "
                        f"{max_duration}s, hard stop {timeout}s")
    logger.info(f"LAN sync: {config.paths.source_drive} -> {config.paths.lan_destination}")
    result = lan_sync.run_lan_mirror(
        config.lan, config.paths.source_drive, config.paths.lan_destination, artefacts_dir,
        max_delete=max_delete, max_duration_s=max_duration, timeout=timeout,
        mode=mode, backup_dir=backup_dir or _auto_backup_dir(config, artefacts_dir))
    if limited and result.get("timed_out") and max_duration is not None:
        # S5: rclone had --max-duration with --cutoff-mode SOFT and still did not stop in
        # time: it hung (NAS I/O) and was killed. That is not a clean budget stop.
        stats = result.get("stats") or {}
        done = (f"{stats.get('transfers', 'unknown')} file(s) copied in this run"
                if stats else "progress unknown (no rclone stats)")
        logger.error("LAN mirror: rclone did not stop after its time limit and was killed")
        return {**result, "status": "LAN_PARTIAL", "killed_after_cutoff": True,
                "fault_code": "AAM-LAN-04",
                "error": (f"rclone did not stop within {cloud_sync.MAX_DURATION_MARGIN_S} s after its time "
                          f"limit (--max-duration {max_duration}s, --cutoff-mode SOFT) and was killed after "
                          f"{timeout}s - a large file may still have been copying, or NAS I/O may be "
                          f"hung. {done}")}
    stopped = result.get("exit_code") == 10 or (limited and result.get("timed_out"))
    if stopped and limited:
        stats = result.get("stats") or {}
        done = (f"{stats.get('transfers', 'unknown')} file(s) copied in this run"
                if stats else "progress unknown (no rclone stats)")
        logger.warning(f"LAN mirror stopped at the run budget - {done}")
        return {**result, "status": "LAN_PARTIAL", "budget_stopped": True,
                "error": _budget_stop_text(budget, done)}
    if result["status"] in ("LAN_FAILED", "LAN_SUSPECT"):
        raise SyncFailed(result.get("error") or "LAN sync failed", result)
    return result


@task(name="lan-verify")
def lan_verify_task(config, artefacts_dir: str, changed_after_epoch: float,
                    budget: RunBudget | None = None) -> dict:
    """The arrival check (plan P1 section 2 step 7): `rclone check` source -> NAS.
    Raises BudgetExceeded when no run budget is left for it."""
    timeout = config.lan.verify_timeout_seconds
    if budget is not None:
        timeout = int(budget.cap(timeout, 0))
    return lan_sync.check_lan_arrival(
        config.lan, config.paths.source_drive, config.paths.lan_destination, artefacts_dir,
        changed_after_epoch=changed_after_epoch, timeout=timeout)


def lan_hash_task(config, artefacts_dir: str, paths: list[str], changed_after_epoch: float,
                  budget: RunBudget | None = None) -> dict:
    """S3: content check of tonight's copied files. Raises BudgetExceeded when no run budget
    is left for it."""
    timeout = config.lan.arrival_hash_timeout_seconds
    if budget is not None:
        timeout = int(budget.cap(timeout, 0))
    return lan_sync.check_lan_transfers_hash(
        config.lan, config.paths.source_drive, config.paths.lan_destination, artefacts_dir,
        paths=paths, changed_after_epoch=changed_after_epoch, timeout=timeout)


def _hash_plan(config, log_paths: list[str], arrival: dict, *, transfers: int = 0) -> tuple[dict, list[str]]:
    """S3: decide whether tonight's hash check runs. Returns (metrics, paths to hash); the
    metrics carry `ran` False and the reason whenever it does not."""
    def skip(reason, files=None, nbytes=None):
        return {"ran": False, "verified": None, "files": files, "bytes": nbytes,
                "reason": reason}, []

    if not config.lan.arrival_hash_transferred:
        return skip("off (lan.arrival_hash_transferred)")
    if arrival.get("verified") is not True:
        return skip("arrival not verified")
    texts = []
    for p in log_paths:
        try:
            texts.append(Path(p).read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            # Fail closed: files may have been copied but cannot be listed, so the
            # size-only verdict must not stand as verified (caller makes it LAN_SUSPECT).
            info, no_paths = skip("copy log unreadable: could not read tonight's sync log "
                                  f"({e})")
            info["verified"] = False
            return info, no_paths
    paths, total = lan_sync.transferred_files(texts)
    if transfers > 0 and len(paths) < transfers:
        msg = (f"{transfers} transfers reported but no copied files in the log"
               if not paths else
               f"{transfers} transfers reported but only {len(paths)} copied file(s) in the log")
        info, no_paths = skip(msg, len(paths), total)
        info["verified"] = False
        return info, no_paths
    if not paths:
        return skip("nothing copied tonight", 0, 0)
    if total is None:
        return skip("size of the copied files unknown", len(paths), None)
    if total > config.lan.arrival_hash_max_gb * permissions.GB:
        return skip(f"copied {total} bytes: over lan.arrival_hash_max_gb "
                    f"({config.lan.arrival_hash_max_gb} GB)", len(paths), total)
    return {"ran": False, "verified": None, "files": len(paths), "bytes": total,
            "reason": None}, paths


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
    bad_reason = validate_shutdown_target(
        server_ip, getattr(getattr(config, "paths", None), "lan_destination", None),
    )
    if bad_reason:
        logger.error(f"NAS shutdown REFUSED for {server_ip!r}: {bad_reason}")
        result = ShutdownResult(SHUTDOWN_FAILED, server_ip, None, bad_reason, 0.0)
    else:
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
                                files_copied: int | None, bytes_copied: int | None,
                                key: str = "cloud-backup-summary"):
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
            key=key
        )
        logger.info("Published Cloud Backup Markdown Artifact to Prefect Console UI")
    except Exception as e:
        logger.warning(f"Could not publish cloud backup artifact: {e}", exc_info=True)


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


def _pending_context(config, db, leg: str, fy: str | None = None):
    """What the safety check needs to tell "waiting for the next backup" from "wrong": the start
    of this leg's last SUCCESSFUL backup (the shared success definition). Unknown (no backup, an
    unreadable time, a database error) leaves the reference empty, so every difference is real.
    `fy` (plan 2.5): the reference of that FY's own passes (None = the configured FY's)."""
    from core import audit_pending

    try:
        last = db.last_successful_run(leg) if fy is None else db.last_successful_run(leg, fy=fy)
        started = last.get("started_at") if isinstance(last, dict) else None
    except Exception as e:  # noqa: BLE001 - unknown means "no reference", never a crash
        logger.warning(f"Last successful {leg} backup unknown for the safety check: {e}")
        started = None
    return audit_pending.PendingContext(
        source_root=config.paths.source_drive,
        dest_root=config.paths.lan_destination if leg == "lan" else None,
        reference=audit_pending.reference_epoch(started), now=time.time(),
        modify_window_seconds=config.audit.modify_window_seconds,
        max_delete_files=config.guards.max_delete_files,
        max_delete_percent=config.guards.max_delete_percent, files_checked=None)


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
                        approval_id: str | None = None, late_seconds: float = 0.0,
                        fy_pass=None, budget_start: float | None = None):
    """Execute cloud backup tasks sequentially. Each task is independently tracked.

    late_seconds: how long after its scheduled time the run started (plan I2); the budget
    counts from the scheduled time, so a late start gets only what is left of it.
    fy_pass / budget_start (plan 2.3): one FY pass of an overlap. `config` is then that pass's
    config; budget_start is the leg's shared budget origin (the first pass sets it). None = baseline."""
    run_id = _pass_run_id(run_id, fy_pass)
    scope = _alert_scope(fy_pass)
    # WP14: the run budget counts from the job's start (I2: its scheduled start), including
    # the slot wait
    budget = RunBudget(
        "cloud",
        (budget_hours if budget_hours is not None else config.budget.cloud_max_hours) * 3600,
        _budget_origin(budget_start, monotonic_start) - late_seconds)
    _missed_if_no_budget(config, "cloud", run_id, started_at, budget,
                         config.budget.cloud_verify_reserve_minutes * 60, late_seconds,
                         monotonic_start, fy_pass=fy_pass, **scope)
    with _pipeline_slot(config, "cloud", run_id, started_at, monotonic_start, fy_pass=fy_pass, **scope):
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
        # (not for the next-FY pass: the cross-check is the configured FY's, plan D-9)
        before_dict = {}
        if _writes_entries(fy_pass):
            db = ManifestDB.from_config(config)
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
        artefacts = _open_run_artefacts(config, run_id, started_at)
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
        deferred_alert = None   # A2: an interrupted run records itself first, alerts second
        clock = None         # WP12: clock verdict from the health check

        try:
            approval_id = _approval_of_pass(config, approval_id, fy_pass)
            clock = health_check_task(config, "cloud", artefacts_dir)
            preflight(config, fy_prefix, artefacts_dir)
            try:
                guard_record = cloud_guards_task(config, fy_prefix, artefacts_dir,
                                                 approval_id=approval_id, run_id=run_id)
            except guards.GuardBlocked as gb:
                if guards.is_g3_only_block(gb.record):
                    guard_record = gb.record
                    gb.record["copy_only"] = {
                        "attempted": True,
                        "copied": None,
                        "bytes": None,
                        "deletes_held": next(
                            (
                                int((r.get("metrics") or {}).get("planned_deletes") or 0)
                                for r in (gb.record.get("results") or ())
                                if isinstance(r, dict) and r.get("guard") == "G3_MASS_DELETE"
                            ),
                            gb.record.get("planned_deletes", 0),
                        ),
                        "error": None,
                    }
                    try:
                        cloud_access_task(config, run_id, artefacts_dir)
                        copy_res = sync(config, fy_prefix, artefacts_dir,
                                        max_delete=0, budget=budget, mode="copy")
                        cstats = (copy_res or {}).get("stats") or {}
                        files_copied = int(cstats.get("transfers") or 0)
                        bytes_copied = int(cstats.get("bytes") or 0)
                        gb.record["copy_only"]["copied"] = files_copied
                        gb.record["copy_only"]["bytes"] = bytes_copied
                    except Exception as copy_exc:  # noqa: BLE001 - copy-only failure never hides the G3 block
                        gb.record["copy_only"]["error"] = str(copy_exc)[:300]
                        logger.warning(f"Cloud copy-only on G3 block failed: {copy_exc}")
                raise
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
                                artefacts_dir=artefacts_dir, **scope)
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
            if "_error" in size:
                logger.warning(
                    f"Cloud size check failed (reporting-only, backup gate unaffected): {size['_error']}"
                )
            evidence_ok = (
                verify_data.get("verified") is True
                and verify_data.get("verify_termination", "abnormal") == "normal"
                and len(diff.get("added", [])) == 0
                and len(diff.get("modified", [])) == 0
                and not diff.get("_partial")
                and verify_data.get("manifest_error") is None
            )
            if (
                not evidence_ok
                and getattr(config.cloud, "verify_recheck_enabled", True) is True
                and verify_data.get("verify_termination", "abnormal") == "normal"
                and not diff.get("_partial")
                and verify_data.get("manifest_error") is None
                and (diff.get("added") or diff.get("modified"))
            ):
                logger.warning("Cloud verify found diff; running one recheck pass (AF-12)")
                _t_recheck = time.monotonic()
                verify_data = verify_report(config, fy_prefix, budget=budget,
                                            artefacts_dir=artefacts_dir)
                stage_seconds["verify_recheck_s"] = round(time.monotonic() - _t_recheck, 1)
                diff = verify_data.get("diff") or {}
                size = verify_data.get("size") or {}
                if "_error" in size:
                    logger.warning(
                        f"Cloud size check failed on recheck (reporting-only): {size['_error']}"
                    )
                evidence_ok = (
                    verify_data.get("verified") is True
                    and verify_data.get("verify_termination", "abnormal") == "normal"
                    and len(diff.get("added", [])) == 0
                    and len(diff.get("modified", [])) == 0
                    and not diff.get("_partial")
                    and verify_data.get("manifest_error") is None
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
                                artefacts_dir=artefacts_dir, **scope)
                alert_sent = True
                raise RuntimeError(verify_err)

            phase = "post"

            # BUG-01: ManifestDB record MUST only happen after evidence_ok passes.
            # M6: a failed manifest query is NEVER recorded as bucket state.
            manifest_error = verify_data.get("manifest_error")
            if not _writes_entries(fy_pass):
                logger.info(f"Overlap pass {fy_pass.fy}: no file_entries rows written (plan D-9)")
            elif manifest_error:
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
            # The next-FY pass has no cross-check: its DB state is the configured FY's (plan D-9).
            cross_check_skipped = not _writes_entries(fy_pass)
            diff_copied = None if cross_check_skipped else _cloud_diff_count(
                verify_data.get("manifest", []), before_dict)
            stats = sync_result.get("stats")
            if isinstance(stats, dict):
                files_copied, bytes_copied = stats.get("transfers"), stats.get("bytes")
                counts_source = "rclone"
            else:
                files_copied = bytes_copied = None
                counts_source = "unavailable"
                logger.warning("rclone final stats missing - files/bytes copied recorded as unknown")
            metrics_consistent = (None if cross_check_skipped
                                  else files_copied is not None and diff_copied == files_copied)
            if not cross_check_skipped and files_copied is not None and not metrics_consistent:
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
                cloud_publish_artifact_task(verify_data, sync_result, files_copied, bytes_copied,
                                            **_artifact_kwargs(fy_pass, "cloud-backup-summary"))
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
                                artefacts_dir=artefacts_dir, **scope)
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

        except BaseException as e:   # A2: a cancel / stop is also decided here, then re-raised
            error_msg = _error_text(e)
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
                alert = ((config, "cloud", status, _failure_alert_text("cloud", status, error_msg),
                          sync_result.get("exit_code"), started_at),
                         dict(run_id=run_id, fault_code=fault_code, artefacts_dir=artefacts_dir,
                              **scope))
                if isinstance(e, Exception):
                    _send_run_alert(*alert[0], **alert[1])
                else:
                    # A cancel gives the process only Prefect's grace (30 s) before a forced kill; the
                    # email (SMTP retries, minutes when the mail server is down) must not delay the
                    # run row - it is sent right after the record below.
                    deferred_alert = alert
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
            if fy_pass is not None:
                extended_metrics = _merge_metrics(extended_metrics,
                                                  **_pass_metrics(fy_pass, cloud=True))
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
            if deferred_alert is not None:
                _send_run_alert(*deferred_alert[0], **deferred_alert[1])
            if status == "CLOUD_BLOCKED" and guard_record:
                _open_block_request(config, "cloud", run_id, guard_record, started_at,
                                    artefacts_dir, sync_attempted=bool(sync_state.get("attempts")),
                                    keep_fy=_other_fy(fy_pass), **scope)
            elif statuses.is_success(status):
                _send_recovery_if_needed(config, "cloud", run_id, **scope)


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
                      late_seconds: float = 0.0, fy_pass=None, budget_start: float | None = None,
                      wake_nas=None):
    """Execute LAN backup tasks sequentially. Each task is independently tracked.

    allow_shutdown=False (the final backup of an FY rollover, plan A4): the NAS is NOT powered
    off at any call site - the new NAS FY folder is created after this run.
    late_seconds: see _run_cloud_pipeline (plan I2).
    fy_pass / budget_start: see _run_cloud_pipeline (plan 2.3)."""
    run_id = _pass_run_id(run_id, fy_pass)
    scope = _alert_scope(fy_pass)
    # WP14: the run budget counts from the job's start (I2: its scheduled start), including
    # the slot wait
    budget = RunBudget(
        "lan",
        (budget_hours if budget_hours is not None else config.budget.lan_max_hours) * 3600,
        _budget_origin(budget_start, monotonic_start) - late_seconds)
    _missed_if_no_budget(config, "lan", run_id, started_at, budget,
                         config.budget.lan_post_reserve_minutes * 60, late_seconds,
                         monotonic_start, fy_pass=fy_pass, **scope)
    with _pipeline_slot(config, "lan", run_id, started_at, monotonic_start, fy_pass=fy_pass, **scope):

        # Apply config-driven retries to tasks that benefit from retrying
        preflight = lan_preflight_task.with_options(
            retries=config.lan.preflight_retries,
            retry_delay_seconds=config.lan.preflight_retry_delay_seconds,
        )
        # XR-2/XR-6: no Prefect retries for the mirror - _sync_with_retries
        # decides (lan.max_attempts / retry_delay_seconds) and re-runs the
        # dry-run measurement + guards before any repeat of the mirror.
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
        artefacts = _open_run_artefacts(config, run_id, started_at)
        artefacts_dir = str(artefacts.dir) if artefacts else None
        # Failure-path telemetry (same contract as cloud): measured stages
        # persist even when the run fails; unexecuted stages stay absent.
        stage_seconds: dict = {}
        phase = "pre"
        guard_record = None
        alert_sent = False   # WP8: exactly one alert per failed run
        deferred_alert = None   # A2: an interrupted run records itself first, alerts second
        nas_shutdown = None  # WP10: verified shutdown result, if one was attempted
        nas_already_up = False
        wake_status = None

        try:
            approval_id = _approval_of_pass(config, approval_id, fy_pass)
            health_check_task(config, "lan", artefacts_dir)
            if wake_nas is not None:
                wake_status = wake_nas()
            else:
                wake_status = wol_check_task(config)
            nas_already_up = (wake_status == "already_up")
            if fy_pass is not None and fy_pass.extra:
                # plan 2.3: the next FY's NAS folder is made once the NAS is awake (offline-safe)
                _ensure_pass_nas_folder(config, fy_pass.fy)
            else:
                # plan A7: a rollover whose NAS folder could not be made (NAS was off) is
                # completed now that the NAS is awake - before G5 looks for the mount marker
                _complete_pending_rollover_nas_folder(config)
            plan = preflight(config, artefacts_dir)
            try:
                guard_record = lan_guards_task(
                    config, _effective_fy_prefix(config), plan,
                    approval_id=approval_id, run_id=run_id,
                )
            except guards.GuardBlocked as gb:
                if guards.is_g3_only_block(gb.record):
                    guard_record = gb.record
                    gb.record["copy_only"] = {
                        "attempted": True,
                        "copied": None,
                        "bytes": None,
                        "deletes_held": plan.get("planned_deletes", 0) if isinstance(plan, dict) else 0,
                        "error": None,
                    }
                    try:
                        lan_access_task(config, plan, run_id, copy_only=True)
                        copy_res = sync(config, artefacts_dir, max_delete=0,
                                        budget=budget, mode="copy")
                        cstats = (copy_res or {}).get("stats") or {}
                        files_copied = int(cstats.get("transfers") or 0)
                        bytes_copied = int(cstats.get("bytes") or 0)
                        gb.record["copy_only"]["copied"] = files_copied
                        gb.record["copy_only"]["bytes"] = bytes_copied
                    except Exception as copy_exc:  # noqa: BLE001 - copy-only failure never hides the G3 block
                        gb.record["copy_only"]["error"] = str(copy_exc)[:300]
                        logger.warning(f"LAN copy-only on G3 block failed: {copy_exc}")
                raise
            lan_access_task(config, plan, run_id)
            phase = "sync"
            _t_sync = time.monotonic()
            caps = {"max_delete": guard_record["max_delete"]}
            attempt_clock = {"epoch": time.time()}

            def remeasure(already_deleted: int) -> dict:
                nonlocal guard_record
                fresh = preflight(config, artefacts_dir)
                guard_record = lan_guards_task(
                    config, _effective_fy_prefix(config), fresh,
                    approval_id=approval_id, run_id=run_id, already_deleted=already_deleted,
                )
                lan_access_task(config, fresh, run_id)
                caps["max_delete"] = guard_record["max_delete"]
                return guard_record

            def run_once() -> dict:
                # the arrival check asks "changed since the LAST attempt began?"
                attempt_clock["epoch"] = time.time()
                try:
                    result = sync(config, artefacts_dir, max_delete=caps["max_delete"],
                                  budget=budget)
                except SyncFailed as e:
                    if isinstance(e.result, dict) and e.result.get("log_path"):
                        sync_logs.append(e.result["log_path"])
                    raise
                if isinstance(result, dict) and result.get("log_path"):
                    sync_logs.append(result["log_path"])
                return result

            sync_logs: list[str] = []   # S3: every attempt's copy log, in order
            sync_result = _sync_with_retries(
                "lan",
                run_once=run_once,
                remeasure=remeasure,
                deletes_of=lambda r: cloud_sync.rclone_deletes_of(r, caps["max_delete"]),
                retryable=lambda r: r.get("retryable") is True,
                max_attempts=config.lan.max_attempts,
                delay_s=config.lan.retry_delay_seconds, budget=budget,
                reserve_s=config.budget.lan_post_reserve_minutes * 60, state=sync_state)
            sync_seconds = round(time.monotonic() - _t_sync, 1)
            stage_seconds["sync_s"] = sync_seconds
            status = sync_result["status"]
            arrival = None
            if status == "LAN_COMPLETE":
                phase = "verify"
                _t_verify = time.monotonic()
                try:
                    arrival = lan_verify_task(
                        config, artefacts_dir, changed_after_epoch=attempt_clock["epoch"],
                        budget=budget)
                except BudgetExceeded as e:
                    logger.error(str(e))
                    status = "LAN_PARTIAL"
                    sync_result = {**sync_result, "status": status, "budget_stopped": True,
                                   "error": _budget_stop_text(
                                       budget, "copied but not verified: no time left for the "
                                               "arrival check")}
                    sync_state["result"] = sync_result
                stage_seconds["verify_s"] = round(time.monotonic() - _t_verify, 1)
                if arrival is not None and arrival.get("verified") is not True:
                    status = "LAN_SUSPECT"
                    sync_result = {**sync_result, "status": status,
                                   "error": f"Arrival check not verified: {arrival.get('reason')}"}
                    sync_state["result"] = sync_result
                if arrival is not None:
                    # S3: content check of tonight's copies, only after a verified arrival
                    sync_transfers = int((sync_result.get("stats") or {}).get("transfers") or 0)
                    hash_info, hash_paths = _hash_plan(config, sync_logs, arrival, transfers=sync_transfers)
                    if hash_info.get("verified") is False and not hash_paths:
                        status = "LAN_SUSPECT"
                        arrival["verified"] = False
                        sync_result = {**sync_result, "status": status,
                                       "error": "Hash check of tonight's copied files not "
                                                f"possible: {hash_info.get('reason')}"}
                        sync_state["result"] = sync_result
                    if hash_paths:
                        _t_hash = time.monotonic()
                        hash_res = None
                        try:
                            hash_res = lan_hash_task(
                                config, artefacts_dir, hash_paths,
                                changed_after_epoch=attempt_clock["epoch"], budget=budget)
                        except BudgetExceeded as e:
                            logger.error(str(e))
                            status = "LAN_PARTIAL"
                            arrival["verified"] = False
                            hash_info.update(ran=False, verified=False,
                                             reason="no time left for tonight's hash check")
                            sync_result = {**sync_result, "status": status,
                                           "budget_stopped": True,
                                           "error": _budget_stop_text(
                                               budget, "copied but not verified: no time left "
                                                       "for tonight's hash check")}
                            sync_state["result"] = sync_result
                        stage_seconds["hash_s"] = round(time.monotonic() - _t_hash, 1)
                        if hash_res is not None:
                            hash_info.update(ran=True, verified=hash_res.get("verified") is True,
                                             reason=hash_res.get("reason"))
                            for _k in ("matching", "missing", "differ", "changed_during_backup"):
                                hash_info[_k] = hash_res.get(_k)
                            hash_info["unverified_paths"] = list(
                                hash_res.get("unverified_paths") or [])[:config.lan.error_tail_lines]
                            if hash_res.get("verified") is not True:
                                status = "LAN_SUSPECT"
                                arrival["verified"] = False
                                sync_result = {**sync_result, "status": status,
                                               "error": "Hash check of tonight's copied files "
                                                        f"not verified: {hash_res.get('reason')}"}
                                sync_state["result"] = sync_result
            phase = "post"
            files_failed = int(sync_result.get("files_failed") or 0)
            stats = sync_result.get("stats")
            if isinstance(stats, dict):
                files_copied, bytes_copied = stats.get("transfers"), stats.get("bytes")
            else:
                files_copied = bytes_copied = None
            failed_paths = list(sync_result.get("failed_paths") or [])[:config.lan.error_tail_lines]
            deletes_held = bool(sync_result.get("deletes_held"))
            metrics = {
                "stage_seconds": dict(stage_seconds),
                "counts_source": "rclone" if isinstance(stats, dict) else "unavailable",
                "tool_counts": {k: stats.get(k) for k in ("transfers", "bytes", "deletes",
                                                          "renames", "errors", "checks")}
                if isinstance(stats, dict) else None,
                "files_deleted": stats.get("deletes") if isinstance(stats, dict) else None,
                "moves": (plan.get("moves")
                          if isinstance(plan, dict) and getattr(config.lan, "versions_enabled", False) is True
                          else (stats.get("renames") if isinstance(stats, dict) else None)),
                "deletes_held": deletes_held,
                "failed_paths": failed_paths,
            }
            if arrival is not None:
                metrics["arrival"] = {k: arrival.get(k) for k in (
                    "verified", "matching", "missing", "differ", "changed_during_backup", "reason")}
                metrics["arrival"]["hash"] = hash_info
                metrics["total_files"] = arrival.get("matching")
            else:
                metrics["verify"] = ("skipped - rclone was killed after its time limit"
                                     if sync_result.get("killed_after_cutoff")
                                     else "skipped - stopped at the run budget"
                                     if sync_result.get("budget_stopped")
                                     else "skipped - the mirror did not complete")
            extended_metrics = json.dumps(metrics)

            logger.info(f"LAN pipeline completed with status {status}")
            # F3: shut the NAS down ONLY after a fully complete mirror (LAN_COMPLETE).
            # A PARTIAL run (rclone exit 1/4/6/10) means some files did not land —
            # powering off the NAS now strands them until the next WoL, and the
            # partial state must be reported, not silent.
            if status == "LAN_COMPLETE":
                if getattr(config.lan, "versions_enabled", False) is True:
                    try:
                        from core import lan_versions
                        purge_res = lan_versions.purge_old_versions(
                            config.paths.lan_destination,
                            config.lan.versions_folder,
                            config.lan.versions_retention_days,
                            artefacts_dir=artefacts_dir,
                            timeout_s=config.lan.versions_purge_timeout_seconds,
                        )
                        if purge_res.get("failed"):
                            logger.warning(f"LAN versions purge had failures: {purge_res['failed']}")
                    except Exception as purge_exc:  # noqa: BLE001 - purge never fails a completed backup
                        logger.warning(f"LAN versions purge failed: {purge_exc}")
                skip_already_up = (
                    nas_already_up
                    and getattr(config.lan, "shutdown_only_if_woken", True) is True
                )
                if allow_shutdown:
                    if skip_already_up:
                        logger.info("skipping shutdown: NAS was already online")
                    else:
                        nas_shutdown = lan_shutdown_task(config, artefacts_dir)
            elif status == "LAN_PARTIAL":
                skip_already_up = (
                    nas_already_up
                    and getattr(config.lan, "shutdown_only_if_woken", True) is True
                )
                shutdown_on_fail = (allow_shutdown and not skip_already_up and getattr(
                    config.lan, "shutdown_on_all_retries_exhausted", False) is True)
                nas_msg = (
                    "The NAS is scheduled for shutdown as shutdown_on_all_retries_exhausted is enabled."
                    if shutdown_on_fail
                    else "The NAS was NOT shut down; the next scheduled run will re-sync."
                )
                logger.error(
                    f"LAN backup PARTIAL (rclone exit {sync_result.get('exit_code')}): "
                    f"some files were not copied. {nas_msg}"
                )
                # The alert goes out BEFORE any NAS shutdown below.
                held_note = (" No deletions were made tonight (rclone deletes nothing after a "
                             "read error); they run on the next clean night."
                             if deletes_held else "")
                partial_text = (f"LAN backup PARTIAL: {sync_result.get('error')}{held_note} "
                                f"{nas_msg}").strip()
                if sync_result.get("budget_stopped"):
                    error_msg = sync_result["error"]
                _send_run_alert(config, "lan", status, partial_text,
                                sync_result.get("exit_code"), started_at,
                                run_id=run_id, sync_result=sync_result,
                                artefacts_dir=artefacts_dir, **scope)
                alert_sent = True
                if shutdown_on_fail:
                    logger.info("shutdown_on_all_retries_exhausted enabled: powering down NAS after partial backup alert")
                    nas_shutdown = lan_shutdown_task(config, artefacts_dir)
            elif status == "LAN_SUSPECT":
                error_msg = sync_result["error"]
                _send_run_alert(config, "lan", status,
                                _failure_alert_text("lan", status, error_msg,
                                                    "The NAS was NOT shut down; the next "
                                                    "scheduled run will re-sync."),
                                sync_result.get("exit_code"), started_at,
                                run_id=run_id, fault_code="AAM-LAN-04",
                                artefacts_dir=artefacts_dir, **scope)
                alert_sent = True
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

        except BaseException as e:   # A2: a cancel / stop is also decided here, then re-raised
            error_msg = _error_text(e)
            # XR: a failed attempt's own verdict (exit code, FAILED / SUSPECT)
            sync_result = sync_state.get("result", sync_result)
            current = sync_result.get("status", status) if phase == "sync" else status
            status = statuses.decide_terminal_status("lan", phase, current, _exception_kind(e))
            if isinstance(e, guards.GuardBlocked):
                guard_record = e.record
            fault_code = faults.classify_run("lan", status, exc=e, guard_record=guard_record,
                                             sync_result=sync_result)
            # An interrupted run (service stop, cancel) never powers the NAS off,
            # and shutdown_only_if_woken applies on failure paths too.
            shutdown_on_fail = (
                allow_shutdown
                and isinstance(e, Exception)
                and wake_status is not None
                and getattr(config.lan, "shutdown_on_all_retries_exhausted", False) is True
                and not (nas_already_up and getattr(config.lan, "shutdown_only_if_woken", True) is True)
            )
            if not alert_sent and not isinstance(e, guards.GuardBlocked):
                # The one alert of this run, sent BEFORE any NAS shutdown below.
                nas_msg = (
                    "The NAS is scheduled for shutdown as shutdown_on_all_retries_exhausted is enabled."
                    if shutdown_on_fail
                    else "The NAS was NOT shut down; the next scheduled run will re-sync."
                )
                alert = ((config, "lan", status, _failure_alert_text("lan", status, error_msg, nas_msg),
                          sync_result.get("exit_code"), started_at),
                         dict(run_id=run_id, fault_code=fault_code, artefacts_dir=artefacts_dir,
                              **scope))
                if isinstance(e, Exception):
                    _send_run_alert(*alert[0], **alert[1])
                else:
                    deferred_alert = alert          # a cancel: record first, alert second (see cloud)
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
            if fy_pass is not None:
                extended_metrics = _merge_metrics(extended_metrics, **_pass_metrics(fy_pass))
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
            if deferred_alert is not None:
                _send_run_alert(*deferred_alert[0], **deferred_alert[1])
            if status == "LAN_BLOCKED" and guard_record:
                _open_block_request(config, "lan", run_id, guard_record, started_at,
                                    artefacts_dir, sync_attempted=bool(sync_state.get("attempts")),
                                    keep_fy=_other_fy(fy_pass), **scope)
            elif statuses.is_success(status):
                _send_recovery_if_needed(config, "lan", run_id, **scope)


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
    maint = getattr(config, "maintenance", None)
    retries = getattr(maint, "record_run_retries", 3)
    if not isinstance(retries, int) or retries < 1:
        retries = 3
    delay_ms = getattr(maint, "record_run_retry_delay_ms", 200)
    if not isinstance(delay_ms, (int, float)) or delay_ms < 0:
        delay_ms = 200
    recorded = False
    db = ManifestDB.from_config(config)
    try:
        for attempt in range(retries):
            if record_run_history(
                db,
                run_id=run_id, mode=mode,
                started_at=started_at, ended_at=ended_at,
                status=status, exit_code=exit_code,
                duration_seconds=duration, error_message=error_msg,
                files_copied=files_copied, bytes_copied=bytes_copied,
                files_failed=files_failed,
                extended_metrics=extended_metrics,
            ):
                recorded = True
                break
            if attempt + 1 < retries:
                time.sleep((delay_ms / 1000.0) * (attempt + 1))
    finally:
        db.close()
    if not recorded:
        logger.critical(
            f"Run history persistence failed for run_id={run_id} mode={mode} "
            f"status={status} exit_code={exit_code}"
        )
        logger.warning(f"Run {run_id} ({mode}) was not recorded to database - check logs above for details")


# ═══════════════════════════════════════════════════════════════
# Report flows
# ═══════════════════════════════════════════════════════════════

def _enabled_legs(config) -> tuple[str, ...]:
    return tuple(leg for leg in ("cloud", "lan") if getattr(config, leg).enabled)


def _scheduled_time():
    """Plan C (G-9): when this flow run was SCHEDULED to start, so a report sent late (the agent was
    down) still covers the right calendar period. A manual run or no flow context: now."""
    try:
        from prefect.context import FlowRunContext
        ctx = FlowRunContext.get()
        run = ctx.flow_run if ctx else None
        if run is not None and getattr(run, "auto_scheduled", False) and run.expected_start_time:
            return pendulum.instance(run.expected_start_time).in_timezone(IST)
    except Exception as e:  # noqa: BLE001 - an unreadable schedule means "now", never a missing report
        logger.warning(f"Scheduled start time unknown ({e}); the report covers the period before now")
    return pendulum.now(IST)


def _send_summary_report(config, days: int, period: str) -> None:
    """Send a summary report truthfully (plan WP8, C): the previous calendar week / month of the
    flow run's scheduled time; delivery is recorded in the notifications table and a failed
    delivery FAILS the flow. A period with no runs is still reported ("action needed: no backup
    ran") - the week nothing ran is the week the owner must hear about."""
    db = ManifestDB.from_config(config)
    try:
        scheduled = _scheduled_time()
        report_period = (ReportPeriod.previous_month(scheduled) if period == "Monthly"
                         else ReportPeriod.previous_week(scheduled))
        email = report.build_summary_email(db, config.firm_name, days, period,
                                           legs=_enabled_legs(config),
                                           report_period=report_period,
                                           schedule=config.schedule,
                                           alerts_off=alert_class.switched_off(config),
                                           config=config)
        subject, body, attachments = email
        rungs, labels = attachment_ladder(attachments or [])
        result = send_email(config.notifications, subject, body, audience=AUDIENCE_REPORTS,
                            attachment_rungs=rungs or None)
        try:
            db.record_notification({
                "event_key": f"report:{period.lower()}", "kind": "REPORT", "mode": "report",
                "subject": subject, "status": result.status, "error": result.error,
                "attempts": result.attempts, "created_at": now_iso(),
                "attachments_rung": (labels[result.rung] if labels and result.rung is not None
                                     else None),
                "audience": AUDIENCE_REPORTS, "recipients": result.recipients})
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

    * run artefacts (full rclone logs): run_logs.retention_days
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
        # FY overlap (plan section 3): add the next year on its activation day, before the alarm below (which
        # then sees the placeholder-only folder). Never raises; a waiting clock is mailed inside.
        fy_overlap.activate_if_due(config, db)
        _alarm_unprotected_new_fy(config, db)     # first: the blocked days below raise before the end
        _send_pending_approval_reminders(config, db)
        return _rollover_resume(config, config_path, approval_id, db)
    finally:
        db.close()


def _send_pending_approval_reminders(config, db) -> None:
    """B5: send at most one reminder per due ladder rung (1, 2, 4, 7, then weekly) for PENDING approvals."""
    try:
        pending = [r for r in db.open_approvals() if r.get("status") == approvals.PENDING]
        now = now_iso()
        rungs = list(getattr(getattr(config, "approvals", None), "reminder_days", [1, 2, 4, 7]))
        for req in pending:
            aid = req.get("approval_id")
            if not aid or not req.get("created_at"):
                continue
            sent = set()
            for r in rungs + [rungs[-1] + 7 * k for k in range(1, 53)]:
                if db.last_notification(f"approval:reminder:{aid}:day{r}", status="SENT"):
                    sent.add(r)
            due = approvals.due_reminder_rung(req["created_at"], now, rungs, sent)
            if due is None:
                continue
            leg = req.get("leg") or "lan"
            guard_items = [g for g in (req.get("guards") or []) if isinstance(g, dict)]
            first_guard = guard_items[0] if guard_items else {}
            guard_name = first_guard.get("guard") or ""
            fault_code = (
                first_guard.get("fault_code")
                or faults.GUARD_CODES.get(guard_name)
                or ("AAM-ROL-02" if leg == "rollover" else "AAM-DEL-01")
            )
            status = (
                "ROLLOVER_BLOCKED" if leg == "rollover"
                else ("LAN_BLOCKED" if leg == "lan" else "CLOUD_BLOCKED")
            )
            summary = approvals.waiting_summary(req, now_iso=now)
            if guard_name == "G3_MASS_DELETE" or not guard_name:
                msg = f"Pending mass-delete approval {aid}: {summary['waiting_label']}."
            else:
                guard_list = ", ".join(str(g.get("guard") or "") for g in guard_items if g.get("guard"))
                msg = f"Pending {leg} approval {aid} ({guard_list}): {summary['waiting_label']}."
            notify(config, AlertEvent(
                key=f"approval:reminder:{aid}:day{due}",
                kind="ACTION_REQUIRED",
                mode=leg,
                title=f"Reminder: {summary['waiting_label']}",
                message=msg,
                status=status,
                fault_code=fault_code,
            ), db=db)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Approval reminder check failed: {e}")


def _resolve_unprotected_new_fy(config, db, new_fy: str | None = None) -> None:
    """The matching RESOLVED mail - once - when the switch made the new year's files protected, but only
    for a problem the office was actually mailed about. `new_fy` is passed by the rollover run that just
    switched (its config object still holds the old year); the daily check reads the config instead.
    Own mail group `rollover_files`, so it never ends an unrelated rollover incident (the archive warning)."""
    configured = new_fy or fy_rollover.configured_fy(config.paths.source_drive, config.paths.lan_destination)
    if configured is None:
        return
    problem = db.last_notification(f"rollover:unprotected:{configured}", status="SENT")
    if problem is None or db.last_notification(f"recovered:rollover:{configured}", status="SENT"):
        return
    notify(config, AlertEvent(
        key=f"recovered:rollover:{configured}", kind="RECOVERED", mode="rollover_files",
        title=f"Files in {configured} are backed up now",
        message=f"The switch to {configured} is done: the files saved in its folder are part of the "
                "backups now.",
        status="RECOVERED", resolved_code="AAM-ROL-06"))


def _alarm_unprotected_new_fy(config, db) -> None:
    """Plan FY-6: files in the next FY's folder are not backed up until the switch. Read-only, so it also
    runs while the rollover is blocked (G6 / unproven clock); a failure here never stops the rollover."""
    try:
        found = fy_rollover.unprotected_new_fy(config, db)
        if not found:
            _resolve_unprotected_new_fy(config, db)
            return
        notify(config, AlertEvent(
            key=f"rollover:unprotected:{found['to_fy']}", kind="ACTION_REQUIRED", mode="rollover_files",
            title=f"Files in {found['to_fy']} are not backed up yet",
            message=fy_rollover.unprotected_message(found), occurred_at=now_iso(),
            fault_code="AAM-ROL-06", cooldown_hours=config.alerts.stale_repeat_hours))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Check for unprotected files in the new financial year failed: {e}")


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
    hand_switched = False
    reentered = False
    if journal is not None:
        configured = fy_rollover.configured_fy(source, lan_dest)
        switch_done = rj.is_finished(journal["steps"][rj.CONFIG_SWITCH])
        if not switch_done and configured == journal["to_fy"]:
            if rj.is_finished(journal["steps"][rj.FINAL_BACKUPS]):
                # R3: the config was written but the process died before the journal saw it
                # (the steps run in order, so the final backup is already done). The switch is
                # done; a "final backup" now would back up the NEW folders.
                logger.warning(f"FY rollover {journal['from_fy']} -> {journal['to_fy']}: "
                               "config.yaml already points at the new FY - switch step closed")
                db.rollover_step_done(journal["from_fy"], journal["to_fy"], rj.CONFIG_SWITCH,
                                      now_iso())
                journal = db.rollover_get(journal["from_fy"], journal["to_fy"])
                switch_done = True
            else:
                # D7: NOT a crash - the final backup never completed, so config.yaml was switched
                # by hand while the rollover was blocked. Closing the switch would back up the NEW
                # year as the "final backup" and archive the old year without its last changes.
                hand_switched = True
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
        finished = db.rollover_get(from_fy, to_fy)         # none is open (checked above), so it is finished
        reentered = finished is not None and kind in (fy_rollover.FORWARD, fy_rollover.JUMP)

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

    if hand_switched:
        msg = (f"FY rollover {from_fy} -> {to_fy} stopped: config.yaml already points at {to_fy} "
               f"(it was changed by hand), but the last backup of {from_fy} was never completed, "
               f"so the last changes of {from_fy} are not protected yet. Nothing was changed or "
               f"archived. Ask support to change source_drive and lan_destination in config.yaml on the "
               f"server back to {from_fy} (Settings no longer changes the financial year); the daily "
               "check then finishes the old year first and switches by itself.")
        logger.error(msg)
        record("ROLLOVER_BLOCKED", msg, {})
        notify(config, AlertEvent(
            key="rollover:switched_by_hand", kind="ACTION_REQUIRED", mode="rollover",
            title="FY rollover stopped - settings were switched before the old year was backed up",
            message=msg, status="ROLLOVER_BLOCKED", occurred_at=started_at,
            fault_code="AAM-ROL-02", run_id=run_id))
        raise fy_rollover.RolloverError(msg)

    if reentered:
        msg = fy_rollover.reentered_message(from_fy, to_fy)
        logger.error(msg)
        record("ROLLOVER_BLOCKED", msg, {})
        notify(config, AlertEvent(
            key=f"rollover:reentered:{to_fy}", kind="ACTION_REQUIRED", mode="rollover",
            title="FY rollover not repeated - the settings were put back to the old year",
            message=msg, status="ROLLOVER_BLOCKED", occurred_at=started_at,
            fault_code="AAM-ROL-04", run_id=run_id))
        raise fy_rollover.RolloverError(msg)

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
    final_copy = None      # plan section 4, only while the overlap of to_fy is active
    forced_legs: list[str] = []
    try:    # final_backups may be finished already (a resume): what it found was saved with the overlap record
        saved = fy_overlap.load_final_copy(db, to_fy) if rj.is_finished(journal["steps"][rj.FINAL_BACKUPS]) \
            else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not read the saved final-copy state of {from_fy}: {e}")
        saved = None
    if saved:
        final_copy, forced_legs = saved["final_copy"], saved["forced_legs"]
        final_status = saved["final_status"]

    def step_final_backups():
        nonlocal final_status, guard_record, final_copy, forced_legs
        logger.info(f"FY rollover {from_fy} -> {to_fy} ({kind}): final backup of {from_fy}")
        overlap = fy_overlap.overlap_open(config, db, to_fy)
        started: dict = {}
        final_status, final_errors = _run_final_backups(config, run_id, skip_unscheduled=overlap,
                                                        started=started)
        g6_extra: dict = {}
        if overlap:
            # a manual-only leg is not run by the check; a verified copy from the closing moment on counts
            final_copy = fy_overlap.final_copy_state(config, db, from_fy, {
                leg: {"status": status, "started_at": started.get(leg)} for leg, status in final_status.items()})
            final_status = {leg: item["status"] for leg, item in final_copy.items() if item["required"]}
            g6_extra = {"overlap_active": True, "hints": {leg: item["hint"] for leg, item in final_copy.items()
                                                          if item["outstanding"]}}
        g6 = guards.g6_fy_rollover(from_fy, to_fy, kind, final_status, **g6_extra)
        approved, approval_record = _apply_approval(config, "rollover", approval_id, run_id, [g6])
        try:
            guard_record = guards.enforce("rollover", [g6], approved_guards=approved,
                                          **{k: v for k, v in metrics.items()
                                             if k in ("from_fy", "to_fy", "kind", "clock")},
                                          **approval_record)
        except guards.GuardBlocked as e:
            record("ROLLOVER_BLOCKED", str(e),
                   {"guards": e.record, "final_errors": final_errors,
                    **({"final_copy": final_copy} if overlap else {})})
            _open_block_request(config, "rollover", run_id, e.record, started_at)
            raise
        if overlap:
            forced_legs = fy_overlap.outstanding_legs(final_copy)   # non-empty only for a forced switch
            try:    # a resume after the switch (announce / ROLLOVER_COMPLETE row) does not repeat this step
                fy_overlap.save_final_copy(db, to_fy, final_copy, forced_legs, final_status)
            except Exception as e:  # noqa: BLE001 - only the wording of a later resume depends on it
                logger.warning(f"Could not save the final-copy state of {from_fy}: {e}")

    def step_source_folder():
        fy_rollover.create_source_folder(
            src_root, to_fy, placeholder_name=config.rollover.source_placeholder_name)

    nas_wake_status = None

    def step_nas_folder():
        nonlocal nas_wake_status
        if not config.lan.enabled:
            return ("skipped", "LAN backup is disabled")
        if config.wol.enabled:
            nas_wake_status = wol.ensure_server_online_status(config)
            if nas_wake_status == "failed":
                raise RuntimeError("NAS did not wake up for rollover folder creation")
        fy_rollover.create_nas_folder(lan_root, to_fy)

    def step_config_switch():
        nonlocal config_backup
        config_backup = fy_rollover.update_config_yaml(
            config_path, src_root, lan_root, to_fy, expect_fy=from_fy)
        # AF-16: stamp overlap ended inside the backup slot right when the switch happens
        # (and before archive/other steps), rather than waiting until after the slot closes.
        try:
            fy_overlap.end_overlap(db, from_fy, to_fy, now_iso())
        except Exception as e:  # noqa: BLE001 - retried idempotently after run_steps
            logger.warning(f"Could not end the FY overlap record of {to_fy} in step_config_switch: {e}")

    def step_archive():
        nonlocal archive_ok
        if not config.cloud.enabled:
            return ("skipped", "cloud backup is disabled")
        archive_ok = fy_rollover.run_archive_transition(
            bucket=config.cloud.bucket, old_fy=from_fy,
            gcs_key_path=config.paths.gcs_key_path,
            auth_timeout=config.health.rollover_auth_timeout_seconds,
            archive_timeout=config.health.rollover_archive_timeout_seconds,
            config_dir=os.path.join(config.paths.runtime_dir, "gcloud"),
        )
        if not archive_ok:
            raise RuntimeError(f"Archive transition of {from_fy} failed - retried every morning")

    def step_nas_shutdown():
        if not (config.lan.enabled and config.lan.shutdown_after_backup
                and config.wol.enabled):
            return ("skipped", "NAS shutdown is not configured")
        if nas_wake_status == "already_up" and getattr(config.lan, "shutdown_only_if_woken", True) is True:
            return ("skipped", "NAS was already online before rollover")
        lan_shutdown_task(config)

    def step_announce():
        pending = [s for s in rj.STEPS if s != rj.ANNOUNCE
                   and not rj.is_finished(db.rollover_get(from_fy, to_fy)["steps"][s])]
        todo = ("" if not pending else
                " A few follow-up steps are still being retried automatically "
                f"({', '.join(p.replace('_', ' ') for p in pending)}); your data is safe.")
        overlap_note = fy_overlap.announce_text(final_copy or {}, from_fy, forced_legs)
        message = (f"From today your backups use the new folder {to_fy}. The {from_fy} "
                   f"backups are kept unchanged.{todo or ' Nothing to do.'}")
        if overlap_note:
            message += f" {overlap_note}"
        notify(config, AlertEvent(
            key=f"rollover:done:{to_fy}", kind="INFO", mode="rollover",
            title=f"New financial year {to_fy} started",
            message=message,
            status="ROLLOVER_COMPLETE", run_id=run_id, occurred_at=started_at,
            audience=AUDIENCE_REPORTS))

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
        if (config.rollover.create_source_folder_early and kind == fy_rollover.FORWARD and switch_pending
                and not rj.is_finished(journal["steps"][rj.SOURCE_FOLDER])):
            # plan FY-7: the office can save into the new year from day 1 even if the final backup below
            # fails. Needs no slot. A failure here does not stop the old year's final backup; the step
            # stays failed and run_steps retries it as a BLOCKING step before the switch.
            early_ok, early_error = _rollover_step(db, from_fy, to_fy, rj.SOURCE_FOLDER, run_id,
                                                   step_source_folder)
            journal = db.rollover_get(from_fy, to_fy)
            if not early_ok:       # nobody would otherwise know the office has no new folder to save into
                notify(config, AlertEvent(
                    key=f"rollover:{to_fy}:source_folder_early", kind="WARNING", mode="rollover",
                    title=f"The new {to_fy} folder could not be created yet",
                    message=(f"The folder {to_fy} for the new financial year could not be created: {early_error}. "
                             f"Until it exists the office has nowhere to save new-year files. It is tried again "
                             "every morning and before the switch; your data is safe."),
                    status="ROLLOVER_FAILED", occurred_at=started_at, fault_code="AAM-ROL-04", run_id=run_id))
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
    if rj.is_finished(final["steps"][rj.CONFIG_SWITCH]):       # the new year's files are protected from now on
        try:
            fy_overlap.end_overlap(db, from_fy, to_fy, now_iso())    # plan 4: no-op without an overlap record
        except Exception as e:  # noqa: BLE001 - the switch stands; retried only while an unfinished step makes the next morning's check resume this journal. Once the journal is finished the record keeps no ended_at (harmless: the config points at the new FY)
            logger.warning(f"Could not end the FY overlap record of {to_fy}: {e}")
        try:
            _resolve_unprotected_new_fy(config, db, new_fy=to_fy)
        except Exception as e:  # noqa: BLE001 - a mail problem never changes the rollover's result
            logger.warning(f"Could not send the 'files are backed up now' notice: {e}")
    if blocking_error is not None:
        fail(*blocking_error)

    overlap_metrics = {"final_copy": final_copy, "forced_legs": forced_legs} if final_copy is not None else {}
    record("ROLLOVER_COMPLETE", None, {"guards": guard_record, "final_status": final_status,
                                       "archive_ok": archive_ok,
                                       "config_backup": str(config_backup), **overlap_metrics})
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


def _run_final_backups(config, run_id: str, *, skip_unscheduled: bool = False,
                       started: dict | None = None) -> tuple[dict, dict]:
    """Final backup of the closing FY on every enabled leg, through the normal
    pipelines (config still points at the closing FY). Returns
    ({leg: terminal status}, {leg: error}).

    skip_unscheduled (plan section 4, overlap only): a manual-only leg (`scheduled: false`) is not run by the
    check. `started` (optional dict) receives each leg's start time, so the April gate can date its run."""
    status: dict = {}
    errors: dict = {}
    for leg, enabled, pipeline in (("cloud", config.cloud.enabled, _run_cloud_pipeline),
                                   ("lan", config.lan.enabled, _run_lan_pipeline)):
        if not enabled:
            continue
        if skip_unscheduled and not getattr(config, leg).scheduled:
            continue
        try:
            # plan A4: the LAN final backup leaves the NAS on - its new FY folder follows
            extra = {"allow_shutdown": False} if leg == "lan" else {}
            started_at = now_iso()
            if started is not None:
                started[leg] = started_at
            result = pipeline(config, f"{run_id}-final-{leg}", started_at, time.monotonic(),
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
def integrity_audit_flow(config_path: str = CONFIG_PATH, mode: str = "all", fy: str | None = None):
    """Read-only weekly integrity audit: source↔backup content verification.

    This flow NEVER modifies source or destination and NEVER rewrites
    historical backup rows. It records one `integrity_audits` manifest row
    per enabled leg (VERIFIED | VERIFICATION_FAILED) and alerts + fails on
    divergence. Absence of an audit row means NOT_VERIFIED.

    Modes: cloud | lan | all (both sequentially, cloud first).

    FY overlap (plan 2.5, D-13): while the overlap is active every audited leg runs a pass for the next FY first and
    then for the configured FY, in this one slot and on this one budget; the LAN leg wakes the NAS once and shuts it
    down after the last pass. `fy` audits one active FY only (the dashboard). A scheduled run skips a leg that is not
    scheduled (`scheduled: false`: a manual-only NAS may be off); a run started by hand audits every enabled leg.
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

    # FY overlap (plan 2.5, D-13): the passes of each audited leg, the next FY first (it is small), then the
    # configured FY. Without an active overlap there is one baseline pass per leg (no suffix, the config as is).
    overlap_error = None
    try:
        active = fy_overlap.active_passes(config, lambda f: _overlap_record(config, f))
    except Exception as e:  # noqa: BLE001 - the configured FY is still audited; the flow ends FAILED (below)
        active, overlap_error = [], e
    if fy is not None and not any(p.fy == fy.upper() for p in active):
        why = f" (the FY overlap records could not be read: {overlap_error})" if overlap_error else ""
        raise ValueError(f"{fy} is not an active financial year{why}; refusing to audit it.")
    if len(active) > 1:
        ordered = [p for p in active if p.extra] + [p for p in active if not p.extra]
        passes = [p for p in ordered if fy is None or p.fy == fy.upper()]
    else:
        passes = [None]

    db = ManifestDB.from_config(config)
    scheduled_run = _auto_scheduled_run()
    legs = [leg for leg in ("cloud", "lan")
            if mode in (leg, "all") and getattr(config, leg).enabled
            and not (scheduled_run and not getattr(config, leg).scheduled)]
    recorded: set[tuple[str, str | None]] = set()
    excs = []
    outcomes: list[str] = []   # "<leg>[@<FY>]=<status>" of each non-VERIFIED pass: part of the alert key
    if overlap_error is not None:
        excs.append(RuntimeError(f"the FY overlap records could not be read ({overlap_error}): the next FY was not "
                                 "audited; the flow ends FAILED so it is not dropped silently"))

    def _suffix_of(p) -> str | None:
        return p.fy if p is not None and p.extra else None

    def _audit_id_of(leg: str, suffix: str | None) -> str:
        audit_id = new_audit_id(leg, "full")
        return fy_overlap.pass_run_id(audit_id, suffix) if suffix else audit_id

    def _record(leg: str, audit_id: str, started_at: str, result: dict, suffix: str | None = None) -> None:
        # A VERIFIED verdict is durable only once persisted: a failed DB
        # write must fail the audit loudly (alert + FAILED flow), never
        # silently drop a passing result or leave NOT_VERIFIED behind a
        # COMPLETED run.
        name = ("Cloud" if leg == "cloud" else "LAN") + (f" {suffix}" if suffix else "")
        key = f"{leg}@{suffix}" if suffix else leg
        recorded.add((leg, suffix))
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
            outcomes.append(f"{key}=NOT_RECORDED")
        if result["status"] not in (statuses.AuditStatus.VERIFIED, statuses.AuditStatus.PENDING_BACKUP):
            excs.append(RuntimeError(_audit_outcome_text(name, result)))
            outcomes.append(f"{key}={result['status']}")

    def _audit_cloud_leg(audit_id: str, pc, suffix: str | None, last: bool) -> dict:
        try:
            cloud_timeout = int(budget.cap(pc.cloud.verify_timeout_seconds))
        except BudgetExceeded as e:
            return _audit_budget_incomplete(e)
        audit_artefacts = _open_run_artefacts(config, audit_id)
        try:
            with temp_rclone_config(
                pc.paths.gcs_key_path,
                pc.cloud.location,
                pc.cloud.project_number,
                pc.cloud.storage_class,
            ) as rclone_cfg:
                return audit_cloud(
                    pc.paths.source_drive, pc.cloud.bucket,
                    _effective_fy_prefix(pc), rclone_cfg,
                    timeout=cloud_timeout, artefacts=audit_artefacts,
                    pending=_pending_context(pc, db, "cloud", suffix),
                    **_audit_settings(config),
                )
        finally:
            _finalize_run_artefacts(config, audit_artefacts)

    # One NAS wake-up and one shutdown for every LAN pass of this run (plan D-8). A wake-up that failed is not
    # retried for the next pass: that pass fails the same way without a second wake-up.
    nas = {"woken": False, "wake_error": None, "wake_status": None}

    def _wake_nas_once() -> None:
        if nas["wake_error"] is not None:
            raise nas["wake_error"]
        if not nas["woken"]:
            try:
                nas["wake_status"] = wol_check_task(config)
            except Exception as e:
                nas["wake_error"] = e
                raise
            nas["woken"] = True

    def _shut_nas_down(artefacts_dir: str | None) -> None:
        # Only after a successful wake; a failed shutdown never replaces the verdict.
        if nas["woken"] and config.lan.shutdown_after_backup and config.wol.enabled:
            if (nas.get("wake_status") == "already_up"
                    and getattr(config.lan, "shutdown_only_if_woken", True) is True):
                logger.info("skipping shutdown: NAS was already online")
                return
            try:
                lan_shutdown_task(config, artefacts_dir)
            except Exception as e:  # noqa: BLE001
                logger.error(f"NAS shutdown after the LAN audit failed: {e}")

    def _audit_lan_leg(audit_id: str, pc, suffix: str | None, last: bool) -> dict:
        try:
            lan_timeout = int(budget.cap(pc.lan.subprocess_timeout_seconds,
                                         config.budget.lan_post_reserve_minutes * 60))
        except BudgetExceeded as e:
            if last:
                _shut_nas_down(None)    # an earlier pass may have woken the NAS: this pass is its last word
            return _audit_budget_incomplete(e)   # this pass did not wake the NAS: nothing to do
        _wake_nas_once()
        audit_artefacts = _open_run_artefacts(config, audit_id)
        try:
            return audit_lan(
                pc.paths.source_drive, pc.paths.lan_destination,
                timeout=lan_timeout, artefacts=audit_artefacts,
                pending=_pending_context(pc, db, "lan", suffix), **_audit_settings(config),
            )
        finally:
            if last:
                _shut_nas_down(str(audit_artefacts.dir) if audit_artefacts else None)
            _finalize_run_artefacts(config, audit_artefacts)

    try:
        try:
            with _backup_slot(config):
                for leg, run_leg in (("cloud", _audit_cloud_leg), ("lan", _audit_lan_leg)):
                    if leg not in legs:
                        continue
                    for index, p in enumerate(passes):
                        suffix = _suffix_of(p)
                        audit_id, started_at = _audit_id_of(leg, suffix), now_iso()
                        try:
                            result = run_leg(audit_id, p.config if p is not None else config, suffix,
                                             index == len(passes) - 1)
                        except Exception as e:  # noqa: BLE001 - recorded + alerted, never silent
                            logger.error(f"Integrity audit ({leg}{'@' + suffix if suffix else ''}) could not run: {e}")
                            result = {"status": "AUDIT_INCOMPLETE", "scope": "full",
                                      "files_checked": None, "bytes_checked": None,
                                      "mismatches": 0,
                                      "detail": f"could not run - {type(e).__name__}: {e}"}
                        _record(leg, audit_id, started_at, result, suffix)
        except Exception as e:  # noqa: BLE001 - backup slot / lock not obtained
            logger.error(f"Integrity audit could not run: {e}")
            for leg in legs:
                for p in passes:
                    suffix = _suffix_of(p)
                    if (leg, suffix) not in recorded:
                        _record(leg, _audit_id_of(leg, suffix), now_iso(), {
                            "status": "AUDIT_INCOMPLETE", "scope": "full", "files_checked": None,
                            "bytes_checked": None, "mismatches": 0,
                            "detail": f"could not run - backup slot not obtained: {e}"}, suffix)

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
        logger.info("AAM integrity audit finished - no enabled leg has a real difference "
                    "(newer files may wait for the next backup)")
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# Main backup flow — entry point for all modes
# ═══════════════════════════════════════════════════════════════

def _pass_kwargs(fy_pass, overlap_on: bool, leg_start: float, index: int = 0, leg_late: float = 0.0) -> dict:
    """The FY keywords of a pipeline call (plan 2.2): none while there is no overlap (the baseline call).
    Every pass of a leg shares the first pass's budget origin `leg_start - leg_late` (plan D-7): the first pass
    gets late_seconds=leg_late with budget_start=leg_start, a later pass late_seconds=0 with the late origin as
    its budget_start (the pipeline counts `budget_start - late_seconds`)."""
    if not overlap_on:
        return {}
    return {"fy_pass": fy_pass, "budget_start": leg_start if index == 0 else leg_start - leg_late}


def _overlap_record(config, fy: str):
    """The activation record of an FY (plan 2.1); the database is opened only when the overlap is asked."""
    db = ManifestDB.from_config(config)
    try:
        return db.overlap_record(fy)
    finally:
        db.close()


def _switched_fy(config_path: str, started_fy: str) -> str | None:
    """Race guard (plan 2.2): the rollover switched the configured FY while this run was going. config.yaml is
    read again before each later pass. Returns the new configured FY, or None when it is unchanged. An
    unreadable config counts as switched ("unreadable"): the pass does not start."""
    try:
        now = load_config(config_path)
        current = fy_rollover.configured_fy(now.paths.source_drive, now.paths.lan_destination)
    except Exception as e:  # noqa: BLE001 - fail closed: the remaining passes do not start
        logger.error(f"FY race guard: config.yaml could not be read ({e}) - the remaining passes do not start")
        return "unreadable"
    if current == started_fy:
        return None
    logger.error(f"FY race guard: the configured FY changed from {started_fy} during this run - "
                 "the remaining passes do not start")
    return str(current)


@flow(name="aam-backup", log_prints=True)
def backup(config_path: str = CONFIG_PATH, mode: str = "all", approval_id: str | None = None,
           budget_hours: float | None = None, fy: str | None = None):
    """AAM Backup Automation — nightly backup orchestrator.

    Each pipeline step is a separate Prefect task — visible in the Prefect UI
    with individual state, timing, logs, and retries.

    Modes:
        cloud — Run only cloud backup (rclone sync → GCS)
        lan   — Run only LAN backup (rclone mirror, includes WoL + shutdown)
        all   — Run both sequentially (cloud first, then LAN)

    approval_id: set only by the dashboard when a human approved a BLOCKED
    run once (plan §6.2); scheduled runs never pass one.

    budget_hours: manual runs only (e.g. the first full upload over a
    weekend): replaces budget.<leg>_max_hours for this run. Scheduled runs
    never pass it, so the schedule check (WP14) stays valid.

    fy: one financial year's pass of an overlap (plan 2.2, the dashboard's per-FY buttons). An FY that is
    not active raises before anything runs. Without it every active FY runs, the configured one first.
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

    # FY overlap (plan 2.2): the passes of this run, the configured FY first. Without an active
    # overlap there is one pass and every call is the baseline call (fy_pass None, no FY keywords).
    overlap_unchecked = ""   # the next FY, when its activation record could not be read (review F2)
    try:
        active = fy_overlap.active_passes(config, lambda fy: _overlap_record(config, fy))
    except Exception as e:  # noqa: BLE001 - a database that cannot be read never blocks the configured FY's backup
        logger.error(f"FY overlap: the next FY's activation record could not be read ({e}) - this run backs up "
                     "the configured FY only; the flow ends FAILED so the next FY is not dropped silently")
        active = []
        try:
            configured = fy_rollover.configured_fy(config.paths.source_drive, config.paths.lan_destination)
            overlap_unchecked = fy_overlap.next_fy(configured) if configured else "the next financial year"
        except Exception:  # noqa: BLE001 - only names the year in the message
            overlap_unchecked = "the next financial year"
        if fy is not None:
            raise ValueError(f"{fy} could not be checked: the FY overlap record could not be read ({e}); "
                             "refusing to run.") from e
    overlap_on = len(active) > 1
    if fy is not None:
        wanted = fy.upper()
        if not any(p.fy == wanted for p in active):
            active_names = ", ".join(p.fy for p in active) or "none"
            raise ValueError(f"{fy} is not an active financial year (active: {active_names}); refusing to run.")
    passes = [p for p in active if fy is None or p.fy == fy.upper()]
    runs = passes or [None]   # no FY folder to back up: the baseline call, once per leg
    started_fy = active[0].fy if active else None

    # ── Watchdog lock + serialization now live INSIDE each pipeline ──
    # P2-CONC: the concurrency slot and the lock moved into _backup_slot()
    # (acquired per pipeline) so direct callers - scripts, manual runs,
    # tests - serialize on the same limit as production flows. The old
    # flow-level wrapper is intentionally GONE in this same commit; keeping
    # both would self-deadlock the limit=1 resource.
    excs = []
    # plan I2: only the FIRST leg of this flow run is late; a second leg starts when it starts
    late_s = 0.0 if budget_hours is not None else _scheduled_lateness_seconds()
    pass_no = 0   # passes started so far in this run (the race guard applies from the second one)
    switched_to: str | None = None   # set by the race guard: the in-memory config is stale for every later pass
    not_run: list[str] = []          # "<leg> <FY>" of the passes the race guard stopped

    try:
        # ── Cloud ──
        if mode in ("cloud", "all") and config.cloud.enabled:
            logger.info("Starting cloud backup pipeline")
            cloud_run_id = _stable_run_id("cloud")
            leg_start = time.monotonic()
            for index, p in enumerate(runs):
                if overlap_on and switched_to is None:
                    switched_to = _switched_fy(config_path, started_fy)
                if switched_to is not None:
                    not_run.append(f"cloud {p.fy}")
                    continue
                pass_no += 1
                try:
                    _run_cloud_pipeline(p.config if p is not None else config, cloud_run_id, now_iso(),
                                        time.monotonic(), approval_id=approval_id, budget_hours=budget_hours,
                                        late_seconds=late_s if index == 0 else 0.0,
                                        **_pass_kwargs(p, overlap_on, leg_start, index, late_s))
                except Exception as e:
                    excs.append(e)
            late_s = 0.0

        # ── LAN ──
        if mode in ("lan", "all") and config.lan.enabled:
            logger.info("Starting LAN backup pipeline")
            lan_run_id = _stable_run_id("lan")
            leg_start = time.monotonic()
            earlier_completed = True   # every pass before this one ended LAN_COMPLETE (plan D-8)
            nas = {"woken": False, "wake_error": None, "wake_status": None}

            def _wake_nas_once():
                if nas["wake_error"] is not None:
                    raise nas["wake_error"]
                if not nas["woken"]:
                    try:
                        nas["wake_status"] = wol_check_task(config)
                    except Exception as e:
                        nas["wake_error"] = e
                        raise
                    nas["woken"] = True
                return nas["wake_status"]

            for index, p in enumerate(runs):
                if overlap_on and switched_to is None:
                    switched_to = _switched_fy(config_path, started_fy)
                if switched_to is not None:
                    not_run.append(f"lan {p.fy}")
                    continue
                pass_no += 1
                last = index == len(runs) - 1
                extra = _pass_kwargs(p, overlap_on, leg_start, index, late_s)
                if overlap_on:
                    # plan D-8: one NAS shutdown, after the last pass, and only when every earlier pass completed
                    # (or the failure shutdown is configured, the rule of today)
                    extra["allow_shutdown"] = last and (
                        earlier_completed or config.lan.shutdown_on_all_retries_exhausted is True)
                    extra["wake_nas"] = _wake_nas_once
                try:
                    result = _run_lan_pipeline(p.config if p is not None else config, lan_run_id, now_iso(),
                                               time.monotonic(), approval_id=approval_id,
                                               budget_hours=budget_hours,
                                               late_seconds=late_s if index == 0 else 0.0, **extra)
                    earlier_completed = earlier_completed and isinstance(result, dict) \
                        and result.get("status") == "LAN_COMPLETE"
                except Exception as e:
                    earlier_completed = False
                    excs.append(e)
            if (
                overlap_on
                and nas["woken"]
                and switched_to is not None
                and config.lan.shutdown_after_backup
                and (earlier_completed or config.lan.shutdown_on_all_retries_exhausted is True)
                and not (nas["wake_status"] == "already_up"
                         and getattr(config.lan, "shutdown_only_if_woken", True) is True)
            ):
                try:
                    lan_shutdown_task(config, None)
                except Exception as e:
                    excs.append(e)

        if switched_to is not None:
            # a non-success end must raise (CLAUDE.md): the passes after the switch did not run
            excs.append(PartialRun(
                f"configured FY changed {started_fy}->{switched_to} while this run was going; "
                f"passes not run: {', '.join(not_run)}. The next scheduled run backs up the new year."))
        if overlap_unchecked:
            excs.append(RuntimeError(
                f"{overlap_unchecked} could not be checked (the FY overlap record could not be read), so "
                "only the configured FY was backed up this run. Open the dashboard; the next run checks again."))

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
