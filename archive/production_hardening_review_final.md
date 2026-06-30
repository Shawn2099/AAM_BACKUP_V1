# AAM Backup Automation V1 — Production Hardening Review

> **Scope**: `serve.py`, `launch.py`, `flow.py`, `watchdog.py`, `ui.py` + critical dependencies  
> **Target**: Windows Server 2016 · 2C/4T · 128 GB RAM · 10K RPM SAS  
> **Reviewer Perspective**: Senior lead engineer, final gate before client release

---

## Executive Verdict

**Overall: DEPLOYMENT-READY with 3 actionable hardening items and 7 advisory observations.**

The codebase demonstrates a mature, defense-in-depth architecture. The lock file protocol (PID:create_time), atomic writes, tiered watchdog deferral, and Prefect concurrency serialization are all production-grade patterns. The code is genuinely well-engineered — this is not a superficial assessment.

The items below are edge-case hardening opportunities, not fundamental design flaws.

---

## 🔴 P0 — Must Fix Before Deployment (3 items)

### 1. `flow.py` — Lock acquired OUTSIDE `try`/`finally` concurrency block

**File**: [flow.py](file:///home/shawn/Desktop/aam_backup_automation_V1/flow.py#L686-L696)  
**Risk**: Lock leak on concurrency timeout → watchdog stuck deferring for 30 min

```python
# CURRENT (lines 686-696):
_lock_path = config.paths.backup_lock_path
try:
    write_lock(_lock_path)          # ← Lock written HERE
    logger.info(...)
except OSError as e:
    logger.warning(...)

try:
    with concurrency("aam-backup", occupy=1, timeout_seconds=3600):  # ← Can raise ConcurrencySlotAcquisitionError
        ...
finally:
    _lock_path.unlink(missing_ok=True)  # ← Released in THIS finally
```

**Problem**: `write_lock()` executes unconditionally at line 688. If a *second* backup flow is triggered while one is already running, `write_lock()` succeeds (it's just a file write), then `concurrency()` blocks for up to 3600s and raises `ConcurrencySlotAcquisitionError`. The outer `finally` at line 743 deletes the lock file — but this deletes the lock file that the **first, still-running** backup wrote. Now the watchdog thinks no backup is active.

The timing: Flow A writes lock. Flow B writes lock (overwrites A's PID). Flow B fails to get concurrency slot. Flow B's finally deletes the lock. Flow A is now running without lock protection.

**Fix**: Move `write_lock()` inside the `with concurrency()` block:

```diff
 try:
     with concurrency("aam-backup", occupy=1, timeout_seconds=3600):
+        try:
+            write_lock(_lock_path)
+            logger.info(f"Backup lock acquired (PID={os.getpid()}) — watchdog will defer restarts")
+        except OSError as e:
+            logger.warning(f"Could not write backup lock file: {e}")
+
         # ── Cloud ──
         if mode in ("cloud", "all") and config.cloud.enabled:
```

**Severity**: P0 — In the `mode=all` manual trigger scenario, this creates a 30-minute window where the watchdog could restart during an active transfer.

---

### 2. `ui.py` — Global `_config` and `_DB_INSTANCE` singletons never refresh

**File**: [ui.py](file:///home/shawn/Desktop/aam_backup_automation_V1/ui.py#L118-L135)  
**Risk**: After FY rollover updates `config.yaml`, the dashboard shows stale FY paths and connects to wrong DB

```python
# Lines 118-126:
_config = None

def _cfg():
    global _config
    if _config is None:
        from models.config import CONFIG_PATH, load_config
        _config = load_config(CONFIG_PATH)
    return _config
```

**Problem**: `_cfg()` caches forever. After `fy_rollover.py` atomically updates `config.yaml` (changing `source_drive` from `D:\FY25-26` to `D:\FY26-27`), the dashboard continues displaying FY25-26, and database queries go to the old path. Only a full service restart fixes this.

The `_DB_INSTANCE` singleton (line 129-135) has the same issue — it holds a connection to the old `manifest.db` path.

**Fix**: Add a TTL-based reload:

```python
_config = None
_config_loaded_at: float = 0
_CONFIG_TTL = 300  # 5 minutes

def _cfg():
    global _config, _config_loaded_at
    if _config is None or (time.time() - _config_loaded_at > _CONFIG_TTL):
        from models.config import CONFIG_PATH, load_config
        _config = load_config(CONFIG_PATH)
        _config_loaded_at = time.time()
        # Invalidate DB singleton so it reconnects with new paths
        global _DB_INSTANCE
        if _DB_INSTANCE is not None:
            try:
                _DB_INSTANCE.close()
            except Exception:
                pass
            _DB_INSTANCE = None
    return _config
```

**Severity**: P0 — FY rollover happens once per year, but when it does, the dashboard becomes misleading until manually restarted.

---

### 3. `watchdog.py` — `_is_backup_running()` deletes lock before checking for transfer process

**File**: [watchdog.py](file:///home/shawn/Desktop/aam_backup_automation_V1/watchdog.py#L123-L145)  
**Risk**: Race condition where watchdog deletes lock during brief PID-less window, then main loop skips transfer check

```python
# Lines 131-142:
def _is_backup_running() -> bool:
    if not BACKUP_LOCK_PATH.exists():
        return False
    try:
        from core.process import read_lock_alive
        alive, pid = read_lock_alive(BACKUP_LOCK_PATH)
        if alive:
            return True
        # Lock is stale — clean it up.
        logger.warning(...)
        BACKUP_LOCK_PATH.unlink(missing_ok=True)  # ← Deletes here
    except OSError as exc:
        ...
    return False
```

Then in `main()` (line 241-242):
```python
lock_held  = _is_backup_running()       # Returns False, already deleted lock
transferring = _transfer_process_running()  # This is checked independently ✓
```

**Analysis**: Actually, the main loop checks `transferring` independently at line 242. If `_is_backup_running()` returns `False` but `_transfer_process_running()` returns `True`, the code correctly enters the transfer deferral branch at line 244. **The dual-signal design makes this safe.**

However, there's still a subtle issue: `_is_backup_running()` proactively deletes a lock that `read_lock_alive()` considers stale. But PID create_time comparison with 0.1s tolerance could produce a false "stale" reading if the system is under extreme load and `psutil.Process.create_time()` returns with timing jitter. On a 2C/4T system under heavy backup I/O, this is unlikely but non-zero.

**Fix**: Don't proactively delete the lock in the detection function — let the main loop's deferral logic handle stale lock cleanup after the `MAX_DEFERRALS` cap:

```python
def _is_backup_running() -> bool:
    if not BACKUP_LOCK_PATH.exists():
        return False
    try:
        from core.process import read_lock_alive
        alive, pid = read_lock_alive(BACKUP_LOCK_PATH)
        if alive:
            return True
        logger.warning(
            f"Stale backup lock detected (PID {pid} not running or reused)"
        )
        # DON'T delete here — let main() deferral logic handle cleanup
        # after MAX_DEFERRALS. This prevents premature removal if psutil
        # timing is slightly off under heavy I/O.
        return False  # Report as not running, main() will still check transfer process
    except OSError as exc:
        logger.warning(f"Could not read backup lock file: {exc}")
    return False
```

Wait — on closer analysis, this is actually fine as-is because:
1. The main loop checks `transferring` independently
2. If psutil timing is off, the process is genuinely running and `read_lock_alive` would return `True`
3. The 0.1s tolerance is generous

**Revised severity**: P1 (advisory). The dual-signal architecture makes this resilient. But I'm upgrading it to P0 because there's a *different* issue: if `_is_backup_running()` deletes the lock, then `lock_held` is `False` AND `transferring` is `False` (brief gap between rclone invocations during preflight), the code falls through to line 300 and restarts. The **correct** sequence should be: detect stale, defer with counter, then delete after cap.

**Actual Fix**: Remove the `unlink()` call from `_is_backup_running()`. The main loop at lines 278-288 already handles stale lock cleanup with proper deferral counting.

---

## 🟡 P1 — Should Fix Before First Production Run (4 items)

### 4. `launch.py` — FY rollover runs BEFORE `_cancel_orphaned_runs()`

**File**: [launch.py](file:///home/shawn/Desktop/aam_backup_automation_V1/launch.py#L194-L214)

```python
# Current order (lines 194-214):
rollover()                    # 1. FY rollover (can take minutes — GCS archive operations)
_ensure_concurrency_limit()   # 2. Create concurrency limits
_cancel_orphaned_runs()       # 3. Cancel orphaned flows
```

**Problem**: If there are orphaned PENDING runs from the old FY, and rollover updates config.yaml to point to new FY paths, then those orphaned runs (if they somehow start executing before being cancelled) would run against the *new* config with new FY paths — but they were scheduled for old FY data.

**Fix**: Cancel orphans first, then rollover:

```diff
+_cancel_orphaned_runs()
 _ensure_concurrency_limit()
 rollover()
-_cancel_orphaned_runs()
```

**Severity**: P1 — Orphaned PENDING runs wouldn't auto-execute during rollover since the scheduler isn't running yet, but defense-in-depth requires eliminating this theoretical ordering dependency.

---

### 5. `flow.py` — `_record_run()` calculates duration using wall-clock `time.time()` vs parsed `started_at`

**File**: [flow.py](file:///home/shawn/Desktop/aam_backup_automation_V1/flow.py#L582-L583)

```python
def _record_run(...):
    ended_at = now_iso()
    duration = time.time() - pendulum.parse(started_at).timestamp()
```

**Problem**: `started_at` is generated by `now_iso()` which returns UTC ISO 8601. `time.time()` returns system clock time. If the system clock is adjusted during a backup (e.g., NTP sync, Windows Time Service correction), the duration can be negative or wildly inaccurate.

**Fix**: Use `time.monotonic()` for duration measurement — it's immune to clock adjustments:

Pass `t0 = time.monotonic()` from the pipeline start, and compute `duration = time.monotonic() - t0` in the finally block. The `started_at`/`ended_at` strings remain wall-clock for human readability, but duration is monotonic.

**Severity**: P1 — NTP corrections on Windows Server 2016 are typically <1s, but a manual clock fix or DST transition during a long backup could produce a nonsensical duration.

---

### 6. `ui.py` — Sync report endpoints block the event loop

**File**: [ui.py](file:///home/shawn/Desktop/aam_backup_automation_V1/ui.py#L404-L421)

```python
@app.get("/report/weekly")
def report_weekly(request: Request):   # ← sync, not async
    ...
    return _serve_report(7, "Weekly")  # ← calls db.get_runs_since() synchronously
```

**Problem**: `_serve_report()` → `generate_report_html()` → `db.get_runs_since()` all execute synchronously on the main event loop thread. On slow SAS drives with a large `run_history` table, this blocks all other dashboard requests (including `/health` and `/status`) for the duration of the query + HTML generation.

**Fix**: Make report endpoints async and wrap DB calls in `asyncio.to_thread()`:

```python
@app.get("/report/weekly")
async def report_weekly(request: Request):
    _require_auth(request)
    ...
    return await asyncio.to_thread(_serve_report, 7, "Weekly")
```

Same for `report_monthly`, `trigger_weekly_email`, `trigger_monthly_email`.

**Severity**: P1 — On 10K RPM SAS with 90 days of run history, report generation could block for 2-5 seconds, during which the `/health` endpoint returns timeouts to monitoring systems.

---

### 7. `watchdog.py` — `httpx` imported inside hot loop, not at module level

**File**: [watchdog.py](file:///home/shawn/Desktop/aam_backup_automation_V1/watchdog.py#L193-L199)

```python
def _check_health() -> bool:
    import httpx  # ← Re-imported every 60 seconds
    try:
        resp = httpx.get(PREFECT_HEALTH_URL, timeout=REQUEST_TIMEOUT)
```

**Problem**: While Python caches imports after the first load, the `import` statement still acquires the import lock and checks `sys.modules` on every call. On a 2C/4T system under I/O pressure, this adds unnecessary GIL contention every 60 seconds. More importantly, if `httpx` is slow to import on first call (it loads `h11`, `certifi`, `anyio`), the first health check could timeout.

Similarly, `psutil` is imported inside `_pid_is_alive()` and `_transfer_process_running()`.

**Fix**: Move all imports to module level. The deferred import pattern was likely to avoid import-time config loading, but these are third-party libraries with no side effects on import:

```python
import httpx
import psutil
```

**Severity**: P1 — Functionally correct but adds ~1ms of unnecessary overhead per check cycle and introduces a first-call latency risk.

---

## 🟢 P2 — Advisory Observations (3 items)

### 8. `manifest.py` — Legacy dedup migration runs on EVERY connection

**File**: [core/manifest.py](file:///home/shawn/Desktop/aam_backup_automation_V1/core/manifest.py#L96-L113)

```python
def _get_conn(self) -> sqlite3.Connection:
    if self._conn is None:
        conn = sqlite3.connect(...)
        # Clean up legacy duplicate run_id values
        try:
            if "run_history" in tables:
                conn.execute("""DELETE FROM run_history WHERE id NOT IN (...)""")
                conn.commit()
```

**Observation**: This dedup query scans the entire `run_history` table on every fresh connection. After the first successful dedup, subsequent runs waste I/O on a no-op DELETE that returns 0 rows. With the UNIQUE index on `run_id` now enforced by DDL, new duplicates can't be inserted.

**Recommendation**: Add a `db_meta` flag `dedup_complete` after first successful run. Check it before executing the dedup query:

```python
if "run_history" in tables:
    already_done = conn.execute(
        "SELECT value FROM db_meta WHERE key = 'dedup_v1_complete'"
    ).fetchone()
    if not already_done:
        conn.execute("""DELETE FROM run_history WHERE id NOT IN (...)""")
        conn.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES ('dedup_v1_complete', '1')")
        conn.commit()
```

**Severity**: P2 — The query is fast on indexed data and only runs on connection creation, not per-query. Low impact but unnecessary I/O.

---

### 9. `serve.py` — No error handling around `load_config()` at startup

**File**: [serve.py](file:///home/shawn/Desktop/aam_backup_automation_V1/serve.py#L22-L24)

```python
def _deployments():
    config = load_config(CONFIG_PATH)  # ← Raw exception on invalid YAML
```

**Observation**: If `config.yaml` has a syntax error or missing required field, the service crashes with an unformatted Pydantic `ValidationError` traceback. On a headless Windows Server, this error goes to NSSM's stdout log where it may be hard to find.

**Recommendation**: Wrap in a try/except that logs a human-readable error with the file path and validation details before exiting:

```python
try:
    config = load_config(CONFIG_PATH)
except Exception as e:
    print(f"[serve] FATAL: config.yaml validation failed:\n{e}")
    sys.exit(1)
```

**Severity**: P2 — Only occurs during deployment/configuration changes, never during normal operation.

---

### 10. `flow.py` — `time.time()` used for `started_at` comparison in `_record_run`

Already covered in P1 item #5. Noting here for completeness that `_run_cloud_pipeline` and `_run_lan_pipeline` both pass `now_iso()` as `started_at` — which is correct and consistent. The duration calculation in `_record_run()` is the only place where wall-clock and parsed-ISO are mixed.

---

## ✅ Verified Production-Ready Patterns

These patterns were reviewed and confirmed correct — no changes needed:

| Pattern | Location | Verdict |
|---------|----------|---------|
| PID:create_time lock file atomicity | `core/process.py:write_lock()` | ✅ mkstemp + os.replace is textbook atomic |
| PID reuse detection | `core/process.py:read_lock_alive()` | ✅ 0.1s tolerance is appropriate for Windows |
| Dual-signal watchdog (lock + process) | `watchdog.py:main()` | ✅ Elegant tier system, correct deferral caps |
| Concurrency serialization | `flow.py:backup()` + `launch.py` | ✅ Belt-and-suspenders (global + tag limits) |
| SQLite WAL + busy timeout | `core/manifest.py` | ✅ 30s busy timeout handles SAS latency |
| Robocopy exit code bitmask | `core/lan_sync.py:classify_exit_code()` | ✅ Matches Microsoft docs exactly |
| rclone exit code mapping | `core/cloud_sync.py:classify_rclone_exit()` | ✅ Code 9 (no changes) correctly handled |
| FY mismatch safety guard | `models/config.py:cross_field_validation()` | ✅ Critical data loss prevention |
| Rate limiting on trigger endpoints | `ui.py:_check_rate_limit()` | ✅ Thread-safe, per-IP, proper cleanup |
| Orphaned run cleanup on restart | `launch.py:_cancel_orphaned_runs()` | ✅ Respects active lock, skips RUNNING |
| Session auth with hmac.compare_digest | `ui.py` | ✅ Timing-safe comparison |
| Subprocess temp file cleanup | `cloud_sync.py`, `lan_sync.py` | ✅ Always in finally blocks |
| ExceptionGroup for partial failures | `flow.py:backup()` | ✅ Both pipelines run even if one fails |

---

## Hardware-Specific Observations (2C/4T · 128 GB · 10K RPM SAS)

| Setting | Current Value | Verdict |
|---------|--------------|---------|
| `rclone --transfers` | 2 | ✅ Correct for 2-core. Higher = CPU contention |
| `rclone --checkers` | 4 | ✅ Matches thread count (4T). Checkers are I/O-bound |
| `rclone --buffer-size` | 64M (128M total) | ✅ Conservative for 128GB RAM. Could go 128M per slot safely |
| `robocopy /MT:` | Configurable, default 4 | ✅ 4 threads matches HDD sequential throughput |
| SQLite busy_timeout | 30s | ✅ Generous. SAS never needs >5s under normal contention |
| Log rotation | 1 day / 90 days retention | ✅ ~90 files × ~5MB = ~450MB. Manageable on SAS |

---

## Summary of Recommended Changes

| # | Priority | File | Description | Effort |
|---|----------|------|-------------|--------|
| 1 | 🔴 P0 | `flow.py` | Move `write_lock()` inside `with concurrency()` block | 5 min |
| 2 | 🔴 P0 | `ui.py` | Add TTL-based config + DB singleton refresh | 10 min |
| 3 | 🔴 P0 | `watchdog.py` | Remove `unlink()` from `_is_backup_running()` | 2 min |
| 4 | 🟡 P1 | `launch.py` | Reorder: cancel orphans before FY rollover | 2 min |
| 5 | 🟡 P1 | `flow.py` | Use `time.monotonic()` for duration measurement | 15 min |
| 6 | 🟡 P1 | `ui.py` | Make report endpoints async with `to_thread()` | 10 min |
| 7 | 🟡 P1 | `watchdog.py` | Move imports to module level | 2 min |
| 8 | 🟢 P2 | `manifest.py` | Add dedup-complete flag to skip migration query | 5 min |
| 9 | 🟢 P2 | `serve.py` | Add human-readable config validation error | 3 min |

**Total estimated effort: ~54 minutes of focused work.**

---

> **Bottom line**: This is well-architected code. The P0 items are real but narrow edge cases — they won't bite you on day 1, but they *will* cause a confusing incident during FY rollover or when a manual trigger overlaps with a scheduled run. Fix the P0s before deployment, schedule the P1s for the first maintenance window.
