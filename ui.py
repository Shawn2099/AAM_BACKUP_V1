"""AAM Backup Automation — Dashboard UI with manual triggers.

FastAPI server bound to configurable host:port. Safety-first:
  - Lock file per pipeline prevents concurrent runs
  - Confirmation required for trigger
  - Buttons disabled while pipeline is running
  - Sign-in always on: dashboard password -> session cookie; Advanced has its own password
"""

import asyncio
import html
import json
import re
import secrets
import shutil
import threading
import time
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pendulum
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
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

from core import (admin_auth, alert_class, app_restart, messages, night_strip, approvals, config_edit, faults, fy_overlap, fy_rollover,
                  rollover_journal, statuses)
from core import manual_leg
from core.attention import RED, compute_attention, leg_timing
from core.manifest import ALL_FYS, ManifestDB
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


@app.middleware("http")
async def _refuse_cross_site_writes(request: Request, call_next):
    """A state-changing request that a browser marks as coming from another site is refused (CSRF defence
    on top of the SameSite cookie). No Origin and no Sec-Fetch-Site (curl, scripts) stays allowed."""
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin")
        cross = (request.headers.get("sec-fetch-site") or "").lower() == "cross-site"
        if origin is not None and not cross:
            host = (request.headers.get("host") or "").lower()
            cross = urlsplit(origin).netloc.lower() != host
        if cross:
            return JSONResponse({"detail": messages.act("Open the dashboard directly and try again",
                                                         "The request came from another web site")},
                                status_code=403)
    return await call_next(request)


def _object_body(body) -> dict:
    """A JSON body must be an object; anything else is a plain 400, never a server error."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail=messages.act("Send the form again", "The request was not in the expected form"))
    return body

# ── In-memory session store ──────────────────────────

_sessions: dict[str, dict] = {}


def _session_ttl() -> timedelta:
    return timedelta(hours=_dash("session_hours"))


def _login_fingerprint() -> str:
    return admin_auth.fingerprint(_cfg().dashboard.login_password_hash)


def _admin_fingerprint() -> str:
    return admin_auth.fingerprint(_cfg().dashboard.admin_password_hash)


def _create_session() -> str:
    token = secrets.token_hex(32)
    _sessions[token] = {"created_at": time.time(), "fp": _login_fingerprint()}
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
    """A session is valid while it is young AND the dashboard password it was opened with is still
    the one in config.yaml (plan E2): a changed or reset password ends every older session,
    including a reset done by the script in another process (the config refresh picks it up)."""
    if not token:
        return False
    session = _sessions.get(token)
    if not session:
        return False
    if time.time() - session["created_at"] > _session_ttl().total_seconds():
        del _sessions[token]
        return False
    if not admin_auth.credentials_set(_cfg().dashboard) or session.get("fp") != _login_fingerprint():
        _sessions.pop(token, None)
        return False
    return True


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


# One process-wide lock for every manual start (the dashboard is a single process): held from the busy
# check until the deployment run has been CREATED (arun_deployment(timeout=0) returned), so a double click or
# two tabs cannot both pass the busy check. A freshly created run is SCHEDULED and due now, which
# _prefect_has_active_run counts as active, so the second request is refused with the normal 400
# already_running. It is never held while waiting for the run to finish (_wait_for_run runs outside it).
# Different legs share it: the busy check covers every leg. One lock per event loop (tests use many loops).
_TRIGGER_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _trigger_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _TRIGGER_LOCKS.get(loop)
    if lock is None:
        lock = _TRIGGER_LOCKS[loop] = asyncio.Lock()
    return lock


async def _create_run(**kwargs):
    """Create the deployment run and return at once (timeout=0); call under _trigger_lock()."""
    return await arun_deployment(timeout=0, **kwargs)


async def _wait_for_run(flow_run, timeout: float):
    """The F2 bounded wait, outside the lock. Never cancels the run; any failure returns the snapshot
    (the answer is then the truthful 'still running' 202)."""
    state = getattr(flow_run, "state", None)
    if not timeout or (state is not None and state.is_final()):
        return flow_run
    try:
        from prefect.flow_runs import wait_for_flow_run
        return await asyncio.wait_for(wait_for_flow_run(flow_run.id, timeout=int(timeout)), timeout + 5)
    except Exception as e:  # noqa: BLE001 - timeout or API error: report the run as still running
        logger.info(f"Stopped waiting for flow run {getattr(flow_run, 'id', '?')}: {e!r}")
        return flow_run


def _flow_run_statuses(run_id: str) -> list[str]:
    """Statuses of the run rows this flow run wrote (empty when unknown: the plain 'ended as' text is used)."""
    try:
        return get_db().statuses_of_flow_run(run_id)
    except Exception as e:  # noqa: BLE001 - the answer must still be given
        logger.warning(f"Could not read the outcome of flow run {run_id}: {e}")
        return []


def _trigger_result(label: str, flow_run, noun: str = "backup") -> JSONResponse:
    run_id = str(flow_run.id)
    state = getattr(flow_run, "state", None)
    state_type = getattr(state, "type", None)
    if state is not None and state.is_final():
        if state_type == StateType.COMPLETED:
            return JSONResponse({
                "status": "completed",
                "flow_run_id": run_id,
                "detail": messages.act(f"Open Recent backups to see the result", f"The {label} {noun} completed", f"run {run_id}"),
            })
        if state_type in (StateType.CANCELLED, StateType.CANCELLING):
            return JSONResponse({
                "status": "cancelled",
                "flow_run_id": run_id,
                "detail": messages.act("Start it again when you are ready", f"The {label} {noun} was cancelled", f"run {run_id}"),
            }, status_code=409)
        # F5: a guard BLOCK and a PARTIAL run are failed Prefect runs; say what happened and where to go
        categories = {statuses.category(s) for s in _flow_run_statuses(run_id)}
        if "blocked" in categories:
            return JSONResponse({
                "status": "failed", "outcome": "blocked", "flow_run_id": run_id,
                "detail": messages.act("Open the pending decision on this page and approve or dismiss it",
                                       f"The {label} {noun} was stopped for your safety", f"run {run_id}"),
            }, status_code=500)
        if "partial" in categories:
            return JSONResponse({
                "status": "failed", "outcome": "partial", "flow_run_id": run_id,
                "detail": messages.act("Open Recent backups on this page to see what was left out",
                                       f"The {label} {noun} was only partly done", f"run {run_id}"),
            }, status_code=500)
        return JSONResponse({
            "status": "failed",
            "flow_run_id": run_id,
            "detail": messages.act("Open Recent backups to see why", f"The {label} {noun} ended as {state.name if state else 'failed'}", f"run {run_id}"),
        }, status_code=500)
    return JSONResponse({
        "status": "running",
        "flow_run_id": run_id,
        "detail": messages.act("Leave this page open to follow it", f"The {label} {noun} started", f"run {run_id}"),
    }, status_code=202)


# ── API endpoints ────────────────────────────────────────────


_LOGIN_STYLE = """
body { display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 1rem; }
.login { background: var(--panel); border: 1px solid var(--hair); border-radius: 18px; padding: 2rem; width: min(24rem, 100%);
         box-shadow: 0 0 0 6px var(--shell), 0 0 0 7px var(--hair), 0 30px 50px -34px var(--lift); }
