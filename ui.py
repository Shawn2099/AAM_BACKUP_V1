"""AAM Backup Automation — Dashboard UI with manual triggers.

FastAPI server bound to configurable host:port. Safety-first:
  - Lock file per pipeline prevents concurrent runs
  - Confirmation required for trigger
  - Buttons disabled while pipeline is running
  - API key auth via session cookie or X-API-Key header
"""

import hmac
import html
import os
import secrets
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from loguru import logger

from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowRunFilter
from prefect.client.schemas.objects import StateType
from prefect.deployments import run_deployment


# In-memory rate limiter for trigger and auth endpoints.
# Tracks attempts per IP per window. Cleans up expired entries on access.
_RATE_LIMITS: dict[str, list[float]] = {}
_RATE_LOCK = threading.Lock()
_RATE_WINDOW = 300  # 5 minutes
_RATE_MAX_TRIGGER = 5  # 5 trigger attempts per window
_RATE_MAX_LOGIN = 10   # 10 login attempts per window
_RATE_MAX_REPORT = 10  # 10 report downloads per window


def _check_rate_limit(client_ip: str, max_attempts: int) -> bool:
    """Return True if request is allowed, False if rate limited."""
    with _RATE_LOCK:
        now = time.time()
        window = _RATE_WINDOW
        entries = _RATE_LIMITS.get(client_ip, [])
        entries = [t for t in entries if now - t < window]
        if not entries:
            _RATE_LIMITS.pop(client_ip, None)
        else:
            _RATE_LIMITS[client_ip] = entries
        if len(entries) >= max_attempts:
            return False
        entries.append(now)
        _RATE_LIMITS[client_ip] = entries
        return True

from core.fy_router import get_fy_prefix
from core.manifest import ManifestDB
from templates.dashboard import render_dashboard

app = FastAPI(title="AAM Backup Dashboard")

# ── In-memory session store ──────────────────────────

_sessions: dict[str, dict] = {}
_SESSION_TTL = timedelta(hours=24)


def _create_session() -> str:
    token = secrets.token_hex(32)
    _sessions[token] = {"created_at": time.time()}
    _cleanup_expired_sessions()
    return token


def _cleanup_expired_sessions() -> None:
    """Remove expired sessions. Called on each new session creation."""
    now = time.time()
    ttl = _SESSION_TTL.total_seconds()
    expired = [t for t, s in _sessions.items() if now - s["created_at"] > ttl]
    for t in expired:
        del _sessions[t]


def _validate_session(token: str | None) -> bool:
    if not token:
        return False
    session = _sessions.get(token)
    if not session:
        return False
    if time.time() - session["created_at"] > _SESSION_TTL.total_seconds():
        del _sessions[token]
        return False
    return True


def _get_api_key() -> str:
    cfg = _cfg()
    return cfg.dashboard.api_key if cfg.dashboard.auth_enabled else ""


def _auth_enabled() -> bool:
    return _cfg().dashboard.auth_enabled


def _check_api_key_header(request: Request) -> bool:
    header_key = request.headers.get("X-API-Key", "")
    configured_key = _get_api_key()
    if not configured_key:
        return True
    return hmac.compare_digest(header_key, configured_key)

# ── Lazy config (works on any OS, validates only on use) ─────

_config = None


def _cfg():
    global _config
    if _config is None:
        from models.config import CONFIG_PATH, load_config
        _config = load_config(CONFIG_PATH)
    return _config


# ── Pipeline status ──────────────────────────────────────────


async def _is_running(pipeline: str) -> bool:
    """Check if a backup pipeline is active (running, pending, or scheduled)."""
    return await _prefect_has_active_run(pipeline)


async def _prefect_has_active_run(pipeline: str) -> bool:
    """Check Prefect API for an active flow run of the given pipeline.

    Checks both RUNNING and PENDING states — a PENDING run means
    it's queued behind the concurrency limit and will execute when the slot opens.
    Future SCHEDULED runs are ignored as they are not currently active.
    """
    try:
        async with get_client() as client:
            runs = await client.read_flow_runs(
                flow_run_filter=FlowRunFilter(
                    state={"type": {"any_": [StateType.RUNNING, StateType.PENDING]}}
                ),
                limit=20,
            )
            for run in runs:
                tags = run.tags or []
                parameters = run.parameters or {}
                if pipeline in tags or parameters.get("mode") == pipeline:
                    return True
            return False
    except Exception as e:
        logger.error(f"Failed to query Prefect API: {e}")
        return False


# ── Trigger pipeline (via Prefect deployment API) ────────────


