"""AAM Backup Watchdog — Prefect Server health monitor.

Runs as AamWatchdog Windows Service (via NSSM).

Pattern: External Watchdog Service — same concept as Kubernetes liveness
probes, Docker HEALTHCHECK, and IIS Windows Process Activation Service (WAS).

Watches BOTH services:
  - AamPrefectServer (API health via HTTP)
  - AamBackupAgent (SCM state) — G4: the in-process scheduler lives there;
    a healthy API with a stopped agent is a silent total stoppage.

F6: services that are STOPPED (not hung) are started directly via `sc start`
— SCM failure actions never revive a merely-stopped service. Auto-starts are
circuit-breaker limited to 3/hour per service (mirrors the SCM failure
budget set at install time); exhaustion logs CRITICAL and demands manual
intervention instead of flapping the service forever.

HOW IT WORKS
────────────
1. Polls http://127.0.0.1:4200/api/health every 60 seconds.
2. After FAILURE_THRESHOLD (5) consecutive failures (~5 minutes), considers
   the Prefect server hung-but-alive.
3. Before acting, ALWAYS checks for an active backup via two signals:
    - Signal A (real transfer): rclone.exe / robocopy.exe process is alive.
      → Defer up to MAX_TRANSFER_DEFERRALS (default 240 × 2 min = 8 h).
        A legitimate transfer (max 6 h by subprocess_timeout_seconds) is
        never cut short. At the cap with a PID-live lock owner, log a
        file/log-only CRITICAL and keep deferring (never unlink a live
        lock, never restart under it). Only a dead/absent lock owner
        proceeds to restart.
    - Signal B (lock only, no transfer process): lock file exists + PID alive
      but no rclone/robocopy detected. Expected between rclone calls
      (verify/manifest/report phases) — defer indefinitely while the owner
      is PID-live; at the MAX_DEFERRALS cap (~30 min) log a file/log-only
      CRITICAL and keep deferring. A PID-dead/reused lock is not a live
      backup: it is treated as no-backup and collected via restart/boot-heal.
   - No lock and no transfer process → restart promptly.
   - If no backup → restarts the stack IN ORDER (WP13): stop AamBackupAgent
     → wait STOPPED → stop AamPrefectServer → wait STOPPED → start the server
     → wait for the API → start the agent. (The agent depends on the server:
     `sc stop AamPrefectServer` alone is refused with 1051 - production logs.)
4. After a restart, sleeps RESTART_COOLDOWN before resuming health checks.
5. A service stuck in STOP_PENDING beyond watchdog.stop_pending_timeout_seconds
   has its process tree killed - ONLY when no backup holds the lock; otherwise
   a CRITICAL alert and the watchdog waits (production: 14+ min STOP_PENDING
   with no escalation).

Every CRITICAL is also emailed through core.notify (de-duplicated, recorded
in the notifications table -> dashboard); a RECOVERED email follows once the
API is healthy and the agent RUNNING again. All tunables: config `watchdog:`.
The watchdog's own log is watchdog_app.log (NSSM keeps watchdog_svc.log for
stdout), so the two writers never interleave in one file (plan WP2).

BACKUP LOCK PROTOCOL
─────────────────────
flow.py writes a PID-stamped backup.lock file (derived from config.yaml
database_path.parent) at the START of every backup flow, and deletes it
in a finally block on completion (normal or exception). The watchdog reads
the same path from config.yaml at startup via _resolve_paths(). It validates
the PID is still alive before honouring the lock, so stale locks from a
previous crash do not prevent restarts indefinitely.

Zero new dependencies: httpx and loguru are already project requirements.
"""

import contextlib
import sys
import time
from pathlib import Path

from loguru import logger

from core import services
from models.config import RunLogsConfig, WatchdogConfig

# ── Configuration ─────────────────────────────────────────────────────────────
# The live values are module globals (tests patch them); they are set from the
# `watchdog:` config section by _apply_config() - defaults live in the model.
WATCHED_SERVICE        = "AamPrefectServer"
AGENT_SERVICE          = "AamBackupAgent"   # G4: hosts the scheduler + dashboard


