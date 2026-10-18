"""AAM Backup Automation — Dashboard UI with manual triggers.

FastAPI server bound to configurable host:port. Safety-first:
  - Lock file per pipeline prevents concurrent runs
  - Confirmation required for trigger
  - Buttons disabled while pipeline is running
  - API key auth via session cookie or X-API-Key header
"""

import asyncio
import hmac
import html
import json
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pendulum
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import (
    FlowRunFilter,
    FlowRunFilterState,
    FlowRunFilterStateType,
)
from prefect.client.schemas.objects import StateType
from prefect.deployments import arun_deployment

from core import (admin_auth, approvals, config_edit, faults, fy_rollover, rollover_journal,
                  statuses)
from core.attention import RED, compute_attention
from core.manifest import ManifestDB
from core.time_utils import IST, cron_to_human, format_ist, get_fy_prefix, now_iso

# In-memory rate limiter, one bucket per kind of action and address (audit
# 2026-10-18 item 7: dashboard.rate_limit_* - settings saves must never use up
# the right to approve a blocked backup). Cleans up expired entries on access.
_RATE_LIMITS: dict[str, list[float]] = {}
_RATE_LOCK = threading.Lock()


def _dash(name: str):
    """A dashboard.* setting; the model default when a partial config lacks it
    (a loaded config.yaml always has every key)."""
    from models.config import DashboardConfig, model_default

    return getattr(getattr(_cfg(), "dashboard", None), name,
                   model_default(DashboardConfig, name))


def _check_rate_limit(client_ip: str, max_attempts: int) -> bool:
    """Return True if request is allowed, False if rate limited."""
    with _RATE_LOCK:
        now = time.time()
        window = _dash("rate_limit_window_seconds")
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


def _session_ttl() -> timedelta:
    return timedelta(hours=_dash("session_hours"))


def _create_session() -> str:
    token = secrets.token_hex(32)
    _sessions[token] = {"created_at": time.time()}
    _cleanup_expired_sessions()
    return token


def _cleanup_expired_sessions() -> None:
    """Remove expired sessions. Called on each new session creation."""
    now = time.time()
    ttl = _session_ttl().total_seconds()
    expired = [t for t, s in _sessions.items() if now - s["created_at"] > ttl]
    for t in expired:
        del _sessions[t]


def _validate_session(token: str | None) -> bool:
    if not token:
        return False
    session = _sessions.get(token)
    if not session:
        return False
    if time.time() - session["created_at"] > _session_ttl().total_seconds():
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
# TTL-based refresh ensures the dashboard picks up config.yaml changes (e.g. FY
# rollover) within dashboard.config_refresh_seconds without a service restart.

_config = None
_config_loaded_at: float = 0.0

# F13: uvicorn runs the sync endpoints in a threadpool, so two requests can
# refresh config / open the DB concurrently. The old code had no lock: one
# thread could CLOSE the cached DB connection while another was mid-query
# (transient 503s), or two threads could both open a connection on a path
# change. One lock now serializes the whole config-refresh + DB-lifecycle
# critical section. Cost: requests serialize briefly only at the 5-minute
# refresh boundary (config load is a small YAML parse) — negligible for an
# ops dashboard.
_CFG_LOCK = threading.RLock()  # RLock: re-entrant — get_db() acquires this, then calls _cfg() which re-acquires it (same thread)


def _cfg():
    global _config, _config_loaded_at
    with _CFG_LOCK:
        now = time.time()
        refresh = getattr(getattr(_config, "dashboard", None), "config_refresh_seconds", 0)
        if _config is None or (now - _config_loaded_at) > refresh:
            from models.config import CONFIG_PATH, load_config
            new_cfg = load_config(CONFIG_PATH)
            # If the database path changed (e.g. FY rollover), close and evict
            # the cached DB connection so get_db() reconnects with the new
            # path. Done under the same lock so no thread can use the
            # connection between close() and the reopen in get_db().
            if _config is not None and _config.paths.database_path != new_cfg.paths.database_path:
                global _DB_INSTANCE
                if _DB_INSTANCE is not None:
                    try:
                        _DB_INSTANCE.close()
                    except Exception:
                        pass
                    _DB_INSTANCE = None
                logger.info(
                    f"Config refreshed: database path changed from "
                    f"{_config.paths.database_path} to {new_cfg.paths.database_path}"
                )
            _config = new_cfg
            _config_loaded_at = now
        return _config


_DB_INSTANCE = None

def get_db():
    global _DB_INSTANCE
    with _CFG_LOCK:
        _cfg()  # ensure the config (and any DB eviction) is current first
        if _DB_INSTANCE is None:
            _DB_INSTANCE = ManifestDB.from_config(_cfg())
        return _DB_INSTANCE



# ── Pipeline status ──────────────────────────────────────────


async def _is_running(pipeline: str) -> bool | None:
    """Check if a backup pipeline is active (running or pending).

    H3: returns None when Prefect is unreachable — callers decide
    fail-open (status display) vs fail-closed (triggers).
    """
    return await _prefect_has_active_run(pipeline)