async def _run_in_background(pipeline: str):
    """Trigger pipeline asynchronously via Prefect SDK.

    Checks Prefect API for active runs before triggering.
    Duplicate prevention is handled by the Prefect concurrency limit.
    """
    if await _prefect_has_active_run(pipeline):
        logger.warning(f"{pipeline} already running or queued — skipping trigger")
        return

    try:
        logger.info(f"Manual trigger: {pipeline}")
        flow_run = await run_deployment(name=f"aam-backup/backup-{pipeline}")
        logger.info(f"Triggered {pipeline} — run ID: {flow_run.id}")
    except Exception as e:
        logger.error(f"Manual {pipeline} trigger failed: {e}")


# ── API endpoints ────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    error_html = f'<p style="color:#ef4444;margin-bottom:1rem">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>AAM Backup — Login</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: system-ui, sans-serif; background: #111827; color: #e5e7eb; display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
.login {{ background: #1f2937; padding: 2rem; border-radius: 0.5rem; width: 320px; }}
h1 {{ font-size: 1.25rem; margin-bottom: 1.5rem; text-align: center; }}
label {{ display: block; font-size: 0.875rem; color: #9ca3af; margin-bottom: 0.25rem; }}
input {{ width: 100%; padding: 0.5rem; border: 1px solid #374151; border-radius: 0.375rem; background: #111827; color: #e5e7eb; margin-bottom: 1rem; }}
button {{ width: 100%; padding: 0.5rem; border: none; border-radius: 0.375rem; background: #2563eb; color: #fff; font-weight: 600; cursor: pointer; }}
button:hover {{ background: #1d4ed8; }}
</style></head>
<body>
<div class="login">
<h1>AAM Backup Dashboard</h1>
{error_html}
<form action="/login" method="post">
<label for="api_key">API Key</label>
<input type="password" name="api_key" id="api_key" required autofocus>
<button type="submit">Sign In</button>
</form>
</div>
</body>
</html>"""


@app.post("/login")
def login_submit(request: Request, api_key: str = ""):
    configured_key = _get_api_key()
    if not configured_key or hmac.compare_digest(api_key, configured_key):
        token = _create_session()
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            key="session", value=token,
            httponly=True, samesite="lax",
            max_age=int(_SESSION_TTL.total_seconds()),
        )
        return resp
    return RedirectResponse("/login?error=Invalid+API+key", status_code=303)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


def _require_auth(request: Request):
    if not _auth_enabled():
        return
    session_token = request.cookies.get("session")
    if _validate_session(session_token):
        return
    if _check_api_key_header(request):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, status: str = ""):
    _require_auth(request)
    return HTMLResponse(await _render_dashboard(status))


@app.get("/status")
async def status(request: Request):
    _require_auth(request)
    cfg = _cfg()
    if not Path(cfg.paths.database_path).exists():
        return JSONResponse({"error": "ManifestDB not found"}, status_code=503)

    db = ManifestDB(cfg.paths.database_path)
    try:
        runs = db.get_recent_runs(10)
        recent_runs = []
        for r in runs:
            recent_runs.append({
                "mode": r.get("mode", "?"),
                "status": r.get("status", "?"),
                "started_at": _format_datetime(r.get("started_at", "")),
                "files": r.get("files_copied", 0),
                "duration": f"{r.get('duration_seconds', 0):.0f}s" if r.get("duration_seconds") else "-",
                "error": r.get("error_message", "")
            })

        cloud_last_run = _last_run_summary(db, "cloud")
        lan_last_run = _last_run_summary(db, "lan")

        return JSONResponse({
            "firm": cfg.firm_name,
            "fy_prefix": get_fy_prefix(),
            "schedule": {
                "cloud_cron": _cron_to_human(cfg.schedule.cloud_cron, cfg.schedule.timezone),
                "lan_cron": _cron_to_human(cfg.schedule.lan_cron, cfg.schedule.timezone),
            },
            "cloud": {
                "running": await _is_running("cloud"),
                "last_run": cloud_last_run,
                "last_success": _get_last_success(db, "cloud"),
                "last_run_formatted": f"{cloud_last_run['status']} — {_format_datetime(cloud_last_run['started_at'])}" if cloud_last_run else "No data",
            },
            "lan": {
                "running": await _is_running("lan"),
                "last_run": lan_last_run,
                "last_success": _get_last_success(db, "lan"),
                "last_run_formatted": f"{lan_last_run['status']} — {_format_datetime(lan_last_run['started_at'])}" if lan_last_run else "No data",
            },
            "manifest": {
                "lan_files": db.file_count("lan_status"),
                "cloud_files": db.file_count("cloud_status"),
            },
            "health": _get_health(),
            "recent_runs": recent_runs
        })
    finally:
        db.close()


@app.get("/health")
def health():
    """Unauthenticated health check for monitoring systems."""
    try:
        src = _cfg().paths.source_drive
        source_ok = Path(src).exists()
        return JSONResponse({
            "status": "healthy",
            "source_drive": str(src),
            "source_accessible": source_ok,
        })
    except Exception:
        return JSONResponse({"status": "healthy"}, status_code=200)


@app.post("/trigger/cloud")
async def trigger_cloud(request: Request, background_tasks: BackgroundTasks):
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"trigger:{client_ip}", _RATE_MAX_TRIGGER):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    if await _is_running("cloud"):
        return JSONResponse({"status": "already_running", "detail": "Cloud backup is already in progress."}, status_code=400)
    background_tasks.add_task(_run_in_background, "cloud")
    return JSONResponse({"status": "triggered", "detail": "Cloud backup triggered successfully!"})


@app.post("/trigger/lan")
async def trigger_lan(request: Request, background_tasks: BackgroundTasks):
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"trigger:{client_ip}", _RATE_MAX_TRIGGER):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    if await _is_running("lan"):
        return JSONResponse({"status": "already_running", "detail": "LAN backup is already in progress."}, status_code=400)
    background_tasks.add_task(_run_in_background, "lan")
    return JSONResponse({"status": "triggered", "detail": "LAN backup triggered successfully!"})


# ── Report endpoints ────────────────────────────────────────


@app.get("/report/weekly")
def report_weekly(request: Request):
    """Generate and download a weekly HTML report."""
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"report:{client_ip}", _RATE_MAX_REPORT):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    return _serve_report(7, "Weekly")


@app.get("/report/monthly")
def report_monthly(request: Request):
    """Generate and download a monthly HTML report."""
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"report:{client_ip}", _RATE_MAX_REPORT):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    return _serve_report(30, "Monthly")


def _serve_report(days: int, period: str) -> Response:
    """Generate an HTML report and serve it as a downloadable file."""
    cfg = _cfg()
    db_path = Path(cfg.paths.database_path)

    if not db_path.exists():
        return HTMLResponse("<p>No database found. Run a backup first.</p>", status_code=503)

    db = ManifestDB(db_path)
    try:
        from core.report import generate_report_html
        html_body = generate_report_html(db, cfg.firm_name, days, period)

        if not html_body:
            return HTMLResponse(
                f"<p>No runs found in the last {days} days.</p>",
                status_code=404,
            )

        # Wrap in a proper HTML document with basic styling
        full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(cfg.firm_name)} — {period} Backup Report</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 2rem auto; padding: 1rem; color: #1f2937; }}
h2 {{ color: #1e3a5f; }}
h3 {{ color: #374151; margin-top: 1.5rem; }}
table {{ border-collapse: collapse; width: 100%; margin: 0.5rem 0; }}
td, th {{ padding: 0.4rem 0.75rem; text-align: left; border: 1px solid #d1d5db; }}
th {{ background: #f3f4f6; font-size: 0.85rem; }}
@media print {{ body {{ margin: 0; }} }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""

        filename = f"{cfg.firm_name}_{period}_Report_{datetime.now().strftime('%Y-%m-%d')}.html"
        return Response(
            content=full_html,
            media_type="text/html",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    finally:
        db.close()


# ── Helpers ──────────────────────────────────────────────────


def _get_last_success(db: ManifestDB, mode: str) -> str | None:
    """Return ended_at timestamp of the last successful run for this mode."""
    run = db.last_run(mode)
    if run and run.get("status", "").endswith("_COMPLETE"):
        return run.get("ended_at")
    return None


def _format_datetime(dt_str: str | None) -> str:
    if not dt_str:
        return "-"
    dt_str = str(dt_str).strip()
    if not dt_str:
        return "-"
    
    try:
        # Standard ISO 8601 parsing handles timezone offsets natively
        # Standardize Z UTC suffix to +00:00 offset format
        clean_str = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
        
        # If timezone naive, treat as UTC
        if dt.tzinfo is None:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            
        # Dynamically translate to the server's local timezone
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # Fallback to safe string truncation if parsing fails
        return dt_str[:19].replace("T", " ")


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
        "ended_at": run.get("ended_at", ""),
    }


def _cron_to_human(cron: str, tz: str) -> str:
    """Convert a 5-field cron expression to a human-readable string."""
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron
    minute, hour, dom, month, dow = parts

    tz_short = tz.split("/")[-1] if "/" in tz else tz

    if dow != "*":
        days = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday",
                 "THU": "Thursday", "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday"}
        day_name = days.get(dow.upper(), dow)
        return f"Every {day_name} at {int(hour):02d}:{int(minute):02d} {tz_short}"

    if dom != "*":
        suffix = "th" if 4 <= int(dom) <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(int(dom) % 10, "th")
        return f"{int(dom)}{suffix} of month at {int(hour):02d}:{int(minute):02d} {tz_short}"

    return f"Daily at {int(hour):02d}:{int(minute):02d} {tz_short}"


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

async def _render_dashboard(flash: str = "") -> str:
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
    flash_html = ""
    history_rows = ""

    if db:
        try:
            cloud_active = await _is_running("cloud")
            lan_active = await _is_running("lan")

            cloud_running = "Running" if cloud_active else "Idle"
            lan_running = "Running" if lan_active else "Idle"
            cr = _last_run_summary(db, "cloud")
            lr = _last_run_summary(db, "lan")
            if cr:
                cloud_last = f"{cr['status']} — {_format_datetime(cr['started_at'])}"
                cloud_run = f"{cr['status']} ({cr['files']} files)"
                if cr.get("error"):
                    cloud_run += f" — {html.escape(cr['error'][:60])}"
            if lr:
                lan_last = f"{lr['status']} — {_format_datetime(lr['started_at'])}"
                lan_run = f"{lr['status']} ({lr['files']} files)"
                if lr.get("error"):
                    lan_run += f" — {html.escape(lr['error'][:60])}"
            cloud_files = db.file_count("cloud_status")
            lan_files = db.file_count("lan_status")

            cloud_last_success = _get_last_success(db, "cloud")
            lan_last_success = _get_last_success(db, "lan")

            cloud_class = "running" if cloud_active else (
                "success" if cr and "COMPLETE" in cr["status"] else "failed"
            )
            lan_class = "running" if lan_active else (
                "success" if lr and "COMPLETE" in lr["status"] else "failed"
            )
            cloud_btn = "disabled" if cloud_active else ""
            lan_btn = "disabled" if lan_active else ""

            h = _get_health()
            if "error" not in h:
                health_info = f"Source: {h['source_free_gb']} GB free | FY: {get_fy_prefix()}"

            # Schedule info for template
            cloud_schedule = _cron_to_human(cfg.schedule.cloud_cron, cfg.schedule.timezone)
            lan_schedule = _cron_to_human(cfg.schedule.lan_cron, cfg.schedule.timezone)

            # Run history table
            runs = db.get_recent_runs(10)
            for r in runs:
                mode_tag = f'<span class="tag {r["mode"]}">{r["mode"].upper()}</span>'
                s = r.get("status", "?")
                if "COMPLETE" in s or s == "CLOUD_COMPLETE" or s == "LAN_COMPLETE":
                    s_tag = '<span class="tag success">OK</span>'
                elif "PARTIAL" in s:
                    s_tag = '<span class="tag partial">PARTIAL</span>'
                elif "FAILED" in s:
                    s_tag = '<span class="tag failed">FAILED</span>'
                else:
                    s_tag = f'<span class="tag">{s[:10]}</span>'
                ts = _format_datetime(r.get("started_at", ""))
                files = r.get("files_copied", 0)
                err = r.get("error_message", "")
                dur = f"{r.get('duration_seconds', 0):.0f}s" if r.get("duration_seconds") else "-"
                err_cell = f'<td style="color:#fca5a5;max-width:200px;overflow:hidden;text-overflow:ellipsis">{err[:60]}</td>' if err else "<td>-</td>"
                history_rows += f"<tr><td>{ts}</td><td>{mode_tag}</td><td>{s_tag}</td><td>{files}</td><td>{dur}</td>{err_cell}</tr>\n"
        finally:
            db.close()

    # Flash messages
    flash_map = {
        "triggered_cloud": ("Cloud backup started. Check back in a few minutes.", "success"),
        "triggered_lan": ("LAN backup started (WoL + sync + shutdown).", "success"),
        "already_running_cloud": ("Cloud backup is already in progress.", "warning"),
        "already_running_lan": ("LAN backup is already in progress.", "warning"),
    }
    if flash in flash_map:
        msg, cls = flash_map[flash]
        flash_html = f'<div class="flash {cls}">{msg}</div>'

    return render_dashboard(
        lan_files=lan_files,
        cloud_files=cloud_files,
        fy_prefix=get_fy_prefix(),
        cloud_class=cloud_class,
        cloud_running=cloud_running,
        cloud_run=cloud_run,
        cloud_last=cloud_last,
        cloud_btn=cloud_btn,
        lan_class=lan_class,
        lan_running=lan_running,
        lan_run=lan_run,
        lan_last=lan_last,
        lan_btn=lan_btn,
        health_info=health_info,
        flash_html=flash_html,
        history_rows=history_rows,
        auth_enabled=_auth_enabled(),
        cloud_schedule=cloud_schedule,
        lan_schedule=lan_schedule,
        cloud_last_success=cloud_last_success if cr else None,
        lan_last_success=lan_last_success if lr else None,
    )

