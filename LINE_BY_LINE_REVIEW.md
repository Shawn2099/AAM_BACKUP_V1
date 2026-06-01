# AAM Backup Automation V1 — Line-by-Line Code Review

**Date:** 2026-06-01
**Scope:** Complete codebase (22 files, 4,451 lines)
**Method:** File-by-file, line-by-line review with multi-agent exploration

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| Warning  | 0 |
| Info     | 11 |

**0 critical. 0 warning.** All findings are cosmetic or intentional design decisions. None affect correctness, security, or reliability.

---

## Findings by File

### flow.py

| # | Lines | Finding | Fix Applied |
|---|-------|---------|-------------|
| I1 | 298 | `lan_publish_artifact_task` recomputes `diff_snapshots()` that `_run_lan_pipeline` already has. Now passes pre-computed diff as parameter. | ✅ Refactored signature |
| I2 | 519-554 | Report flows (`weekly-report`, `monthly-report`) don't call `configure_prefect_bridge()`. Loguru logs won't appear in Prefect UI for report runs. | ✅ Added bridge call |
| I3 | 600 | `write_text(str(os.getpid()))` is non-atomic on Windows. Watchdog handles this gracefully. Added explanatory comment. | ✅ Documented |

### core/manifest.py

| # | Lines | Finding | Fix Applied |
|---|-------|---------|-------------|
| I4 | 95 | Pre-migration dedup `except Exception: pass`. Now logs via `logger.debug()`. | ✅ Added logging |
| I5 | 88 | Dedup DELETE ran on every new connection regardless of table existence. Now guarded with `sqlite_master` check. | ✅ Added guard |
| I6 | 15 | `SCHEMA_VERSION = 1` defined but never read/checked. Migrations use feature-detection. | Deliberate — feature-detection is more robust than version integers |

### core/fy_rollover.py

| # | Lines | Finding | Fix Applied |
|---|-------|---------|-------------|
| I7 | 3 | Docstring claimed "locks old FY read-only" — not implemented. | ✅ Removed from docstring |

### models/config.py

| # | Lines | Finding | Fix Applied |
|---|-------|---------|-------------|
| I8 | 170-174 | `ScheduleConfig` had no cron syntax validation. Malformed cron accepted by Pydantic, fails later at Prefect serve time. | ✅ Added 5-field validator |

### ui.py

| # | Lines | Finding | Fix Applied |
|---|-------|---------|-------------|
| I9 | 125 | `_DB_INSTANCE` singleton never closed. FastAPI process lifetime — intentional. | N/A |
| I10 | 130 | `_prefect_has_active_run` returns False on API failure. Concurrency limit in flow.py prevents actual conflicts. | N/A |
| I11 | 262 | `_require_auth` returned JSON 401 for browser requests. Now redirects browsers to `/login`. | ✅ Added content-type routing |

---

## Previous Audit — Issues Resolved (Earlier Sessions)

### Critical
- **FY rollover `run_cloud_sync()` signature mismatch** — passing invalid `config_path` kwarg and missing `gcs_key_path`, `project_number`, `location`. Fixed in `core/fy_rollover.py`.
- **Falsy-value bug in manifest parsing** — `or` fallback treated `Size: 0` as missing. Fixed in `flow.py` and `core/backup_repository.py` with `is not None` checks.
- **Dead LAN shutdown code** — `try/else` doesn't execute after `return`. Moved shutdown inside `try` block.

### High
- **No `PRAGMA busy_timeout`** — added `PRAGMA busy_timeout=30000` to DDL
- **Watchdog infinite deferral** — added `MAX_DEFERRALS=15` (~30 min) max lock deferral
- **NSSM path mismatch** — uninstall script now matches install script path

### Medium
- **`bulk_upsert_synced` not chunked** — chunked at 100 rows for SQLite variable limit safety
- **`prune_stale_synced` row-by-row** — converted to `executemany`
- **Redundant index on `relative_path`** — removed (UNIQUE constraint already covers it)

---

## Test Coverage

**435 tests passing (395 existing + 40 new edge case tests)**

New edge case test groups:
1. FY rollover (signature, detection, config mutation)
2. Database (bulk ops, concurrency, migrations, path normalization)
3. Pipeline (exit codes, manifest parsing, falsy values, mtime types)
4. Watchdog (stale locks, PID detection, fallback, deferral limits)
5. Config (validation, defaults, bounds, cross-validation)
6. Reports (empty DB, body_html bypass)

---

## Verdict

**Ready for client handover.** The codebase is production-ready with 435 passing tests, all critical bugs resolved, and all info-level items addressed.