async def _prefect_has_active_run(pipeline: str) -> bool | None:
    """Check Prefect API for an active flow run of the given pipeline.

    Checks both RUNNING and PENDING states — a PENDING run means
    it's queued behind the concurrency limit and will execute when the slot opens.
    Future SCHEDULED runs are ignored as they are not currently active.

    H3 tri-state contract:
        True / False  — Prefect answered; result is authoritative.
        None          — the API could not be reached; "unknown".
                        Callers decide fail-open vs fail-closed:
                        trigger endpoints REFUSE (503), /status degrades.
                        The old code returned False on any exception, which
                        silently disabled the duplicate-run guard.

    Uses Prefect's typed filter models (same construction as launch.py) —
    a raw nested dict relied on Pydantic coercion and broke silently if
    validation tightened. limit=200: >20 queued runs previously could hide
    an active one from this check.
    """
    try:
        async with get_client() as client:
            runs = await client.read_flow_runs(
                flow_run_filter=FlowRunFilter(
                    state=FlowRunFilterState(
                        type=FlowRunFilterStateType(
                            any_=[StateType.RUNNING, StateType.PENDING, StateType.SCHEDULED]
                        )
                    )
                ),
                limit=200,
            )
    except Exception as e:
        logger.error(f"Failed to query Prefect API: {e}")
        return None
    for run in runs:
        tags = run.tags or []
        parameters = run.parameters or {}
        # Exclude read-only integrity audits from hijacking backup status
        if "integrity" in tags:
            continue
        run_mode = parameters.get("mode", "")
        if not (pipeline in tags or run_mode == pipeline or run_mode == "all"):
            continue
        state = getattr(run, "state", None)
        state_type = getattr(state, "type", None) or getattr(run, "state_type", None)
        if state_type in (StateType.RUNNING, StateType.PENDING):
            return True
        if state_type == StateType.SCHEDULED:
            # F2: a just-triggered run is SCHEDULED until the runner picks it
            # up (~poll frequency). Count only due-now runs as active so a
            # double-click cannot create a duplicate backup, while future
            # cron-scheduled runs never block manual triggers.
            nxt = getattr(run, "next_scheduled_start_time", None)
            if nxt is None:
                return True
            try:
                now = datetime.now(timezone.utc)
                nxt_dt = nxt if nxt.tzinfo else nxt.replace(tzinfo=timezone.utc)
                if nxt_dt <= now + timedelta(seconds=60):
                    return True
            except Exception:
                return True
            continue
        # Fail-closed (H3): an unrecognized/missing state on a matching run
        # must not read as idle — refusing a trigger is safe, a duplicate
        # backup is not.
        return True
    return False


async def _is_verify_running() -> bool | None:
    """Check if an integrity-audit flow run is active (running or pending).

    Mirrors _prefect_has_active_run but matches the read-only audit runs
    (``"integrity"`` tag) that _is_running deliberately skips. Tri-state:
    True / False / None (Prefect unreachable — callers fail closed).
    """
    try:
        async with get_client() as client:
            runs = await client.read_flow_runs(
                flow_run_filter=FlowRunFilter(
                    state=FlowRunFilterState(
                        type=FlowRunFilterStateType(
                            any_=[StateType.RUNNING, StateType.PENDING, StateType.SCHEDULED]
                        )
                    )
                ),
                limit=200,
            )
    except Exception as e:
        logger.error(f"Failed to query Prefect API: {e}")
        return None
    for run in runs:
        tags = run.tags or []
        if "integrity" not in tags:
            continue
        state = getattr(run, "state", None)
        state_type = getattr(state, "type", None) or getattr(run, "state_type", None)
        if state_type in (StateType.RUNNING, StateType.PENDING):
            return True
        if state_type == StateType.SCHEDULED:
            nxt = getattr(run, "next_scheduled_start_time", None)
            if nxt is None:
                return True
            try:
                now = datetime.now(timezone.utc)
                nxt_dt = nxt if nxt.tzinfo else nxt.replace(tzinfo=timezone.utc)
                if nxt_dt <= now + timedelta(seconds=60):
                    return True
            except Exception:
                return True
            continue
        return True
    return False


def _run_state_fields(state: bool | None) -> dict:
    """JSON fields for one pipeline's activity state (H3).

    ``running`` stays a strict boolean — dashboard.js does truthiness on it
    every 2 s (a string "unknown" would freeze badges at "Running").
    ``run_state`` carries the honest tri-state for future consumers.
    """
    if state is True:
        run_state = "running"
    elif state is False:
        run_state = "idle"
    else:
        run_state = "unknown"
    return {"running": state is True, "run_state": run_state}


# ── Trigger pipeline (via Prefect deployment API) ────────────
# (G15: the endpoints now await arun_deployment inline and surface failures
# as HTTP 500 — the old fire-and-forget _run_in_background helper is gone.)
#
# F2: bounded synchronous wait. arun_deployment is called with a finite
# timeout (dashboard.trigger_wait_seconds) so the HTTP response never hangs for the
# whole backup (50s-hours) and proxies never 504. Deployment-lookup errors
# still raise before run creation (G15 preserved: timeout=0/180 only bounds
# the post-creation poll; dashboard.trigger_wait_seconds). The returned FlowRun
# is mapped truthfully:
# terminal states reported as completed/failed/cancelled; a still-running
# run after the bound returns 202 + authoritative flow_run_id and keeps
# running server-side (arun_deployment never cancels on timeout).

def _trigger_timeout(request: Request) -> float:
    wait = request.query_params.get("wait", "true").lower()
    if wait in ("false", "0", "no", "off"):
        return 0
    return _dash("trigger_wait_seconds")


