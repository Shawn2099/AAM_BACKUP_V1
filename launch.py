"""AAM Backup Automation V1 — Single Launch Script.

Starts all three services in one process:

    uv run python launch.py

Services:
  - Prefect API server (subprocess, port 4200)
  - Dashboard UI (in-process, port 8080)
  - Backup scheduler (in-process, 4 deployments)

Ctrl+C stops all services cleanly. Shutdown order: scheduler → dashboard → Prefect API.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# Force Prefect to use a single, shared local server on port 4200 instead of
# spawning multiple heavy, isolated ephemeral server processes on random ports.
os.environ["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"
os.environ["PREFECT_SERVER_API_PORT"] = "4200"

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


def _wait_for_prefect_api(url="http://127.0.0.1:4200/api", timeout=60):
    """Poll the Prefect health endpoint until the API is ready."""
    import httpx
    for _ in range(timeout):
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return True
        except httpx.ConnectError:
            pass
        except Exception:
            pass
        time.sleep(1)
    print("[launch] WARNING: Prefect API did not become ready within timeout")
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
    """Cancel any PENDING or RUNNING flow runs left over from a previous crashed session.
    PENDING/RUNNING runs that were never finished before the scheduler or server died
    will never complete. Cancel them on launch to ensure a clean UI and lock state.
    """
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

    # Start Prefect API in a subprocess (needs its own process group)
    prefect_proc = subprocess.Popen(
        ["prefect", "server", "start"],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
    )

    # Start dashboard in daemon thread — uvicorn has no critical state to clean up.
    # Lock files are handled by main thread cleanup. Daemon ensures process
    # exits cleanly even if the Python runtime struggles with thread joins on Win32.
    dash_thread = threading.Thread(target=_run_dashboard, daemon=True)
    dash_thread.start()

    # Wait for Prefect API to be ready by polling the health endpoint
    print("[launch] Waiting for Prefect API to be ready...")
    _wait_for_prefect_api()

    # Create the global concurrency limit for backup serialization
    _ensure_concurrency_limit()

    # Cancel orphaned flow runs from previous crashed/shutdown sessions
    _cancel_orphaned_runs()

    # Run scheduler in main thread — serve() handles SIGINT internally,
    # so it returns cleanly on Ctrl+C. With pause_on_shutdown=False,
    # deployment schedules stay active across restarts.
    print("[launch] Starting backup scheduler (main thread)...")
    print("[launch] All services started")
    from models.config import CONFIG_PATH, load_config as _lc
    _cfg = _lc(CONFIG_PATH)
    print(f"[launch] Dashboard: http://{_cfg.dashboard.bind_address}:{_cfg.dashboard.port}")
    print("[launch] Prefect:   http://localhost:4200")
    print("[launch] Press Ctrl+C to stop all services")
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

        # 1) Terminate Prefect API — don't block on wait to avoid signal races
        try:
            prefect_proc.terminate()
        except Exception:
            pass
        try:
            prefect_proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                prefect_proc.kill()
                prefect_proc.wait(timeout=3)
            except Exception:
                pass

        # 2) Cleanup
        print("[launch] All services stopped")


if __name__ == "__main__":
    main()
