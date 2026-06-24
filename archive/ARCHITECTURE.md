# AAM Backup Automation V1 — Architecture & Design

Version 1.0.0 · Prefect 3.7+ · Python 3.12 · Windows Server 2016

---

## Table of Contents

1. [Overview](#overview)
2. [Production Deployment](#production-deployment)
3. [Startup Sequence](#startup-sequence)
4. [Backup Pipelines](#backup-pipelines)
5. [Codebase Structure](#codebase-structure)
6. [Configuration Model](#configuration-model)
7. [Database Schema](#database-schema)
8. [Dashboard UI](#dashboard-ui)
9. [Notification System](#notification-system)
10. [Concurrency & Safety](#concurrency--safety)
11. [Cross-Platform Windows/Linux Notes](#cross-platform-notes)
12. [Naming Conventions](#naming-conventions)

---

## Overview

AAM Backup Automation performs nightly differential backups from a Windows source drive to two independent destinations — a local LAN server via `robocopy /MIR` and Google Cloud Storage via `rclone sync`. Both pipelines are orchestrated by Prefect 3 with cron schedules defined in `config.yaml`. A FastAPI dashboard provides real-time status, manual triggers, and downloadable reports.

**Destinations:**

| Pipeline | Tool | Transport | Destination | Schedule (IST) |
|----------|------|-----------|-------------|----------------|
| Cloud (GCS) | rclone sync | HTTPS | `gs://{bucket}/FY26-27/` | Daily 18:00 |
| LAN | robocopy /MIR | SMB (port 445) | `\\{server}\lan_backup` | Daily 01:00 |

---

## Production Deployment

Two Windows Task Scheduler entries boot the system:

### Entry 1: Prefect API Server (`start_server.bat`)
```
Delay: 10 seconds after boot
Command: uv run prefect server start
Port: 4200
Environment: PREFECT_API_URL=http://127.0.0.1:4200/api
              PREFECT_SERVER_API_PORT=4200
Lifetime: Persistent (Task Scheduler restarts on failure)
```

### Entry 2: Dashboard + Scheduler (`start.bat`)
```
Delay: 60 seconds after boot
Command: uv run python launch.py
Environment: PREFECT_API_URL=http://127.0.0.1:4200/api
Lifetime: Persistent (Task Scheduler restarts on failure)
```

**Why two entries:** Separating the Prefect server from the dashboard/scheduler means restarting the app layer never takes down Prefect's API or run history. The 60-second delay ensures the API is ready before `launch.py` tries to connect.

---

## Startup Sequence

`launch.py` executes in order every boot:

```
1. os.environ["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"
2. _check_prefect_api() — HTTP GET /health, exits if unreachable
3. _run_dashboard() — starts uvicorn in daemon thread
4. _ensure_concurrency_limit() — creates global + tag-based limits via Prefect client
5. _cancel_orphaned_runs() — cancels orphaned runs via Prefect SDK async client (no subprocess shell-out)
6. serve(deployments) — blocks on Prefect scheduler (main thread)
```

Each step is independent — failure at any step does not cascade. Steps 2-5 run sequentially, then step 6 blocks forever. Ctrl+C stops steps 3 and 6; step 1 (Prefect server) continues independently.

---

## Backup Pipelines

Both pipelines follow the same pattern: **Health → Preflight → Sync → Verify → Record**.

### Cloud Pipeline (`_run_cloud_pipeline`)
```
health_check_task        → pre_backup_health() validates source drive
cloud_preflight_task     → rclone check --one-way (metadata-only, exit 0/1 = OK)
cloud_sync_task          → rclone sync with retries, stderr capture, exit classification
cloud_verify_and_report  → verify_cloud_integrity() + get_cloud_size/manifest/diff
cloud_record_task        → bulk_upsert_synced() to ManifestDB
```

**rclone exit classification** (`classify_rclone_exit`):
| Code | Status | Meaning |
|------|--------|---------|
| 0, 9 | CLOUD_COMPLETE | Synced or nothing to transfer |
| 4, 5, 6, 10 | CLOUD_PARTIAL | Transient (file not found, network, duration limit) |
| 1, 2, 3, 7, 8, other | CLOUD_FAILED | Fatal (auth, bucket, config) |

**Retry behavior:** `max_attempts` from config drives flow-level retries. Each retry uses the same stable `run_id`, so `INSERT OR REPLACE` deduplicates run_history. Task-level retries use Prefect's `.with_options(retries=N)`.

**Differential transferred metrics:** Before the cloud sync task runs, a pre-sync snapshot of all `cloud_status='synced'` entries is captured from the database. After sync completes, a post-sync snapshot is taken. The difference (new + modified files count/bytes) is computed and passed to `record_run_history()` as `files_copied` and `bytes_copied`. This provides accurate per-run transfer statistics rather than total-destination counts.

**Self-healing stale entries:** After `bulk_upsert_synced()` updates the database with current destination state, `record_sync_results()` identifies file entries marked `synced` in the database that no longer exist on the destination. These stale entries are pruned — their status is set to NULL and if both `lan_status` and `cloud_status` become NULL, the row is deleted entirely. This differential approach prevents phantom files from persisting in the database after they're deleted from the destination.

### LAN Pipeline (`_run_lan_pipeline`)
```
health_check_task        → pre_backup_health() validates source drive
wol_check_task           → send WoL magic packet, poll SMB port 445 until online
lan_preflight_task       → robocopy /L dry-run (no files touched)
lan_snapshot_before_task → os.walk destination, capture {path: (size, mtime)}
lan_sync_task            → robocopy /MIR with /R:{retries} /W:{wait}
lan_snapshot_after_task  → os.walk destination again
lan_record_task          → diff_snapshots(before, after) → record_sync_results()
lan_shutdown_task        → shutdown.exe /s /t 300 on remote server (only on success)
```

**robocopy exit classification** (`classify_exit_code`):
Robocopy uses bitmask exit codes. Any code 0-7 is success.
| Bit | Value | Meaning | Result |
|-----|-------|---------|--------|
| 0 | 1 | Files copied | COMPLETE |
| 1 | 2 | Extra files detected | COMPLETE |
| 2 | 4 | Mismatched files | COMPLETE |
| 3 | 8 | Copy errors occurred | PARTIAL |
| 4 | 16 | Fatal error | FAILED |

**Wake-on-LAN:** Sends magic packet to `255.255.255.255:9`, then polls TCP port 445 (SMB) every `ping_interval` seconds until server responds or `wake_timeout` expires. Uses `wakeonlan` library. No ICMP ping (blocked by Windows firewalls).

**Stability wait:** After SMB port opens, waits `stability_wait` seconds before proceeding. Default 30s — allows Windows services to fully initialize.

---

## Codebase Structure

```
aam_backup_automation_V1/
├── launch.py              # Boot orchestration — API check, limits, orphans, serve()
├── serve.py               # Deployment definitions — cron schedules from config.yaml
├── flow.py                # Prefect tasks + pipeline orchestrators + backup() flow
├── ui.py                  # FastAPI app — dashboard, triggers, reports, auth
├── config.yaml            # Single-file runtime configuration
│
├── core/
│   ├── time_utils.py      # All datetime ops — pendulum-based, single source of truth
│   ├── manifest.py        # ManifestDB — SQLite file catalog + run history (WAL mode)
│   ├── backup_repository.py  # DB write operations — insert_run, record_sync_results
│   ├── cloud_sync.py      # rclone sync wrapper — command builder, exit classifier, orchestrator
│   ├── cloud_preflight.py # rclone check --one-way dry-run validator
│   ├── cloud_verify.py    # Post-sync rclone check integrity verification
│   ├── cloud_reporter.py  # get_cloud_size / manifest / diff via rclone lsjon
│   ├── rclone_config.py   # Temp config writer + context manager (auto-cleanup)
│   ├── lan_sync.py        # robocopy /MIR wrapper — command builder, exit classifier, orchestrator
│   ├── lan_preflight.py   # robocopy /L dry-run validator
│   ├── lan_manifest.py    # os.walk destination → snapshot → diff (added/removed/modified)
│   ├── wol.py             # Wake-on-LAN — magic packet + SMB port polling
│   ├── shutdown.py        # shutdown.exe /s /t 300 remote command
│   ├── health.py          # Pre-backup checks — source drive, binaries, clock skew, GCS key
│   ├── report.py          # SMTP email delivery — failure alerts, weekly/monthly summaries
│   ├── logging.py         # Loguru configuration + Prefect log bridge
│   ├── hashing.py         # MD5 file hashing (compute, verify, pending sentinel)
│   ├── process.py         # pid_alive() — cross-platform PID check via psutil
│   └── fy_router.py       # Re-export of get_fy_prefix from time_utils (backward compat)
│
├── models/
│   └── config.py          # Pydantic v2 AppConfig — all settings, validation, YAML loader
│
├── templates/
│   └── dashboard.py       # Dashboard HTML renderer — pure function, no imports
│
├── tests/                 # 356 tests across 25 files
│   ├── test_cloud_sync.py     # rclone exit codes, command builder, subprocess orchestration
│   ├── test_lan_sync.py       # robocopy exit codes, command builder, subprocess orchestration
│   ├── test_flow_orchestration.py  # Tasks, pipelines, failure alert path
│   ├── test_ui.py             # Auth, rate limiter, endpoints, dashboard rendering
│   ├── test_launch.py         # API check, orphaned run cleanup
│   ├── test_manifest.py       # ManifestDB file_entries + run_history CRUD
│   ├── test_report.py         # Email sending, HTML report generation
│   ├── test_config.py         # Pydantic model validation
│   ├── test_workflows.py      # End-to-end logical workflow tests
│   ├── test_health.py         # Source drive, clock skew, binary checks
│   ├── test_lan_manifest.py   # Snapshot + diff logic
│   ├── ... (25 files total)
│
├── start_server.bat       # Windows Task Scheduler entry 1 — Prefect API server
├── start.bat              # Windows Task Scheduler entry 2 — Dashboard + scheduler
├── pyproject.toml         # Project metadata, dependencies, ruff + pytest config
├── WINDOWS_LEARNINGS.md   # Windows Server 2016 compatibility notes
└── ARCHITECTURE.md        # This file
```

---

## Configuration Model

All configuration is a single Pydantic v2 model loaded from `config.yaml` at runtime.

```
AppConfig
├── firm_name: str
├── paths: PathsConfig
│   ├── source_drive: str          # E:\ or D:\ — validated not empty
│   ├── lan_destination: str       # \\server\share — validated UNC regex
│   ├── database_path: str         # *.db — validated .db extension
│   ├── log_directory: str         # Default: C:\BackupAgent\logs
│   └── gcs_key_path: str          # Required when cloud enabled
├── lan: LanConfig
│   ├── enabled: bool
│   ├── retry_count: int           # robocopy /R: (1-10)
│   ├── retry_wait_seconds: int    # robocopy /W: (1-300)
│   ├── subprocess_timeout_seconds: int   # >= 3600
│   ├── shutdown_after_backup: bool
│   ├── max_attempts: int          # Flow-level retries (1-10)
│   ├── retry_delay_seconds: int   # Delay between flow retries (60-3600)
│   └── mt_threads: int            # robocopy /MT: (1-128)
├── wol: WolConfig
│   ├── enabled: bool
│   ├── mac_address: str           # Validated MAC regex
│   ├── server_ip: str             # Validated IPv4
│   ├── wake_timeout_seconds: int  # 60-600
│   ├── ping_interval_seconds: int # 5-60 (SMB port poll interval)
│   └── stability_wait_seconds: int # >= 0 (post-wake grace period)
├── cloud: CloudConfig
│   ├── enabled: bool
│   ├── bucket: str                # Validated bucket name regex
│   ├── project_number: str
│   ├── location: str              # GCS region (asia-south1)
│   ├── storage_class: str         # STANDARD|NEARLINE|COLDLINE|ARCHIVE
│   ├── bandwidth_limit: str       # rclone --bwlimit (10M, 500k, 1G)
│   ├── retry_count: int           # rclone --retries (1-10)
│   ├── subprocess_timeout_seconds: int  # >= 3600
│   ├── max_attempts: int          # Flow-level retries (1-10)
│   ├── retry_delay_seconds: int   # Delay between flow retries (60-3600)
│   ├── verify_timeout_seconds: int # Post-sync check timeout (60-7200)
│   ├── transfers: int             # rclone --transfers (1-64)
│   └── checkers: int              # rclone --checkers (1-64)
├── schedule: ScheduleConfig
│   ├── cloud_cron: str            # "0 18 * * *" — daily 6 PM IST
│   ├── lan_cron: str              # "0 1 * * *" — daily 1 AM IST
│   ├── weekly_cron: str           # "0 8 * * MON" — Monday 8 AM IST
│   ├── monthly_cron: str          # "0 8 1 * *" — 1st of month 8 AM IST
│   └── timezone: str              # "Asia/Kolkata" — IANA timezone
├── notifications: NotificationConfig
│   ├── smtp_host: str             # Empty = disabled
│   ├── smtp_port: int             # Default 587 (STARTTLS)
│   ├── smtp_username: str
│   ├── smtp_password: str         # Gmail app password
│   ├── sender: str
│   ├── recipients: list[str]
│   └── send_on_failure: bool      # Immediate alert on backup failure
└── dashboard: DashboardConfig
    ├── auth_enabled: bool
    ├── api_key: str               # Required when auth enabled
    ├── bind_address: str          # Default 127.0.0.1
    └── port: int                  # Default 8080
```

**Cross-field validation:** At least one destination (lan or cloud) must be enabled. gcs_key_path is required when cloud is enabled. LAN destination must be UNC path when enabled.

---

## Database Schema

Single SQLite database (`manifest.db`) in WAL mode. Thread-safe via `threading.Lock`. All timestamps are stored as timezone-aware ISO 8601 strings in IST.

### Table: file_entries
```sql
CREATE TABLE file_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    file_size       INTEGER NOT NULL DEFAULT 0,
    mtime           REAL NOT NULL DEFAULT 0,
    md5_checksum    TEXT DEFAULT 'pending',
    lan_status      TEXT DEFAULT 'unknown',
    cloud_status    TEXT DEFAULT 'unknown',
    lan_last_synced_at      TEXT,
    cloud_last_synced_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

**Indexes:** `idx_file_entries_lan_status`, `idx_file_entries_cloud_status`, `idx_file_entries_relative_path`

**Upsert behavior:** `ON CONFLICT(relative_path) DO UPDATE` — size, mtime, and checksums update; status/timestamps change only on first sync (`!= 'synced'` check). All paths are normalized with `replace("\\", "/")` at the database boundary, ensuring consistent forward-slash paths regardless of platform. File discovery on Windows (with backslashes) is transparently normalized before any write.

### Table: run_history
```sql
CREATE TABLE run_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL UNIQUE,
    mode            TEXT NOT NULL,          -- 'cloud' or 'lan'
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL,          -- CLOUD_COMPLETE, LAN_PARTIAL, etc.
    exit_code       INTEGER,
    files_copied    INTEGER DEFAULT 0,
    bytes_copied    INTEGER DEFAULT 0,
    files_failed    INTEGER DEFAULT 0,
    duration_seconds REAL,
    error_message   TEXT
);
```

**Index:** `idx_run_history_started_at`, `idx_run_history_mode`, `idx_run_history_run_id` (UNIQUE)

**Retry deduplication:** Same `run_id` used across Prefect retries → `ON CONFLICT(run_id) DO UPDATE` overwrites previous attempt. Only the final state persists.

**Maintenance:** `purge_old_runs(retention_days=90)` deletes old run_history entries on every successful backup. Conditional VACUUM runs when freelist exceeds 1000 pages (~4 MB).

---

## Dashboard UI

FastAPI server bound to `config.dashboard.bind_address:config.dashboard.port`. Inline HTML/CSS/JS — no external CDN dependencies. Works fully offline.

### Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | Required | Dashboard HTML with real-time status |
| GET | `/status` | Required | JSON — cloud/LAN status, manifest stats, recent runs |
| GET | `/health` | None | `{"status": "healthy"}` — monitoring probe |
| GET | `/login` | None | Login form HTML |
| POST | `/login` | None | Submit API key → session cookie (24h TTL) |
| GET | `/logout` | None | Clear session cookie |
| POST | `/trigger/cloud` | Required | Manual cloud backup trigger |
| POST | `/trigger/lan` | Required | Manual LAN backup trigger |
| GET | `/report/weekly` | Required | Download 7-day HTML report |
| GET | `/report/monthly` | Required | Download 30-day HTML report |

### Authentication
- API key from `config.yaml` → session cookie (`httponly`, `samesite=lax`, 24-hour TTL)
- Bearer: `X-API-Key` header also accepted for programmatic access
- `hmac.compare_digest()` for constant-time key comparison

### Rate Limiting
In-memory sliding window per IP:
- Trigger endpoints: 5 attempts per 5 minutes
- Login: 10 attempts per 5 minutes
- Report downloads: 10 per 5 minutes

### JavaScript Polling
The dashboard polls `/status` every 2 seconds via `setInterval(updateStatus, 2000)`. All timestamps are delivered as proper ISO 8601 strings with timezone offsets (via `format_iso_for_js()` using pendulum), eliminating the need for client-side timezone parsing hacks.

---

## Notification System

### Failure Alerts
`send_failure_alert()` fires immediately when any backup pipeline raises an exception. Multiple pipeline failures are joined with `"; "` via `ExceptionGroup`. If `send_on_failure` is disabled or SMTP is unconfigured, the alert is silently skipped — backup completion/error logging continues normally.

### Summary Reports
Two scheduled Prefect deployments:
- **Weekly report:** Monday 8 AM IST → `send_weekly_report()` → last 7 days
- **Monthly report:** 1st of month 8 AM IST → `send_monthly_report()` → last 30 days

Reports are also downloadable from the dashboard UI (`/report/weekly`, `/report/monthly`).

### SMTP Configuration
- `smtp.gmail.com:587` with STARTTLS (default)
- Port 465 uses `SMTP_SSL` (legacy implicit TLS)
- `smtp_password` expects a Gmail app password (16 characters, no spaces)
- Gracefully degrades — missing credentials → warning log, no crash

### Report HTML
`generate_report_html()` queries `run_history` for the time window, classifies runs by suffix matching (`_COMPLETE`, `_PARTIAL`, remainder = `_FAILED`) rather than explicit status sets — future-proof against new status values. Aggregates `files_copied`, `bytes_copied` from run_history. Renders an HTML table with localized timestamps. Shared between email delivery and dashboard download.

---

## Concurrency & Safety

### Serialization
The `backup()` flow wraps execution in `concurrency("aam-backup", occupy=1, timeout_seconds=3600)`. This prevents overlapping cloud and LAN runs from corrupting the SQLite database (single-writer model).

### Limit Creation
`launch.py` creates both limit types at startup:
1. **Global concurrency limit** via `upsert_global_concurrency_limit_by_name()` — satisfies the `concurrency()` context manager in `flow.py`
2. **Tag-based concurrency limit** via `create_concurrency_limit()` — satisfies parallel tag-based enforcement paths

Both calls are idempotent — they succeed silently if limits already exist.

### Pipeline Guards
- `trigger_cloud` / `trigger_lan` check `_is_running()` before spawning — returns 400 if already active
- Prefect concurrency limit provides server-side enforcement for race conditions
- `_cancel_orphaned_runs()` cleans up PENDING/RUNNING runs from crashed sessions on every boot, using Prefect's async SDK (`client.set_flow_run_state(force=True)`) instead of subprocess shell commands

### Rate Limiting
In-memory, per-IP sliding window protects trigger endpoints and login from abuse. Expired entries are evicted on each access.

---

## Fiscal Year Routing

Cloud backups are organized into GCS folders by fiscal year. The fiscal year starts April 1 (Indian financial year).

- `get_fy_prefix()`: April 2026 – March 2027 → `"FY26-27"`
- Auto-rollover on April 1 at midnight IST
- Used as the GCS path prefix: `gs://{bucket}/FY26-27/`
- Single source of truth in `core/time_utils.py`

---

## Time Handling

All datetime operations are centralized in `core/time_utils.py`, using the **pendulum** library. No file in the project imports `datetime.now(IST)` or `zoneinfo` directly.

**Key functions:**
| Function | Returns | Used by |
|----------|---------|---------|
| `now_iso()` | `"2026-05-30T19:52:00+05:30"` | manifest.py, flow.py |
| `parse_iso_to_local(iso)` | `"2026-05-30 19:52:00"` | ui.py, dashboard rendering |
| `format_iso_for_js(iso)` | `"2026-05-30T19:52:00+05:30"` | ui.py `/status` endpoint |
| `cutoff_iso(days)` | N days ago in IST | manifest.py queries |
| `get_fy_prefix(date)` | `"FY26-27"` | flow.py, ui.py |
| `cron_to_human(cron, tz)` | `"Daily at 18:00 Kolkata"` | ui.py dashboard |
| `now_formatted()` | `"2026-05-30 19:52 IST"` | report.py |

The JS dashboard receives pendulum-formatted ISO strings (always with explicit `+00:00` offset), so `new Date(isoStr)` parses reliably without client-side timezone hacks.

---

## Cross-Platform Notes

See `WINDOWS_LEARNINGS.md` for the complete reference. Key items:

1. **robocopy exit codes** are bitmask-based — codes 0-7 are all success
2. **SMB port 445** is checked instead of ICMP ping (Windows firewalls block ping)
3. **SQLite migrations** use `CREATE UNIQUE INDEX IF NOT EXISTS` to avoid breaking legacy databases
4. **Windows Task Scheduler** requires explicit `cd /d "%~dp0"` and `PATH` setup in `.bat` files
5. **Prefect concurrency** requires both global AND tag-based limits — global for `concurrency()` context manager, tag-based for parallel enforcement paths
6. **rclone temp config** uses `tempfile.mkstemp()` + `os.close(fd)` to avoid Windows file handle locks

---

## Naming Conventions

### Python
- **Files:** `snake_case.py` for modules, `ALL_CAPS.py` for deployment/launch scripts
- **Functions:** `snake_case()` for public, `_snake_case()` for private
- **Classes:** `PascalCase` — `ManifestDB`, `AppConfig`, `LanConfig`
- **Constants:** `UPPER_CASE` — `SCHEMA_VERSION`, `DDL`, `PROJECT_DIR`

### Database
- **Tables:** `snake_case` — `file_entries`, `run_history`, `db_meta`
- **Columns:** `snake_case` — `relative_path`, `lan_last_synced_at`
- **Status values:** `UPPER_CASE` with `_` — `CLOUD_COMPLETE`, `LAN_PARTIAL`, `LAN_FAILED`

### YAML
- **Keys:** `snake_case` — `source_drive`, `lan_destination`, `api_key`

### Git
- **Branch:** `main` (trunk-based — all work committed directly)
- **Commit format:** Present-tense imperative — `"Add ..."`, `"Fix ..."`, `"Remove ..."`

---

## Test Strategy

356 tests across 25 files. Test categories:

| Category | Files | Coverage |
|----------|-------|----------|
| **Unit — pure functions** | test_cloud_sync, test_lan_sync, test_fy_router, test_hashing, test_time_utils | Exit classifiers, command builders, schema validation, MD5 |
| **Unit — subprocess orchestration** | test_cloud_sync, test_lan_sync | Mocked subprocess.run → timeout, error, cleanup paths |
| **Unit — database** | test_manifest, test_manifest_edge_cases, test_backup_repository | CRUD, upsert, dedup, purge, VACUUM |
| **Unit — config** | test_config | Pydantic validation, defaults, cross-field rules |
| **Unit — reports** | test_report | Email sending, HTML generation, humanize formatting |
| **Integration — HTTP** | test_ui | FastAPI TestClient — endpoints, auth, triggers, rate limiter |
| **Integration — flow tasks** | test_flow_orchestration | Individual @tasks, pipeline orchestrators, failure alert path |
| **Integration — launch** | test_launch | API check, orphaned run cleanup |
| **Workflow — E2E logic** | test_workflows | Config → DB → sync → record → diff → report lifecycle |
| **Template rendering** | test_dashboard_template | render_dashboard() with real data |

**What is NOT tested (intentionally):**
- `_ensure_concurrency_limit()` — requires live Prefect API (exercised every boot)
- `_run_dashboard()` — starts uvicorn (exercised every boot)
- Actual `rclone` binary execution — mocked via `subprocess.run`
- Actual `robocopy.exe` execution — only runs on Windows
- Actual SMTP delivery — tested manually with Gmail app password (confirmed working)

---

## Dependencies

| Library | Version | Purpose |
|---------|---------|---------|
| prefect | ≥3.4.0 | Flow orchestration, scheduling, concurrency |
| pydantic | ≥2.0 | Configuration validation |
| loguru | ≥0.7.3 | Structured logging |
| pyyaml | ≥6.0 | YAML config parsing |
| wakeonlan | ≥3.1.0 | WoL magic packet |
| fastapi | ≥0.115.0 | Dashboard HTTP server |
| uvicorn | ≥0.30.0 | ASGI server |
| httpx | ≥0.27.0 | HTTP client (API health check) |
| pendulum | ≥3.0.0 | Timezone-aware datetime (single source of truth) |
| psutil | ≥6.0.0 | Cross-platform process checks |
| exceptiongroup | (backport) | ExceptionGroup for Python 3.10-3.11 compatibility |
| humanize | (transitive) | Human-readable byte sizes (e.g., "5.0 MiB") |
| jinja2 | (transitive) | Available if needed (currently unused) |
| tenacity | (transitive) | Available if needed (currently unused) |