def _trigger_result(label: str, flow_run, noun: str = "backup") -> JSONResponse:
    run_id = str(flow_run.id)
    state = getattr(flow_run, "state", None)
    state_type = getattr(state, "type", None)
    if state is not None and state.is_final():
        if state_type == StateType.COMPLETED:
            return JSONResponse({
                "status": "completed",
                "flow_run_id": run_id,
                "detail": f"{label} {noun} completed (flow run {run_id}).",
            })
        if state_type in (StateType.CANCELLED, StateType.CANCELLING):
            return JSONResponse({
                "status": "cancelled",
                "flow_run_id": run_id,
                "detail": f"{label} {noun} was cancelled (flow run {run_id}).",
            }, status_code=409)
        return JSONResponse({
            "status": "failed",
            "flow_run_id": run_id,
            "detail": f"{label} {noun} run {state.name if state else 'failed'} (flow run {run_id}).",
        }, status_code=500)
    return JSONResponse({
        "status": "running",
        "flow_run_id": run_id,
        "detail": f"{label} {noun} started (flow run {run_id}).",
    }, status_code=202)


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
    if not _check_rate_limit(f"login:{client_ip}", _dash("rate_limit_logins")):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    is_https = request.url.scheme == "https"
    if not _auth_enabled():
        token = _create_session()
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            key="session", value=token,
            httponly=True, samesite="lax",
            secure=is_https,
            max_age=int(_session_ttl().total_seconds()),
        )
        return resp
    form = await request.form()
    api_key = form.get("api_key", "")
    configured_key = _get_api_key()
    if configured_key and hmac.compare_digest(str(api_key), configured_key):
        token = _create_session()
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            key="session", value=token,
            httponly=True, samesite="lax",
            secure=is_https,
            max_age=int(_session_ttl().total_seconds()),
        )
        return resp
    return RedirectResponse("/login?error=Invalid+API+key", status_code=303)


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        _sessions.pop(token, None)
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
async def dashboard(request: Request):
    _require_auth(request)
    cfg = _cfg()

    cloud_schedule = cron_to_human(cfg.schedule.cloud_cron, cfg.schedule.timezone)
    lan_schedule = cron_to_human(cfg.schedule.lan_cron, cfg.schedule.timezone)

    context = {
        "request": request,
        "fy_prefix": fy_rollover.effective_fy_prefix(cfg.paths.source_drive),
        "auth_enabled": _auth_enabled(),
        "cloud_schedule": cloud_schedule,
        "lan_schedule": lan_schedule,
        "cloud_enabled": cfg.cloud.enabled,
        "lan_enabled": cfg.lan.enabled,
        # the LAN confirm text says what a manual run really does (audit 2026-10-18)
        "lan_wol": getattr(getattr(cfg, "wol", None), "enabled", False),
        "lan_shutdown": getattr(cfg.lan, "shutdown_after_backup", False),
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
            **_status_fields(r),
            "started_at": format_ist(r.get("started_at")),
            "files": r.get("files_copied", 0),
            "files_failed": r.get("files_failed", 0),
            "duration": f"{r.get('duration_seconds', 0):.0f}s" if r.get("duration_seconds") else "-",
            "error": err_msg,
            "extended_metrics": r.get("extended_metrics", "")
        })

    cloud_last_run = _last_run_summary(db, "cloud")
    lan_last_run = _last_run_summary(db, "lan")

    # H3: None (Prefect unreachable) degrades to running=False + run_state
    # "unknown". The boolean type is load-bearing — dashboard.js polls this
    # every 2 s and does truthiness on it.
    cloud_fields = _run_state_fields(await _is_running("cloud"))
    lan_fields = _run_state_fields(await _is_running("lan"))

    return JSONResponse({
        "firm": cfg.firm_name,
        "fy_prefix": fy_rollover.effective_fy_prefix(cfg.paths.source_drive),
        "attention": _attention(db, cfg),
        "schedule": {
            "cloud_cron": cron_to_human(cfg.schedule.cloud_cron, cfg.schedule.timezone),
            "lan_cron": cron_to_human(cfg.schedule.lan_cron, cfg.schedule.timezone),
        },
        "cloud": {
            **cloud_fields,
            "enabled": cfg.cloud.enabled,
            "last_run": cloud_last_run,
            "last_success": _get_last_success(db, "cloud"),
            "last_run_formatted": format_ist(cloud_last_run["started_at"]) if cloud_last_run else "No data",
            # Integrity status is a SEPARATE fact from the backup result:
            # VERIFIED only after a passing audit; NOT_VERIFIED when no
            # audit has run; VERIFICATION_FAILED on divergence. A COMPLETE
            # backup does NOT imply VERIFIED.
            "integrity": _integrity_summary(db, "cloud"),
        },
        "lan": {
            **lan_fields,
            "enabled": cfg.lan.enabled,
            "last_run": lan_last_run,
            "last_success": _get_last_success(db, "lan"),
            "last_run_formatted": format_ist(lan_last_run["started_at"]) if lan_last_run else "No data",
            "integrity": _integrity_summary(db, "lan"),
        },
        "manifest": {
            "lan_files": db.file_count("lan_status"),
            "cloud_files": db.file_count("cloud_status"),
        },
        "health": await _get_health(),
        "recent_runs": recent_runs
    })


def _attention(db, cfg) -> list[dict]:
    try:
        return compute_attention(db, cfg)
    except Exception as e:  # noqa: BLE001 - say so instead of hiding it
        logger.error(f"Attention banner could not be evaluated: {e}")
        return [{"level": RED, "code": "attention_error",
                 "message": "The dashboard could not check the backup state "
                            f"({type(e).__name__}) - see the agent log."}]


