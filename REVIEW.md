# Code Review: AAM Backup Automation V1

## Summary
- **Total files reviewed**: 17
- **BLOCKER findings**: 2 (✅ 2 fixed)
- **WARNING findings**: 10 (✅ 8 fixed, ⬚ 2 deferred)
- **Review depth**: standard (all source files + cross-file analysis)

---

## Findings

### BLOCKER: Prefect task retries create duplicate run_history records per attempt
**File**: `flow.py:49` (cloud_backup_task), `flow.py:154` (lan_backup_task)
**Status**: ✅ FIXED
**Fix applied**: Removed `@task(retries=N)` decorators from both tasks. Moved retry logic into the function body via an inner loop with `max_attempts` and `time.sleep(retry_delay)`. The single `finally` block records exactly one entry per logical backup run with the final outcome. Cloud: 3 attempts (original + 2 retries), LAN: 2 attempts (original + 1 retry).

### BLOCKER: Dashboard lock released immediately — "Running" status is misleading
**File**: `ui.py:53-100` (_is_running)
**Status**: ✅ FIXED
**Fix applied**: `_is_running()` now checks two sources: the short-lived lock file (for double-click prevention on trigger), then the Prefect API (`prefect flow-run ls --state RUNNING`) for active flow runs matching the deployment name. The dashboard now correctly shows "Running" throughout the backup. Also added `_pid_alive()` with Windows-specific `tasklist` fallback to replace the broken `os.kill(pid, 0)` on Windows.

---

### WARNING: Clock skew check compares two calls to the same clock
**File**: `core/health.py:68-87` (check_clock_skew)
**Status**: ✅ FIXED
**Fix applied**: Replaced the `datetime.now() vs time.time()` comparison with an HTTPS request to `www.googleapis.com`. Compares the `Date` HTTP header (parsed via `email.utils.parsedate_to_datetime`) against `datetime.now(timezone.utc)`. This actually detects clock drift relative to Google's servers — the servers that will reject JWT tokens if skew exceeds 10 minutes. Falls back to passing on network errors.

### WARNING: verify_checksum passes through "pending" without actual verification
**File**: `core/hashing.py:18-24` (verify_checksum)
**Status**: ✅ FIXED
**Fix applied**: Returns `False` for `PENDING_CHECKSUM` instead of `True`. Callers now get a correct signal that the file hasn't been verified. This function is not currently called in production code (only declared/exposed), so no existing callers are broken.

### WARNING: SMTP connection leaked on sendmail failure
**File**: `core/report.py:47-54` (_send_email)
**Status**: ✅ FIXED
**Fix applied**: Initialized `server = None` before the try block, and added `if server is not None: server.quit()` in the except handler. The SMTP connection is now properly closed on both success and failure paths.

### WARNING: LAN dry-run treats robocopy exit codes 8-15 as "ok"
**File**: `core/lan_preflight.py:41-42` (run_lan_dry_run)
**Status**: ✅ FIXED
**Fix applied**: Changed threshold from `code < 16` to `code < 8`. Exit codes 8-15 (which include bit 3 = copy errors) now fail the preflight. This is consistent with `lan_sync.classify_exit_code()` which treats bit 3 as `LAN_PARTIAL`.

### WARNING: Hardcoded COLDLINE storage class in rclone sync command
**File**: `core/cloud_sync.py:90` (build_rclone_sync_command)
**Status**: ✅ FIXED
**Fix applied**: Added `storage_class` parameter to `build_rclone_sync_command()` and `run_cloud_sync()`. The caller in `flow.py` now passes `config.cloud.storage_class`. The shared `rclone_config.write_temp_config()` also accepts `storage_class`. Default remains `"COLDLINE"`.

### WARNING: os.kill(pid, 0) unreliable on Windows for liveness check
**File**: `ui.py:54-59` (_is_running)
**Status**: ✅ FIXED
**Fix applied**: Extracted `_pid_alive()` function with dual-check: `os.kill(pid, 0)` first, then on Windows falls back to `tasklist /FI "PID eq N" /NH`. Same fix applied as part of BLOCKER #2 refactor.

### WARNING: No authentication on dashboard or trigger endpoints
**File**: `ui.py:121-140` (trigger endpoints)
**Status**: ⬚ DEFERRED
**Rationale**: The dashboard is intended for internal LAN use and bound to `0.0.0.0` for convenience. Adding auth requires an infrastructure decision (API key, BasicAuth, OAuth, or bind to localhost). If external exposure is a concern, the simplest fix is to bind to `127.0.0.1` in `launch.py`. Will address if remote access becomes a requirement.

### WARNING: Duplicate rclone temp config functions across modules
**File**: `core/cloud_preflight.py:17-39`, `core/cloud_sync.py:16-38`
**Status**: ✅ FIXED
**Fix applied**: Created `core/rclone_config.py` with a single `write_temp_config()` function. Both `cloud_preflight.py` and `cloud_sync.py` now import from it. `cloud_preflight.py` re-exports it as `_write_temp_config` for backward compatibility with `flow.py` and `test_cloud.py` callers.

### WARNING: Dashboard templating inline in Python
**File**: `ui.py:160-395` (_render_dashboard, _CSS)
**Status**: ⬚ DEFERRED
**Rationale**: Extracting to Jinja2 templates would add a `templates/` directory and `jinja2` dependency. The inline HTML is functional and only ~230 lines. Worth doing if the dashboard UI grows, but not critical for correctness.

### WARNING: backup() flow catches task exceptions and re-raises a single RuntimeError
**File**: `flow.py:321-353` (backup flow)
**Status**: ✅ FIXED
**Fix applied**: Changed from accumulating string error messages and raising `RuntimeError` to accumulating actual `Exception` instances and raising `ExceptionGroup` (Python 3.11+). Prefect now sees the original exception types with stack traces. If cloud and LAN both fail, both exceptions are preserved in the group.

---

## Files Reviewed
- [x] `launch.py`
- [x] `serve.py`
- [x] `flow.py`
- [x] `ui.py`
- [x] `config.yaml`
- [x] `models/config.py`
- [x] `core/cloud_preflight.py`
- [x] `core/cloud_sync.py`
- [x] `core/cloud_verify.py`
- [x] `core/cloud_reporter.py`
- [x] `core/lan_preflight.py`
- [x] `core/lan_sync.py`
- [x] `core/lan_manifest.py`
- [x] `core/manifest.py`
- [x] `core/report.py`
- [x] `core/health.py`
- [x] `core/shutdown.py`
- [x] `core/wol.py`
- [x] `core/hashing.py`
- [x] `core/logging.py`
- [x] `core/fy_router.py`
- [x] `core/rclone_config.py` (new)
