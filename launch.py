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
import subprocess
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
    """Create the global concurrency limit for backup serialization if it doesn't exist."""
    import asyncio

    async def _create():
        from prefect.client.orchestration import get_client
        try:
            async with get_client() as client:
                await client.create_global_concurrency_limit(
                    name="aam-backup",
                    limit=1,
                    active=False,
                    slot_decay_per_second=0.0,
                )
                print("[launch] Created global concurrency limit 'aam-backup' (limit=1)")
        except Exception:
            # Limit already exists — this is expected on subsequent runs
            pass

    asyncio.run(_create())


def _cancel_orphaned_runs():
    """Cancel any PENDING or RUNNING flow runs left over from a previous crashed session."""
    try:
        for state in ["PENDING", "RUNNING"]:
            result = subprocess.run(
                [
                    "prefect", "flow-run", "ls",
                    "--state", state,
                    "--limit", "50",
                    "--output", "json",
                ],
                capture_output=True, text=True, timeout=15,
                cwd=str(PROJECT_DIR),
            )
            if result.returncode != 0:
                continue

            import json as _json
            runs = _json.loads(result.stdout) if result.stdout.strip() else []
            cancelled = 0
            for run in runs:
                run_id = run.get("id", "")
                run_name = run.get("name", "")
                if run_id:
                    subprocess.run(
                        ["prefect", "flow-run", "cancel", run_id],
                        capture_output=True, timeout=10,
                        cwd=str(PROJECT_DIR),
                    )
                    print(f"[launch] Cancelled orphaned {state} run: {run_name} ({run_id[:8]}...)")
                    cancelled += 1
            if cancelled:
                print(f"[launch] Cleaned up {cancelled} orphaned {state} flow run(s)")
    except Exception:
        pass


def main():
    print("=" * 50)
    print("  AAM Backup Automation V1 — Launch")
    print("=" * 50)

    # Verify Prefect API server is running
    print("[launch] Checking Prefect API server...")
    if not _check_prefect_api():
        print("[launch] ERROR: Prefect API server is not running on http://127.0.0.1:4200")
        print("[launch] Start it first: start_server.bat")
        print("[launch] Or run: uv run prefect server start")
        sys.exit(1)
    print("[launch] Prefect API server is running")

    # Start dashboard in daemon thread
    dash_thread = threading.Thread(target=_run_dashboard, daemon=True)
    dash_thread.start()

    # Create the global concurrency limit for backup serialization
    _ensure_concurrency_limit()

    # Cancel orphaned flow runs from previous crashed/shutdown sessions
    _cancel_orphaned_runs()

    # Run scheduler in main thread — serve() handles SIGINT internally,
    # so it returns cleanly on Ctrl+C. With pause_on_shutdown=False,
    # deployment schedules stay active across restarts.
    print("[launch] Starting backup scheduler (main thread)...")
    from models.config import CONFIG_PATH, load_config as _lc
    _cfg = _lc(CONFIG_PATH)
    print(f"[launch] Dashboard: http://{_cfg.dashboard.bind_address}:{_cfg.dashboard.port}")
    print("[launch] Prefect:   http://localhost:4200")
    print("[launch] All services started")
    print("[launch] Press Ctrl+C to stop dashboard + scheduler")
    print()

    from prefect import serve

    from serve import deployments
    cloud_deployment, lan_deployment, report_deployment, monthly_deployment = deployments()

    shutdown_clean = False
    try:
        serve(
            cloud_deployment,
            lan_deployment,
            report_deployment,
            monthly_deployment,
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