@app.get("/health")
async def health():
    """Unauthenticated health check for monitoring systems (plan WP9).

    200 only when nothing needs attention (amber warnings are listed); 503
    with the reasons when anything is red - or when health cannot even be
    evaluated (that used to answer "healthy"). No paths, no secrets."""
    try:
        items = compute_attention(get_db(), _cfg())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"status": "unknown",
                             "reasons": [f"health could not be evaluated ({type(e).__name__})"]},
                            status_code=503)
    red = [i["message"] for i in items if i["level"] == RED]
    warnings = [i["message"] for i in items if i["level"] != RED]
    if red:
        return JSONResponse({"status": "attention", "reasons": red, "warnings": warnings},
                            status_code=503)
    return JSONResponse({"status": "healthy", "warnings": warnings})


@app.post("/trigger/cloud")
async def trigger_cloud(request: Request):
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"trigger:{client_ip}", _dash("rate_limit_triggers")):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    if not _cfg().cloud.enabled:
        return JSONResponse({"status": "disabled", "detail": "Cloud backup leg is disabled in configuration; not started."}, status_code=409)
    cloud_running = await _is_running("cloud")
    if cloud_running is None:
        # H3: fail-CLOSED. The old code treated an unreachable API as
        # "nothing running" and queued a duplicate backup.
        raise HTTPException(
            status_code=503,
            detail="Prefect API unavailable - cannot verify active runs; trigger refused. Retry shortly.",
        )
    if cloud_running:
        return JSONResponse({"status": "already_running", "detail": "Cloud backup is already in progress."}, status_code=400)
    # G15: await the actual deployment run creation before answering. The
    # old code returned 200 via background_tasks BEFORE arun_deployment
    # resolved — a missing deployment (e.g. after a serve() crash-loop) or an
    # API error showed as "triggered successfully" while nothing ran.
    # F2: bound the post-creation wait (?wait=false returns instantly).
    try:
        flow_run = await arun_deployment(
            name="aam-backup/backup-cloud",
            timeout=_trigger_timeout(request),
        )
    except Exception as e:
        logger.error(f"Manual cloud trigger failed: {e}")
        return JSONResponse(
            {"status": "trigger_failed",
             "detail": f"Could not start the cloud backup run: {e}"},
            status_code=500,
        )
    return _trigger_result("Cloud", flow_run)


@app.post("/trigger/lan")
async def trigger_lan(request: Request):
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"trigger:{client_ip}", _dash("rate_limit_triggers")):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    if not _cfg().lan.enabled:
        return JSONResponse({"status": "disabled", "detail": "LAN backup leg is disabled in configuration; not started."}, status_code=409)
    lan_running = await _is_running("lan")
    if lan_running is None:
        raise HTTPException(
            status_code=503,
            detail="Prefect API unavailable - cannot verify active runs; trigger refused. Retry shortly.",
        )
    if lan_running:
        return JSONResponse({"status": "already_running", "detail": "LAN backup is already in progress."}, status_code=400)
    try:
        flow_run = await arun_deployment(
            name="aam-backup/backup-lan",
            timeout=_trigger_timeout(request),
        )
    except Exception as e:
        logger.error(f"Manual LAN trigger failed: {e}")
        return JSONResponse(
            {"status": "trigger_failed",
             "detail": f"Could not start the LAN backup run: {e}"},
            status_code=500,
        )
    return _trigger_result("LAN", flow_run)


# ── Approvals for BLOCKED runs (plan §6.2, owner DEC-3) ──────
# A guard blocked a backup before anything was copied or deleted. A signed-in
# person reviews the reason and numbers here, types the confirmation phrase,
# and approves the backup ONCE: the leg's deployment is re-run with
# approval_id, re-measures, and proceeds only if nothing got worse. Approving
# needs dashboard sign-in (it is the accountable record of who decided).

# leg -> (label, deployment re-run on approval, has an `enabled` switch)
_APPROVAL_TARGETS = {
    "cloud": ("Cloud", "aam-backup/backup-cloud", True),
    "lan": ("LAN", "aam-backup/backup-lan", True),
    "rollover": ("FY rollover", "rollover-check/rollover-check", False),
}


def _approval_actor(request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    how = "session" if _validate_session(request.cookies.get("session")) else "api-key"
    return f"{ip} ({how})"


def _require_approval_rights(request: Request) -> None:
    _require_auth(request)
    if not _auth_enabled():
        raise HTTPException(
            status_code=403,
            detail="Approving or dismissing a blocked backup needs dashboard sign-in: "
                   "set dashboard.auth_enabled: true (with an api_key) in config.yaml.")
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"decision:{client_ip}", _dash("rate_limit_decisions")):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")


def _approval_view(req: dict) -> dict:
    expired = False
    if req.get("expires_at"):
        try:
            expired = pendulum.now("UTC") >= pendulum.parse(req["expires_at"])
        except Exception:  # noqa: BLE001 - unparsable expiry is never valid
            expired = True
    keys = ("approval_id", "leg", "status", "created_at", "blocked_run_id", "fy",
            "guards", "phrase", "approved_at", "approved_by", "expires_at")
    return {**{k: req.get(k) for k in keys}, "expired": expired}


def _load_open_approval(approval_id: str) -> dict:
    req = get_db().get_approval(approval_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"No approval request {approval_id}")
    return req


