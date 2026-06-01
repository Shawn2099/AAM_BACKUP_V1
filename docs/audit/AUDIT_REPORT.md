# AAM Backup Automation V1 — Comprehensive Code Audit

**Date:** 2026-06-01  
**Scope:** All 28 Python source files (~5,300 lines) + 27 test files (~4,500 lines)  
**Tools used:** GitNexus (call graph, 89 execution flows), Graphify (semantic graph, 1,110 nodes, 74 communities), Python Analyzer MCP, 7 parallel review agents

---

## Fixes Applied (2026-06-01)

All fixes verified against source before applying. Impact analysis run via `gitnexus_impact` before each change. **435/435 tests pass** after all fixes.

| ID | Severity | Fix | Files changed |
|----|----------|-----|---------------|
| **S2** | SECURITY | Wire `_check_rate_limit` into `login_submit` with `_RATE_MAX_LOGIN` | `ui.py` |
| **S1** | SECURITY | Allowlist `r.mode` against `["cloud", "lan"]` in JS; `html.escape()` on `mode`, `status`, and `error` in server-rendered rows | `ui.py`, `templates/dashboard.py` |
| **C2** | BUG | `record_run_history` now returns `bool`; caller `_record_run` logs warning on failure | `core/backup_repository.py`, `flow.py` |
| **C4** | BUG | Lock file write now uses `tempfile.mkstemp` + `os.replace` for atomic PID write | `flow.py` |
| **C5** | BUG | `/status` endpoint uses try/except around DB call instead of `Path.exists()` pre-check | `ui.py` |
| **C6** | BUG (docs) | Swapped exit code 1/2 descriptions in `classify_rclone_exit` docstring | `core/cloud_sync.py` |
| **C7** | BUG | `_cancel_orphaned_runs` reads `backup.lock` PID via `core.process.pid_alive` before cancelling RUNNING flows; always cancels PENDING | `launch.py` |
| **C8** | BUG (UX) | Log tail now captured on `LAN_PARTIAL` in addition to `LAN_FAILED` | `core/lan_sync.py` |

Test updates: `tests/test_ui.py` updated for new `/status` error-handling behavior (mock `get_db` exception instead of `Path.exists`).

---

## 1. Executive Summary

The codebase is **well-engineered overall** — consistent architecture, strong separation of concerns between cloud/LAN pipelines, comprehensive test coverage (66+ tests pass), and thoughtful design decisions (zero-import architecture, WAL-mode SQLite, deferred config loading, watchdog lock protocol).

**However, 3 real bugs, 2 security vulnerabilities, and 5 significant structural issues were found:**

### Critical (fix immediately)

| # | Severity | File:Line | Finding |
|---|----------|-----------|---------|
| C1 | **BUG** | `core/manifest.py:78` | `_get_conn()` checks `self._conn is None` **outside** `self._lock`. Two threads racing on first call both create connections; the first is leaked (file handle + WAL handle). **[FALSE — every one of the 17 call sites wraps the call in `with self._lock:`; the Lock serializes all _get_conn access making the race impossible]** |
| C2 | **BUG** | `core/backup_repository.py:81-82` | `record_run_history` silently swallows all DB exceptions with `except Exception: logger.error(...)` and returns `None`. The caller (`flow.py:511-523`) never knows the run was not recorded. Run history is silently lost on DB errors. |
| C3 | **BUG** | `core/backup_repository.py:50-64` | `record_run_history` drops `files_failed` — the signature and `insert_run` schema both support it, but the function never accepts or passes it. Data loss. |
| S1 | **SECURITY** | `templates/dashboard.py:289-348` + `ui.py:610-626` | **Stored XSS** — 17 unescaped f-string interpolations in dashboard HTML. `r["mode"]` is spliced into CSS class attributes without `html.escape`. An attacker with DB write access can execute JS in the dashboard. |
| S2 | **SECURITY** | `ui.py:226` | **Login has no rate limit.** `_RATE_MAX_LOGIN = 10` is defined at `ui.py:40` but **never used** — an attacker can brute-force the API key at line speed. |

### High Priority

