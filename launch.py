"""AAM Backup Automation V1 — Single Launch Script.

Starts all three services in one process:

    uv run python launch.py

Services:
  - Prefect API server (subprocess, port 4200)
  - Dashboard UI (in-process, port 8080)
  - Backup scheduler (in-process, 4 deployments)

Ctrl+C stops all services cleanly. Shutdown order: scheduler → dashboard → Prefect API.
Lock files (.lock_cloud, .lock_lan) are cleaned up on exit.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def _run_dashboard():
    """Start dashboard UI on port 8080 — imports in-thread to avoid early config load."""
    print("[launch] Starting Dashboard UI on http://0.0.0.0:8080 ...")
    import uvicorn

    from ui import app
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")


def _cleanup_lock_files():
    """Remove stale lock files from temp directory on exit."""
    try:
        from models.config import load_config
        cfg = load_config("config.yaml")
        temp_dir = Path(cfg.paths.temp_directory)
        for lock in temp_dir.glob(".lock_*"):
            try:
                lock.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _cancel_orphaned_runs():
    """Cancel any PENDING flow runs left over from a previous crashed session.

    PENDING runs from `prefect deployment run` that were never picked up
    by the old serve() loop before it died will never execute. Cancel them
    so they don't clutter the UI and incorrectly show as "running."
    """
    try:
        result = subprocess.run(
            [
                "prefect", "flow-run", "ls",
                "--state", "PENDING",
                "--limit", "50",
                "--output", "json",
            ],
            capture_output=True, text=True, timeout=15,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode != 0:
            return

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
                print(f"[launch] Cancelled orphaned run: {run_name} ({run_id[:8]}...)")
                cancelled += 1
        if cancelled:
            print(f"[launch] Cleaned up {cancelled} orphaned flow run(s)")
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

    # Wait for Prefect API to be ready
    print("[launch] Waiting 30s for Prefect API to be ready...")
    time.sleep(30)

    # Cancel orphaned flow runs from previous crashed/shutdown sessions
    _cancel_orphaned_runs()

    # Run scheduler in main thread — serve() handles SIGINT internally,
    # so it returns cleanly on Ctrl+C. With pause_on_shutdown=False,
    # deployment schedules stay active across restarts.
    print("[launch] Starting backup scheduler (main thread)...")
    print("[launch] All services started")
    print("[launch] Dashboard: http://localhost:8080")
    print("[launch] Prefect:   http://localhost:4200")
    print("[launch] Press Ctrl+C to stop all services")
    print()

    from prefect import serve

    from serve import cloud_deployment, lan_deployment, monthly_deployment, report_deployment

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

        # 2) Cleanup lock files
        _cleanup_lock_files()

        print("[launch] All services stopped")


if __name__ == "__main__":
    main()