.login h1 { font-size: 1.5rem; font-weight: 600; line-height: 1.2; margin-bottom: 1.25rem; }
.login .mark { width: 34px; height: 34px; margin-bottom: .9rem; }
label { display: block; font-size: 0.9rem; color: var(--ink-2); margin-bottom: 0.3rem; }
input { width: 100%; margin-bottom: 1rem; }
button { width: 100%; margin-right: 0; font: inherit; font-weight: 600; border: 0; border-radius: 999px; padding: .65rem 1rem; background: var(--navy); color: var(--on-navy); }
button:hover { background: var(--navy-hover); }
p { line-height: 1.5; }
"""

NOT_SET_UP = ("The dashboard passwords are not set up yet. On the server, run the installer "
              "(deploy\\06_install_services.ps1) or deploy\\11_reset_dashboard_passwords.ps1 as "
              "administrator to set them.")


def _login_html(body: str) -> str:
    return (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<meta name=\"color-scheme\" content=\"light dark\">"
            f"<title>AAM Backup - Sign in</title>"
            f"<link rel=\"stylesheet\" href=\"/static/css/dashboard.css\">"
            f"<link rel=\"icon\" href=\"data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 24 24%27 fill=%27none%27 stroke=%27%2314365a%27 stroke-width=%272%27 stroke-linecap=%27round%27 stroke-linejoin=%27round%27%3E%3Cpath d=%27M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6l8-3z%27/%3E%3Cpath d=%27M8.5 12l2.5 2.5L16 9.5%27/%3E%3C/svg%3E\">"
            f"<style>{_LOGIN_STYLE}</style></head><body>"
            f"<div class=\"login\"><svg class=\"mark\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"var(--navy)\" "
            f"stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\">"
            f"<path d=\"M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6l8-3z\"/><path d=\"M8.5 12l2.5 2.5L16 9.5\"/></svg>"
            f"<h1>Sign in to AAM Backup</h1>{body}</div></body></html>")


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    if not admin_auth.credentials_set(_cfg().dashboard):
        return HTMLResponse(_login_html(f"<p class=\"banner amber\">{html.escape(NOT_SET_UP)}</p>"),
                            status_code=503)
    error_html = f'<p class="banner red">{html.escape(error)}</p>' if error else ""
    return _login_html(f"""{error_html}
<form action="/login" method="post">
<label for="password">Password</label>
<input type="password" name="password" id="password" required autofocus>
<button type="submit">Sign In</button>
</form>""")


@app.post("/login")
async def login_submit(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"login:{client_ip}", _dash("rate_limit_logins")):
        raise HTTPException(status_code=429, detail=messages.TOO_MANY_SIGN_INS)
    dashboard = _cfg().dashboard
    if not admin_auth.credentials_set(dashboard):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    if await run_in_threadpool(admin_auth.verify_login_password, str(form.get("password", "")), dashboard):
        token = _create_session()
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            key="session", value=token,
            httponly=True, samesite="lax",
            secure=request.url.scheme == "https",
            max_age=int(_session_ttl().total_seconds()),
        )
        return resp
    logger.warning(f"Dashboard sign-in refused for {client_ip}")
    return RedirectResponse("/login?error=Wrong+password", status_code=303)


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        _sessions.pop(token, None)
    # signing out also locks Advanced: on a shared PC the next person must not inherit it
    _admin_sessions.pop(request.cookies.get("admin_session") or "", None)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    resp.delete_cookie("admin_session")
    return resp


def _require_auth(request: Request):
    """Every dashboard endpoint but /health and /login needs a signed-in session (plan E10).
    Until both passwords are set (plan E4) nothing opens: browsers get the 'not set up' page at
    /login, API callers a 503 (fail closed)."""
    if not admin_auth.credentials_set(_cfg().dashboard):
        if "text/html" in request.headers.get("Accept", ""):
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        raise HTTPException(status_code=503, detail=NOT_SET_UP)
    if _validate_session(request.cookies.get("session")):
        return
    # Browser requests (Accept: text/html) get redirected to login page.
    # API requests get JSON 401.
    if "text/html" in request.headers.get("Accept", ""):
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
        "cloud_schedule": cloud_schedule,
        "lan_schedule": lan_schedule,
        "cloud_enabled": cfg.cloud.enabled,
        "lan_enabled": cfg.lan.enabled,
        # "Next scheduled backup" only for a leg that runs on a schedule (off / manual only: no such line)
        "cloud_nightly": manual_leg.is_nightly(cfg, "cloud"),
        "lan_nightly": manual_leg.is_nightly(cfg, "lan"),
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
        runs = db.get_recent_runs(25, fy=ALL_FYS)
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
    cloud_state = await _is_running("cloud")
    lan_state = await _is_running("lan")
    cloud_fields = _run_state_fields(cloud_state)
    lan_fields = _run_state_fields(lan_state)
    # the manual / weekly safety check (read-only audit); None (Prefect unreachable) shows as "not running"
    verify_running = (await _is_verify_running()) is True

    return JSONResponse({
        "firm": cfg.firm_name,
        "fy_prefix": fy_rollover.effective_fy_prefix(cfg.paths.source_drive),
        "attention": _attention(db, cfg),
        "verify_running": verify_running,
        "schedule": {
            "cloud_cron": cron_to_human(cfg.schedule.cloud_cron, cfg.schedule.timezone),
            "lan_cron": cron_to_human(cfg.schedule.lan_cron, cfg.schedule.timezone),
        },
        "cloud": {
            **cloud_fields,
            "enabled": cfg.cloud.enabled,
            "last_run": cloud_last_run,
            "last_success": _get_last_success(db, "cloud"),
            "timing": _leg_timing(db, cfg, "cloud"),
            "mode": _mode_info(db, cfg, "cloud"),
            "last_run_formatted": format_ist(cloud_last_run["started_at"]) if cloud_last_run else "No data",
            # Integrity status is a SEPARATE fact from the backup result:
            # VERIFIED only after a passing audit; NOT_VERIFIED when no
            # audit has run; VERIFICATION_FAILED on divergence. A COMPLETE
            # backup does NOT imply VERIFIED.
            "integrity": _integrity_summary(db, "cloud"),
            "nights": _nights(db, "cloud", cfg),
        },
        "lan": {
            **lan_fields,
            "enabled": cfg.lan.enabled,
            "last_run": lan_last_run,
            "last_success": _get_last_success(db, "lan"),
            "timing": _leg_timing(db, cfg, "lan"),
            "mode": _mode_info(db, cfg, "lan"),
            "last_run_formatted": format_ist(lan_last_run["started_at"]) if lan_last_run else "No data",
            "integrity": _integrity_summary(db, "lan"),
            "nights": _nights(db, "lan", cfg),
        },
        "manifest": {
            "lan_files": db.file_count("lan_status", fy=_configured_fy()),
            "cloud_files": db.file_count("cloud_status", fy=_configured_fy()),
        },
        "health": await _get_health(),
        "recent_runs": recent_runs,
        "folders": _folders(db, cfg, {"cloud": cloud_state, "lan": lan_state}),
    })


def _active_fy_list() -> list[str]:
    """The FY folders backed up now, new FY first (plan 5.3 / 5.7): the same answer as flow._active_fys. Any
    trouble working it out falls back to the configured FY alone - the safe answer."""
    configured = _configured_fy()
    try:
        cfg = _cfg()
        if getattr(cfg.rollover, "overlap_enabled", True) is False:
            return [configured]
        folders = list(fy_overlap.active_folders(cfg, get_db(), fy_overlap.run_date()))
        return folders or [configured]
    except Exception as e:  # noqa: BLE001 - the dashboard still renders / triggers for the configured FY
        logger.warning(f"Active FY folders could not be worked out for the dashboard ({e}); using {configured}")
        return [configured]


def _folders(db, cfg, prefect_state: dict) -> list[dict]:
    """One entry per active FY folder, new FY first (plan 5.7): its role, since when it is backed up and, per
    leg, the last run / success, timing, integrity and open approval. `running` of a (leg, FY) comes from its
    RUNNING run row, and only while the scheduler agrees that something runs (a row left behind by a crash is
    cleaned up by the sweep, never shown as running forever)."""
    configured = _configured_fy()
    try:
        pending = {(a.get("fy") or configured, a["leg"]): a["status"] for a in db.open_approvals()}
    except Exception:  # noqa: BLE001 - the cards still render
        pending = {}
    out = []
    for fy in _active_fy_list():
        role = fy_overlap.CURRENT if fy == configured else fy_overlap.EARLY
        try:
            activation = db.get_activation(fy)
        except Exception:  # noqa: BLE001
            activation = None
        legs = {}
        for leg in ("cloud", "lan"):
            payload = _leg_payload(db, cfg, leg, fy, role)
            last = payload["last_run"]
            row_running = bool(last) and last.get("status_category") == statuses.CATEGORY_RUNNING
            scheduler = prefect_state.get(leg)
            running = row_running and scheduler is not False
            legs[leg] = {
                "enabled": getattr(cfg, leg).enabled,
                **payload,
                "running": running,
                "run_state": "running" if running else ("unknown" if scheduler is None else "idle"),
                "approval": pending.get((fy, leg)),
            }
        out.append({
            "fy": fy,
            "role": role,
            "active_since": activation.get("activated_at") if activation else None,
            "active_since_formatted": format_ist(activation.get("activated_at")) if activation else None,
            "cloud": legs["cloud"],
            "lan": legs["lan"],
        })
    return out


def _leg_payload(db, cfg, leg: str, fy: str, role: str) -> dict:
    """The FY-dependent facts of one leg's card (folders[] entries)."""
    last_run = _last_run_summary(db, leg, fy)
    return {
        "last_run": last_run,
        "last_success": _get_last_success(db, leg, fy, role),
        "timing": _leg_timing(db, cfg, leg, fy, role),
        "last_run_formatted": format_ist(last_run["started_at"]) if last_run else "No data",
        "integrity": _integrity_summary(db, leg, fy),
        "nights": _nights(db, leg, cfg, fy),
    }


