"""AAM Backup Automation — Dashboard UI with manual triggers.

FastAPI server bound to configurable host:port. Safety-first:
  - Lock file per pipeline prevents concurrent runs
  - Confirmation required for trigger
  - Buttons disabled while pipeline is running
  - API key auth via session cookie or X-API-Key header
"""

import hmac
import html
import secrets
import shutil
import threading
import time
import asyncio
import re
from datetime import timedelta
from pathlib import Path

import pendulum
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger

from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowRunFilter
from prefect.client.schemas.objects import StateType
from prefect.deployments import run_deployment, arun_deployment


from core.time_utils import IST, cron_to_human, get_fy_prefix
from core.manifest import ManifestDB


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

app = FastAPI(title="AAM Backup Dashboard")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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


_DB_INSTANCE = None

def get_db():
    global _DB_INSTANCE
    if _DB_INSTANCE is None:
        _DB_INSTANCE = ManifestDB(_cfg().paths.database_path)
    return _DB_INSTANCE


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
        flow_run = await arun_deployment(name=f"aam-backup/backup-{pipeline}")
        logger.info(f"Triggered {pipeline} — run ID: {flow_run.id}")
    except Exception as e:
        logger.error(f"Manual {pipeline} trigger failed: {e}")


# ── API endpoints ────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    error_html = f'<p style="color:#ef4444;margin-bottom:1rem">{html.escape(error)}</p>' if error else ""
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
async def login_submit(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"login:{client_ip}", _RATE_MAX_LOGIN):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    form = await request.form()
    api_key = form.get("api_key", "")
    configured_key = _get_api_key()
    if not configured_key or hmac.compare_digest(str(api_key), configured_key):
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
    # Browser requests (Accept: text/html) get redirected to login page.
    # API requests get JSON 401.
    accept = request.headers.get("Accept", "")
    if "text/html" in accept:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, status: str = ""):
    _require_auth(request)
    cfg = _cfg()

    flash_map = {
        "triggered_cloud": ("Cloud backup started. Check back in a few minutes.", "success"),
        "triggered_lan": ("LAN backup started (WoL + sync + shutdown).", "success"),
        "already_running_cloud": ("Cloud backup is already in progress.", "warning"),
        "already_running_lan": ("LAN backup is already in progress.", "warning"),
    }
    flash_html = ""
    if status in flash_map:
        msg, cls = flash_map[status]
        flash_html = f'<div class="flash {cls}">{msg}</div>'

    cloud_schedule = cron_to_human(cfg.schedule.cloud_cron, cfg.schedule.timezone)
    lan_schedule = cron_to_human(cfg.schedule.lan_cron, cfg.schedule.timezone)

    context = {
        "request": request,
        "fy_prefix": get_fy_prefix(),
        "flash_html": flash_html,
        "auth_enabled": _auth_enabled(),
        "cloud_schedule": cloud_schedule,
        "lan_schedule": lan_schedule,
    }
    try:
        # Starlette >= 0.28
        return templates.TemplateResponse(request=request, name="dashboard.html", context=context)
    except TypeError:
        # Starlette < 0.28
        return templates.TemplateResponse("dashboard.html", context)


@app.get("/status")
async def status(request: Request):
    _require_auth(request)
    cfg = _cfg()

    db = get_db()
    try:
        runs = db.get_recent_runs(25)
    except Exception:
        return JSONResponse({"error": "ManifestDB not found"}, status_code=503)

    recent_runs = []
    for r in runs:
        err_msg = r.get("error_message") or ""
        if len(err_msg) > 2000:
            err_msg = err_msg[:2000] + "..."
        recent_runs.append({
            "mode": r.get("mode", "?"),
            "status": r.get("status", "?"),
            "started_at": (r.get("started_at") or "-")[:19].replace("T", " "),
            "files": r.get("files_copied", 0),
            "files_failed": r.get("files_failed", 0),
            "duration": f"{r.get('duration_seconds', 0):.0f}s" if r.get("duration_seconds") else "-",
            "error": err_msg,
            "extended_metrics": r.get("extended_metrics", "")
        })

    cloud_last_run = _last_run_summary(db, "cloud")
    lan_last_run = _last_run_summary(db, "lan")

    return JSONResponse({
        "firm": cfg.firm_name,
        "fy_prefix": get_fy_prefix(),
        "schedule": {
            "cloud_cron": cron_to_human(cfg.schedule.cloud_cron, cfg.schedule.timezone),
            "lan_cron": cron_to_human(cfg.schedule.lan_cron, cfg.schedule.timezone),
        },
        "cloud": {
            "running": await _is_running("cloud"),
            "last_run": cloud_last_run,
            "last_success": _get_last_success(db, "cloud"),
            "last_run_formatted": (cloud_last_run["started_at"] or "-")[:19].replace("T", " ") if cloud_last_run else "No data",
        },
        "lan": {
            "running": await _is_running("lan"),
            "last_run": lan_last_run,
            "last_success": _get_last_success(db, "lan"),
            "last_run_formatted": (lan_last_run["started_at"] or "-")[:19].replace("T", " ") if lan_last_run else "No data",
        },
        "manifest": {
            "lan_files": db.file_count("lan_status"),
            "cloud_files": db.file_count("cloud_status"),
        },
        "health": await _get_health(),
        "recent_runs": recent_runs
    })