def _apply_config(wc: WatchdogConfig, run_logs: RunLogsConfig | None = None) -> None:
    global PREFECT_HEALTH_URL, CHECK_INTERVAL_SECONDS, FAILURE_THRESHOLD, REQUEST_TIMEOUT
    global BACKUP_WAIT_INTERVAL, MAX_DEFERRALS, MAX_TRANSFER_DEFERRALS, RESTART_COOLDOWN
    global MAX_STARTS_PER_WINDOW, START_WINDOW_SECONDS, STOP_PENDING_TIMEOUT, KILL_WAIT
    global API_READY_TIMEOUT, SERVICE_POLL, SC_TIMEOUT, LOG_ROTATION_MB, LOG_RETENTION_DAYS
    global STALE_CHECK_INTERVAL
    PREFECT_HEALTH_URL = wc.prefect_health_url
    CHECK_INTERVAL_SECONDS = wc.check_interval_seconds
    FAILURE_THRESHOLD = wc.failure_threshold
    REQUEST_TIMEOUT = wc.request_timeout_seconds
    BACKUP_WAIT_INTERVAL = wc.backup_wait_interval_seconds
    # MAX_DEFERRALS: lock held, no transfer process. MAX_TRANSFER_DEFERRALS: a
    # real transfer - well above the longest legitimate one (6 h), so it is
    # never cut short while a zombie rclone is still recovered.
    MAX_DEFERRALS = wc.max_deferrals
    MAX_TRANSFER_DEFERRALS = wc.max_transfer_deferrals
    RESTART_COOLDOWN = wc.restart_cooldown_seconds
    # Circuit breaker for auto-starts (mirrors the SCM failure-action budget):
    # a crash-looping service is never flapped forever by the watchdog.
    MAX_STARTS_PER_WINDOW = wc.max_starts_per_window
    START_WINDOW_SECONDS = wc.start_window_seconds
    STOP_PENDING_TIMEOUT = wc.stop_pending_timeout_seconds
    KILL_WAIT = wc.kill_wait_seconds
    API_READY_TIMEOUT = wc.api_ready_timeout_seconds
    SERVICE_POLL = wc.service_poll_seconds
    SC_TIMEOUT = wc.sc_timeout_seconds
    LOG_ROTATION_MB = wc.log_rotation_mb
    LOG_RETENTION_DAYS = (run_logs or RunLogsConfig()).service_log_retention_days
    STALE_CHECK_INTERVAL = wc.stale_check_interval_seconds


_apply_config(WatchdogConfig())

# Defaults — overwritten by _resolve_paths() at startup from config.yaml.
BACKUP_LOCK_PATH: Path = Path(r"C:\BackupAgent\backup.lock")
LOG_DIR: Path          = Path(r"C:\BackupAgent\logs")


def _resolve_paths() -> None:
    """Derive BACKUP_LOCK_PATH, LOG_DIR and the watchdog tunables from config.yaml.

    Falls back to the defaults if config is missing or invalid.
    Called once at the start of main() — before _configure_logging().
    """
    global BACKUP_LOCK_PATH, LOG_DIR
    try:
        from models.config import CONFIG_PATH, load_config
        cfg = load_config(CONFIG_PATH)
        BACKUP_LOCK_PATH = cfg.paths.backup_lock_path
        LOG_DIR = Path(cfg.paths.log_directory)
        _apply_config(cfg.watchdog, cfg.run_logs)
    except Exception as exc:
        logger.warning(f"Could not load config.yaml ({exc}) - using default paths and settings")


def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()
    # Own file: NSSM writes this process's stdout to watchdog_svc.log; loguru
    # writing that same file interleaved lines in production (plan WP2).
    logger.add(
        LOG_DIR / "watchdog_app.log",
        level="INFO",
        rotation=f"{LOG_ROTATION_MB} MB",
        retention=f"{LOG_RETENTION_DAYS} days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {message}",
    )
    logger.add(sys.stdout, level="INFO",
               format="{time:HH:mm:ss} | {level:<7} | {message}")


# ── Backup detection ──────────────────────────────────────────────────────────

def _pid_is_alive(pid: int) -> bool:
    """Thin wrapper kept for internal use; prefer read_lock_alive for lock checks."""
    import psutil
    return psutil.pid_exists(pid)


def _transfer_process_running() -> bool:
    """Return True if rclone.exe or robocopy.exe is actively running.

    This is the definitive signal that a real data transfer is in progress.
    Used to decide whether deferral should be indefinite (real transfer) or
    capped (lock-without-transfer — suspicious stale lock scenario).
    """
    try:
        import psutil
        transfer_procs = {"rclone.exe", "robocopy.exe"}
        for proc in psutil.process_iter(["name"]):
            p_name = proc.info.get("name")
            if p_name and p_name.lower() in transfer_procs:
                return True
    except Exception as exc:
        logger.warning(f"Transfer process check failed: {exc}")
    return False


