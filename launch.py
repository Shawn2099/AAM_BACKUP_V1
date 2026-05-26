"""AAM Backup Automation V1 — Single Launch Script.

Starts all three services in one command:

    uv run python launch.py

Services:
  - Prefect API server (port 4200)
  - Dashboard UI (port 8080)
  - Backup scheduler (4 deployments)
"""

import subprocess
import sys
import time
from pathlib import Path

import uvicorn

PROJECT_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = PROJECT_DIR / "config.yaml"


def start_prefect_server():
    """Start Prefect API server in a subprocess."""
    print("[launch] Starting Prefect API server...")
    return subprocess.Popen(
        ["prefect", "server", "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
    )


def start_dashboard():
    """Start dashboard UI (uvicorn → FastAPI)."""
    print("[launch] Starting Dashboard UI on http://0.0.0.0:8080 ...")
    return subprocess.Popen(
        [sys.executable, "-c", "from ui import run; run()"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(PROJECT_DIR),
    )


def start_scheduler():
    """Start Prefect deployment scheduler."""
    print("[launch] Starting backup scheduler...")
    return subprocess.Popen(
        [sys.executable, str(PROJECT_DIR / "serve.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(PROJECT_DIR),
    )


def main():
    print("=" * 50)
    print("  AAM Backup Automation V1 — Launch")
    print("=" * 50)

    processes = []

    try:
        processes.append(start_prefect_server())
        print("[launch] Dashboard starting...")
        processes.append(start_dashboard())

        print("[launch] Waiting 30s for Prefect API to be ready...")
        time.sleep(30)

        processes.append(start_scheduler())

        print("[launch] All services started")
        print("[launch] Dashboard: http://localhost:8080")
        print("[launch] Prefect:   http://localhost:4200")
        print("[launch] Press Ctrl+C to stop all services")
        print()

        # Keep running until interrupted
        for proc in processes:
            proc.wait()

    except KeyboardInterrupt:
        print("\n[launch] Shutting down...")
    finally:
        for proc in processes:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        print("[launch] All services stopped")


if __name__ == "__main__":
    main()
