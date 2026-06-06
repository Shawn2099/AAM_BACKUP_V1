"""AAM Backup Watchdog — Prefect Server health monitor.

Runs as AamWatchdog Windows Service (via NSSM).

Pattern: External Watchdog Service — same concept as Kubernetes liveness
probes, Docker HEALTHCHECK, and IIS Windows Process Activation Service (WAS).

HOW IT WORKS
────────────
1. Polls http://127.0.0.1:4200/api/health every 60 seconds.
2. After FAILURE_THRESHOLD (5) consecutive failures (~5 minutes), considers
   the Prefect server hung-but-alive.
3. Before acting, ALWAYS checks for an active backup via backup.lock:
   - If a backup is running → logs a deferral and waits. Checks again in
     2 minutes. Repeats indefinitely until backup completes. Under NO
     condition does the watchdog interrupt a running backup.
   - If no backup → issues sc stop AamPrefectServer. NSSM + sc failure
     actions restart both AamPrefectServer and AamBackupAgent automatically.
4. After a restart, sleeps 120 seconds (RESTART_COOLDOWN) before resuming
   health checks, giving Prefect time to fully boot.

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

import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

# ── Configuration ─────────────────────────────────────────────────────────────
PREFECT_HEALTH_URL     = "http://127.0.0.1:4200/api/health"
CHECK_INTERVAL_SECONDS = 60     # normal poll interval
FAILURE_THRESHOLD      = 5      # consecutive failures before considering action
REQUEST_TIMEOUT        = 10     # HTTP timeout per health check (seconds)
BACKUP_WAIT_INTERVAL   = 120    # re-check interval while a backup is running
MAX_DEFERRALS          = 15     # force restart after this many consecutive deferrals (~30 min)
RESTART_COOLDOWN       = 120    # wait after triggering a restart before resuming
WATCHED_SERVICE        = "AamPrefectServer"

# Defaults — overwritten by _resolve_paths() at startup from config.yaml.
# Kept as fallback so the service can still start if config is missing.
BACKUP_LOCK_PATH: Path = Path(r"C:\BackupAgent\backup.lock")
LOG_DIR: Path          = Path(r"C:\BackupAgent\logs")


def _resolve_paths() -> None:
    """Derive BACKUP_LOCK_PATH and LOG_DIR from config.yaml.

    Falls back to the hardcoded defaults if config is missing or invalid.
    Called once at the start of main() — before _configure_logging().
    """
    global BACKUP_LOCK_PATH, LOG_DIR
    try:
        from models.config import CONFIG_PATH, load_config
        cfg = load_config(CONFIG_PATH)
        BACKUP_LOCK_PATH = Path(cfg.paths.database_path).parent / "backup.lock"
        LOG_DIR = Path(cfg.paths.log_directory)
    except Exception:
        pass  # defaults already set at module level


def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        LOG_DIR / "watchdog_svc.log",
        level="INFO",
        rotation="10 MB",
        retention=5,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {message}",
    )
    logger.add(sys.stdout, level="INFO",
               format="{time:HH:mm:ss} | {level:<7} | {message}")


# ── Backup detection ──────────────────────────────────────────────────────────

def _pid_is_alive(pid: int) -> bool:
    """Return True if a process with the given PID is currently running."""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return False



def _is_backup_running() -> bool:
    """Return True if a backup flow is actively in progress.

    Primary check: PID-stamped lock file written by flow.py.
    Fallback check: rclone.exe / robocopy.exe process existence, which covers
    the data-transfer phase even if the lock file was lost (e.g. OS crash).

    Stale lock detection: if the PID in the lock file is no longer alive, the
    lock is from a previous crash and is removed automatically.
    """
    # ── Primary: PID lock file ───────────────────────────────────────────────
    if BACKUP_LOCK_PATH.exists():
        try:
            pid = int(BACKUP_LOCK_PATH.read_text().strip())
            if _pid_is_alive(pid):
                return True
            # PID is gone — stale lock, clean it up
            logger.warning(
                f"Stale backup lock detected (PID {pid} not running) — removing"
            )
            BACKUP_LOCK_PATH.unlink(missing_ok=True)
        except (ValueError, OSError) as exc:
            logger.warning(f"Could not read backup lock file: {exc}")

    # ── Fallback: check for active rclone / robocopy processes ───────────────
    try:
        import psutil
        backup_procs = {"rclone.exe", "robocopy.exe"}
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] in backup_procs:
                logger.info(
                    f"Fallback: {proc.info['name']} (PID {proc.pid}) is running "
                    "— treating as active backup"
                )
                return True
    except Exception as exc:
        logger.warning(f"Fallback process check failed: {exc}")

    return False


# ── Service management ────────────────────────────────────────────────────────

def _service_is_running(service: str) -> bool:
    """Return True only when the service SCM state is RUNNING.

    Prevents restarting a service that is in START_PENDING (NSSM just
    restarted it) or STOP_PENDING — avoids kicking a transitioning service.
    """
    try:
        r = subprocess.run(
            ["sc", "query", service],
            capture_output=True, text=True, timeout=5,
        )
        return "RUNNING" in r.stdout
    except Exception:
        return False


def _stop_service(service: str) -> None:
    """Stop the service via Windows SCM.

    With sc failureflag=1 (set during install), any stop increments the SCM
    failure counter and triggers the sc failure restart actions.
    DependOnService causes AamBackupAgent to stop and restart automatically
    once AamPrefectServer is Running again.
    """
    logger.warning(f"Issuing: sc stop {service}")
    try:
        r = subprocess.run(
            ["sc", "stop", service],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            logger.info("Stop accepted — NSSM will restart within ~30s")
        else:
            logger.error(f"sc stop returned {r.returncode}: {r.stderr.strip()}")
    except subprocess.TimeoutExpired:
        logger.error("sc stop timed out (30s) — service may need manual attention")
    except Exception as exc:
        logger.error(f"Stop attempt failed: {exc}")


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
    deferrals = 0

    while True:
        healthy = _check_health()

        if healthy:
            if failures > 0:
                logger.info(f"Prefect API healthy (recovered after {failures} failure(s))")
            failures = 0
            deferrals = 0
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        # ── Unhealthy ────────────────────────────────────────────────────────
        failures += 1
        logger.warning(
            f"Prefect API unreachable — failure {failures}/{FAILURE_THRESHOLD}"
        )

        if failures < FAILURE_THRESHOLD:
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        # ── Threshold reached — check for active backup before acting ────────
        if _is_backup_running():
            deferrals += 1
            if deferrals >= MAX_DEFERRALS:
                logger.error(
                    f"Prefect API has been unhealthy for {failures} checks and backup "
                    f"lock has persisted for {deferrals} deferrals (~{deferrals * BACKUP_WAIT_INTERVAL // 60} min). "
                    f"Possible stale lock from PID reuse. Forcing restart."
                )
                # Force-remove the lock and proceed with restart
                try:
                    BACKUP_LOCK_PATH.unlink(missing_ok=True)
                except OSError:
                    pass
                deferrals = 0
                # Fall through to restart logic below
            else:
                logger.warning(
                    f"Prefect API has been unhealthy for {failures} checks but a backup "
                    f"is currently in progress. Deferring restart to avoid interrupting "
                    f"backup. Will re-check in {BACKUP_WAIT_INTERVAL}s. "
                    f"(deferral {deferrals}/{MAX_DEFERRALS})"
                )
                time.sleep(BACKUP_WAIT_INTERVAL)
                continue

        # ── No backup running — safe to restart ──────────────────────────────
        if not _service_is_running(WATCHED_SERVICE):
            # Service is in START_PENDING / STOP_PENDING — NSSM is already
            # handling a restart. Reset counter and wait.
            logger.info(
                f"{WATCHED_SERVICE} is not in RUNNING state (transitioning). "
                "NSSM is already handling a restart — resetting failure counter."
            )
            failures = 0
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        # Service is RUNNING but API is dead — genuine hung state.
        logger.error(
            f"{WATCHED_SERVICE} reports RUNNING but Prefect API has been "
            f"unreachable for {failures} consecutive checks. Triggering restart."
        )
        _stop_service(WATCHED_SERVICE)
        failures = 0

        logger.info(
            f"Restart triggered. Cooling down {RESTART_COOLDOWN}s before "
            "resuming health checks (gives Prefect time to fully boot)."
        )
        time.sleep(RESTART_COOLDOWN)


if __name__ == "__main__":
    main()