async def _ensure_leg_idle(leg: str) -> None:
    if leg not in _APPROVAL_TARGETS:
        raise HTTPException(status_code=409, detail=f"Unknown approval target {leg!r}.")
    if _APPROVAL_TARGETS[leg][2] and not getattr(_cfg(), leg).enabled:
        raise HTTPException(status_code=409,
                            detail=f"The {leg} backup leg is disabled in configuration.")
    running = await _is_running(leg)
    if running is None:
        raise HTTPException(status_code=503, detail="Prefect API unavailable - cannot verify "
                                                    "active runs; nothing was changed.")
    if running:
        raise HTTPException(status_code=409, detail=f"A {leg} backup is already in progress; "
                                                    "nothing was changed. Try again after it ends.")


async def _start_approved_run(req: dict, request: Request) -> JSONResponse:
    leg = req["leg"]
    label, deployment, _ = _APPROVAL_TARGETS[leg]
    try:
        flow_run = await arun_deployment(
            name=deployment,
            parameters={"approval_id": req["approval_id"]},
            timeout=_trigger_timeout(request),
        )
    except Exception as e:
        logger.error(f"Approved {leg} re-run could not be started: {e}")
        return JSONResponse({
            "status": "trigger_failed",
            "detail": f"Approved, but the {label} backup could not be started: {e}. "
                      "Use 'Run approved backup' to retry before the approval expires.",
        }, status_code=500)
    return _trigger_result(label, flow_run, noun="backup (approved re-run)")


@app.get("/approvals")
def list_approvals(request: Request):
    _require_auth(request)
    rows = get_db().open_approvals()
    return {"auth_enabled": _auth_enabled(), "approvals": [_approval_view(r) for r in rows]}


@app.post("/approvals/{approval_id}/approve")
async def approve_blocked_run(approval_id: str, request: Request):
    _require_approval_rights(request)
    try:
        typed = (await request.json()).get("phrase", "")
    except Exception:  # noqa: BLE001 - malformed body = no phrase
        typed = ""
    req = _load_open_approval(approval_id)
    if req["status"] != approvals.PENDING:
        raise HTTPException(status_code=409, detail=f"This request is {req['status']}; "
                                                    "only a pending request can be approved.")
    if not approvals.phrase_matches(req["phrase"], typed):
        raise HTTPException(status_code=400,
                            detail=f"To approve, type exactly: {req['phrase']}")
    await _ensure_leg_idle(req["leg"])
    approved_at = now_iso()
    actor = _approval_actor(request)
    if not get_db().approve_request(
            approval_id, approved_by=actor, approved_at=approved_at,
            expires_at=approvals.expires_at(approved_at, _cfg().approvals.expiry_hours)):
        raise HTTPException(status_code=409, detail="This request changed meanwhile; refresh.")
    logger.warning(f"Approval {approval_id} ({req['leg']}: "
                   f"{', '.join(g['guard'] for g in req['guards'])}) given by {actor}")
    return await _start_approved_run(get_db().get_approval(approval_id), request)


@app.post("/approvals/{approval_id}/run")
async def run_approved_backup(approval_id: str, request: Request):
    _require_approval_rights(request)
    req = _load_open_approval(approval_id)
    view = _approval_view(req)
    if req["status"] != approvals.APPROVED:
        raise HTTPException(status_code=409, detail=f"This request is {req['status']}; "
                                                    "only an approved, unused one can be run.")
    if view["expired"]:
        raise HTTPException(status_code=409, detail=f"The approval expired at "
                                                    f"{req['expires_at']}; wait for the next "
                                                    "block and approve again.")
    await _ensure_leg_idle(req["leg"])
    return await _start_approved_run(req, request)


@app.post("/approvals/{approval_id}/dismiss")
def dismiss_blocked_run(approval_id: str, request: Request):
    _require_approval_rights(request)
    _load_open_approval(approval_id)
    actor = _approval_actor(request)
    if not get_db().dismiss_request(approval_id, dismissed_by=actor, dismissed_at=now_iso()):
        raise HTTPException(status_code=409, detail="Only a pending or approved, unused "
                                                    "request can be dismissed.")
    logger.warning(f"Approval request {approval_id} dismissed by {actor}")
    return {"status": "dismissed", "detail": "Dismissed. Nothing will run on this request; "
                                             "the next scheduled run re-checks the guards."}


# ── Settings (owner decision DEC-9) ──────────────────────────
# Every config key except the lock-out keys can be changed here by a signed-in
# person. core.config_edit validates the whole candidate exactly like service
# start before anything is written, asks a typed phrase for risky keys, backs
# config.yaml up, writes it atomically (comments kept) and records who changed
# what. Secrets are write-only.

def _settings_config_path() -> str:
    from models.config import CONFIG_PATH
    return str(CONFIG_PATH)


def _settings_lock_busy() -> str | None:
    """A reason when a backup / the FY rollover holds the backup lock."""
    from core.process import read_lock_alive

    try:
        alive, pid = read_lock_alive(_cfg().paths.backup_lock_path)
    except Exception:  # noqa: BLE001 - unreadable lock: fail closed
        return "the backup lock could not be read"
    return f"a backup or the FY rollover is running (backup lock held by PID {pid})" if alive else None


def _require_settings_rights(request: Request) -> None:
    _require_auth(request)
    if not _auth_enabled():
        raise HTTPException(
            status_code=403,
            detail="Changing settings needs dashboard sign-in: set dashboard.auth_enabled: true "
                   "(with an api_key) in config.yaml.")
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"settings:{client_ip}", _dash("rate_limit_settings")):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")