def _nights(db, mode: str, cfg, fy: str | None = None) -> list[dict]:
    """One mark per night for the dashboard strip (core/night_strip.py)."""
    try:
        days = cfg.dashboard.history_nights
        end = pendulum.now(IST).add(days=1).start_of("day")
        return night_strip.build(db.get_runs_between(end.subtract(days=days), end, mode, fy=fy or _configured_fy()),
                                 days=days)
    except Exception as e:  # noqa: BLE001 - the strip is decoration; never break the status call
        logger.warning(f"Night strip could not be built for {mode}: {e}")
        return []


def _mode_info(db, cfg, leg: str) -> dict:
    """off | nightly | manual for a leg's card, with the truth line of an off / manual-only leg
    (core.manual_leg): reports and dashboard use the same wording."""
    try:
        line = manual_leg.mode_line(cfg, db, leg)
    except Exception as e:  # noqa: BLE001 - the card is decoration; the banner has the truth
        logger.warning(f"Mode line could not be evaluated for {leg}: {e}")
        line = None
    return line or {"mode": manual_leg.NIGHTLY, "text": "", "soft_warn": False}


def _leg_timing(db, cfg, leg: str, fy: str | None = None, role: str | None = None) -> dict:
    """on_time | late | overdue | waiting | catching_up for the leg's card (core.attention.leg_timing)."""
    try:
        return leg_timing(db, cfg, leg, fy=fy, role=role)
    except Exception as e:  # noqa: BLE001 - the card is decoration; the banner has the truth
        logger.warning(f"Leg timing could not be evaluated for {leg}: {e}")
        return {"state": "on_time", "due_at": None, "overdue_at": None}


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
        return JSONResponse({"status": "unknown", "boot_id": app_restart.BOOT_ID,
                             "reasons": [f"health could not be evaluated ({type(e).__name__})"]},
                            status_code=503)
    red = [i["message"] for i in items if i["level"] == RED]
    warnings = [i["message"] for i in items if i["level"] != RED]
    if red:
        return JSONResponse({"status": "attention", "boot_id": app_restart.BOOT_ID,
                             "reasons": red, "warnings": warnings}, status_code=503)
    return JSONResponse({"status": "healthy", "boot_id": app_restart.BOOT_ID,
                         "warnings": warnings})


def _requested_fy(request: Request) -> str | None:
    """`?fy=` of a manual trigger: None when absent (the flow decides: every active FY), else a FY that is
    in the active set - anything else is refused (plan 5.7), the flow would refuse it too."""
    raw = (request.query_params.get("fy") or "").strip()
    if not raw:
        return None
    active = _active_fy_list()
    for fy in active:
        if fy.lower() == raw.lower():
            return fy
    raise HTTPException(status_code=409, detail=messages.act(
        "Pick one of the year folders shown on the dashboard",
        f"{raw} is not backed up at the moment (active: {', '.join(active)})"))


async def _busy_detail(target: str) -> str | None:
    """Why a manual run of `target` must wait, or None when nothing runs. Any backup, the FY rollover
    (tag `rollover`, it moves the config and the backup slot) or a safety check counts: the next run would
    only queue behind the one backup slot and end as a false SLOT_TIMEOUT alarm (plan 5.7). 503 when
    Prefect does not answer (fail closed). `target` is the leg asked for, to word the same-leg case."""
    for leg in ("cloud", "lan", "rollover"):
        running = await _is_running(leg)
        if running is None:
            raise HTTPException(status_code=503, detail=messages.SCHEDULER_NOT_ANSWERING)
        if running:
            if leg == target:
                return messages.leg_running(leg)
            if leg == "rollover":
                return messages.act("Wait for the running FY rollover check to finish, then try again",
                                    "The rollover is running and changes the backup folders")
            return messages.act("Wait for the running backup to finish, then try again",
                                "Only one backup or safety check can run at a time")
    verifying = await _is_verify_running()
    if verifying is None:
        raise HTTPException(status_code=503, detail=messages.SCHEDULER_NOT_ANSWERING)
    if verifying:
        return messages.act("Wait for the running safety check to finish, then try again",
                            "A backup and a safety check cannot run at the same time")
    return None


async def _refuse_if_busy(target: str) -> JSONResponse | None:
    detail = await _busy_detail(target)
    if detail is None:
        return None
    return JSONResponse({"status": "already_running", "detail": detail}, status_code=400)


async def _agent_serving(deployment: str) -> bool | None:
    """F1: is a backup service asking Prefect for this deployment's runs right now? A served deployment's
    `last_polled` is refreshed by every poll of the service; when it is older than
    `dashboard.agent_max_silence_seconds` (or never set, or the deployment is gone) nobody would pick up a
    manual run, which would sit SCHEDULED and start whenever the service returns. Tri-state: True / False /
    None (Prefect does not answer: callers fail closed with the scheduler-not-answering 503)."""
    from prefect.exceptions import ObjectNotFound
    try:
        async with get_client() as client:
            dep = await client.read_deployment_by_name(deployment)
    except ObjectNotFound:
        return False
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to read deployment {deployment}: {e}")
        return None
    polled = getattr(dep, "last_polled", None)
    if polled is None:
        return False
    if polled.tzinfo is None:
        polled = polled.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - polled).total_seconds() <= _dash("agent_max_silence_seconds")


