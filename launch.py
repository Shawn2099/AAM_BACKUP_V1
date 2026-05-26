"""AAM Backup Automation V1 — Single Launch Script.

Starts all three services in one process:

    uv run python launch.py

Services:
  - Prefect API server (subprocess, port 4200)
  - Dashboard UI (in-process, port 8080)
  - Backup scheduler (in-process, 4 deployments)

Ctrl+C stops all services cleanly.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def _run_prefect():
    """Start Prefect API server."""
    print("[launch] Starting Prefect API server...")
    subprocess.run(
        ["prefect", "server", "start"],
        cwd=str(PROJECT_DIR),
        creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
    )


def _run_dashboard():
    """Start dashboard UI on port 8080 — imports in-thread to avoid early config load."""
    print("[launch] Starting Dashboard UI on http://0.0.0.0:8080 ...")
    import uvicorn
    from ui import app
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")


def _run_scheduler():
    """Start Prefect deployment scheduler — retries on crash."""
    import traceback
    from prefect import serve
    from serve import cloud_deployment, lan_deployment, report_deployment, monthly_deployment
    print("[launch] Starting backup scheduler...")
    while True:
        try:
            serve(cloud_deployment, lan_deployment, report_deployment, monthly_deployment)
        except Exception as e:
            print(f"[launch] Scheduler crashed, restarting in 10s: {e}")
            traceback.print_exc()
            time.sleep(10)


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

    # Start dashboard in background thread
    dashboard_thread = threading.Thread(target=_run_dashboard, daemon=True)
    dashboard_thread.start()

    # Wait for Prefect API to be ready
    print("[launch] Waiting 30s for Prefect API to be ready...")
    time.sleep(30)

    # Start scheduler in background thread
    scheduler_thread = threading.Thread(target=_run_scheduler, daemon=True)
    scheduler_thread.start()

    print("[launch] All services started")
    print("[launch] Dashboard: http://localhost:8080")
    print("[launch] Prefect:   http://localhost:4200")
    print("[launch] Press Ctrl+C to stop all services")
    print()

    try:
        # Keep main process alive — wait on scheduler thread (now has retry loop)
        scheduler_thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[launch] Shutting down...")
        try:
            prefect_proc.terminate()
            prefect_proc.wait(timeout=5)
        except Exception:
            prefect_proc.kill()
        print("[launch] All services stopped")


if __name__ == "__main__":
    main()