# ── Advanced-settings login (owner 2026-10-15) ───────────────
# A second, separate login on top of the dashboard sign-in. Its password is
# the dashboard key until changed here; then only a PBKDF2 hash is stored.
# Enforced on the server: without it, advanced settings are neither sent to
# the browser nor accepted in a save / validate.

_admin_sessions: dict[str, float] = {}


def _is_admin(request: Request) -> bool:
    token = request.cookies.get("admin_session")
    created = _admin_sessions.get(token) if token else None
    if created is None:
        return False
    if time.time() - created > _cfg().dashboard.admin_session_minutes * 60:
        _admin_sessions.pop(token, None)
        return False
    return True


@app.get("/api/admin/status")
def admin_status(request: Request):
    _require_auth(request)
    return {"admin": _is_admin(request),
            "password_changed": bool(_cfg().dashboard.admin_password_hash),
            "session_minutes": _cfg().dashboard.admin_session_minutes}


@app.post("/api/admin/login")
async def admin_login(request: Request):
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"admin_login:{client_ip}", _dash("rate_limit_logins")):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    try:
        password = str((await request.json()).get("password", ""))
    except Exception:  # noqa: BLE001
        password = ""
    if not admin_auth.verify_admin_password(password, _cfg().dashboard):
        logger.warning(f"Advanced login refused for {client_ip}")
        raise HTTPException(status_code=401, detail="Wrong admin password.")
    token = secrets.token_hex(32)
    _admin_sessions[token] = time.time()
    logger.warning(f"Advanced login by {_approval_actor(request)}")
    resp = JSONResponse({"status": "ok", "session_minutes": _cfg().dashboard.admin_session_minutes})
    resp.set_cookie(key="admin_session", value=token, httponly=True, samesite="strict",
                    secure=request.url.scheme == "https",
                    max_age=_cfg().dashboard.admin_session_minutes * 60)
    return resp


@app.post("/api/admin/logout")
def admin_logout(request: Request):
    _admin_sessions.pop(request.cookies.get("admin_session") or "", None)
    resp = JSONResponse({"status": "locked"})
    resp.delete_cookie("admin_session")
    return resp


@app.post("/api/admin/password")
async def admin_change_password(request: Request):
    _require_settings_rights(request)
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Sign in to Advanced first.")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    dashboard = _cfg().dashboard
    if not admin_auth.verify_admin_password(str(body.get("current", "")), dashboard):
        raise HTTPException(status_code=400, detail="The current admin password is wrong.")
    problem = admin_auth.new_password_problem(str(body.get("new", "")), str(body.get("confirm", "")),
                                              dashboard.admin_password_min_length)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    actor = _approval_actor(request)
    try:
        config_edit.set_admin_password_hash(
            _settings_config_path(), admin_auth.hash_password(str(body["new"])),
            actor=actor, db=get_db(), now_iso=now_iso)
    except config_edit.ConfigEditError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status)
    global _config_loaded_at
    with _CFG_LOCK:
        _config_loaded_at = 0.0
    # every OTHER Advanced session ends: they signed in with the old password
    keep = request.cookies.get("admin_session")
    for token in [t for t in _admin_sessions if t != keep]:
        _admin_sessions.pop(token, None)
    logger.warning(f"Advanced (admin) password changed by {actor}")
    return {"status": "changed", "detail": "Admin password changed. Other Advanced sessions "
                                           "were signed out."}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    _require_auth(request)
    context = {"request": request, "auth_enabled": _auth_enabled()}
    try:
        return templates.TemplateResponse(request=request, name="settings.html", context=context)
    except TypeError:
        return templates.TemplateResponse("settings.html", context)


@app.get("/api/settings")
def get_settings(request: Request):
    _require_auth(request)
    from models.config import load_config

    try:
        current = load_config(_settings_config_path())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"config.yaml does not load: {e}") from e
    admin = _is_admin(request)
    return {"auth_enabled": _auth_enabled(), "admin": admin,
            "sections": config_edit.catalog(current, include_advanced=admin),
            "effects": config_edit.EFFECT_TEXT}


