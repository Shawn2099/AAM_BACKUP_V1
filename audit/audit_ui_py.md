# Code Audit - File 5 of 26: ui.py
Lines: 677 | Size: 27 KB | Audited: 2026-09-02

---

## Summary

ui.py is a FastAPI-based web dashboard. It provides manual backup triggers,
live pipeline status, report downloads, and session/API-key authentication.
Syntactically clean. The security model (hmac.compare_digest, session TTL,
rate limiting, fail-closed trigger checks) is solid overall. Several real
bugs were found in the authentication path, shared DB state, and health endpoint.

---

## Findings

### CRITICAL - 0

---

### HIGH - 2

HIGH-1: login_submit - Auth Bypass When api_key is Empty String (Line 296)

    if not configured_key or hmac.compare_digest(str(api_key), configured_key):

The condition 'not configured_key' is True when:
  (a) auth is disabled (correct, intended bypass)
  (b) auth IS enabled but api_key in config.yaml is set to an empty string ""

In case (b), ANY password the user submits passes authentication because
the entire right side of the 'or' is short-circuited. An operator who
accidentally leaves api_key as "" in config.yaml with auth_enabled: true
will have a fully open dashboard with no warning.

The check on line 106:
    return cfg.dashboard.api_key if cfg.dashboard.auth_enabled else ""
also returns "" when auth is enabled but key is blank - so _get_api_key()
returns "" and _check_api_key_header() at line 116 also returns True for
any X-API-Key header value including empty string.

Fix: In _get_api_key(), if auth_enabled is True and key is blank, either
raise a startup error or return a sentinel that forces auth failure. At
minimum, log a CRITICAL warning at startup if auth is enabled with an
empty key.

HIGH-2: get_db() - Shared ManifestDB Instance Not Thread-Safe for Concurrent Queries (Lines 169-175)

    def get_db():
        global _DB_INSTANCE
        with _CFG_LOCK:
            _cfg()
            if _DB_INSTANCE is None:
                _DB_INSTANCE = ManifestDB(_cfg().paths.database_path)
            return _DB_INSTANCE

The lock protects CREATION and EVICTION of _DB_INSTANCE. However, after
returning, multiple concurrent request handlers use the same ManifestDB
instance simultaneously WITHOUT the lock. If ManifestDB uses a single
sqlite3.Connection internally (typical pattern), concurrent reads from
different threads will trigger "ProgrammingError: SQLite objects created
in a thread can only be used in that same thread".

The uvicorn threadpool (default 40 threads) makes this highly reproducible
under any moderate load, or even two simultaneous dashboard polls.

Fix: Either use check_same_thread=False on the SQLite connection (and
accept the risk of statement interleaving on concurrent writes), or return
a per-request connection from get_db() rather than a shared singleton,
or use a connection pool. The /status endpoint should open and close its
own ManifestDB per request.

---

### MEDIUM - 3

MEDIUM-1: /health Endpoint - Reports "healthy" Even on Exception (Lines 430-442)

    @app.get("/health")
    async def health():
        try:
            ...
            return JSONResponse({"status": "healthy", ...})
        except Exception:
            return JSONResponse({"status": "healthy"}, status_code=200)

The except block returns {"status": "healthy"} with HTTP 200 even when
the config or source drive check fails. A monitoring system polling /health
will always see 200/healthy regardless of whether the system is actually
functioning. This defeats the purpose of a health endpoint.

Fix: Return {"status": "degraded"} with HTTP 503 on exception.

MEDIUM-2: _prefect_has_active_run - Not Paginated (Lines 211-231)

    runs = await client.read_flow_runs(..., limit=200)

The function queries Prefect with limit=200 and checks if the pipeline tag
or mode is in results. If more than 200 RUNNING+PENDING runs exist (possible
after a crash-loop scenario), the actual active backup run may not be in
the first 200 results. The trigger guard (fail-closed) would then return
False and allow a duplicate trigger.

For a limit=1 concurrency system this is an edge case, but the logic can
be made correct with pagination (same as the launch.py fix in bdcdc7e).

MEDIUM-3: _check_rate_limit - Rate Limiter Uses IP Only, Bypassed by Proxies (Lines 49-64)

    client_ip = request.client.host if request.client else "unknown"

The rate limiter keys on the direct TCP connection IP. In any deployment
behind a reverse proxy (Nginx, Cloudflare, Windows IIS ARR), all requests
arrive from 127.0.0.1 and share the same rate limit bucket - meaning the
limit of 5 trigger attempts per 5 minutes is shared across ALL users,
or conversely an attacker behind the proxy has unlimited attempts.

Fix: Check X-Forwarded-For or X-Real-IP headers (with trusted proxy
validation), or configure the rate limiter to key on session token instead
of IP when behind a proxy.

---

### LOW - 3

LOW-1: _sessions Dict - No Lock for Concurrent Session Access (Lines 72-100)

_sessions is a plain dict mutated by _create_session(), _validate_session(),
and _cleanup_expired_sessions() without any lock. Under uvicorn's threadpool,
two simultaneous login requests can race on _cleanup_expired_sessions()
(iterating while another thread modifies), causing RuntimeError: dictionary
changed size during iteration.

Fix: Add a threading.Lock() around _sessions mutations (separate from
_CFG_LOCK to avoid nesting complexity).

LOW-2: /logout - Does Not Invalidate Server-Side Session Token (Lines 308-312)

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("session")
        return resp

The cookie is deleted client-side, but the session token is NOT removed
from the server-side _sessions dict. If an attacker extracted the session
cookie before logout (e.g., via XSS, log exposure, or shoulder-surfing),
they can continue using it until the 24-hour TTL expires.

Fix: Read the session token from the request cookie and del _sessions[token]
before redirecting.

LOW-3: _serve_report - Content-Disposition Filename Not RFC 5987 Encoded (Line 631)

    headers={"Content-Disposition": f'attachment; filename="{filename}"'}

The firm_name is sanitized with re.sub (line 626), so alphanumeric-only
names are safe. However, if the regex allows underscores and hyphens that
result in a filename with spaces or special chars on edge cases, the
Content-Disposition header can be malformed. RFC 5987 encoding
(filename*=UTF-8''...) is more correct for cross-browser compatibility.
Minor in practice given the regex guard.

---

## INFO

- Line 11: hmac.compare_digest used correctly for API key comparison - no timing oracle.
- Line 221: limit=200 on _prefect_has_active_run is an improvement over the old
  unbounded query, though full pagination would be more correct (see MEDIUM-2).
- Lines 451-458: H3 fail-closed trigger logic is well-implemented.
- Lines 466, 495: await arun_deployment() before responding (G15 fix) is correct.
- Line 136: RLock for re-entrant cfg/db access is the right tool - noted and appropriate.
- Lines 380-381: error_message truncated to 2000 chars prevents large payloads in JSON.

---

## Verdict
| Severity | Count |
|----------|-------|
| CRITICAL |   0   |
| HIGH     |   2   |
| MEDIUM   |   3   |
| LOW      |   3   |
| Total    |   8   |

NOT fully production-ready. HIGH-1 (auth bypass on empty key) and HIGH-2
(shared DB instance in threadpool) are real bugs that will hit in production.
HIGH-2 in particular will cause intermittent 500 errors under any concurrent
dashboard usage.
