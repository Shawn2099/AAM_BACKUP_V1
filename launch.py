"""AAM Backup Automation V1 — Dashboard + Scheduler.

Connects to an already-running Prefect API server on port 4200.
The Prefect server runs as a separate process (start_server.bat).

    uv run python launch.py

Services managed by this script:
  - Dashboard UI (in-process, configurable host:port)
  - Backup scheduler (in-process, 4 deployments)

Ctrl+C stops both cleanly. The Prefect server continues running independently.
"""
import os
import sys
import threading
import time
from pathlib import Path

# Force Prefect to connect to the local server on port 4200.
os.environ["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"

PROJECT_DIR = Path(__file__).resolve().parent


def _run_dashboard():
    """Start dashboard UI — imports in-thread to avoid early config load."""
    from models.config import CONFIG_PATH, load_config
    cfg = load_config(CONFIG_PATH)
    bind = cfg.dashboard.bind_address
    port = cfg.dashboard.port
    print(f"[launch] Starting Dashboard UI on http://{bind}:{port} ...")
    import uvicorn

    from ui import app
    uvicorn.run(app, host=bind, port=port, log_level="warning")


def _check_prefect_api(url="http://127.0.0.1:4200/api"):
    """Verify Prefect API server is running. Raises if not reachable."""
    import httpx
    try:
        resp = httpx.get(f"{url}/health", timeout=5)
        if resp.status_code == 200:
            return True
    except httpx.ConnectError:
        pass
    except Exception:
        pass
    return False


def _ensure_concurrency_limit():
    """Create the global and tag-based concurrency limits for backup serialization.

    Uses the Prefect Python client API (not the CLI) so it connects to the
    already-running server on PREFECT_API_URL, not a temporary ephemeral server.
    """
    import asyncio

    async def _create():
        from prefect.client.orchestration import get_client
        async with get_client() as client:
            # 1. Create/Upsert Global Concurrency Limit (used by flow.py's `with concurrency("aam-backup")`)
            try:
                await client.upsert_global_concurrency_limit_by_name(
                    name="aam-backup",
                    limit=1,
                )
                print("[launch] Ensured global concurrency limit 'aam-backup' (limit=1)")
            except Exception as e:
                print(f"[launch] Warning: failed to create global concurrency limit: {e}")

            # 2. Create Tag-based Concurrency Limit (used by tagged runs/tasks)
            try:
                await client.create_concurrency_limit(
                    tag="aam-backup",
                    concurrency_limit=1,
                )
                print("[launch] Ensured tag-based concurrency limit 'aam-backup' (limit=1)")
            except Exception:
                # Limit already exists — expected on subsequent runs
                pass

    asyncio.run(_create())


def _cancel_orphaned_runs():
    """Cancel any PENDING or RUNNING flow runs left over from a previous crashed session.

    Respects the backup lock file — if a backup is actively running (lock held
    by a live PID), RUNNING flows are left untouched. Only PENDING flows are
    always cancelled (they haven't started work yet).
    """
    import asyncio

    from core.process import read_lock_alive

    # Derive lock path from config (same derivation as flow.py)
    try:
        from models.config import CONFIG_PATH, load_config
        cfg = load_config(CONFIG_PATH)
        lock_path = cfg.paths.backup_lock_path
    except Exception:
        lock_path = None

    backup_active = False
    if lock_path:
        alive, pid = read_lock_alive(lock_path)
        if alive:
            print(f"[launch] Backup lock held by PID {pid} — skipping cancellation of RUNNING flows")
            backup_active = True
        elif pid is not None:
            print(f"[launch] Stale backup lock (PID {pid} not running or reused) — cleaning up")
            lock_path.unlink(missing_ok=True)


    async def _cancel():
        from prefect.client.orchestration import get_client
        from prefect.client.schemas.filters import (
            FlowRunFilter,
            FlowRunFilterState,
            FlowRunFilterStateType,
        )
        from prefect.client.schemas.objects import StateType
        from prefect.states import Cancelled

        try:
            async with get_client() as client:
                for state_type in [StateType.PENDING, StateType.RUNNING]:
                    # If a backup is actively running, only cancel PENDING flows
                    if state_type == StateType.RUNNING and backup_active:
                        print("[launch] Skipping RUNNING flows — backup lock is active")
                        continue

                    runs = await client.read_flow_runs(
                        flow_run_filter=FlowRunFilter(
                            state=FlowRunFilterState(
                                type=FlowRunFilterStateType(any_=[state_type])
                            )
                        )
                    )
                    cancelled = 0
                    for r in runs:
                        try:
                            await client.set_flow_run_state(
                                flow_run_id=r.id,
                                state=Cancelled(message="Cancelled orphaned run on service startup"),
                                force=True,
                            )
                            print(f"[launch] Cancelled orphaned {state_type.value} run: {r.name} ({str(r.id)[:8]}...)")
                            cancelled += 1
                        except Exception as e:
                            print(f"[launch] Warning: failed to cancel run {r.id}: {e}")
                    if cancelled:
                        print(f"[launch] Cleaned up {cancelled} orphaned {state_type.value} flow run(s)")
        except Exception as e:
            print(f"[launch] Warning: failed to clean up orphaned runs: {e}")

    asyncio.run(_cancel())


def _reconcile_disabled_legs(config, legs: dict | None = None) -> dict:
    """P2-SCHED: make server-side deployment state match the enabled flags.

    For each backup leg:
      * disabled in config  -> its deployment (if registered server-side from
        any previous boot) is PAUSED so the scheduler stops firing it;
      * enabled in config   -> it is RESUMED if a stale paused state exists.

    Pause is used instead of delete deliberately: it is reversible, preserves
    run history, and survives operator mistakes. Idempotent - safe on every
    boot. Returns {deployment_name: outcome} for logging.

    Args:
        config: Loaded AppConfig - supplies the production legs when `legs`
            is not given.
        legs: Optional explicit {deployment_name: enabled} override used by
            tests/isolated reconciliation of specific deployments.
    """
    import asyncio

    from prefect.client.orchestration import get_client
    from prefect.exceptions import ObjectNotFound

    if legs is None:
        legs = {
            "backup-lan": bool(getattr(config.lan, "enabled", False)),
            "backup-cloud": bool(getattr(config.cloud, "enabled", True)),
        }

    async def _reconcile() -> dict:
        results: dict = {}
        async with get_client() as client:
            for dep_name, enabled in legs.items():
                full_name = f"aam-backup/{dep_name}"
                try:
                    dep = await client.read_deployment_by_name(full_name)
                except ObjectNotFound:
                    results[dep_name] = "absent (nothing to reconcile)"
                    continue
                paused = bool(getattr(dep, "paused", False))
                if enabled and paused:
                    await client.resume_deployment(dep.id)
                    results[dep_name] = "resumed (leg enabled)"
                elif not enabled and not paused:
                    await client.pause_deployment(dep.id)
                    results[dep_name] = "PAUSED (leg disabled)"
                else:
                    results[dep_name] = (
                        "active" if enabled else "already-paused"
                    )
        return results

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_reconcile())
    # Already inside an event loop (programmatic/test callers): run on a
    # worker thread with its own loop instead of nesting asyncio.run.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
        return _pool.submit(asyncio.run, _reconcile()).result()