_agent_serving_real = _agent_serving


async def _refuse_if_service_down(deployment: str) -> JSONResponse | None:
    serving = await _agent_serving(deployment)
    if serving is None:
        raise HTTPException(status_code=503, detail=messages.SCHEDULER_NOT_ANSWERING)
    if serving:
        return None
    return JSONResponse({"status": "service_not_running", "detail": messages.SERVICE_NOT_RUNNING},
                        status_code=503)


@app.post("/trigger/cloud")
async def trigger_cloud(request: Request):
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"trigger:{client_ip}", _dash("rate_limit_triggers")):
        raise HTTPException(status_code=429, detail=messages.TOO_MANY_REQUESTS)
    if not _cfg().cloud.enabled:
        return JSONResponse({"status": "disabled", "detail": messages.leg_off("cloud")}, status_code=409)
    fy = _requested_fy(request)
    # H3: fail-CLOSED. The old code treated an unreachable API as "nothing running" and queued a duplicate
    # backup. WP7: any running backup or safety check refuses (it would only queue behind the one slot).
    async with _trigger_lock():
        busy = await _refuse_if_busy("cloud")
        if busy is not None:
            return busy
        down = await _refuse_if_service_down("aam-backup/backup-cloud")
        if down is not None:
            return down
        try:
            flow_run = await _create_run(
                name="aam-backup/backup-cloud",
                **({"parameters": {"fy": fy}} if fy else {}),
            )
        except Exception as e:
            logger.error(f"Manual cloud trigger failed: {e}")
            return JSONResponse(
                {"status": "trigger_failed",
                 "detail": messages.could_not_start("cloud backup", e)},
                status_code=500,
            )
    flow_run = await _wait_for_run(flow_run, _trigger_timeout(request))
    return _trigger_result("Cloud", flow_run)


@app.post("/trigger/lan")
async def trigger_lan(request: Request):
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"trigger:{client_ip}", _dash("rate_limit_triggers")):
        raise HTTPException(status_code=429, detail=messages.TOO_MANY_REQUESTS)
    if not _cfg().lan.enabled:
        return JSONResponse({"status": "disabled", "detail": messages.leg_off("lan")}, status_code=409)
    fy = _requested_fy(request)
    async with _trigger_lock():
        busy = await _refuse_if_busy("lan")
        if busy is not None:
            return busy
        down = await _refuse_if_service_down("aam-backup/backup-lan")
        if down is not None:
            return down
        try:
            flow_run = await _create_run(
                name="aam-backup/backup-lan",
                **({"parameters": {"fy": fy}} if fy else {}),
            )
        except Exception as e:
            logger.error(f"Manual LAN trigger failed: {e}")
            return JSONResponse(
                {"status": "trigger_failed",
                 "detail": messages.could_not_start("LAN backup", e)},
                status_code=500,
            )
    flow_run = await _wait_for_run(flow_run, _trigger_timeout(request))
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
    return f"{ip} (session)"


def _require_approval_rights(request: Request) -> None:
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"decision:{client_ip}", _dash("rate_limit_decisions")):
        raise HTTPException(status_code=429, detail=messages.TOO_MANY_REQUESTS)


def _approval_view(req: dict) -> dict:
    expired = False
    if req.get("expires_at"):
        try:
            expired = pendulum.now("UTC") >= pendulum.parse(req["expires_at"])
        except Exception:  # noqa: BLE001 - unparsable expiry is never valid
            expired = True
    keys = ("approval_id", "leg", "status", "created_at", "blocked_run_id", "fy",
            "guards", "phrase", "approved_at", "approved_by", "expires_at")
    # one word confirms every approval, including requests stored with an older, longer phrase
    approvable = approvals.request_approvable(req.get("guards"))
    return {**{k: req.get(k) for k in keys}, "phrase": approvals.APPROVE_PHRASE, "expired": expired,
            "approvable": approvable,
            "not_approvable_note": None if approvable else "Do NOT approve. " + approvals.NOT_APPROVABLE_MESSAGE}


def _load_open_approval(approval_id: str) -> dict:
    req = get_db().get_approval(approval_id)
    if req is None:
        raise HTTPException(status_code=404, detail=messages.act("Refresh the page", "This approval request no longer exists"))
    return req


async def _ensure_leg_idle(leg: str) -> None:
    if leg not in _APPROVAL_TARGETS:
        raise HTTPException(status_code=409, detail=messages.act("Refresh the page", f"The approval target {leg!r} is not known"))
    if _APPROVAL_TARGETS[leg][2] and not getattr(_cfg(), leg).enabled:
        raise HTTPException(status_code=409,
                            detail=messages.leg_off(leg, "changed"))
    detail = await _busy_detail(leg)                 # any backup / rollover / safety check, not just this leg
    if detail is not None:
        raise HTTPException(status_code=409, detail=detail)


async def _start_approved_run(req: dict, request: Request) -> JSONResponse:
    leg = req["leg"]
    label, deployment, _ = _APPROVAL_TARGETS[leg]
    # F1: no service to pick the run up -> start nothing; the approval stays APPROVED and /run retries it
    async with _trigger_lock():
        # re-check inside the lock: the callers' idle check ran before it (two /run clicks at once)
        await _ensure_leg_idle(leg)
        down = await _refuse_if_service_down(deployment)
        if down is not None:
            return down
        try:
            flow_run = await _create_run(
                name=deployment,
                parameters={"approval_id": req["approval_id"],
                            # only the backup flows take `fy`; rollover_check_flow does not (the rollover is
                            # the configured FY's business anyway)
                            **({"fy": req["fy"]} if req.get("fy") and leg in ("cloud", "lan") else {})},
            )
        except Exception as e:
            logger.error(f"Approved {leg} re-run could not be started: {e}")
            return JSONResponse({
                "status": "trigger_failed",
                "detail": messages.act("Press 'Run approved backup' to try again before the approval expires",
                                       f"The {label} backup could not be started: {e}"),
            }, status_code=500)
    flow_run = await _wait_for_run(flow_run, _trigger_timeout(request))
    return _trigger_result(label, flow_run, noun="backup (approved re-run)")


@app.get("/approvals")
def list_approvals(request: Request):
    _require_auth(request)
    rows = get_db().open_approvals()
    return {"approvals": [_approval_view(r) for r in rows]}


@app.post("/approvals/{approval_id}/approve")
async def approve_blocked_run(approval_id: str, request: Request):
    _require_approval_rights(request)
    try:
        typed = (await request.json()).get("phrase", "")
    except Exception:  # noqa: BLE001 - malformed body = no phrase
        typed = ""
    req = _load_open_approval(approval_id)
    if req["status"] != approvals.PENDING:
        raise HTTPException(status_code=409, detail=messages.act("Refresh the page", f"This request is already {req['status']}, so it cannot be approved again"))
    if not approvals.phrase_matches(approvals.APPROVE_PHRASE, typed):
        raise HTTPException(status_code=400,
                            detail=messages.act(f"Type {approvals.APPROVE_PHRASE} exactly, then press Approve", "The word typed did not match"))
    if not approvals.request_approvable(req.get("guards")):
        raise HTTPException(status_code=409, detail=messages.act(
            "Fix the cause (mount or restore the NAS share, check the path); the next backup clears this by itself",
            "This block is about the backup target itself, so it cannot be approved"))
    if req.get("fy") and req["fy"] not in _active_fy_list():
        raise HTTPException(status_code=409, detail=messages.act(
            "Dismiss this request; approve the one for the current financial year instead",
            f"{req['fy']} is no longer backed up, so a run for it cannot be approved"))
    await _ensure_leg_idle(req["leg"])
    approved_at = now_iso()
    actor = _approval_actor(request)
    if not get_db().approve_request(
            approval_id, approved_by=actor, approved_at=approved_at,
            expires_at=approvals.expires_at(approved_at, _cfg().approvals.expiry_hours)):
        raise HTTPException(status_code=409, detail=messages.act("Refresh the page and try again", "Someone changed this request a moment ago"))
    logger.warning(f"Approval {approval_id} ({req['leg']}: "
                   f"{', '.join(g['guard'] for g in req['guards'])}) given by {actor}")
    return await _start_approved_run(get_db().get_approval(approval_id), request)