@app.post("/api/settings")
async def save_settings(request: Request):
    _require_settings_rights(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    actor = _approval_actor(request)
    try:
        result = config_edit.apply_changes(
            _settings_config_path(), body.get("changes") or {}, phrase=body.get("phrase"),
            actor=actor, lock_busy=_settings_lock_busy, db=get_db(), now_iso=now_iso,
            allow_advanced=_is_admin(request))
    except config_edit.ConfigEditError as e:
        return JSONResponse({"detail": str(e), **e.extra}, status_code=e.status)
    if result.changed:
        global _config_loaded_at
        with _CFG_LOCK:
            _config_loaded_at = 0.0          # the dashboard re-reads config.yaml now
        logger.warning(f"Settings changed by {actor}: "
                       f"{', '.join(c['key'] for c in result.changed)} (backup {result.backup})")
    return {"status": "saved" if result.changed else "unchanged", "changed": result.changed,
            "effects": result.effects, "backup": result.backup}


@app.post("/api/settings/validate")
async def validate_settings(request: Request):
    """Live check while editing: the exact verdict a save would give, nothing written."""
    _require_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return config_edit.validate_changes(_settings_config_path(), body.get("changes") or {},
                                        allow_advanced=_is_admin(request))


def _open_rollover_journal() -> dict | None:
    """The unfinished rollover journal (plan A8), if any."""
    return get_db().rollover_incomplete()


def _unfinished_rollover_message(journal: dict) -> str:
    todo = ", ".join(rollover_journal.LABELS[s].lower()
                     for s in rollover_journal.attempt_order(journal))
    return (f"The rollover {journal['from_fy']} to {journal['to_fy']} is not finished yet. "
            f"Still to do: {todo}. It continues automatically every morning; "
            "'Run' continues it now.")


@app.get("/api/rollover")
async def rollover_status(request: Request):
    """FY rollover panel: the backups' FY, today's FY, the last attempt, and
    whether "Run FY rollover now" is allowed (fy_rollover.manual_run_state)."""
    _require_auth(request)
    cfg = _cfg()
    configured = fy_rollover.configured_fy(cfg.paths.source_drive, cfg.paths.lan_destination)
    current = get_fy_prefix()
    state = fy_rollover.manual_run_state(configured, current)
    journal = _open_rollover_journal()
    if journal is not None:           # an unfinished rollover can always be continued
        state = {**state, "can_run": True, "message": _unfinished_rollover_message(journal)}
    last = get_db().last_run("rollover")
    approval = next((a for a in get_db().open_approvals() if a["leg"] == "rollover"), None)
    return {
        "configured_fy": configured, "current_fy": current, **state,
        "journal": None if journal is None else {
            "from_fy": journal["from_fy"], "to_fy": journal["to_fy"],
            "steps": rollover_journal.describe_steps(journal)},
        "last_attempt": None if last is None else {
            "status": last.get("status"),
            "label": statuses.label(last.get("status")),
            "when": format_ist(last.get("started_at")),
            "error": last.get("error_message"),
        },
        "approval_pending": approval is not None,
        "running": await _is_running("rollover"),
    }


@app.post("/api/rollover/run")
async def run_rollover_now(request: Request):
    """Start the normal rollover check now (e.g. the automatic one failed or the
    server was off on April 1). Same rules as the 06:00 run - never a bypass."""
    _require_settings_rights(request)
    cfg = _cfg()
    configured = fy_rollover.configured_fy(cfg.paths.source_drive, cfg.paths.lan_destination)
    state = fy_rollover.manual_run_state(configured, get_fy_prefix())
    if not state["can_run"] and _open_rollover_journal() is None:
        raise HTTPException(status_code=409, detail=state["message"])
    running = await _is_running("rollover")
    if running is None:
        raise HTTPException(status_code=503, detail="Prefect API unavailable - cannot check "
                                                    "for a running rollover; nothing was started.")
    if running:
        raise HTTPException(status_code=409, detail="A rollover check is already running.")
    try:
        flow_run = await arun_deployment(name="rollover-check/rollover-check",
                                         timeout=_trigger_timeout(request))
    except Exception as e:
        logger.error(f"Manual rollover run could not be started: {e}")
        raise HTTPException(status_code=500, detail=f"Could not start the rollover check: {e}") from e
    logger.warning(f"FY rollover check started manually by {_approval_actor(request)}")
    return _trigger_result("FY rollover", flow_run, noun="check")


@app.post("/api/settings/test-email")
def send_test_email(request: Request):
    """Send a test email through the real alert path (recorded like every email),
    so a changed mail setting is proven before a real alert depends on it
    (audit 2026-10-18 item 4). Not stopped by notifications.send_on_failure."""
    _require_settings_rights(request)
    from core.notify import SENT, AlertEvent, notify

    actor = _approval_actor(request)
    result = notify(_cfg(), AlertEvent(
        key=f"test:{now_iso()}", kind="INFO", mode="test",
        title="Test email from your backup system",
        message=(f"This is a test sent from the dashboard by {actor}. If you can read it, "
                 "backup emails reach you. Nothing to do.")), db=get_db())
    recipients = ", ".join(_cfg().notifications.recipients)
    if result.status == SENT:
        return {"status": "sent", "detail": f"Test email sent to {recipients}."}
    return JSONResponse({"status": "failed",
                         "detail": f"The test email was NOT delivered: {result.error}"},
                        status_code=502)


@app.get("/api/settings/history")
def settings_history(request: Request):
    _require_auth(request)
    return {"changes": get_db().config_changes(limit=200)}


# ── Verification trigger (read-only integrity audit) ─────────
# Exposes the scheduled integrity-audit deployment on demand. The audit
# never modifies source or destination; it records integrity_audits rows
# and alerts on divergence. Refuses while a backup is active (the audit
# takes the same backup lock and would queue behind it) or while another
# audit is already running.


@app.post("/trigger/verify/{mode}")
async def trigger_verify(mode: str, request: Request):
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"trigger:{client_ip}", _dash("rate_limit_triggers")):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    mode = mode.lower()
    if mode not in ("lan", "cloud", "all"):
        raise HTTPException(status_code=400, detail=f"Invalid mode '{mode}'. Must be one of: lan, cloud, all.")
    cfg = _cfg()
    if mode == "lan" and not cfg.lan.enabled:
        return JSONResponse({"status": "disabled", "detail": "LAN leg is disabled in configuration; verification not started."}, status_code=409)
    if mode == "cloud" and not cfg.cloud.enabled:
        return JSONResponse({"status": "disabled", "detail": "Cloud leg is disabled in configuration; verification not started."}, status_code=409)
    if mode == "all" and not (cfg.lan.enabled or cfg.cloud.enabled):
        return JSONResponse({"status": "disabled", "detail": "Both legs are disabled in configuration; verification not started."}, status_code=409)
    for leg in ("lan", "cloud"):
        running = await _is_running(leg)
        if running is None:
            raise HTTPException(
                status_code=503,
                detail="Prefect API unavailable - cannot verify active runs; trigger refused. Retry shortly.",
            )
        if running:
            return JSONResponse({"status": "already_running", "detail": "A backup is already in progress; verification refused to avoid lock contention."}, status_code=400)
    verifying = await _is_verify_running()
    if verifying is None:
        raise HTTPException(
            status_code=503,
            detail="Prefect API unavailable - cannot verify active runs; trigger refused. Retry shortly.",
        )
    if verifying:
        return JSONResponse({"status": "already_running", "detail": "A verification audit is already in progress."}, status_code=400)
    try:
        flow_run = await arun_deployment(
            name="integrity-audit/integrity-audit",
            parameters={"mode": mode},
            timeout=_trigger_timeout(request),
        )
    except Exception as e:
        logger.error(f"Manual verification trigger failed: {e}")
        return JSONResponse(
            {"status": "trigger_failed",
             "detail": f"Could not start the verification run: {e}"},
            status_code=500,
        )
    label = {"lan": "LAN", "cloud": "Cloud", "all": "Full"}[mode]
    return _trigger_result(label, flow_run, noun="verification")