def _is_backup_running() -> bool:
    """Return True if the backup flow that wrote backup.lock is still running.

    Reads the lock file as 'PID:create_time' (new format) or bare 'PID'
    (legacy).  The create_time check guarantees stale PID-reuse is detected
    correctly even if a different Python process inherited the same PID after
    a crash.

    NOTE: This function is intentionally side-effect-free.  It does NOT
    delete a stale lock file.  Stale lock cleanup is handled exclusively by
    the main loop's MAX_DEFERRALS branch, which only acts after enough
    deferral cycles to rule out a transient gap between rclone calls.  Doing
    the cleanup here would bypass all deferral protection and could cause a
    premature restart during an active backup.
    """
    if not BACKUP_LOCK_PATH.exists():
        return False
    try:
        from core.process import read_lock_alive
        alive, pid = read_lock_alive(BACKUP_LOCK_PATH)
        if alive:
            return True
        # Lock PID is gone or reused — report as not running.
        # Main loop deferral logic will force-remove after MAX_DEFERRALS.
        logger.warning(
            f"Stale backup lock detected (PID {pid} not running or reused) "
            f"- deferral counter in main loop will handle cleanup"
        )
    # G6: catch EVERY parse/psutil failure, not just OSError. A corrupted
    # lock file must make the watchdog treat the lock as stale, never crash
    # the main loop (verified crash modes: negative PID -> ValueError, huge
    # PID -> OverflowError — both escaped the old OSError-only catch and
    # crash-looped the service under NSSM).
    except Exception as exc:
        logger.warning(f"Could not read backup lock file (treating as stale): {exc!r}")
    return False


# ── Service management ────────────────────────────────────────────────────────

def _service_state(service: str) -> str:
    """Return the service's SCM STATE string (e.g. 'RUNNING', 'STOPPED',
    'START_PENDING', 'STOP_PENDING', 'PAUSED'), or '' when it cannot be
    determined (service missing, sc timeout, unparseable output).

    F6: the old boolean check collapsed STOPPED and START_PENDING into the
    same 'not running' answer, so the watchdog could not tell 'NSSM is
    already restarting it' from 'it is stopped and nothing will ever
    restart it'. Parsing the state makes that distinction possible.
    """
    status = services.query(service, timeout_s=SC_TIMEOUT)
    if not status.state:
        logger.warning(f"sc queryex {service}: state unknown ({status.detail})")
    return status.state


def _service_is_running(service: str) -> bool:
    """Return True only when the service SCM state is RUNNING.

    Prevents restarting a service that is in START_PENDING (NSSM just
    restarted it) or STOP_PENDING — avoids kicking a transitioning service.
    """
    return _service_state(service) == "RUNNING"


def _start_service(service: str) -> bool:
    """Start a stopped service via Windows SCM.

    F6: SCM failure actions only fire for services that STARTED and then
    failed — a service that is merely STOPPED is never auto-revived by
    Windows itself. The watchdog is the designated reviver (this is the
    standard external-watchdog pattern, cf. NSSM's own documentation).

    Returns True if the start request was accepted by the SCM.
    """
    logger.warning(f"Issuing: sc start {service}")
    return services.start(service, timeout_s=SC_TIMEOUT).ok


# Per-service list of auto-start timestamps (module-level so main() can pass it).
service_start_log: dict = {}


def _start_allowed(service: str) -> bool:
    """Circuit breaker: allow an auto-start unless MAX_STARTS_PER_WINDOW
    starts were already issued for this service within START_WINDOW_SECONDS.

    When the window naturally empties (no starts for an hour), the breaker
    resets on its own. On recovery (service observed RUNNING) main() clears
    the log entry immediately.
    """
    now = time.time()
    recent = [t for t in service_start_log.get(service, []) if now - t < START_WINDOW_SECONDS]
    if len(recent) >= MAX_STARTS_PER_WINDOW:
        return False
    recent.append(now)
    service_start_log[service] = recent
    return True


# ── Alerts ────────────────────────────────────────────────────────────────────

# True after a CRITICAL was emailed; the next fully healthy check sends RECOVERED.
_attention_raised = False