@app.post("/approvals/{approval_id}/run")
async def run_approved_backup(approval_id: str, request: Request):
    _require_approval_rights(request)
    req = _load_open_approval(approval_id)
    view = _approval_view(req)
    if req["status"] != approvals.APPROVED:
        raise HTTPException(status_code=409, detail=messages.act("Refresh the page", f"This request is {req['status']}; only an approved one can be run"))
    if view["expired"]:
        raise HTTPException(status_code=409, detail=messages.act("Wait for the next blocked backup and approve it again",
                                                    f"This approval expired at {req['expires_at']}"))
    await _ensure_leg_idle(req["leg"])
    return await _start_approved_run(req, request)


@app.post("/approvals/{approval_id}/dismiss")
def dismiss_blocked_run(approval_id: str, request: Request):
    _require_approval_rights(request)
    _load_open_approval(approval_id)
    actor = _approval_actor(request)
    if not get_db().dismiss_request(approval_id, dismissed_by=actor, dismissed_at=now_iso()):
        raise HTTPException(status_code=409, detail=messages.act("Refresh the page", "This request was already handled"))
    logger.warning(f"Approval request {approval_id} dismissed by {actor}")
    return {"status": "dismissed", "detail": messages.act("Nothing more to do",
                                    "Dismissed. Nothing will run on this request; the next scheduled backup checks again")}


@app.post("/attention/db-recreated/ack")
def acknowledge_db_recreated(request: Request):
    """opus-decision L2: an admin has seen that the backup database was created new; clears the red item."""
    _require_approval_rights(request)
    from core import db_identity
    if not db_identity.acknowledge(get_db(), _approval_actor(request)):
        raise HTTPException(status_code=409, detail=messages.act("Refresh the page", "There is nothing to acknowledge"))
    return {"status": "acknowledged", "detail": "Acknowledged. The red notice is cleared."}


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
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"settings:{client_ip}", _dash("rate_limit_settings")):
        raise HTTPException(status_code=429, detail=messages.TOO_MANY_REQUESTS)


# ── Advanced-settings login (owner 2026-10-15) ───────────────
# A second, separate login on top of the dashboard sign-in. Its password is
# its own stored PBKDF2 hash (plan E1-E3); the sign-in password never opens it.
# Enforced on the server: without it, advanced settings are neither sent to
# the browser nor accepted in a save / validate.

_admin_sessions: dict[str, dict] = {}


def _is_admin(request: Request) -> bool:
    token = request.cookies.get("admin_session")
    session = _admin_sessions.get(token) if token else None
    if session is None:
        return False
    if time.time() - session["created_at"] > _cfg().dashboard.admin_session_minutes * 60:
        _admin_sessions.pop(token, None)
        return False
    if session["fp"] != _admin_fingerprint():          # the Advanced password changed since
        _admin_sessions.pop(token, None)
        return False
    return True


@app.get("/api/admin/status")
def admin_status(request: Request):
    _require_auth(request)
    return {"admin": _is_admin(request),
            "session_minutes": _cfg().dashboard.admin_session_minutes}


@app.post("/api/admin/login")
async def admin_login(request: Request):
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"admin_login:{client_ip}", _dash("rate_limit_logins")):
        raise HTTPException(status_code=429, detail=messages.TOO_MANY_SIGN_INS)
    try:
        password = str((await request.json()).get("password", ""))
    except Exception:  # noqa: BLE001
        password = ""
    if not await run_in_threadpool(admin_auth.verify_admin_password, password, _cfg().dashboard):
        logger.warning(f"Advanced login refused for {client_ip}")
        raise HTTPException(status_code=401, detail=messages.act("Type the Advanced password again", "The one entered is wrong"))
    token = secrets.token_hex(32)
    _admin_sessions[token] = {"created_at": time.time(), "fp": _admin_fingerprint()}
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


def _reload_config_now() -> None:
    global _config_loaded_at
    with _CFG_LOCK:
        _config_loaded_at = 0.0


async def _change_password(request: Request, which: str) -> JSONResponse | dict:
    """Change the dashboard sign-in password (`which="login"`) or the Advanced one
    (`which="admin"`). Needs an Advanced session AND the current Advanced password typed again
    (plan E3); the two passwords must differ; every OTHER session signed in with the old password
    ends (plan E2) while the person who changed it stays signed in."""
    _require_settings_rights(request)
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail=messages.act("Open the Advanced tab and sign in first", "Passwords can only be changed there"))
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    body = _object_body(body)
    dashboard = _cfg().dashboard
    if not await run_in_threadpool(admin_auth.verify_admin_password, str(body.get("current", "")), dashboard):
        raise HTTPException(status_code=400, detail=messages.act("Type the current Advanced password again", "The one entered is wrong"))
    new = str(body.get("new", ""))
    problem = admin_auth.new_password_problem(new, str(body.get("confirm", "")),
                                              dashboard.admin_password_min_length)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    other = dashboard.login_password_hash if which == "admin" else dashboard.admin_password_hash
    if other and await run_in_threadpool(admin_auth._check_hash, new, other):
        raise HTTPException(status_code=400, detail=messages.act("Choose a different password", "The dashboard password and the Advanced password must not be the same"))
    key, name = (("dashboard.admin_password_hash", "Advanced") if which == "admin"
                 else ("dashboard.login_password_hash", "Dashboard"))
    actor = _approval_actor(request)
    new_hash = await run_in_threadpool(admin_auth.hash_password, new)
    try:
        config_edit.set_password_hash(_settings_config_path(), key, new_hash,
                                      actor=actor, db=get_db(), now_iso=now_iso)
    except config_edit.ConfigEditError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status)
    _reload_config_now()
    fresh = admin_auth.fingerprint(new_hash)
    if which == "admin":
        keep = request.cookies.get("admin_session")
        for token in [t for t in _admin_sessions if t != keep]:
            _admin_sessions.pop(token, None)
        if keep in _admin_sessions:
            _admin_sessions[keep]["fp"] = fresh
    else:
        keep = request.cookies.get("session")
        for token in [t for t in _sessions if t != keep]:
            _sessions.pop(token, None)
        if keep in _sessions:
            _sessions[keep]["fp"] = fresh
    logger.warning(f"{name} password changed by {actor}")
    await run_in_threadpool(_announce_password_change, request, name, actor)
    return {"status": "changed", "detail": messages.act("Use the new password from now on",
                                    f"The {name} password was changed and everyone else who used the old one was signed out")}