# ── Report endpoints ────────────────────────────────────────


@app.get("/report/weekly")
def report_weekly(request: Request):
    """Generate and download a weekly HTML report."""
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"report:{client_ip}", _dash("rate_limit_reports")):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    return _serve_report(7, "Weekly")


@app.get("/report/monthly")
def report_monthly(request: Request):
    """Generate and download a monthly HTML report."""
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"report:{client_ip}", _dash("rate_limit_reports")):
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
    if not _check_rate_limit(f"report:{client_ip}", _dash("rate_limit_reports")):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    cfg = _cfg()
    db = get_db()
    from core.report import generate_report_html, send_weekly_report
    legs = _enabled_legs(cfg)
    html_body = generate_report_html(db, cfg.firm_name, 7, "Weekly", legs=legs)
    if not html_body:
        return JSONResponse(
            {"status": "no_data", "detail": "No backup runs in the last 7 days — nothing to send."},
            status_code=404,
        )
    try:
        ok = send_weekly_report(db, cfg.notifications, cfg.firm_name, body_html=html_body,
                                legs=legs)
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
    if not _check_rate_limit(f"report:{client_ip}", _dash("rate_limit_reports")):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    cfg = _cfg()
    db = get_db()
    from core.report import generate_report_html, send_monthly_report
    legs = _enabled_legs(cfg)
    html_body = generate_report_html(db, cfg.firm_name, 30, "Monthly", legs=legs)
    if not html_body:
        return JSONResponse(
            {"status": "no_data", "detail": "No backup runs in the last 30 days — nothing to send."},
            status_code=404,
        )
    try:
        ok = send_monthly_report(db, cfg.notifications, cfg.firm_name, body_html=html_body,
                                 legs=legs)
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


def _enabled_legs(cfg) -> tuple[str, ...]:
    return tuple(leg for leg in ("cloud", "lan")
                 if getattr(getattr(cfg, leg, None), "enabled", False))


def _serve_report(days: int, period: str) -> Response:
    """Generate an HTML report and serve it as a downloadable file."""
    cfg = _cfg()
    db_path = Path(cfg.paths.database_path)

    if not db_path.exists():
        return HTMLResponse("<p>No database found. Run a backup first.</p>", status_code=503)

    db = get_db()
    from core.report import generate_report_html
    html_body = generate_report_html(db, cfg.firm_name, days, period, legs=_enabled_legs(cfg))

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


def _integrity_summary(db: ManifestDB, mode: str) -> dict:
    """Return the integrity-verification status for a mode.

    Backup result and integrity are separate facts: a COMPLETE backup with
    no audit yet reports NOT_VERIFIED (never implied VERIFIED).
    """
    try:
        audit = db.latest_audit(mode)
    except Exception:
        audit = None
    if not isinstance(audit, dict):
        return {"status": "NOT_VERIFIED", "checked_at": None, "detail": None}
    return {
        "status": audit.get("status", "NOT_VERIFIED"),
        "checked_at": audit.get("ended_at") or audit.get("started_at"),
        "detail": audit.get("detail"),
    }


def _status_fields(run: dict) -> dict:
    """Label/category/description from core.statuses (single source), plus the
    run's stored fault code and its plain title (audit 2026-10-18). A row from
    before fault codes has none - never invented from the status."""
    status = run.get("status")
    try:
        metrics = json.loads(run.get("extended_metrics") or "{}")
    except (TypeError, ValueError):
        metrics = {}
    code = metrics.get("fault_code") if isinstance(metrics, dict) else None
    return {
        "status_label": statuses.label(status),
        "status_category": statuses.category(status),
        "status_description": statuses.describe(
            status, run.get("files_copied", 0), run.get("files_failed", 0)
        ),
        "fault_code": code if code in faults.CATALOG else None,
        "fault_title": faults.CATALOG[code].title if code in faults.CATALOG else None,
    }


def _last_run_summary(db: ManifestDB, mode: str) -> dict | None:
    run = db.last_run(mode)
    if not run:
        return None
    err_msg = run.get("error_message") or ""
    if len(err_msg) > 2000:
        err_msg = err_msg[:2000] + "..."
    return {
        "status": run.get("status", "unknown"),
        **_status_fields(run),
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