def _notify(event_kwargs: dict) -> None:
    try:
        from core.notify import AlertEvent, notify
        from models.config import CONFIG_PATH, load_config

        notify(load_config(CONFIG_PATH), AlertEvent(mode="watchdog", **event_kwargs))
    except Exception as e:
        logger.warning(f"Watchdog alert delivery skipped/failed: {e}")


def _watchdog_fault(key: str) -> str:
    """core.faults code of a watchdog CRITICAL (docs/EMAIL_CONTENT_SPEC.md §4.8)."""
    if key.startswith("start_suppressed"):
        return "AAM-SVC-03"
    if key.startswith(("stuck_stop", "live_lock_cap")):
        return "AAM-SVC-02"
    return "AAM-SVC-01"


def _critical(key: str, title: str, message: str, status: str,
              fault_code: str | None = None) -> None:
    """Log CRITICAL and email it (ACTION_REQUIRED, de-duplicated by key, recorded)."""
    global _attention_raised
    logger.critical(message)
    _notify({"key": f"watchdog:{key}", "kind": "ACTION_REQUIRED", "title": title,
             "message": message, "status": status,
             "fault_code": fault_code or _watchdog_fault(key)})
    _attention_raised = True


def _send_recovered_if_needed() -> None:
    global _attention_raised
    if not _attention_raised:
        return
    _notify({"key": "watchdog:recovered", "kind": "RECOVERED",
             "title": "Scheduler healthy again",
             "message": (f"The Prefect API is healthy and {AGENT_SERVICE} is RUNNING again "
                         "after the earlier watchdog alert."),
             "status": "RECOVERED"})
    _attention_raised = False


def _check_stale_legs(cfg=None) -> list[str]:
    """Email every enabled leg with no successful backup within
    dashboard.stale_after_hours (plan §2 rule 11) - the same evaluation as the
    dashboard banner (core.attention), so both always agree. This is the only
    alert for runs that never start (scheduler not firing, a flow crashing
    before our code): they write no row and send nothing themselves.
    notify() records every outcome and repeats per alerts.repeat_cooldown_hours.
    Returns the stale legs; never raises."""
    try:
        from core import notify as nt
        from core.attention import compute_attention
        from core.manifest import ManifestDB
        from models.config import CONFIG_PATH, load_config

        cfg = cfg or load_config(CONFIG_PATH)
        db = ManifestDB.from_config(cfg)
        try:
            stale = [i for i in compute_attention(db, cfg) if i["code"].startswith("stale:")]
            for item in stale:
                leg = item["code"].split(":", 1)[1]
                logger.error(f"Stale backup: {item['message']}")
                nt.notify(cfg, nt.AlertEvent(
                    key=f"stale:{leg}", kind="ACTION_REQUIRED", mode=leg,
                    title=f"{leg.upper()} backup overdue",
                    message=(f"{item['message']} Scheduled backups are not completing - "
                             "either each run fails (see its own alert) or the runs are not "
                             "starting at all. Check the dashboard and the services."),
                    status="STALE", fault_code="AAM-SYS-03"), db=db)
        finally:
            db.close()
        return [i["code"].split(":", 1)[1] for i in stale]
    except Exception as e:  # noqa: BLE001 - the watchdog loop must keep running
        logger.warning(f"Stale-backup check skipped: {e}")
        return []


def _alert_wedged_lock(reason: str, pid: int | None = None) -> None:
    """Alert when watchdog deferrals hit their cap on a live lock owner."""
    _critical("live_lock_cap", "Backup lock held while the scheduler is unhealthy",
              f"Watchdog alert: backup lock held by live PID {pid} while Prefect API "
              f"is unhealthy: {reason}. Manual review required.",
              "LIVE_LOCK_CAP_EXCEEDED")


# ── Stop / restart (WP13) ─────────────────────────────────────────────────────

