"""AAM Backup Automation — Dashboard UI with manual triggers.

FastAPI server on port 8080. Safety-first:
  - Lock file per pipeline prevents concurrent runs
  - Confirmation required for trigger
  - Buttons disabled while pipeline is running
"""

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from core.fy_router import get_fy_prefix
from core.manifest import ManifestDB

app = FastAPI(title="AAM Backup Dashboard")

# ── Lazy config (works on any OS, validates only on use) ─────

_config = None


def _cfg():
    global _config
    if _config is None:
        from models.config import load_config
        _config = load_config("config.yaml")
    return _config


# ── Lock management ──────────────────────────────────────────


def _lock_dir() -> Path:
    p = Path(_cfg().paths.temp_directory)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _lock_path(pipeline: str) -> Path:
    return _lock_dir() / f".lock_{pipeline}"


def _is_running(pipeline: str) -> bool:
    lp = _lock_path(pipeline)
    if not lp.exists():
        return False
    try:
        data = json.loads(lp.read_text())
        pid_running = False
        try:
            os.kill(data["pid"], 0)
            pid_running = True
        except OSError:
            pass
        if not pid_running:
            lp.unlink(missing_ok=True)
            return False
        return True
    except (json.JSONDecodeError, KeyError):
        lp.unlink(missing_ok=True)
        return False