def _announce_password_change(request: Request, name: str, actor: str) -> None:
    """INFO email to the recipients (plan E6): which password, who, when - never the password."""
    try:
        from core.notify import AUDIENCE_BOTH, AlertEvent, notify
        notify(_cfg(), AlertEvent(
            key=f"password:{name.lower()}", kind="INFO", mode="dashboard",
            title=f"{name} password changed",
            message=f"The {name} password of the backup dashboard was changed by {actor} "
                    f"at {format_ist(now_iso(), 'D MMM YYYY, h:mm A')}. If this was not you, "
                    "contact support.", audience=AUDIENCE_BOTH), db=get_db())
    except Exception as e:  # noqa: BLE001 - the change stands; a failed email is logged and visible
        logger.error(f"Could not send the {name} password change notice: {e}")


@app.post("/api/admin/password")
async def admin_change_password(request: Request):
    return await _change_password(request, "admin")


@app.post("/api/admin/dashboard-password")
async def dashboard_change_password(request: Request):
    return await _change_password(request, "login")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    _require_auth(request)
    context = {"request": request}
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
        raise HTTPException(status_code=500, detail=messages.act("Ask support to fix config.yaml on the server", f"It does not load: {e}")) from e
    admin = _is_admin(request)
    return {"admin": admin,
            "sections": config_edit.catalog(current, include_advanced=admin),
            "effects": config_edit.EFFECT_TEXT,
            "pending_restart": _pending_restart_keys(),
            "restart_requested": app_restart.restart_pending(),
            "boot_id": app_restart.BOOT_ID,
            "apply": {"poll_seconds": current.dashboard.apply_poll_seconds,
                      "timeout_seconds": current.dashboard.apply_reconnect_timeout_seconds}}


@app.post("/api/settings")
async def save_settings(request: Request):
    _require_settings_rights(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    body = _object_body(body)
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


def _pending_restart_keys() -> list[str]:
    """Saved settings that only a restart of the backup service puts into effect: every
    restart-effect change in the history newer than this process. Read from the database,
    so it survives a page reload and is correct for every signed-in person."""
    started = pendulum.parse(app_restart.STARTED_AT)
    keys: set[str] = set()
    for row in get_db().config_changes(limit=200):
        try:
            newer = pendulum.parse(row["changed_at"]) >= started
        except Exception:  # noqa: BLE001 - an unreadable timestamp must not hide a change
            newer = True
        if newer and config_edit.effect_of(row["key"]) == config_edit.AGENT_RESTART:
            keys.add(row["key"])
    return sorted(keys)


async def _prefect_busy_reason(due_within_s: int) -> str | None:
    """Why the backup service must not be restarted now: ANY flow run (backup, audit,
    rollover check, report) running or queued, or one due within `due_within_s`.
    Prefect unreachable counts as busy (fail closed)."""
    try:
        async with get_client() as client:
            runs = await client.read_flow_runs(
                flow_run_filter=FlowRunFilter(
                    state=FlowRunFilterState(
                        type=FlowRunFilterStateType(
                            any_=[StateType.RUNNING, StateType.PENDING, StateType.SCHEDULED]))),
                limit=200)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to query Prefect API: {e}")
        return "the scheduler could not be asked whether a run is active"
    horizon = datetime.now(timezone.utc) + timedelta(seconds=due_within_s)
    for run in runs:
        state = getattr(run, "state", None)
        state_type = getattr(state, "type", None) or getattr(run, "state_type", None)
        name = getattr(run, "name", None) or "a run"
        if state_type in (StateType.RUNNING, StateType.PENDING):
            return f"{name} is running"
        nxt = getattr(run, "next_scheduled_start_time", None)
        if nxt is None:
            return f"{name} is about to start"
        nxt = nxt if nxt.tzinfo else nxt.replace(tzinfo=timezone.utc)
        if nxt <= horizon:
            return f"{name} is due to start within {due_within_s // 60 or 1} minute(s)"
    return None


@app.post("/api/settings/apply")
async def apply_settings(request: Request):
    """Restart the backup service so saved settings that need it take effect.

    Never queued: refused (409) while any backup / audit / rollover is running or about to
    start, so a restart can never cut a run short."""
    _require_settings_rights(request)
    cfg = _cfg()
    if app_restart.restart_pending():
        return {"status": "restarting", "boot_id": app_restart.BOOT_ID}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - no body = a normal "apply"
        body = {}
    manual = bool(isinstance(body, dict) and body.get("force"))
    pending = await run_in_threadpool(_pending_restart_keys)
    if not pending and not manual:
        return {"status": "nothing_to_apply", "boot_id": app_restart.BOOT_ID}
    busy = _settings_lock_busy() or await _prefect_busy_reason(
        cfg.dashboard.apply_block_before_run_seconds)
    if busy:
        raise HTTPException(
            status_code=409,
            detail=messages.act("Try again when it has finished", f"Nothing was restarted because {busy}"))
    actor = _approval_actor(request)
    get_db().record_config_change({
        "changed_at": now_iso(), "changed_by": actor, "key": "service.restart",
        "old_value": "''", "new_value": repr(", ".join(pending) or "(manual restart)"), "risky": 0,
        "backup_file": ""})
    app_restart.request_restart(delay_s=cfg.dashboard.apply_delay_seconds,
                                force_exit_s=cfg.dashboard.apply_force_exit_seconds, actor=actor)
    logger.warning(f"Apply changes by {actor}: restarting for {', '.join(pending) or 'a manual restart'}")
    return {"status": "restarting", "boot_id": app_restart.BOOT_ID, "applied": pending}


@app.post("/api/settings/validate")
async def validate_settings(request: Request):
    """Live check while editing: the exact verdict a save would give, nothing written."""
    _require_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    body = _object_body(body)
    return config_edit.validate_changes(_settings_config_path(), body.get("changes") or {},
                                        allow_advanced=_is_admin(request))


def _require_advanced(request: Request) -> None:
    _require_auth(request)
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail=messages.act(
            "Open the Advanced tab and sign in with the Advanced password", "Only support settings use this"))


@app.get("/api/settings/browse")
def browse_folders(request: Request, path: str = ""):
    """Folders on the server for the Advanced source picker (read-only; plan FY-8)."""
    _require_advanced(request)
    from core import source_picker

    try:
        return source_picker.list_folders(path)
    except source_picker.PickerError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/settings/check-source")
async def check_source_folder(request: Request):
    """Would <parent>/<current FY> work as the source folder? Nothing is created or saved."""
    _require_advanced(request)
    from core import source_picker
    from models.config import load_config

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    parent = body.get("parent") if isinstance(body, dict) else ""
    return await run_in_threadpool(
        source_picker.check_source_parent, load_config(_settings_config_path()), str(parent or ""))


def _open_rollover_journal() -> dict | None:
    """The unfinished rollover journal (plan A8), if any."""
    return get_db().rollover_incomplete()


def _unfinished_rollover_message(journal: dict) -> str:
    todo = ", ".join(rollover_journal.LABELS[s].lower()
                     for s in rollover_journal.attempt_order(journal))
    return (f"The rollover {journal['from_fy']} to {journal['to_fy']} is not finished yet. "
            f"Still to do: {todo}. It continues automatically every morning; "
            "'Run' continues it now.")


def _finished_journal(configured: str | None, current: str) -> bool:
    """Is the transition configured -> current already finished (the settings were put back)? Plan FY-2."""
    if configured is None or configured == current:
        return False
    try:
        return get_db().rollover_get(configured, current) is not None
    except Exception:  # noqa: BLE001 - the panel still renders; the flow itself decides again
        return False