@app.get("/health")
async def health():
    """Unauthenticated health check for monitoring systems."""
    try:
        src = _cfg().paths.source_drive
        source_ok = await asyncio.to_thread(Path(src).exists)
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


# ── Email-report endpoints ────────────────────────────────────────

@app.post("/trigger/report/weekly/email")
def trigger_weekly_email(request: Request):
    """Immediately generate and email the weekly report to configured recipients.

    Acts as both a "Send Test Email" and an on-demand report delivery button.
    Returns 200 on success, 500 if SMTP fails, 404 if no runs found.
    """
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"report:{client_ip}", _RATE_MAX_REPORT):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    cfg = _cfg()
    db = get_db()
    from core.report import send_weekly_report, generate_report_html
    html_body = generate_report_html(db, cfg.firm_name, 7, "Weekly")
    if not html_body:
        return JSONResponse(
            {"status": "no_data", "detail": "No backup runs in the last 7 days — nothing to send."},
            status_code=404,
        )
    try:
        ok = send_weekly_report(db, cfg.notifications, cfg.firm_name, body_html=html_body)
    except Exception as e:
        logger.exception("Failed to send weekly email report manual trigger")
        return JSONResponse(
            {"status": "failed", "detail": f"SMTP / Network Error: {str(e)}"},
            status_code=500,
        )
    if ok:
        recipients = ", ".join(cfg.notifications.recipients)
        return JSONResponse({"status": "sent", "detail": f"Weekly report emailed to: {recipients}"})
    return JSONResponse(
        {"status": "failed", "detail": "Failed to send email. Check SMTP settings in config.yaml and server logs."},
        status_code=500,
    )


@app.post("/trigger/report/monthly/email")
def trigger_monthly_email(request: Request):
    """Immediately generate and email the monthly report to configured recipients.

    Acts as both a "Send Test Email" and an on-demand report delivery button.
    Returns 200 on success, 500 if SMTP fails, 404 if no runs found.
    """
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"report:{client_ip}", _RATE_MAX_REPORT):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    cfg = _cfg()
    db = get_db()
    from core.report import send_monthly_report, generate_report_html
    html_body = generate_report_html(db, cfg.firm_name, 30, "Monthly")
    if not html_body:
        return JSONResponse(
            {"status": "no_data", "detail": "No backup runs in the last 30 days — nothing to send."},
            status_code=404,
        )
    try:
        ok = send_monthly_report(db, cfg.notifications, cfg.firm_name, body_html=html_body)
    except Exception as e:
        logger.exception("Failed to send monthly email report manual trigger")
        return JSONResponse(
            {"status": "failed", "detail": f"SMTP / Network Error: {str(e)}"},
            status_code=500,
        )
    if ok:
        recipients = ", ".join(cfg.notifications.recipients)
        return JSONResponse({"status": "sent", "detail": f"Monthly report emailed to: {recipients}"})
    return JSONResponse(
        {"status": "failed", "detail": "Failed to send email. Check SMTP settings in config.yaml and server logs."},
        status_code=500,
    )


def _serve_report(days: int, period: str) -> Response:
    """Generate an HTML report and serve it as a downloadable file."""
    cfg = _cfg()
    db_path = Path(cfg.paths.database_path)

    if not db_path.exists():
        return HTMLResponse("<p>No database found. Run a backup first.</p>", status_code=503)

    db = get_db()
    from core.report import generate_report_html
    html_body = generate_report_html(db, cfg.firm_name, days, period)

    if not html_body:
        return HTMLResponse(
            f"<p>No runs found in the last {days} days.</p>",
            status_code=404,
        )

    safe_firm = re.sub(r'[^a-zA-Z0-9_\-]', '_', cfg.firm_name)
    filename = f"{safe_firm}_{period}_Report_{pendulum.now(IST).format('YYYY-MM-DD')}.html"
    return Response(
        content=html_body,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Helpers ──────────────────────────────────────────────────


def _get_last_success(db: ManifestDB, mode: str) -> str | None:
    """Return ended_at timestamp of the last successful run for this mode."""
    run = db.last_successful_run(mode)
    if run:
        return run.get("ended_at")
    return None


def _last_run_summary(db: ManifestDB, mode: str) -> dict | None:
    run = db.last_run(mode)
    if not run:
        return None
    err_msg = run.get("error_message") or ""
    if len(err_msg) > 2000:
        err_msg = err_msg[:2000] + "..."
    return {
        "status": run.get("status", "unknown"),
        "started_at": run.get("started_at", ""),
        "files": run.get("files_copied", 0),
        "files_failed": run.get("files_failed", 0),
        "duration": f"{run.get('duration_seconds', 0):.0f}s" if run.get("duration_seconds") else "?",
        "error": err_msg,
        "ended_at": run.get("ended_at", ""),
    }


async def _get_health() -> dict:
    try:
        src = _cfg().paths.source_drive
        du = await asyncio.to_thread(shutil.disk_usage, src)
        return {
            "source_free_gb": f"{du.free / (1024**3):.1f}",
            "source_exists": await asyncio.to_thread(Path(src).exists),
        }
    except Exception:
        return {"error": "unavailable"}