def _resolve_stuck_stop(service: str) -> bool:
    """A service stuck in STOP_PENDING: kill its process tree - never under a backup.

    Returns True when the service ended STOPPED.
    """
    if _is_backup_running():
        _critical(f"stuck_stop:{service}", f"{service} stuck stopping during a backup",
                  f"{service} has been STOP_PENDING for over {STOP_PENDING_TIMEOUT}s while a "
                  "backup holds the lock - NOT killing it (that would kill the backup). "
                  f"Manual review required: sc queryex {service}",
                  "STOP_PENDING_UNDER_BACKUP")
        return False
    status = services.query(service, timeout_s=SC_TIMEOUT)
    if status.state == services.STOPPED:
        return True
    if status.state != services.STOP_PENDING or not status.pid:
        _critical(f"stuck_stop:{service}", f"{service} could not be stopped",
                  f"{service} did not stop within {STOP_PENDING_TIMEOUT}s and cannot be killed "
                  f"(state {status.state or 'unknown'}, PID {status.pid}). "
                  f"Manual intervention required: sc queryex {service}",
                  "STOP_FAILED")
        return False
    logger.error(f"{service} STOP_PENDING for over {STOP_PENDING_TIMEOUT}s and no backup is "
                 f"running - killing service process PID {status.pid}")
    outcome = services.kill_process_tree(status.pid)
    logger.error(f"{service}: {outcome}")
    after = services.wait_for(service, {services.STOPPED}, timeout_s=KILL_WAIT,
                              poll_s=SERVICE_POLL, sc_timeout_s=SC_TIMEOUT,
                              clock=time.monotonic, sleep=time.sleep)
    if after.state != services.STOPPED:
        _critical(f"stuck_stop:{service}", f"{service} could not be stopped",
                  f"{service} still {after.state or 'unknown'} {KILL_WAIT}s after killing PID "
                  f"{status.pid} ({outcome}). Manual intervention required.",
                  "STOP_FAILED")
        return False
    return True


def _stop_and_wait(service: str) -> bool:
    status = services.query(service, timeout_s=SC_TIMEOUT)
    if status.state == services.STOPPED:
        return True
    if status.state != services.STOP_PENDING:
        logger.warning(f"Issuing: sc stop {service}")
        result = services.stop(service, timeout_s=SC_TIMEOUT)
        if not result.ok:
            _critical("restart_failed", f"{service} could not be stopped",
                      f"sc stop {service} failed (exit {result.exit_code}): {result.detail}. "
                      "The scheduler restart was abandoned - manual intervention required.",
                      "RESTART_FAILED")
            return False
    status = services.wait_for(service, {services.STOPPED}, timeout_s=STOP_PENDING_TIMEOUT,
                               poll_s=SERVICE_POLL, sc_timeout_s=SC_TIMEOUT,
                               clock=time.monotonic, sleep=time.sleep)
    if status.state == services.STOPPED:
        return True
    return _resolve_stuck_stop(service)


def _wait_api_healthy() -> bool:
    deadline = time.monotonic() + API_READY_TIMEOUT
    while True:
        if _check_health():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(SERVICE_POLL)


def _restart_stack() -> bool:
    """Stop agent -> stop server -> start server -> API healthy -> start agent.

    The agent DEPENDS ON the server: stopping the server first is refused with
    1051 while the agent runs (production logs), so the agent goes first.
    """
    logger.error(f"Restarting {AGENT_SERVICE} + {WATCHED_SERVICE} in order")
    for service in (AGENT_SERVICE, WATCHED_SERVICE):
        if not _stop_and_wait(service):
            return False
    for service in (WATCHED_SERVICE, AGENT_SERVICE):
        logger.warning(f"Issuing: sc start {service}")
        result = services.start(service, timeout_s=SC_TIMEOUT)
        if not result.ok:
            _critical("restart_failed", f"{service} could not be started",
                      f"sc start {service} failed (exit {result.exit_code}): {result.detail}. "
                      "Manual intervention required.", "RESTART_FAILED")
            return False
        status = services.wait_for(service, {services.RUNNING}, timeout_s=API_READY_TIMEOUT,
                                   poll_s=SERVICE_POLL, sc_timeout_s=SC_TIMEOUT,
                                   clock=time.monotonic, sleep=time.sleep)
        if status.state != services.RUNNING:
            _critical("restart_failed", f"{service} did not start",
                      f"{service} is {status.state or 'unknown'} {API_READY_TIMEOUT}s after "
                      "sc start. Manual intervention required.", "RESTART_FAILED")
            return False
        if service == WATCHED_SERVICE and not _wait_api_healthy():
            _critical("restart_failed", "Prefect API not healthy after restart",
                      f"{WATCHED_SERVICE} is RUNNING but its API did not answer within "
                      f"{API_READY_TIMEOUT}s; {AGENT_SERVICE} was not started. "
                      "Manual intervention required.", "RESTART_FAILED",
                      fault_code="AAM-SVC-04")
            return False
    logger.info("Restart complete: server RUNNING, API healthy, agent RUNNING")
    return True