@app.get("/api/rollover")
async def rollover_status(request: Request):
    """FY rollover panel: the backups' FY, today's FY, the last attempt, and
    whether "Run FY rollover now" is allowed (fy_rollover.manual_run_state)."""
    _require_auth(request)
    cfg = _cfg()
    configured = fy_rollover.configured_fy(cfg.paths.source_drive, cfg.paths.lan_destination)
    current = get_fy_prefix()
    state = fy_rollover.manual_run_state(configured, current, _finished_journal(configured, current))
    journal = _open_rollover_journal()
    if journal is not None:           # an unfinished rollover can always be continued
        state = {**state, "can_run": True, "message": _unfinished_rollover_message(journal)}
    last = get_db().last_run("rollover", fy=ALL_FYS)
    approval = next((a for a in get_db().open_approvals() if a["leg"] == "rollover"), None)
    return {
        "configured_fy": configured, "current_fy": current, **state,
        "active_fys": _active_fy_cards(),
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


def _active_fy_cards() -> list[dict]:
    """The FY folders being backed up now with their role and 'backed up since' (rollover card, plan 5.7)."""
    configured = _configured_fy()
    db = get_db()
    out = []
    for fy in _active_fy_list():
        try:
            activation = db.get_activation(fy)
        except Exception:  # noqa: BLE001 - the card still renders
            activation = None
        since = activation.get("activated_at") if activation else None
        out.append({"fy": fy, "role": fy_overlap.CURRENT if fy == configured else fy_overlap.EARLY,
                    "active_since": since, "active_since_formatted": format_ist(since) if since else None})
    return out


@app.post("/api/rollover/run")
async def run_rollover_now(request: Request):
    """Start the normal rollover check now (e.g. the automatic one failed or the
    server was off on April 1). Same rules as the 06:00 run - never a bypass."""
    _require_settings_rights(request)
    cfg = _cfg()
    configured = fy_rollover.configured_fy(cfg.paths.source_drive, cfg.paths.lan_destination)
    current = get_fy_prefix()
    state = fy_rollover.manual_run_state(configured, current, _finished_journal(configured, current))
    if not state["can_run"] and _open_rollover_journal() is None:
        raise HTTPException(status_code=409, detail=state["message"])
    async with _trigger_lock():
        running = await _is_running("rollover")
        if running is None:
            raise HTTPException(status_code=503, detail=messages.SCHEDULER_NOT_ANSWERING)
        if running:
            raise HTTPException(status_code=409, detail=messages.act("Wait for the running rollover check to finish", "Only one can run at a time"))
        try:
            flow_run = await _create_run(name="rollover-check/rollover-check")
        except Exception as e:
            logger.error(f"Manual rollover run could not be started: {e}")
            raise HTTPException(status_code=500, detail=messages.could_not_start("rollover check", e)) from e
    flow_run = await _wait_for_run(flow_run, _trigger_timeout(request))
    logger.warning(f"FY rollover check started manually by {_approval_actor(request)}")
    return _trigger_result("FY rollover", flow_run, noun="check")


@app.post("/api/settings/test-email")
def send_test_email(request: Request):
    """Send a test email through the real alert path (recorded like every email),
    so a changed mail setting is proven before a real alert depends on it
    (audit 2026-10-18 item 4). Not stopped by notifications.send_on_failure."""
    _require_settings_rights(request)
    from core.notify import AUDIENCE_BOTH, SENT, AlertEvent, notify, resolve_recipients

    actor = _approval_actor(request)
    cfg = _cfg()
    result = notify(cfg, AlertEvent(
        key=f"test:{now_iso()}", kind="INFO", mode="test",
        title="Test email from your backup system",
        message=(f"This is a test sent from the dashboard by {actor}. If you can read it, "
                 "backup emails reach you. Nothing to do."),
        audience=AUDIENCE_BOTH), db=get_db())
    recipients = ", ".join(resolve_recipients(cfg.notifications, AUDIENCE_BOTH).addresses)
    if result.status == SENT:
        return {"status": "sent", "detail": messages.act("Check that it arrived", f"The test email was sent to {recipients}")}
    if "no recipients" in (result.error or ""):
        action = "Add at least one email address under \"Who gets problem alerts\" or \"Who gets reports\", then send it again"
    else:
        action = "Check the mail settings (server, port, user and password), then send it again"
    return JSONResponse({"status": "failed",
                         "detail": messages.act(action, f"The test email was NOT delivered: {result.error}")},
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


def _backup_first_question(cfg, mode: str, fy: str | None = None) -> str | None:
    """Why "Check now" should wait for a backup, or None. The check never backs up by itself: this
    only informs and the person chooses. Advisory, so any failure here lets the check start (the
    check classifies files that are not backed up yet, so it stays safe)."""
    try:
        limit = cfg.audit.backup_first_prompt_minutes
        db = get_db()
        now = pendulum.now(IST)
        old = []
        for leg in ("lan", "cloud"):
            if mode not in (leg, "all") or not getattr(cfg, leg).enabled:
                continue
            last = db.last_successful_run_or_earlier(leg, fy=fy or _configured_fy())
            started = last.get("started_at") if isinstance(last, dict) else None
            name = "LAN" if leg == "lan" else "cloud"
            if not started:
                old.append(f"there is no successful {name} backup yet")
            elif (now - pendulum.parse(started)).total_seconds() > limit * 60:
                old.append(f"the last {name} backup was at {format_ist(started)}")
        if not old:
            return None
        return messages.act("Back up now first, then check again",
                            "Files changed since the last backup are not copied yet: "
                            + " and ".join(old))
    except Exception as e:  # noqa: BLE001 - advisory only
        logger.warning(f"Check-now pre-check skipped: {e}")
        return None


@app.post("/trigger/verify/{mode}")
async def trigger_verify(mode: str, request: Request):
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"trigger:{client_ip}", _dash("rate_limit_triggers")):
        raise HTTPException(status_code=429, detail=messages.TOO_MANY_REQUESTS)
    mode = mode.lower()
    if mode not in ("lan", "cloud", "all"):
        raise HTTPException(status_code=400, detail=messages.act("Use lan, cloud or all", f"'{mode}' is not a safety-check option"))
    cfg = _cfg()
    if mode == "lan" and not cfg.lan.enabled:
        return JSONResponse({"status": "disabled", "detail": messages.leg_off("lan", "checked")}, status_code=409)
    if mode == "cloud" and not cfg.cloud.enabled:
        return JSONResponse({"status": "disabled", "detail": messages.leg_off("cloud", "checked")}, status_code=409)
    if mode == "all" and not (cfg.lan.enabled or cfg.cloud.enabled):
        return JSONResponse({"status": "disabled", "detail": messages.act("Switch on the LAN or the cloud backup in Settings, then try again", "Both are turned off, so there is nothing to check")}, status_code=409)
    fy = _requested_fy(request)
    async with _trigger_lock():
        for leg in ("lan", "cloud", "rollover"):
            running = await _is_running(leg)
            if running is None:
                raise HTTPException(
                    status_code=503,
                    detail=messages.SCHEDULER_NOT_ANSWERING,
                )
            if running:
                what = "FY rollover check" if leg == "rollover" else "backup"
                return JSONResponse({"status": "already_running", "detail": messages.act(f"Wait for the running {what} to finish, then check again", f"The safety check and the {what} cannot run at the same time")}, status_code=400)
        verifying = await _is_verify_running()
        if verifying is None:
            raise HTTPException(
                status_code=503,
                detail=messages.SCHEDULER_NOT_ANSWERING,
            )
        if verifying:
            return JSONResponse({"status": "already_running", "detail": messages.act("Wait for the running safety check to finish", "Only one can run at a time")}, status_code=400)
        if request.query_params.get("anyway") != "1":
            ask = _backup_first_question(cfg, mode, fy)
            if ask:
                return JSONResponse({"status": "backup_first", "detail": ask}, status_code=409)
        try:
            flow_run = await _create_run(
                name="integrity-audit/integrity-audit",
                parameters={"mode": mode, **({"fy": fy} if fy else {})},
            )
        except Exception as e:
            logger.error(f"Manual verification trigger failed: {e}")
            return JSONResponse(
                {"status": "trigger_failed",
                 "detail": messages.could_not_start("safety check", e)},
                status_code=500,
            )
    flow_run = await _wait_for_run(flow_run, _trigger_timeout(request))
    label = {"lan": "LAN", "cloud": "Cloud", "all": "Full"}[mode]
    return _trigger_result(label, flow_run, noun="verification")


# ── Report endpoints ────────────────────────────────────────


@app.get("/report/weekly")
def report_weekly(request: Request):
    """Generate and download a weekly HTML report."""
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"report:{client_ip}", _dash("rate_limit_reports")):
        raise HTTPException(status_code=429, detail=messages.TOO_MANY_REQUESTS)
    return _serve_report(7, "Weekly")