def _acquire_lock(pipeline: str, run_id: str) -> bool:
    if _is_running(pipeline):
        return False
    _lock_path(pipeline).write_text(
        json.dumps({
            "pid": os.getpid(),
            "run_id": run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
    )
    return True


def _release_lock(pipeline: str):
    _lock_path(pipeline).unlink(missing_ok=True)


# ── Trigger pipeline (runs in background) ────────────────────


def _run_in_background(pipeline: str, config_path: str):
    run_id = str(uuid.uuid4())[:8]
    if not _acquire_lock(pipeline, run_id):
        logger.warning(f"{pipeline} already running — skipping trigger")
        return

    try:
        from models.config import load_config
        from flow import backup

        config = load_config(config_path)
        logger.info(f"Manual trigger: {pipeline} (run={run_id})")
        backup(config_path=config_path, mode=pipeline)

    except Exception as e:
        logger.error(f"Manual {pipeline} trigger failed: {e}")
    finally:
        _release_lock(pipeline)


# ── API endpoints ────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(_render_dashboard())


@app.get("/status")
def status():
    cfg = _cfg()
    if not Path(cfg.paths.database_path).exists():
        return JSONResponse({"error": "ManifestDB not found"}, status_code=503)

    db = ManifestDB(cfg.paths.database_path)
    try:
        return JSONResponse({
            "firm": "AAM",
            "fy_prefix": get_fy_prefix(),
            "cloud": {
                "running": _is_running("cloud"),
                "last_run": _last_run_summary(db, "cloud"),
            },
            "lan": {
                "running": _is_running("lan"),
                "last_run": _last_run_summary(db, "lan"),
            },
            "manifest": {
                "lan_files": db.file_count("lan_status"),
                "cloud_files": db.file_count("cloud_status"),
            },
            "health": _get_health(),
        })
    finally:
        db.close()


@app.post("/trigger/cloud")
def trigger_cloud(config_path: str = "config.yaml"):
    if _is_running("cloud"):
        raise HTTPException(409, "Cloud backup is already running")
    threading.Thread(target=_run_in_background, args=("cloud", config_path), daemon=True).start()
    return {"status": "triggered", "pipeline": "cloud"}


@app.post("/trigger/lan")
def trigger_lan(config_path: str = "config.yaml"):
    if _is_running("lan"):
        raise HTTPException(409, "LAN backup is already running")
    threading.Thread(target=_run_in_background, args=("lan", config_path), daemon=True).start()
    return {"status": "triggered", "pipeline": "lan"}


# ── Helpers ──────────────────────────────────────────────────


def _last_run_summary(db: ManifestDB, mode: str) -> dict | None:
    run = db.last_run(mode)
    if not run:
        return None
    return {
        "status": run.get("status", "unknown"),
        "started_at": run.get("started_at", ""),
        "files": run.get("files_copied", 0),
        "bytes": run.get("bytes_copied", 0),
        "duration": f"{run.get('duration_seconds', 0):.0f}s" if run.get("duration_seconds") else "?",
        "error": run.get("error_message"),
    }


def _get_health() -> dict:
    try:
        src = _cfg().paths.source_drive
        du = shutil.disk_usage(src)
        return {
            "source_free_gb": f"{du.free / (1024**3):.1f}",
            "source_exists": Path(src).exists(),
        }
    except Exception:
        return {"error": "unavailable"}


# ── Dashboard HTML ───────────────────────────────────────────

_CSS = """<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; background: #111827; color: #e5e7eb; padding: 2rem; }
h1 { font-size: 1.5rem; margin-bottom: 1.5rem; color: #f9fafb; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
.card { background: #1f2937; border-radius: 0.5rem; padding: 1.25rem; border-left: 4px solid; }
.card.success { border-color: #10b981; }
.card.running { border-color: #f59e0b; }
.card.failed { border-color: #ef4444; }
.card.unknown { border-color: #6b7280; }
.card h2 { font-size: 1rem; margin-bottom: 0.75rem; }
.status-badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; }
.status-badge.running { background: #f59e0b; color: #000; }
.status-badge.success { background: #10b981; color: #000; }
.status-badge.failed { background: #ef4444; color: #fff; }
.status-badge.unknown { background: #6b7280; color: #fff; }
.stats { display: flex; gap: 1rem; margin-bottom: 1rem; flex-wrap: wrap; }
.stat { background: #1f2937; border-radius: 0.5rem; padding: 1rem; flex: 1; min-width: 120px; text-align: center; }
.stat .num { font-size: 2rem; font-weight: 700; color: #60a5fa; }
.stat .label { font-size: 0.75rem; color: #9ca3af; margin-top: 0.25rem; }
button { padding: 0.5rem 1rem; border: none; border-radius: 0.375rem; font-weight: 600; cursor: pointer; margin-right: 0.5rem; }
.btn-trigger { background: #2563eb; color: #fff; }
.btn-trigger:hover:not(:disabled) { background: #1d4ed8; }
.btn-trigger:disabled { background: #374151; color: #6b7280; cursor: not-allowed; }
.info { color: #9ca3af; font-size: 0.8rem; margin-top: 2rem; }
</style>"""


def _render_dashboard() -> str:
    cfg = _cfg()
    db_path = Path(cfg.paths.database_path)
    db = ManifestDB(db_path) if db_path.exists() else None

    cloud_run = cloud_running = "Unknown"
    lan_run = lan_running = "Unknown"
    cloud_class = lan_class = "unknown"
    cloud_btn = lan_btn = ""
    cloud_last = lan_last = "No data"
    lan_files = cloud_files = 0
    health_info = "Unavailable"

    if db:
        try:
            cloud_running = "Running" if _is_running("cloud") else "Idle"
            lan_running = "Running" if _is_running("lan") else "Idle"
            cr = _last_run_summary(db, "cloud")
            lr = _last_run_summary(db, "lan")
            if cr:
                cloud_last = f"{cr['status']} — {cr['started_at'][:16]}"
                cloud_run = f"{cr['status']} ({cr['files']} files)"
                if cr.get("error"):
                    cloud_run += f" — {cr['error'][:60]}"
            if lr:
                lan_last = f"{lr['status']} — {lr['started_at'][:16]}"
                lan_run = f"{lr['status']} ({lr['files']} files)"
                if lr.get("error"):
                    lan_run += f" — {lr['error'][:60]}"
            cloud_files = db.file_count("cloud_status")
            lan_files = db.file_count("lan_status")

            cloud_class = "running" if _is_running("cloud") else (
                "success" if cr and "COMPLETE" in cr["status"] else "failed"
            )
            lan_class = "running" if _is_running("lan") else (
                "success" if lr and "COMPLETE" in lr["status"] else "failed"
            )
            cloud_btn = "disabled" if _is_running("cloud") else ""
            lan_btn = "disabled" if _is_running("lan") else ""

            h = _get_health()
            if "error" not in h:
                health_info = f"Source: {h['source_free_gb']} GB free | FY: {get_fy_prefix()}"
        finally:
            db.close()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AAM Backup Dashboard</title>
{_CSS}
</head>
<body>
<h1>AAM Backup Dashboard</h1>

<div class="stats">
    <div class="stat"><div class="num">{lan_files:,}</div><div class="label">LAN Files</div></div>
    <div class="stat"><div class="num">{cloud_files:,}</div><div class="label">Cloud Files</div></div>
    <div class="stat"><div class="num">{get_fy_prefix()}</div><div class="label">FY Prefix</div></div>
</div>

<div class="grid">
    <div class="card {cloud_class}">
        <h2>Cloud Backup <span class="status-badge {cloud_class}">{cloud_running}</span></h2>
        <p>{cloud_run}</p>
        <p style="font-size:0.75rem;color:#9ca3af;margin-top:0.5rem">Last: {cloud_last}</p>
        <form style="margin-top:1rem" onsubmit="return confirm('Start cloud backup now?')">
            <button class="btn-trigger" {cloud_btn} formaction="/trigger/cloud" formmethod="post">
                Run Cloud Backup
            </button>
        </form>
    </div>

    <div class="card {lan_class}">
        <h2>LAN Backup <span class="status-badge {lan_class}">{lan_running}</span></h2>
        <p>{lan_run}</p>
        <p style="font-size:0.75rem;color:#9ca3af;margin-top:0.5rem">Last: {lan_last}</p>
        <form style="margin-top:1rem" onsubmit="return confirm('Start LAN backup now? Includes WoL + shutdown.')">
            <button class="btn-trigger" {lan_btn} formaction="/trigger/lan" formmethod="post">
                Run LAN Backup
            </button>
        </form>
    </div>
</div>

<div class="info">
    <p>{health_info}</p>
    <p style="margin-top:0.5rem">AAM Backup Automation V1</p>
</div>
</body>
</html>"""


def run():
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    run()