# ── Health check ──────────────────────────────────────────────────────────────

def _check_health() -> bool:
    """Return True if Prefect API health endpoint responds HTTP 200."""
    import httpx
    try:
        resp = httpx.get(PREFECT_HEALTH_URL, timeout=REQUEST_TIMEOUT)
        return resp.status_code == 200
    except Exception:
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    global BACKUP_LOCK_PATH, LOG_DIR
    _resolve_paths()
    _configure_logging()
    logger.info("=" * 60)
    logger.info("AAM Backup Watchdog starting")
    logger.info(f"  Target:    {PREFECT_HEALTH_URL}")
    logger.info(f"  Interval:  {CHECK_INTERVAL_SECONDS}s")
    logger.info(f"  Threshold: {FAILURE_THRESHOLD} consecutive failures (~{FAILURE_THRESHOLD} min)")
    logger.info(f"  Lock file: {BACKUP_LOCK_PATH}")
    logger.info("=" * 60)

    failures = 0
    # H4: independent deferral counters. A shared counter let a long stretch
    # of transfer-deferrals pre-satisfy the stale-lock cap, so the watchdog
    # force-deleted a LIVE lock the moment the transfer ended (skipping its
    # intended ~30 min grace) and restarted under an active backup.
    transfer_deferrals = 0  # capped at MAX_TRANSFER_DEFERRALS (~8 h)
    lock_deferrals = 0      # capped at MAX_DEFERRALS (~30 min)
    _alert_sentinel = object()
    _alerted_deadlock_pid = _alert_sentinel
    # WP13: when each service was first seen STOP_PENDING (monotonic seconds)
    pending_since: dict[str, float] = {}

    def _stop_pending_too_long(service: str) -> bool:
        since = pending_since.setdefault(service, time.monotonic())
        return time.monotonic() - since >= STOP_PENDING_TIMEOUT

    next_stale_check = time.monotonic()   # first check right at start (e.g. after a reboot)

    while True:
        try:
            # Independent of the API state: a stale leg matters most when the
            # scheduler is broken.
            if time.monotonic() >= next_stale_check:
                _check_stale_legs()
                next_stale_check = time.monotonic() + STALE_CHECK_INTERVAL

            healthy = _check_health()

            if healthy:
                if failures > 0:
                    logger.info(f"Prefect API healthy (recovered after {failures} failure(s))")
                failures = 0
                transfer_deferrals = 0
                lock_deferrals = 0
                _alerted_deadlock_pid = _alert_sentinel
                # G4: a healthy Prefect API does NOT mean backups are being
                # scheduled. The in-process scheduler + dashboard live in
                # AamBackupAgent; if that service is stopped, deployments never
                # run and nothing else reports it (the server looks perfectly
                # fine). Monitor it directly.
                agent_state = _service_state(AGENT_SERVICE)
                if agent_state != "STOP_PENDING":
                    pending_since.pop(AGENT_SERVICE, None)
                if agent_state == "RUNNING":
                    service_start_log.pop(AGENT_SERVICE, None)  # breaker reset
                    _send_recovered_if_needed()
                elif agent_state == "STOP_PENDING" and _stop_pending_too_long(AGENT_SERVICE):
                    # WP13: production saw 14+ min STOP_PENDING with no escalation.
                    if _resolve_stuck_stop(AGENT_SERVICE):
                        pending_since.pop(AGENT_SERVICE, None)  # STOPPED: started next check
                elif agent_state in ("START_PENDING", "STOP_PENDING", "PAUSED", "PAUSE_PENDING"):
                    logger.info(f"{AGENT_SERVICE} is {agent_state} - waiting for NSSM to finish the transition")
                elif agent_state == "STOPPED":
                    if _start_allowed(AGENT_SERVICE):
                        _start_service(AGENT_SERVICE)
                    else:
                        _critical(
                            f"start_suppressed:{AGENT_SERVICE}", f"{AGENT_SERVICE} is stopped",
                            f"{AGENT_SERVICE} is STOPPED and auto-start is suppressed "
                            f"({MAX_STARTS_PER_WINDOW} starts in the last hour). No backups are "
                            f"being scheduled. Manual intervention required: sc query {AGENT_SERVICE}",
                            "START_SUPPRESSED")
                else:
                    logger.warning(
                        f"{AGENT_SERVICE} state unknown ({agent_state!r}) - "
                        "cannot verify the scheduler is running"
                    )
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # ── Unhealthy ────────────────────────────────────────────────────────
            failures += 1
            logger.warning(
                f"Prefect API unreachable - failure {failures}/{FAILURE_THRESHOLD}"
            )

            if failures < FAILURE_THRESHOLD:
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # ── Threshold reached — check for active backup before acting ────────
            lock_held = _is_backup_running()       # PID-validated lock file
            transferring = _transfer_process_running()  # rclone / robocopy alive

            if transferring:
                # ── Real transfer detected — defer with an 8-hour safety cap ─────
                # Protects legitimate multi-hour backups from being interrupted.
                # The cap (MAX_TRANSFER_DEFERRALS) guards against a zombie rclone
                # process that is alive but making no progress and somehow escaped
                # Python's subprocess_timeout_seconds kill.
                transfer_deferrals += 1
                if transfer_deferrals >= MAX_TRANSFER_DEFERRALS:
                    if lock_held:
                        # Live lock owner: NEVER unlink a PID-live lock and NEVER
                        # restart under it — a second backup must not overlap a
                        # live one. File/log-only CRITICAL (watchdog_svc.log);
                        # not emailed, not shown in the dashboard.
                        logger.critical(
                            f"Prefect API has been unhealthy for {failures} checks and a "
                            f"transfer process has been detected for {transfer_deferrals} deferrals "
                            f"(~{transfer_deferrals * BACKUP_WAIT_INTERVAL // 3600} h) while the "
                            f"backup lock owner is still alive. NOT removing the live lock and NOT "
                            f"restarting. Possible zombie rclone/robocopy belonging to "
                            f"{AGENT_SERVICE} - manual review required."
                        )
                        try:
                            from core.process import read_lock_alive
                            _, pid = read_lock_alive(BACKUP_LOCK_PATH)
                        except Exception:
                            pid = None
                        if _alerted_deadlock_pid != pid:
                            _alert_wedged_lock("Transfer process persists at deferral cap", pid)
                            _alerted_deadlock_pid = pid
                        transfer_deferrals = 0
                        time.sleep(BACKUP_WAIT_INTERVAL)
                        continue
                    logger.error(
                        f"Prefect API has been unhealthy for {failures} checks and a "
                        f"transfer process has been detected for {transfer_deferrals} deferrals "
                        f"(~{transfer_deferrals * BACKUP_WAIT_INTERVAL // 3600} h). "
                        f"Possible zombie rclone/robocopy. Forcing restart. "
                        f"NOTE: the zombie process belongs to {AGENT_SERVICE}, not "
                        f"{WATCHED_SERVICE} - manual kill may still be required."
                    )
                    try:
                        from core.process import read_lock_alive as _read_lock
                        _alive, _pid = _read_lock(BACKUP_LOCK_PATH)
                    except Exception:
                        _alive, _pid = False, None
                    if _pid is not None and not _alive:
                        # Definitely-dead owner only. UNKNOWN (None/unreadable)
                        # fails closed — never blindly delete.
                        with contextlib.suppress(OSError):
                            BACKUP_LOCK_PATH.unlink(missing_ok=True)
                    else:
                        logger.warning(
                            "Transfer-cap reached but lock owner is UNKNOWN or live — "
                            "leaving lock in place (fail-closed)."
                        )
                    transfer_deferrals = 0
                    # H4: fall straight through to the restart logic below — the
                    # old code entered the lock branch using the stale pre-unlink
                    # value of lock_held and burned one extra cycle sleeping.
                else:
                    logger.warning(
                        f"Prefect API has been unhealthy for {failures} checks but a real "
                        f"data transfer is in progress (rclone/robocopy detected). "
                        f"Deferring restart. Will re-check in {BACKUP_WAIT_INTERVAL}s. "
                        f"(transfer-deferral {transfer_deferrals}/{MAX_TRANSFER_DEFERRALS} - cap at 8 h)"
                    )
                    time.sleep(BACKUP_WAIT_INTERVAL)
                    continue

            elif lock_held:
                # ── Lock exists but no transfer process — suspicious ─────────────
                # Could be: (a) flow is between rclone calls (pre/post-flight steps),
                # or (b) stale lock from PID reuse after a crash.
                # Apply the MAX_DEFERRALS cap to avoid waiting forever on (b).
                # H4: this branch counts its OWN cycles only, so a preceding run of
                # transfer-deferrals can never pre-satisfy this cap.
                lock_deferrals += 1
                if lock_deferrals >= MAX_DEFERRALS:
                    # lock_held here means the owner PID + creation time is
                    # confirmed alive: NEVER unlink a live lock and NEVER
                    # restart under it. File/log-only CRITICAL
                    # (watchdog_svc.log); not emailed, not in the dashboard.
                    # (A PID-dead/reused lock never reaches this branch:
                    # _is_backup_running() reports it stale, so the loop
                    # treats it as no-backup and boot-heal collects it.)
                    logger.critical(
                        f"Prefect API has been unhealthy for {failures} checks and backup "
                        f"lock has persisted for {lock_deferrals} deferrals (~{lock_deferrals * BACKUP_WAIT_INTERVAL // 60} min) "
                        f"with no active transfer process, but the lock owner is still alive. "
                        f"NOT removing the live lock and NOT restarting. Manual review required "
                        f"if the backup is wedged in a non-transfer phase."
                    )
                    try:
                        from core.process import read_lock_alive
                        _, pid = read_lock_alive(BACKUP_LOCK_PATH)
                    except Exception:
                        pid = None
                    if _alerted_deadlock_pid != pid:
                        _alert_wedged_lock("Lock persisted with no transfer process at deferral cap", pid)
                        _alerted_deadlock_pid = pid
                    lock_deferrals = 0
                    time.sleep(BACKUP_WAIT_INTERVAL)
                    continue
                else:
                    logger.warning(
                        f"Prefect API has been unhealthy for {failures} checks. "
                        f"Backup lock is held but no transfer process detected - "
                        f"possibly between rclone calls. Deferring restart. "
                        f"Will re-check in {BACKUP_WAIT_INTERVAL}s. "
                        f"(lock-deferral {lock_deferrals}/{MAX_DEFERRALS})"
                    )
                    time.sleep(BACKUP_WAIT_INTERVAL)
                    continue

            # ── No backup running — safe to act ──────────────────────────────────
            state = _service_state(WATCHED_SERVICE)
            if state != "STOP_PENDING":
                pending_since.pop(WATCHED_SERVICE, None)
            if state == "STOP_PENDING" and _stop_pending_too_long(WATCHED_SERVICE):
                # WP13: stuck stopping - kill (no backup is running on this
                # path), then the ordered restart.
                pending_since.pop(WATCHED_SERVICE, None)
                if _resolve_stuck_stop(WATCHED_SERVICE):
                    _restart_stack()
                failures = 0
                time.sleep(RESTART_COOLDOWN)
                continue
            if state in ("START_PENDING", "STOP_PENDING", "PAUSED", "PAUSE_PENDING"):
                # NSSM is already handling a restart — wait.
                logger.info(
                    f"{WATCHED_SERVICE} is {state} (transitioning). "
                    "NSSM is already handling a restart - resetting failure counter."
                )
                failures = 0
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue
            if state != "RUNNING":
                # F6: the service is STOPPED (or unqueryable). SCM failure actions
                # only fire for services that started and then failed — a stopped
                # service is never revived by Windows on its own, so the old code
                # waited here forever. Start it directly (circuit-breaker guarded).
                if _start_allowed(WATCHED_SERVICE):
                    _start_service(WATCHED_SERVICE)
                    failures = 0
                else:
                    _critical(
                        f"start_suppressed:{WATCHED_SERVICE}", f"{WATCHED_SERVICE} is down",
                        f"{WATCHED_SERVICE} is down and auto-start is suppressed "
                        f"({MAX_STARTS_PER_WINDOW} starts in the last hour). No backups are "
                        f"being scheduled. Manual intervention required: sc query {WATCHED_SERVICE}",
                        "START_SUPPRESSED")
                time.sleep(BACKUP_WAIT_INTERVAL)
                continue

            # Service is RUNNING but API is dead — genuine hung state.
            logger.error(
                f"{WATCHED_SERVICE} reports RUNNING but Prefect API has been "
                f"unreachable for {failures} consecutive checks. Triggering restart."
            )
            _restart_stack()
            failures = 0

            logger.info(
                f"Restart triggered. Cooling down {RESTART_COOLDOWN}s before "
                "resuming health checks (gives Prefect time to fully boot)."
            )
            time.sleep(RESTART_COOLDOWN)
        except Exception as exc:
            logger.exception(f"Watchdog main loop error (will retry in {CHECK_INTERVAL_SECONDS}s): {exc}")
            time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