| # | Severity | File:Line | Finding |
|---|----------|-----------|---------|
| H1 | **DUPLICATION** | `watchdog.py:55-66` | Re-implements `core/logging.py:configure()` with different rotation/retention. Should call `from core.logging import configure as configure_logging`. |
| H2 | **DUPLICATION** | `watchdog.py:71-80` | Re-implements `core/process.py:pid_alive` with Windows `tasklist` + **substring match** `str(pid) in result.stdout`. Should use `pid_alive()` from `core.process`. |
| H3 | **FRAGILE CONTRACT** | `flow.py:604` vs `watchdog.py:51` | Lock file path mismatch: `flow.py` derives from `config.paths.database_path.parent / "backup.lock"`; `watchdog.py` hardcodes `C:\BackupAgent\backup.lock`. If DB path changes, watchdog bypasses the lock. |
| H4 | **DUPLICATION** | `core/fy_router.py` | 3-line re-export of `core.time_utils.get_fy_prefix` with zero consumer enforcement. Already flagged in `CODE_REVIEW_2026-06-01.md:365-376`. |
| H5 | **DUPLICATION** | `flow.py:380-382` vs `core/backup_repository.py:32-34` | Same entry-key normalization (`Path/Size/ModTime` → `path/size/mtime`) duplicated verbatim in 4 copies. |
| H6 | **DEAD CODE** | `core/process.py` | 8-line module. `pid_alive` has zero production call sites. Only used in `tests/test_ui.py`. |
| H7 | **DEAD CODE** | `core/manifest.py:245,263` | `mark_lan_synced` and `mark_cloud_synced` — zero production callers. Replaced by `bulk_upsert_synced`. |
| H8 | **DEAD CODE** | `core/manifest.py:15` | `SCHEMA_VERSION = 1` constant defined + `db_meta` table created, but **never read**. Migration logic uses `PRAGMA table_info()` instead. |
| H9 | **DEAD CODE** | `core/time_utils.py:60-74` | `format_iso_for_js` — zero call sites in repo. **[FALSE — used at ui.py:305, ui.py:311 in /status endpoint; imported at ui.py:30]** |
| H10 | **DEAD CODE** | `ui.py:40` | `_RATE_MAX_LOGIN = 10` defined but never used (login endpoint doesn't rate-limit — see S2). |
| H11 | **DEAD PARAM** | `flow.py:157` | `sync_result: dict` parameter in `cloud_record_task` accepted but never read. |

---

## 2. Bugs (Verified Against Source)

### C1 — Thread-unsafe `_get_conn` in ManifestDB

**File:** `core/manifest.py:77-78`  
**Severity:** ~~BUG~~ **FALSE POSITIVE**

**[FALSE — every one of the 17 call sites to `_get_conn()` is inside a `with self._lock:` block (verified by grep: `manifest.py:138,205,251,269,287,302,314,325,338,354,383,404,440,460,474,486,500`). The `threading.Lock` serializes all access to `_get_conn`, so two threads can never race on the `self._conn is None` check. The lock is acquired before `_get_conn()` is called, not inside it — and that's fine because the lock guards the entire read-modify-write sequence.]**

### C2 — Silent swallow in record_run_history

**File:** `core/backup_repository.py:81-82`  
**Severity:** BUG — run history lost silently on DB errors

```python
except Exception as e:
    logger.error(f"Failed to record run history: {e}")
```

The function returns `None` on both success and failure. The caller in `flow.py:_record_run` (L511-523) never checks the return value. If `insert_run` or `wal_checkpoint` fails, the run is simply not recorded — no retry, no alert, no data. Fix: re-raise, return a success flag, or let the caller handle the exception.

### C3 — Drop files_failed in record_run_history

**File:** `core/backup_repository.py:50-64`  
**Severity:** BUG — data loss of failed-file count

The `insert_run` schema (`manifest.py:398`) supports `files_failed`, and the `file_entries` tracking already records per-file sync states. But `record_run_history` (the canonical orchestrator) doesn't accept `files_failed` in its signature and never passes it.

### C4 — Non-atomic backup.lock Write

**File:** `flow.py:607-610`  
**Severity:** BUG — watchdog can read truncated PID on crash

```python
_lock_path = Path(config.paths.database_path).parent / "backup.lock"
try:
    _lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Non-atomic on Windows — if the process crashes mid-write, the file may
    # contain partial data. The watchdog handles this gracefully via ValueError
    # catch when parsing the PID, falling through to the process-existence check.
    _lock_path.write_text(str(os.getpid()))     # ← non-atomic write (L610)
```

The code itself acknowledges the problem in a comment at line 607-609. `Path.write_text()` does not atomically create the file — on Windows, if the process crashes between `open()` and `write()`, the file can contain a partial PID string (e.g., `"123"` instead of `"12345"`). The watchdog could then read a valid-but-wrong PID and either fail to detect the running process (false negative) or terminate the wrong process.

**Fix:** Write to a temp file in the same directory, then `os.replace()`. This guarantees the lock file is either the complete old content or the complete new content — never a partial write.

```python
import tempfile

_tmp = tempfile.NamedTemporaryFile(
    dir=_lock_path.parent, prefix=".backup.lock.",
    delete=False
)
try:
    _tmp.write(str(os.getpid()).encode())
    _tmp.close()
    os.replace(_tmp.name, _lock_path)
except:
    try:
        Path(_tmp.name).unlink(missing_ok=True)
    except OSError:
        pass
    raise
```

### C5 — TOCTOU Race on DB File Check

**File:** `ui.py:275-278`  
**Severity:** BUG — race window between existence check and DB open

```python
@app.get("/status")
async def status(request: Request):
    _require_auth(request)
    cfg = _cfg()
    if not Path(cfg.paths.database_path).exists():    # ← check (L275)
        return JSONResponse({"error": "ManifestDB not found"}, status_code=503)

    db = get_db()                                     # ← use (L278)
```

Between the `Path.exists()` check at line 275 and the `get_db()` call at line 278, another process can delete or move the database file. In that case `get_db()` will create a new empty connection to a non-existent path, which would succeed silently and return an empty database.

While harmlessly transient (the next request would see the file missing and return 503), this pattern violates the atomicity principle of check-then-act. In a concurrent scenario with the maintenance module purging old databases, the `/status` endpoint could briefly return misleading data.

**Fix:** Attempt to open the DB directly and catch `OperationalError` if the file doesn't exist.

```python
try:
    db = ManifestDB(cfg.paths.database_path)
except sqlite3.OperationalError:
    return JSONResponse({"error": "ManifestDB not found"}, status_code=503)
```

### C6 — Swapped Exit Codes in classifiy_rclone_exit Docstring

**File:** `core/cloud_sync.py:16-30`  
**Severity:** BUG (docs) — exit codes 1 and 2 descriptions swapped

```python
def classify_rclone_exit(code: int) -> str:
    """Classify rclone exit code per official documentation.

    0  → CLOUD_COMPLETE  (all files synced)
    1  → CLOUD_FAILED     (uncategorised error)    # ← per rclone docs: syntax/usage
    2  → CLOUD_FAILED     (syntax/usage)            # ← per rclone docs: error not otherwise categorised
    3  → CLOUD_FAILED     (directory not found)
    ...
    """
    mapping = {
        0: "CLOUD_COMPLETE",
        1: "CLOUD_FAILED",     # ← correct mapping
        2: "CLOUD_FAILED",     # ← correct mapping (both → same status)
        ...
    }
```

Per rclone's official exit code documentation:
- Exit code 1 = "Syntax or usage error"
- Exit code 2 = "Error not otherwise categorised"

The docstring has these swapped. The implementation mapping is correct (both exit codes map to `CLOUD_FAILED` since the distinction doesn't matter for the backup logic — any non-zero is a failure). But the documentation will mislead any developer reading the docstring who checks against rclone's reference.

**Fix:** Swap the parenthetical descriptions in the docstring.

### C7 — _cancel_orphaned_runs Cancels Active Flows Without Lock Check

**File:** `launch.py:104-126`  
**Severity:** BUG — can cancel a legitimate running backup

```python
async def _cancel():
    async with httpx.AsyncClient() as client:
        for state_type in (Pending(), Running()):
            try:
                runs = await client.post(
                    f"{PREFECT_API_URL}/flow_runs/filter",
                    json={...}
                )
                runs = [FlowRun(**r) for r in runs.json()]
                for r in runs:
                    try:
                        await client.set_flow_run_state(
                            flow_run_id=r.id,
                            state=Cancelled(message="Cancelled orphaned run on service startup"),
                            force=True,
                        )
```

This function runs at startup and cancels all `Pending` and `Running` flow runs. It does **not** check whether `backup.lock` exists with a valid PID. On a normal service restart (e.g., after a watchdog-triggered reboot), the lock file is cleaned up by the `finally` block in `flow.py:660-666` — but only if the flow process actually reached that block. If Prefect crashed hard (e.g., `kill -9` on Linux, or `TerminateProcess` on Windows), the flow run stays as `RUNNING` in Prefect's state store while the actual process is gone. In that case, cancelling on restart is correct.

However, if the watchdog restarts the service **while a backup is still running** (e.g., the lock file check race — see H3), `_cancel_orphaned_runs` will cancel a legitimate in-progress backup. The function should verify that the PID in `backup.lock` (if it exists) is not alive before cancelling `RUNNING` flows.

**Fix:** Read the PID from `backup.lock` and verify it's dead before cancelling `RUNNING` flows. Keep the unconditional cancel for `Pending` flows (they haven't started yet).

```python
async def _cancel():
    lock_path = Path(config.paths.database_path).parent / "backup.lock"
    active_pid = _read_lock_pid(lock_path)   # returns None if no lock
    
    for state_type in (Pending(), Running()):
        # ... filter runs ...
        for r in runs:
            if state_type == Running() and active_pid is not None:
                continue  # lock held — backup may still be active
            # ... cancel ...
```

### C8 — Missing Log Tail on LAN PARTIAL

**File:** `core/lan_sync.py:107-112`  
**Severity:** BUG (UX) — user sees no details when LAN sync partially fails

```python
status = classify_exit_code(result.returncode)
logger.info(f"LAN sync exit {result.returncode} → {status}")

error_msg = None
if status == "LAN_FAILED":                    # ← only FAILED gets log tail (L107)
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        error_msg = log_text[-500:] if len(log_text) > 500 else log_text
    except OSError:
        error_msg = f"robocopy exit {result.returncode} (log unreadable)"

return {
    "status": status,
    "exit_code": result.returncode,
    "error": error_msg,
}
```

When robocopy exits with a `PARTIAL` status (some files failed to copy but the sync completed — exit code 2-5 in robocopy's scheme), the function returns `"error": None`. The log tail containing which files failed is never read. The caller (`flow.py:484-486`) records the result but sees no error detail.

**Fix:** Also read the log tail on `LAN_PARTIAL`.

```python
if status in ("LAN_FAILED", "LAN_PARTIAL"):
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        error_msg = log_text[-500:] if len(log_text) > 500 else log_text
    except OSError:
        error_msg = f"robocopy exit {result.returncode} (log unreadable)"
```

---

## 3. Security Vulnerabilities

### S1 — Stored XSS in Dashboard

**File:** `templates/dashboard.py:289-348` + `ui.py:610-626`  
**Severity:** HIGH — attacker with DB write access can execute JS in admin dashboard

- `templates/dashboard.py` uses **raw f-strings** for all 17 HTML interpolations. None are escaped. The responsibility is pushed to `ui.py`.
- `ui.py:611` splices `r["mode"]` into a CSS class attribute: `f'class="tag {r["mode"]}"'` — no escaping.
- `ui.py:625` splices `r.get("error_message", "")[:60]` — no `html.escape()`.
- The JS in `templates/dashboard.py:194-195` also splices `r.mode` into a CSS class unescaped: `'<span class="tag ' + r.mode + '">'`.
- Mitigation: dashboard binds to `127.0.0.1` with auth, but the risk is session theft / CSRF pivot by a local attacker.

### S2 — No Rate Limiting on Login

**File:** `ui.py:40, 226-239`  
**Severity:** HIGH — brute-force of API key at line speed

```python
_RATE_MAX_LOGIN = 10   # ← defined but NEVER referenced (L40)
...
async def login_submit(request: Request):  # ← no _check_rate_limit call (L226)
```

The `_check_rate_limit` function and the `_RATE_MAX_LOGIN` constant are both present, but the login handler never calls the rate limiter. The trigger/report endpoints DO use it. This is a copy-paste omission: the login path was simply never wired up.

---

## 4. Duplications and Structural Issues

### Flow-level duplication

| Pattern | Locations | Fix |
|---------|-----------|-----|
| Entry-key normalization (`Path/Size/ModTime` → `path/size/mtime`) | `backup_repository.py:32-34` + `flow.py:380-382` — 4 copies of same 3-line pattern | Extract `normalize_entry(e: dict) -> dict` to `core/backup_repository.py` |
| LAN snapshot tasks (before/after) | `flow.py:197-202, 206-212` — identical except log label | Collapse into one `lan_snapshot_task(config, label)` |
| Artifact publishing tasks (cloud/LAN) | `flow.py:263-292, 296-327` — 30-line copy-paste | One `_publish_artifact(mode, payload)` |
| Trigger endpoints (cloud/lan) | `ui.py:339-347, 351-359` — 9-line copy-paste | `_trigger(pipeline, request, background_tasks)` |
| Email-trigger endpoints (weekly/monthly) | `ui.py:387-421, 424-458` — 35-line copy-paste  | One parameterised function |
| Report endpoints (weekly/monthly) | `ui.py:365-372, 375-382` | `_report_endpoint(days, period, request)` |
| ManifestDB open/close pattern | `flow.py` — 7 sites of `ManifestDB(path) + try/finally db.close()` | Add `__enter__/__exit__` to ManifestDB; use `with ManifestDB(path) as db:` |
| Subprocess + temp log file + 3-exception handler | 7 files (lan_sync, lan_preflight, cloud_sync, cloud_preflight, cloud_verify, cloud_reporter, shutdown) | Extract `core/subprocess_runner.py` helper |
| Temp file mkstemp+close+unlink in finally | 5 files | Extract `core/tempfile_helpers.py` `temp_path(suffix, prefix)` context manager |
| Final backup orchestrator (`run_final_backup`) | `fy_rollover.py:69-136` — reimplements WoL→sync→shutdown from `flow.py:436-497` | Extract shared helper |

### Watchdog vs core/* duplication

The watchdog module independently reimplements:
- `_configure_logging` ↦ `core/logging.py:configure()` — different rotation policy, no Prefect bridge
- `_pid_is_alive` ↦ `core/process.py:pid_alive()` — different implementation (tasklist vs psutil), loses cross-platform support
- Lock path — see H3 above

### serve.py indirection

`serve.py:16-18` declares `def deployments()` which is a pure delegation to `def _deployments()`. The only difference is a docstring. `launch.py:196` imports `from serve import deployments`; `__main__` calls `_deployments()` inconsistently.

---

## 5. Anti-Patterns and Best-Practices Violations

### Database Layer (`core/manifest.py`)

- **SQL injection risk (mitigated but brittle):** `f"{mode}_status"` f-string interpolation appears at L201, 212, 223, 291, 327, 352, 356, 376, 385. All safe because `mode` is validated against `{"cloud","lan"}` before use, but if a future maintainer adds `"all"` to the allowlist, the column name `all_status` doesn't exist and the query silently returns nothing. **Fix:** replace with `_STATUS_COL = {"cloud": "cloud_status", "lan": "lan_status"}` dict lookup.
- **Missing context manager:** 19 manual `with self._lock: conn = ... ; conn.commit()` blocks. sqlite3 connections support `with conn:` which auto-commits on success and rolls back on exception.
- **Missing CHECK constraints:** `lan_status` / `cloud_status` columns are TEXT with no CHECK constraint. A typo like `"sync"` inserts silently.
- **chunk size at limit:** `bulk_upsert_synced` chunks at 100 rows × 8 columns = 800 params. Adding 3 more columns would silently hit `SQLITE_MAX_VARIABLE_NUMBER=999`.

### Flow Layer (`flow.py`)

- **Redundant `diff_snapshots` call:** `_run_lan_pipeline` (L467) re-computes the diff that `lan_record_task` (L232) already computed. An O(n) waste of CPU.
- **Missing return type annotations:** All `@task` and `@flow` functions lack return type annotations. Undermines Prefect's runtime type validation.
- **Inline entry normalization:** `_run_cloud_pipeline` (L378-405) duplicates `backup_repository.py:32-34` with additional mtime comparison branching (3 branches for numeric/ISO/string).

### UI Layer (`ui.py`)

- **God function:** `_render_dashboard` (L548-663) is **115 lines** mixing DB queries, Prefect API calls, inline HTML row construction, and template rendering.
- **Dead try/finally:** L627 `finally: pass # singleton — do not close` — the entire block is `if db: <code>`, the try/finally does nothing.
- **Fail-open on Prefect API error:** `_prefect_has_active_run` (L166) returns `False` on any exception, meaning if Prefect is down the UI thinks no backup is running and allows duplicate triggers.
- **Magic numbers:** `[:60]` truncation appears 3 times (L579, 584, 625) — should be a named constant.
- **Fail-open health endpoint:** `health() -> {"status":"healthy"}` returns healthy even if Prefect integration or DB is broken.
- **Missing OpenAPI annotations:** FastAPI handlers without return type annotations generate incomplete OpenAPI schemas.
- `_is_running` (L140) is a pure 1-line wrapper around `_prefect_has_active_run` — adds zero value.

### Security Patterns

- **No env-var indirection for secrets:** SMTP password and API key are stored in `config.yaml` only. No `${VAR}` substitution. `models/config.py:12` hardcodes `CONFIG_PATH = "config.yaml"`.
- **XSS in login page (low risk):** `ui.py:197` interpolates `error` without `html.escape` — currently safe because the only caller passes the static string `"Invalid+API+key"`, but fragile.
- **`config.example.yaml:86`** — `smtp_password` in plaintext (gitignored, but on-disk exposure risk).

### Testing

- **`_mock_result` fixture duplicated** in `tests/test_cloud_reporter.py:10-15` and `tests/test_cloud_verify.py:9-14` — belongs in `conftest.py`.
- **No test for offline-share path** in `lan_manifest.py` — `walk_lan_destination` on an unreachable UNC raises an uncaught `OSError`.
- **`launch.py` has no unit tests** — the `100-line test_launch.py` file exists but the module's side effects at import (PREFECT_API_URL mutation) make it difficult to test.

### Config & Ops

- **Two YAML parsers:** `models/config.py` uses PyYAML (`import yaml`), but `pyproject.toml:21` lists `ruamel.yaml>=0.18.0` as a dependency (used only in `core/fy_rollover.py:update_config_yaml`). Inconsistent.
- **`launch.py:22` mutates `os.environ` at import:** `os.environ["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"` — any code importing `launch` gets a global side effect.
- **No hash verification on NSSM download:** `deploy/download_nssm.py:24-35` downloads `nssm.exe` from Chocolatey CDN over HTTPS but does not verify SHA-256. Binary runs as Administrator. **Supply-chain risk.**
- **Cross-platform mismatch:** The project is Windows-only (NSSM, `shutdown.exe`, `tasklist`, `sc`, UNC paths) but `pyproject.toml` declares no platform constraint.

---

## 6. Schema Design Review

### file_entries table

| Column | Type | Issue |
|--------|------|-------|
| `relative_path` | TEXT NOCASE PK | ✓ Good — Windows case-insensitive collation |
| `lan_status` | TEXT DEFAULT 'unknown' | **Missing CHECK** — accepts `"sync"` typo silently |
| `cloud_status` | TEXT DEFAULT 'unknown' | **Missing CHECK** — same issue |
| `lan_last_synced_at` | TEXT | **Documentation gap:** preserved on first sync only, not updated on re-sync (CASE WHEN preserves first timestamp). Column name implies "most recent". |
| `cloud_last_synced_at` | TEXT | Same design as above |

### run_history table

| Column | Type | Issue |
|--------|------|-------|
| `files_failed` | INTEGER | Schema has it, `insert_run` param supports it, but `record_run_history` (the only production writer) never passes it. **Dead schema column.** |
| `extended_metrics` | TEXT | Added via ad-hoc `ALTER TABLE` migration at `manifest.py:104-110`. Not documented in `IMPLEMENTATION_PLAN.md`. |
| `run_id` | TEXT | UNIQUE INDEX (L57), upsert via `ON CONFLICT(run_id) DO UPDATE`. Schema versioning not checked. |

### Indexes

- `file_entries` on `lan_status` ✓
- `file_entries` on `cloud_status` ✓
- `run_history` on `started_at` ✓
- `run_history` on `run_id` UNIQUE ✓

### Migration Strategy

- `SCHEMA_VERSION = 1` defined, `db_meta` table created and seeded, but **no code reads it**.
- Actual migration: `PRAGMA table_info()` + ad-hoc `ALTER TABLE` inside `_get_conn`.
- **Two processes starting simultaneously:** both run the dedup migration — `DELETE FROM run_history WHERE id NOT IN (SELECT MIN(id) ... GROUP BY run_id)`. Concurrent DELETEs are safe (each deletes different rows), but the `CREATE UNIQUE INDEX` after can fail with `SQLITE_BUSY` on the second process. The 30s `busy_timeout` mitigates this.

### Thread Safety

- `threading.Lock()` used for all reads AND writes → serializes all access. With WAL mode + `check_same_thread=False`, readers could in theory run concurrently. Current design is correct but **suboptimal for multi-threaded read-heavy workloads** (e.g., the dashboard calling `file_count` 4 times per request).
- Three thread-safety bugs found: C1 (`_get_conn` race — inner-thread), C5 (TOCTOU on DB file check — inter-process), C7 (orphaned-run cancel without lock check — inter-process).

---

## 7. Cross-Reference: Graphify + GitNexus Verification

### Graphify's 85 INFERRED Edges for ManifestDB

The graphify report flagged 85 inferred edges for `ManifestDB` as needing verification. **Finding: Most are correct.** The inferred edges to `get_db()`, `cloud_record_task()`, and `ui.py` functions are conceptually correct because ManifestDB is the data backbone. The inference quality is high (confidence 0.79 average). The one false-positive cluster: `ManifestDB` connecting to test infrastructure symbols — those are test-only imports, not architectural dependency.

### Graphify Community Analysis

| Community | Theme | Audit Finding |
|-----------|-------|---------------|
| 0 (Manifest DB writes) | DB operations | Correct — covers all `record_sync_results`, `insert_run` paths |
| 1 (Dashboard UI) | FastAPI + reports | Correct — covers all `ui.py` + `report.py` |
| 7 (Flow tasks) | `@task` definitions | Correct — `flow.py` top section |
| 13 (Launch script) | Services start | Correct — covers `launch.py` |

### GitNexus Execution Flows (89 processes)

| Process | Risk if changed | Audit finding |
|---------|----------------|---------------|
| `backup` | HIGH — entry point, called by Prefect deployment | Lock protocol fragile (H3); ExceptionGroup handling is correct |
| `_run_cloud_pipeline` | HIGH — 7 sub-calls | Correct orchestration; entry normalization duplicated (H5) |
| `_run_lan_pipeline` | HIGH — 9 sub-calls | `diff_snapshots` called twice; shutdown intentional in try block |
| `ManifestDB` | **CRITICAL** — 10 importers, 20 methods | 3 bugs found (C1, C2, C3); thread-safe design except C1 |

### Dead Code Flagged by Both

Both knowledge graphs independently confirm:
- `core/fy_router.py` — isolated node with ≤1 connection (graphify flagged "Thin community") ✓
- `core/process.py` — isolated node ✓
- `mark_lan_synced` / `mark_cloud_synced` — called by 0 production files ✓

---

## 8. Recommendations Summary

### Critical (must-fix)

| ID | File:Line | Effort | Fix |
|----|-----------|--------|-----|
| C1 | manifest.py:78 | 15 min | Move `self._conn is None` inside `with self._lock:` |
| C2 | backup_repository.py:81-82 | 10 min | Re-raise exception; update caller to handle it |
| C3 | backup_repository.py:50-64 | 10 min | Add `files_failed` parameter and pass to `insert_run` |
| C4 | flow.py:610 | 15 min | Write lock via tempfile + `os.replace()` for atomic PID write |
| C5 | ui.py:275 | 5 min | Remove `Path.exists()` check; catch `OperationalError` instead |
| C6 | cloud_sync.py:20-21 | 2 min | Swap exit-code descriptions in docstring |
| C7 | launch.py:122-126 | 30 min | Verify `backup.lock` PID is dead before cancelling RUNNING flows |
| C8 | lan_sync.py:107 | 5 min | Also read log tail on `LAN_PARTIAL` |
| S1 | templates/dashboard.py + ui.py:610-626 | 1 hr | Escape all 17 interpolations with `html.escape()`; validate `r.mode` against `{"cloud","lan"}` before CSS class interpolation |
| S2 | ui.py:226 | 10 min | Add `_check_rate_limit(client_ip, _RATE_MAX_LOGIN)` to `login_submit` |

### High Priority (1-2 weeks)

| ID | File:Line | Effort | Fix |
|----|-----------|--------|-----|
| H1 | watchdog.py:55-66 | 15 min | Replace with `from core.logging import configure as configure_logging` |
| H2 | watchdog.py:71-80 | 15 min | Replace with `from core.process import pid_alive` |
| H3 | flow.py:604 / watchdog.py:51 | 15 min | Extract `BACKUP_LOCK_PATH` shared constant to `core/paths.py` |
| H4 | core/fy_router.py | 15 min | Delete file; update 4 import sites |
| H5 | flow.py:380-382 | 30 min | Extract `normalize_entry()` to `core/backup_repository.py` |
| H6 | core/process.py | 5 min | Delete (zero prod callers) |
| H7 | manifest.py:245,263 | 15 min | Delete methods (replaced by `bulk_upsert_synced`) |
| H8 | manifest.py:15 | 30 min | Either implement `db_meta.schema_version` checking or remove dead constant + table |
| H9 | time_utils.py:60-74 | 5 min | Delete dead `format_iso_for_js` **[FALSE — actually used at ui.py:305,311]** |
| H10 | ui.py:40 | 10 min | Delete dead `_RATE_MAX_LOGIN` or wire it into login (prefer wiring → S2 fix) |
| H11 | flow.py:157 | 5 min | Remove unused `sync_result` parameter |

### Structural Quality (up to 1 month)

- Extract `core/subprocess_runner.py`, `core/tempfile_helpers.py`, `core/lock_paths.py` — eliminates 7+ copies of boilerplate
- Add `__enter__/__exit__` to `ManifestDB` — eliminates 7 `try/finally db.close()` sites
- Normalize return types across health checks (`check_binary_exists` returns `bool` while siblings return `tuple[bool, str]`)
- Replace `f"{mode}_status"` pattern with `_STATUS_COL = {"cloud": "cloud_status", "lan": "lan_status"}` dict
- Add `CHECK (lan_status IN ('unknown','synced','failed','deleted'))` constraints
- Consolidate cloud/LAN trigger/report/email endpoints (saves ~60 lines of copy-paste in `ui.py`)
- Add env-var substitution in `AppConfig.from_yaml` for SMTP password and API key
- Pin NSSM version + SHA-256 in `deploy/download_nssm.py`
- Move `os.environ["PREFECT_API_URL"]` from module-level to inside `main()` in `launch.py`
- Add `classifiers = ["Operating System :: Microsoft Windows"]` to `pyproject.toml`
- Move `humanize>=4.0` to explicit dependency in `pyproject.toml`
- Add server-identity verification in `shutdown.py` (verify shutdown target matches WoL MAC's IP)
- Apply exponential backoff to WoL polling in `wol.py:wait_for_server`
- Add `shutdown_delay_seconds` to `LanConfig` (currently hardcoded 300)

---

## 9. Code Quality Metrics

| Metric | Value |
|--------|-------|
| Source files | 28 |
| Total source lines | ~5,300 |
| Test files | 27 |
| Total test lines | ~4,500 |
| Functions (gitnexus) | 2,095 symbols |
| Execution flows (gitnexus) | 89 |
| Communities (graphify) | 74 |
| Semantic graph nodes (graphify) | 1,110 |
| Semantic graph edges (graphify) | 1,929 |
| **Bugs found** | **8** (C1–C8) |
| **Security vulns** | **2** (S1, S2) |
| **Dead code modules** | **2** (`fy_router.py`, `process.py`) |
| **Dead code functions** | **6** (2 mark_*_synced, format_iso_for_js, _RATE_MAX_LOGIN, _is_running wrapper, sync_result param) |
| **Function-local import violations** | **4** (flow.py:544, 564; ui.py:398, 435, 470) |
| **Bare except: pass** | **12** (watchdog.py:80, 119, 139, 164; flow.py:418, 540, 560; ui.py many) |
| **Missing return type annotations** | **~50 public functions** (all @task, @flow, FastAPI handlers) |
| **`try/finally db.close()` patterns** | 7 sites (candidate for context manager) |
| **Knowledge graph agreement** | Both indexes flag same dead-code and same god-nodes — **high consistency** |

---

## 10. Appendix: Files Not Analyzed

- `core/__init__.py` (7 lines) — trivial re-export
- `templates/__init__.py` (0 lines) — empty
- `models/__init__.py` (5 lines) — trivial
- Test files were referenced for coverage and duplication but not audited for correctness (except where duplication with production code was suspected)

---

## 11. Patch: Additional Findings (Initial Omissions)

The following findings were reported by individual audit agents but omitted from the initial report. Bugs C4–C8 are now expanded with full code snippets and analysis in **Section 2** above. This section covers the remaining non-bug omissions.

### Bugs Promoted to Section 2

C4–C8 were promoted from this table into full prose sections (see Section 2 above). Summary for quick reference:

| ID | File:Line | Short description | Fix effort |
|----|-----------|-------------------|------------|
| C4 | `flow.py:610` | Non-atomic `backup.lock` write — partial PID on crash | 15 min |
| C5 | `ui.py:275` | TOCTOU race between `Path.exists()` and `get_db()` — check-then-act | 5 min |
| C6 | `cloud_sync.py:20-21` | `classify_rclone_exit` docstring — exit codes 1 and 2 descriptions swapped | 2 min |
| C7 | `launch.py:122-126` | `_cancel_orphaned_runs` cancels RUNNING flows without lock-PID check | 30 min |
| C8 | `lan_sync.py:107` | Log tail only on FAILED, not PARTIAL — user sees no error detail | 5 min |

### Additional Duplications (Add to Section 4)

| Pattern | Locations | Fix |
|---------|-----------|-----|
| `f"aam_gcs:{bucket}/{fy_prefix}"` dest string | `cloud_sync.py:56`, `cloud_preflight.py:70`, `cloud_verify.py:64`, `cloud_reporter.py:60,92,119` — **6 copies** | Extract `_gcs_dest(bucket, fy_prefix)` helper to `core/cloud_common.py` |
| Cloud preflight builds own config; verify/report take `config_path` | `cloud_preflight.py:44-49` vs `cloud_verify.py:34` / `cloud_reporter.py:32` | Standardize all three to accept `config_path` |

### Additional Anti-Patterns with Code Examples (Add to Section 5)

**1. `wol.py:30-36` — OSError wrapping loses traceback**

```python
def _send_magic_packet(mac_address: str) -> None:
    try:
        wol_send(mac_address, ip_address="255.255.255.255", port=9)
        logger.info(f"WoL magic packet sent to {mac_address}")
    except OSError as e:
        raise OSError(f"Failed to send WoL packet: {e}")  # ← wraps same type, loses __cause__
```

Catches `OSError` only to raise a new `OSError` with a stringified message. The original traceback and `__cause__` chain are discarded — any post-mortem debugging sees only the wrapper, not the underlying socket error. **Fix:** use bare `raise` to re-raise the original, or `raise ... from e` to chain.

**2. `flow.py:665` — OSError:pass on cleanup**

```python
finally:
    try:
        _lock_path.unlink(missing_ok=True)
        logger.info("Backup lock released")
    except OSError:
        pass  # ← silent swallow (L665-666)
```

The cleanup of `backup.lock` silently swallows all `OSError` cases (permission denied, path is a directory, locked by another process, etc.). If the lock can't be released, the next backup will see a stale lock file and skip. At minimum a `logger.warning()` is needed.

**3. `flow.py:618` — No logging on concurrency acquisition failure**

```python
with concurrency("aam-backup", occupy=1, timeout_seconds=3600):  # ← 1 hour silent wait
```

If the concurrency slot is held by another backup instance, this blocks for up to 3600 seconds without any progress log. The operator sees zero output for an hour before the timeout fires. **Fix:** wrap the concurrency context manager with periodic heartbeat logging (or reduce timeout + loop with exponential backoff + status log).

**4. `launch.py:142-150` — Off-by-one in timer math**

```python
for _elapsed in range(0, _API_MAX_WAIT, _API_INTERVAL):  # range(0, 90, 10) → 0,10,...,80 = 9 iters
    if _check_prefect_api():
        ...
        break
    _remaining = _API_MAX_WAIT - _elapsed - _API_INTERVAL  # 90 - 80 - 10 = 0 (last iter) ✓
    time.sleep(_API_INTERVAL)
```

Semantic confusion: the variable is named `_elapsed` and used in the `_remaining` calculation as if it tracks wall-clock time, but it's actually the loop counter. With `range(0, 90, 10)`, the loop runs 9 iterations with 9 × 10 = 90 seconds of sleep, which is correct — but the `_remaining` goes 80, 70, ..., 0 and never reaches the intended starting value of 90. The display is off by one interval.

**Fix:** Use a wall-clock timer instead of loop-counter math:

```python
start = time.time()
while time.time() - start < _API_MAX_WAIT:
    if _check_prefect_api():
        print(f"[launch] Prefect API ready (waited {time.time() - start:.0f}s)")
        break
    remaining = int(_API_MAX_WAIT - (time.time() - start))
    print(f"[launch] Not ready yet — retrying in {_API_INTERVAL}s ({max(0, remaining)}s remaining)...")
    time.sleep(_API_INTERVAL)
```

**5. `cloud_sync.py:118-120` — Contradictory subprocess flags**

```python
result = subprocess.run(
    cmd,
    stdout=subprocess.DEVNULL,  # ← discard stdout
    stderr=stderr_file,         # ← capture stderr to file
    text=True,                  # ← ...but decode both streams as text?
    timeout=timeout,
)
```

`text=True` tells subprocess to decode stdout and stderr through the universal newlines/encoding layer. But stdout is redirected to `DEVNULL` (never read) and stderr is redirected to a file opened in text mode with `encoding="utf-8"`. This means the `text=True` flag only affects the unread stdout stream. Harmless but confusing — a future reader may add `result.stdout` expecting text and get `None`.

**Fix:** Remove `text=True` since neither stream is read through the return object.

**6. `cloud_sync.py:63,106-108` — storage_class passed twice**

```python
# In build_rclone_sync_command (L63):
"--gcs-storage-class", storage_class,   # ← flag on command line

# In temp_rclone_config called from run_cloud_sync (L105-107):
with temp_rclone_config(
    gcs_key_path, location, project_number, storage_class  # ← same value in INI config
) as config_path:
```

The `storage_class` parameter is passed both as a command-line flag (`--gcs-storage-class`) and as an INI-file config entry in `temp_rclone_config`. Rclone gives precedence to the command-line flag, so the INI value is ignored. **Fix:** Remove the parameter from `temp_rclone_config`; pass only via command line.

**7. `manifest.py:437` — `get_runs_since` has no LIMIT**

```python
def get_runs_since(self, cutoff_iso: str) -> list[dict]:
    with self._lock:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM run_history WHERE started_at >= ? ORDER BY started_at",
            (cutoff_iso,),
        ).fetchall()
```

Over years of daily backups (365+ rows per year), this query returns an ever-growing result set. Every caller currently limits the data themselves in Python after fetching everything. **Fix:** add a default `LIMIT 1000` with an optional `limit` parameter.

**8. `manifest.py:389-392` — Unconditional DELETE on prune call**

```python
rows = conn.execute("SELECT id FROM run_history WHERE ...").fetchall()
if len(rows) > retention_count:
    ids_to_delete = [r[0] for r in rows[:-retention_count]]
    conn.execute(
        "DELETE FROM run_history WHERE id IN ({})".format(",".join("?" * len(ids_to_delete))),
        ids_to_delete,
    )
```

The `DELETE` runs whenever the SELECT returns more rows than `retention_count`, even if the difference is just 1 row. And the SELECT runs even when there's nothing to prune. **Fix:** guard with `if len(rows) > retention_count + 100:` to batch less aggressively. Or use a LIMIT on the DELETE itself.

**9. `manifest.py:288-294` — Inconsistent chunk sizes**

```python
# bulk_upsert_synced chunks at 100:
for i in range(0, len(entries), 100):

# delete_entries chunks at 500:
for i in range(0, len(active_paths), 500):
```

Two chunk sizes for the same table. Should use a shared `_CHUNK_SIZE = 100` constant. The smaller value (100) is correct — 100 rows × 8 columns = 800 params, safely under the `SQLITE_MAX_VARIABLE_NUMBER=999` limit. The larger (500 × 1 column = 500 params) is also safe but inconsistent.

**10. `manifest.py:360-394` — `prune_stale_synced` double-lock window**

```python
def prune_stale_synced(self):
    with self._lock:              # ← lock acquired
        conn = self._get_conn()
        ids = conn.execute("SELECT id FROM run_history WHERE ...").fetchall()
        # lock released here (lock scope ends)
    # ...process ids...
    with self._lock:              # ← lock re-acquired
        conn.execute("DELETE FROM run_history WHERE ...")
```

Between the SELECT and the DELETE, the lock is released. A concurrent thread can insert new run_history rows that match the SELECT criteria. The DELETE misses those rows. Not a data-loss bug (the new rows are valid history) but an inconsistency: the prune operation leaves more rows than intended. **Fix:** hold the lock across both SELECT and DELETE.

### Additional Dead / Redundant Code

- **`ui.py:500` — `pendulum.now().format("YYYY-MM-DD HH:mm:ss")`** instead of `time_utils.utcnow_formatted()`. Bypasses the abstraction.
- **`wol.py:10` — `import wakeonlan as wol_send`** — alias adds no value, hurts grep-ability.

### Minor Security (Add to Section 3)

- **`ui.py:249-262` — `_require_auth` uses `Accept` header for routing:** an attacker can bypass the `text/html` check by omitting the header. Auth is still enforced, but fragile.
- **`check_health.py:59-67` — `check_clock_skew` silently returns 0 on network error:** NTP unreachable → skew check passes as "no skew". False negative masks time-dependent auth failures.

### Updated Metrics

| Metric | Section 9 value | Note |
|--------|-----------------|------|
| **Bugs found** | ~~8 (C1–C8)~~ **7** | C1 flagged false; lock serializes all _get_conn calls |
| **Security vulns** | **2** (S1, S2) | unchanged |
| **Race conditions** | ~~3 (C1, C5, C7)~~ **2** | C1 removed; actual races: C5 (TOCTOU), C7 (orphaned cancel) |
| **Total agent findings** | ~80 | all 7 agents consolidated; 2 false positives tagged |