@app.get("/report/monthly")
def report_monthly(request: Request):
    """Generate and download a monthly HTML report."""
    _require_auth(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"report:{client_ip}", _dash("rate_limit_reports")):
        raise HTTPException(status_code=429, detail=messages.TOO_MANY_REQUESTS)
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
        raise HTTPException(status_code=429, detail=messages.TOO_MANY_REQUESTS)
    cfg = _cfg()
    db = get_db()
    from core.notify import AUDIENCE_REPORTS, resolve_recipients
    from core.report import generate_report_html, send_weekly_report
    from core.report_period import ReportPeriod
    legs = _enabled_legs(cfg)
    rp = ReportPeriod.last_days(7)        # the dashboard sends the last 7 days up to now
    html_body = generate_report_html(db, cfg.firm_name, 7, "Weekly", legs=legs, config=cfg,
                                     report_period=rp, schedule=cfg.schedule,
                                     alerts_off=alert_class.switched_off(cfg))
    try:
        ok = send_weekly_report(db, cfg.notifications, cfg.firm_name, body_html=html_body,
                                legs=legs, report_period=rp, schedule=cfg.schedule,
                                     alerts_off=alert_class.switched_off(cfg))
    except Exception as e:
        logger.exception("Failed to send weekly email report manual trigger")
        return JSONResponse(
            {"status": "failed", "detail": messages.act("Check the mail settings in Settings, then try again", f"The email could not be sent: {e}")},
            status_code=500,
        )
    if ok:
        recipients = ", ".join(resolve_recipients(cfg.notifications, AUDIENCE_REPORTS).addresses)
        return JSONResponse({"status": "sent", "detail": f"Weekly report emailed to: {recipients}"})
    return JSONResponse(
        {"status": "failed", "detail": messages.act("Check the mail settings in Settings, then try again", "The report email was not sent; the agent log has the details")},
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
        raise HTTPException(status_code=429, detail=messages.TOO_MANY_REQUESTS)
    cfg = _cfg()
    db = get_db()
    from core.notify import AUDIENCE_REPORTS, resolve_recipients
    from core.report import generate_report_html, send_monthly_report
    from core.report_period import ReportPeriod
    legs = _enabled_legs(cfg)
    rp = ReportPeriod.last_days(30)        # the dashboard sends the last 30 days up to now
    html_body = generate_report_html(db, cfg.firm_name, 30, "Monthly", legs=legs, config=cfg,
                                     report_period=rp, schedule=cfg.schedule,
                                     alerts_off=alert_class.switched_off(cfg))
    try:
        ok = send_monthly_report(db, cfg.notifications, cfg.firm_name, body_html=html_body,
                                 legs=legs, report_period=rp, schedule=cfg.schedule,
                                     alerts_off=alert_class.switched_off(cfg))
    except Exception as e:
        logger.exception("Failed to send monthly email report manual trigger")
        return JSONResponse(
            {"status": "failed", "detail": messages.act("Check the mail settings in Settings, then try again", f"The email could not be sent: {e}")},
            status_code=500,
        )
    if ok:
        recipients = ", ".join(resolve_recipients(cfg.notifications, AUDIENCE_REPORTS).addresses)
        return JSONResponse({"status": "sent", "detail": f"Monthly report emailed to: {recipients}"})
    return JSONResponse(
        {"status": "failed", "detail": messages.act("Check the mail settings in Settings, then try again", "The report email was not sent; the agent log has the details")},
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
    from core.report_period import ReportPeriod
    html_body = generate_report_html(db, cfg.firm_name, days, period, legs=_enabled_legs(cfg), config=cfg,
                                     report_period=ReportPeriod.last_days(days),
                                     schedule=cfg.schedule,
                                     alerts_off=alert_class.switched_off(cfg))

    safe_firm = re.sub(r'[^a-zA-Z0-9_\-]', '_', cfg.firm_name)
    filename = f"{safe_firm}_{period}_Report_{pendulum.now(IST).format('YYYY-MM-DD')}.html"
    return Response(
        content=html_body,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Helpers ──────────────────────────────────────────────────


def _configured_fy() -> str:
    """The configured FY folder (the dashboard shows one card per FY from WP7 on)."""
    return fy_rollover.effective_fy_prefix(_cfg().paths.source_drive)


def _get_last_success(db: ManifestDB, mode: str, fy: str | None = None, role: str | None = None) -> str | None:
    """Return ended_at timestamp of the last successful run for this mode (of `fy`, default the configured
    one). The configured FY falls back to an earlier FY's success (right after the April switch); the early
    FY has its own history only."""
    if role == fy_overlap.EARLY:
        run = db.last_successful_run(mode, fy=fy)
    else:
        run = db.last_successful_run_or_earlier(mode, fy=fy or _configured_fy())
    if run:
        return run.get("ended_at")
    return None


def _integrity_summary(db: ManifestDB, mode: str, fy: str | None = None) -> dict:
    """Return the integrity-verification status for a mode.

    Backup result and integrity are separate facts: a COMPLETE backup with
    no audit yet reports NOT_VERIFIED (never implied VERIFIED).
    """
    try:
        audit = db.latest_audit(mode, fy=fy or _configured_fy())
    except Exception:
        audit = None
    if not isinstance(audit, dict):
        return {"status": "NOT_VERIFIED", "checked_at": None, "detail": None}
    return {
        "status": audit.get("status", "NOT_VERIFIED"),
        "checked_at": audit.get("ended_at") or audit.get("started_at"),
        "detail": audit.get("detail"),
        "newer_incomplete_at": audit.get("newer_incomplete_at"),
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
            status, run.get("files_copied", 0), run.get("files_failed", 0),
            sync_attempted=bool(metrics.get("sync_attempts")) if isinstance(metrics, dict) else False,
        ),
        "fault_code": code if code in faults.CATALOG else None,
        "fault_title": faults.CATALOG[code].title if code in faults.CATALOG else None,
    }


def _last_run_summary(db: ManifestDB, mode: str, fy: str | None = None) -> dict | None:
    run = db.last_run(mode, fy=fy or _configured_fy())
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