def main():
    print("=" * 50)
    print("  AAM Backup Automation V1 — Launch")
    print("=" * 50)

    from models.config import CONFIG_PATH, load_config
    _cfg = load_config(CONFIG_PATH)

    # Wait for Prefect API — retry loop handles the startup race that occurs after
    # a watchdog-triggered service restart (Prefect takes ~45s to be API-ready).
    # 90s total wait ensures zero sc failure recovery actions are consumed on a
    # normal restart cycle.
    print("[launch] Waiting for Prefect API server...")
    _API_MAX_WAIT  = 300  # seconds
    _API_INTERVAL  = 10   # seconds between attempts
    _api_ready     = False
    for _elapsed in range(0, _API_MAX_WAIT, _API_INTERVAL):
        if _check_prefect_api():
            print(f"[launch] Prefect API ready (waited {_elapsed}s)")
            _api_ready = True
            break
        _remaining = _API_MAX_WAIT - _elapsed - _API_INTERVAL
        print(f"[launch] Not ready yet — retrying in {_API_INTERVAL}s "
              f"({max(0, _remaining)}s remaining)...")
        time.sleep(_API_INTERVAL)
    if not _api_ready:
        print(f"[launch] ERROR: Prefect API not reachable after {_API_MAX_WAIT}s")
        print("[launch] Ensure start_server.bat / AamPrefectServer service is running")
        sys.exit(1)

    # Start dashboard in daemon thread
    dash_thread = threading.Thread(target=_run_dashboard, daemon=True)
    dash_thread.start()

    # Check for FY rollover before starting normal operations.
    # On April 1, this runs a final backup of the closing FY, transitions
    # old GCS data to ARCHIVE storage, creates new FY folders, and
    # atomically updates config.yaml to point to the new FY paths.
    # Backup flows load config fresh on every run, so they pick up the
    # new paths without a restart.
    from core.fy_rollover import RolloverError, rollover
    try:
        if rollover():
            print("[launch] FY rollover completed — config updated for new FY")
    except RolloverError as e:
        print(f"[launch] FY rollover blocked: {e}")
        print("[launch] Will retry on next scheduled run.")
    except Exception as e:
        print(f"[launch] Warning: FY rollover check failed (non-fatal): {e}")

    # Create the global concurrency limit for backup serialization
    _ensure_concurrency_limit()

    # Cancel orphaned flow runs from previous crashed/shutdown sessions
    _cancel_orphaned_runs()

    # P2-SCHED: pause deployments whose leg is disabled in config (and resume
    # re-enabled ones). Filtering registration alone is NOT enough - Prefect's
    # server-side scheduler keeps firing previously-registered deployments
    # even when a later serve() omits them. Pause beats delete: reversible,
    # preserves run history.
    try:
        _reconciliation = _reconcile_disabled_legs(_cfg)
        for dep_name, outcome in _reconciliation.items():
            print(f"[launch] Deployment {dep_name}: {outcome}")
    except Exception as exc:
        # Non-fatal: the flow-level enabled guard still protects the leg, but
        # the operator should know reconciliation did not complete.
        print(f"[launch] Deployment reconciliation FAILED (non-fatal): {exc}")

    # Run scheduler in main thread — serve() handles SIGINT internally,
    # so it returns cleanly on Ctrl+C. With pause_on_shutdown=False,
    # deployment schedules stay active across restarts.
    print("[launch] Starting backup scheduler (main thread)...")
    print(f"[launch] Dashboard: http://{_cfg.dashboard.bind_address}:{_cfg.dashboard.port}")
    print("[launch] Prefect:   http://localhost:4200")
    print("[launch] All services started")
    print("[launch] Press Ctrl+C to stop dashboard + scheduler")
    print()

    from prefect import serve

    from serve import deployments

    shutdown_clean = False
    try:
        serve(
            *deployments(),
            pause_on_shutdown=False,
        )
        shutdown_clean = True
    except KeyboardInterrupt:
        shutdown_clean = True
    finally:
        print(f"\n[launch] Shutting down{' (clean)' if shutdown_clean else ' (interrupted)'}...")
        print("[launch] Dashboard + scheduler stopped")
        print("[launch] Prefect server is still running independently")


if __name__ == "__main__":
    main()
