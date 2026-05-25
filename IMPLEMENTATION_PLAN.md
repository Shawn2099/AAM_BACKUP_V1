# AAM Backup Automation V1 — Implementation Plan

**Status**: Final Spec — Ready for Implementation
**Date**: 2026-05-25
**Key Principle**: Greenfield build. Old code (`AAM_BACKUP_V2/`) is reference material only. Nothing imported, nothing refactored, nothing carried over except proven logic patterns.

---

## 0. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CONFIG (YAML → Pydantic)                  │
│                     Validated once at flow entry                 │
└─────────────────────────────────────────────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
          ┌─────────▼──────────┐      ┌──────────▼─────────┐
          │  CLOUD DEPLOYMENT  │      │  LAN DEPLOYMENT     │
          │  Schedule: 6 PM IST│      │  Schedule: 1 AM IST │
          │  Mode: --mode cloud│      │  Mode: --mode lan   │
          └─────────┬──────────┘      └──────────┬─────────┘
                    │                             │
     ┌──────────────┼──────────────┐   ┌──────────┼──────────────┐
     │              │              │   │          │              │
     ▼              ▼              ▼   ▼          ▼              ▼
 health.py    cloud_preflight  cloud_sync  health.py  lan_preflight  wol.py
              rclone check              robocopy /L    ensure_server
              --one-way                                           _online
                    │                             │
                    ▼                             ▼
              cloud_sync.py                  lan_sync.py
              rclone sync                   robocopy /MIR
                    │                             │
                    ▼                             ▼
              cloud_verify                  lan_manifest.py
              rclone check                  walk UNC → feed DB
              --one-way                           │
                    │                             ▼
                    ▼                        shutdown.py
              cloud_report                  shutdown /s /m
              rclone size/lsjson
                    │
                    ▼
              ┌──────────────────────┐
              │  manifest.py (SQLite) │
              │  ┌─────────────────┐ │
              │  │ file_entries    │ │
              │  │ run_history     │ │
              │  └─────────────────┘ │
              └──────────┬───────────┘
                         │
                         ▼
                   report.py
                   weekly/monthly/failure
                   email alerts
```

### Why Two Deployments

LAN and cloud run at different times, have different preconditions (WoL vs none), and different durations (hours vs minutes). A single flow forcing them together adds coupling with zero benefit.

| Deployment | Schedule | Flow Mode | Precondition | Post-action |
|------------|----------|-----------|-------------|-------------|
| `backup-cloud` | Daily 6:00 PM IST | `--mode cloud` | Source drive online | None (async GCS) |
| `backup-lan` | Daily 1:00 AM IST | `--mode lan` | Source + WoL + SMB | Shutdown server |

Same codebase, same ManifestDB, independent invocations. One `serve()` process manages both.

---

## 1. Project Structure

```
aam_backup_automation_V1/
├── pyproject.toml                    # uv, Python >=3.12, dependencies
├── config.yaml                       # All settings with defaults
├── flow.py                           # Prefect 3 @flow — entry point
│
├── core/
│   ├── __init__.py
│   ├── health.py                     # Pre-backup: source exists, binaries present
│   ├── wol.py                        # WoL magic packet + SMB readiness wait
│   ├── shutdown.py                   # Remote shutdown /s /t 300 /f
│   ├── lan_preflight.py              # robocopy /L dry-run + UNC reachability
│   ├── lan_sync.py                   # robocopy /MIR wrapper + exit classification
│   ├── lan_manifest.py               # Walk LAN destination → file list + snapshot diff
│   ├── cloud_preflight.py            # rclone check --one-way dry-run
│   ├── cloud_sync.py                 # rclone sync wrapper + exit classification
│   ├── cloud_verify.py               # rclone check --one-way post-sync integrity
│   ├── cloud_reporter.py             # rclone size / lsjson / check diff for reports
│   ├── hashing.py                    # MD5 checksums (compatible with rclone hashsum md5)
│   ├── manifest.py                   # SQLite: file_entries + run_history tables
│   ├── report.py                     # Weekly/monthly aggregation + failure alerts
│   ├── logging.py                    # Loguru rotating daily, 30-day retention
│   └── fy_router.py                  # IST date → FY prefix (April 1 auto-rollover)
│
├── models/
│   ├── __init__.py
│   └── config.py                     # Pydantic v2 — validated config model (~80 lines)
│
└── deploy/
    └── serve.py                      # Prefect serve() entrypoint — deploys both schedules
```

### Zero-Import Architecture

No module imports another module. Only `flow.py` orchestrates:

```
health.py               ← pathlib only
wol.py                  ← socket, subprocess only
shutdown.py             ← subprocess only
lan_preflight.py        ← subprocess, config paths
lan_sync.py             ← subprocess, config paths
lan_manifest.py         ← os, pathlib only (pure walk)
cloud_preflight.py      ← subprocess, tempfile
cloud_sync.py           ← subprocess, tempfile
cloud_verify.py         ← subprocess only
cloud_reporter.py       ← subprocess, json only
hashing.py              ← hashlib, pathlib only
manifest.py             ← sqlite3 only
report.py               ← smtplib, json only (reads DB)
fy_router.py            ← datetime, zoneinfo only
logging.py              ← loguru only
                            ↓
flow.py                 ← imports ALL above, orchestrates in sequence
```

---

## 2. Config Model (`models/config.py`)

Only what's actually used. No dead sections. No archive, anomaly, reconciliation, test_restore, lan_integrity, cloud_archive.

```python
class PathsConfig(BaseModel):
    source_drive: str                      # "D:\\"
    lan_destination: str                   # "\\\\192.168.10.10\\share$"
    database_path: str                     # "C:\\BackupAgent\\manifest.db"
    log_directory: str = "C:\\BackupAgent\\logs"
    temp_directory: str = "C:\\BackupAgent\\rclone_temp"
    gcs_key_path: str = "aam-demo-gcs-key.json"
    """Path to GCS service account JSON key file."""


class LanConfig(BaseModel):
    enabled: bool = True
    retry_count: int = 3
    retry_wait_seconds: int = 10
    subprocess_timeout_seconds: int = 14400
    shutdown_after_backup: bool = True


class WolConfig(BaseModel):
    enabled: bool = True
    mac_address: str
    server_ip: str = "192.168.10.10"
    wake_timeout_seconds: int = 300
    ping_interval_seconds: int = 15
    stability_wait_seconds: int = 30


class CloudConfig(BaseModel):
    enabled: bool = True
    bucket: str = "aam-backup-demo-innovizta"
    project_number: str = "920173882190"
    location: str = "asia-south1"
    storage_class: str = "COLDLINE"
    bandwidth_limit: str = "10M"
    retry_count: int = 3
    subprocess_timeout_seconds: int = 21600


class NotificationConfig(BaseModel):
    smtp_host: str
    smtp_port: int = 587
    smtp_username: str
    smtp_password: str
    sender: str
    recipients: list[str]
    send_on_failure: bool = True
    send_on_success: bool = False
    weekly_summary_day: str = "monday"
    weekly_summary_time: str = "08:00"


class AppConfig(BaseModel):
    firm_name: str = "AAM Associates"
    paths: PathsConfig
    lan: LanConfig
    wol: WolConfig
    cloud: CloudConfig
    notifications: NotificationConfig
```

### Validators

| Check | Error if |
|-------|----------|
| `paths.source_drive` | Does not exist / is a file / is empty |
| `paths.lan_destination` | Not UNC format `^\\\\[^\\]+\\[^\\]+$` |
| `paths.database_path` | Does not end with `.db` |
| `wol.mac_address` | Required when `wol.enabled` is True; must match XX:XX:XX:XX:XX:XX |
| `wol.server_ip` | Not valid IPv4 |
| `cloud.bucket` | Empty when `cloud.enabled` is True |
| `notifications.sender` | Not valid email when non-empty |
| `notifications.recipients[*]` | Any entry is invalid email format |
| Cross-field | `lan.enabled` requires `paths.lan_destination` non-empty |
| Cross-field | `cloud.enabled` requires `paths.gcs_key_path` file exists |

### config.yaml Structure

```yaml
firm_name: "AAM Associates"

paths:
  source_drive: "D:\\"
  lan_destination: "\\\\192.168.10.10\\hp srv manual backup$"
  database_path: "C:\\BackupAgent\\manifest.db"
  log_directory: "C:\\BackupAgent\\logs"
  temp_directory: "C:\\BackupAgent\\rclone_temp"
  gcs_key_path: "C:\\BackupAgent\\aam-demo-gcs-key.json"

lan:
  enabled: true
  retry_count: 3
  retry_wait_seconds: 10
  subprocess_timeout_seconds: 14400
  shutdown_after_backup: true

wol:
  enabled: true
  mac_address: "00:1A:2B:3C:4D:5E"
  server_ip: "192.168.10.10"
  wake_timeout_seconds: 300
  ping_interval_seconds: 15
  stability_wait_seconds: 30

cloud:
  enabled: true
  bucket: "aam-backup-demo-innovizta"
  project_number: "920173882190"
  location: "asia-south1"
  storage_class: "COLDLINE"
  bandwidth_limit: "10M"
  retry_count: 3
  subprocess_timeout_seconds: 21600

notifications:
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  smtp_username: "backups@aam.com"
  smtp_password: ""
  sender: "backups@aam.com"
  recipients:
    - "admin@aam.com"
  send_on_failure: true
  send_on_success: false
  weekly_summary_day: "monday"
  weekly_summary_time: "08:00"
```

---

## 3. ManifestDB (`core/manifest.py`)

### Schema

Two tables. Clean, minimal, zero legacy.

#### file_entries

| Column | Type | Constraint | Purpose |
|--------|------|------------|---------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Row ID |
| `relative_path` | TEXT | NOT NULL UNIQUE | Relative path from source root (e.g. `WINMAN\data\tally.dat`) |
| `file_size` | INTEGER | NOT NULL DEFAULT 0 | File size in bytes |
| `mtime` | REAL | NOT NULL DEFAULT 0 | Last modified timestamp (Unix epoch float) |
| `md5_checksum` | TEXT | DEFAULT "pending" | MD5 hex digest or "pending" |
| `lan_status` | TEXT | DEFAULT "unknown" | One of: unknown, synced, failed, deleted |
| `cloud_status` | TEXT | DEFAULT "unknown" | One of: unknown, synced, failed, deleted |
| `lan_last_synced_at` | TEXT | | ISO8601 UTC timestamp of last LAN sync |
| `cloud_last_synced_at` | TEXT | | ISO8601 UTC timestamp of last cloud sync |
| `created_at` | TEXT | NOT NULL | ISO8601 UTC when first discovered |
| `updated_at` | TEXT | NOT NULL | ISO8601 UTC last upsert |

#### run_history

| Column | Type | Constraint | Purpose |
|--------|------|------------|---------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `run_id` | TEXT | NOT NULL | UUID or Prefect flow run ID |
| `mode` | TEXT | NOT NULL | "cloud", "lan", or "all" |
| `started_at` | TEXT | NOT NULL | ISO8601 UTC |
| `ended_at` | TEXT | | ISO8601 UTC |
| `status` | TEXT | NOT NULL | COMPLETE, PARTIAL, FAILED, SKIPPED |
| `exit_code` | INTEGER | | Tool exit code |
| `files_copied` | INTEGER | DEFAULT 0 | |
| `bytes_copied` | INTEGER | DEFAULT 0 | |
| `files_failed` | INTEGER | DEFAULT 0 | |
| `duration_seconds` | REAL | | Wall clock duration |
| `error_message` | TEXT | | Failure details if status = FAILED |

### Connection Strategy

- **WAL mode** enabled on every connection via `PRAGMA journal_mode=WAL`
- **Single writer** — no contention since deployments run at different times
- **Thread safety** — `sqlite3.check_same_thread = False` on connection
- **Foreign keys** enabled via `PRAGMA foreign_keys = ON`

### Key Operations

| Method | SQL | Purpose |
|--------|-----|---------|
| `upsert_file_entry(path, size, mtime, status_field)` | INSERT OR REPLACE | Called per-file after sync |
| `delete_entries(paths: list[str])` | DELETE WHERE relative_path IN (...) | Remove files deleted by mirror |
| `get_lan_synced_files() → dict[path, (size, mtime)]` | SELECT relative_path, file_size, mtime WHERE lan_status = 'synced' | Used for diff comparison |
| `insert_run(run_data: dict)` | INSERT INTO run_history | Record each invocation |
| `get_runs_since(days: int, mode: str) → list[dict]` | SELECT * WHERE started_at >= ? AND mode = ? | Weekly/monthly reports |
| `mark_lan_synced(paths: list[str])` | UPDATE file_entries SET lan_status = 'synced', lan_last_synced_at = ? | Bulk update after sync |
| `mark_cloud_synced(paths: list[str])` | UPDATE file_entries SET cloud_status = 'synced', cloud_last_synced_at = ? | Bulk update after sync |
| `update_checksums(updates: dict[path, md5])` | UPDATE file_entries SET md5_checksum = ? WHERE relative_path = ? | Post-verification |

---

## 4. Module Specifications

### 4.1 `core/health.py`

**Purpose**: Block the pipeline if source drive is missing or empty. Also check required binaries exist.

**Dependencies**: `pathlib`, `shutil`

**Public API**:

```python
def check_source_drive(source_path: str) -> tuple[bool, str]:
    """
    Returns (True, "") if source exists and has files.
    Returns (False, "Drive not accessible") if path doesn't exist.
    Returns (False, "Drive appears empty") if no files found.
    """

def check_binary_exists(name: str) -> bool:
    """
    Returns True if 'name' is found in PATH (via shutil.which).
    For Windows: appends .exe automatically.
    """

def pre_backup_health(config: AppConfig, mode: str) -> None:
    """
    Raises RuntimeError if any check fails.
    
    Always checks:
      - source_drive exists and has files
    
    When mode is "cloud" or "all":
      - rclone binary exists
    
    When mode is "lan" or "all":
      - robocopy binary exists
    """
```

**Caller**: `flow.py` — called once at flow entry, before any backup work.

---

### 4.2 `core/wol.py`

**Purpose**: Wake the backup server and wait until SMB port 445 is reachable.

**Dependencies**: `socket`, `subprocess`, `time`, `wakeonlan` library

**Reference**: `AAM_BACKUP_V2/core/wol.py` — logic is proven. Rewrite clean.

**Public API**:

```python
def is_smb_accessible(server_ip: str, timeout: float = 5.0) -> bool:
    """
    TCP SYN to port 445. Returns True if connect_ex returns 0.
    More reliable than ping — proves the SMB service is running.
    """

def send_magic_packet(mac_address: str) -> None:
    """
    Send WoL to 255.255.255.255:9 via wakeonlan library.
    Raises WolError on failure.
    """

def wait_for_server(server_ip: str, wake_timeout: int, ping_interval: int, stability_wait: int) -> bool:
    """
    Polls is_smb_accessible() every ping_interval seconds.
    Returns True when SMB port opens.
    Waits stability_wait seconds after port opens before returning.
    Raises WolTimeout if wake_timeout seconds pass without SMB response.
    """

def ensure_server_online(config: AppConfig) -> None:
    """
    If wol.enabled is False: returns immediately (server assumed online).
    If wol.enabled is True:
      1. Check is_smb_accessible(server_ip). If yes, return.
      2. Send magic packet.
      3. wait_for_server() with config timeouts.
      4. Raises WolTimeout on failure.
    """
```

---

### 4.3 `core/shutdown.py`

**Purpose**: Remotely shutdown the backup server after LAN backup completes.

**Reference**: `AAM_BACKUP_V2/tasks/shutdown_server_task.py`

**Public API**:

```python
def shutdown_server(server_ip: str) -> dict:
    """
    Sends: shutdown /s /m \\\\SERVER /t 300 /f
    
    This gives staff 5 minutes to cancel with: shutdown /a
    on the target machine.
    
    Returns:
        {"shutdown_initiated": True, "server_ip": server_ip}
        {"shutdown_initiated": False, "server_ip": server_ip, "error": "..."} on failure
    """
```

**Note**: `shutdown.exe` only exists on Windows. Wrapped in try/except FileNotFoundError.

---

### 4.4 `core/lan_preflight.py`

**Purpose**: Dry-run robocopy to validate paths, permissions, and junction point handling before the real `/MIR`.

**Dependencies**: `subprocess`, only. Uses `config.paths`.

**Public API**:

```python
def run_lan_dry_run(source: str, dest: str, timeout: int = 300) -> dict:
    """
    runs: robocopy source dest /L /MIR /XJ /NJH /NJS /NP
    /L = list-only mode — reports what WOULD happen, zero bytes moved.
    
    Returns:
        {"ok": True, "exit_code": int, "summary": str} on success
        {"ok": False, "error": str} on timeout / binary missing
    
    Timeout is short (default 300s) — this is metadata only, no data transfer.
    
    Exit codes (with /L):
       0-7 = No issues found, dry-run complete
       8+  = Issues detected — review needed
      16+  = Fatal — robocopy couldn't run at all
    """
```

**Where it fits**:
```
ensure_server_online(config)  →  run_lan_dry_run()  →  run_lan_sync()
                                    ↑
                            Fast network-only check
                            (30-120 seconds typical)
```

---

### 4.5 `core/lan_sync.py`

**Purpose**: Execute robocopy `/MIR` to mirror source → LAN destination.

**Dependencies**: `subprocess`, `tempfile` (for log file). Uses `config.paths`, `config.lan`.

**Reference**: `AAM_BACKUP_V2/core/robocopy.py` — proven exit code logic, flag handling. Rewrite clean.

**Public API**:

```python
def classify_exit_code(code: int) -> str:
    """
    Robocopy exit code bitmask:
    
    Bit 0 (1): Files copied
    Bit 1 (2): Extra files/dirs detected
    Bit 2 (4): Mismatched files detected
    Bit 3 (8): Copy errors (some files failed)
    Bit 4 (16): Fatal error
    
    Returns:
        "LAN_COMPLETE"  — code & 16 == 0 and code & 8 == 0
        "LAN_PARTIAL"   — code & 16 == 0 and code & 8 != 0
        "LAN_FAILED"    — code & 16 != 0
    """

def build_robocopy_command(source: str, dest: str, config: LanConfig) -> list[str]:
    """
    Flags (verified against MS docs):
        /MIR       Mirror source → destination (includes deletions)
        /Z         Restartable mode for large files
        /XJ        Exclude junction points (prevents infinite loops)
        /MT:8      Multi-threaded (8 threads)
        /R:n       Retry count (from config)
        /W:n       Wait between retries (from config)
        /V         Verbose — log every file
        /TS        Include timestamps in log
        /FP        Full path in log
        /BYTES     Show sizes in bytes
        /NJH       No job header
        /NJS       No job summary
        /NDL       No directory list
        /NP        No progress percentage
        /LOG:path  Write to file
        /XD        Exclude "System Volume Information"
        /XF        Exclude extensions + patterns from config.scope
    
    /NC is explicitly FORBIDDEN — it suppresses file class labels.
    """

def run_lan_sync(source: str, dest: str, config: LanConfig) -> dict:
    """
    1. Build command
    2. Create temp log file
    3. subprocess.run with timeout
    4. classify_exit_code
    5. Clean up temp log in finally
    
    Returns:
        {
            "status": "LAN_COMPLETE" | "LAN_PARTIAL" | "LAN_FAILED",
            "exit_code": int,
            "files_copied": int,
            "bytes_copied": int,
            "files_failed": int,
            "error": str | None
        }
    
    Timeout → {"status": "LAN_FAILED", "exit_code": -1, "error": "Timeout"}
    robocopy.exe not found → {"status": "LAN_FAILED", "exit_code": -1, "error": "robocopy.exe not found"}
    """
```

---

### 4.6 `core/lan_manifest.py`

**Purpose**: Walk the LAN destination share and return every file with size and mtime. Also diff two snapshots.

**Dependencies**: `os`, `pathlib` ONLY. Does NOT import config, DB, or any project module.

**Public API**:

```python
def walk_lan_destination(unc_path: str) -> list[dict]:
    """
    os.walk the UNC share. For every file, call os.stat().
    Skip files where stat() raises OSError (locked/deleted mid-walk).
    
    Returns:
        [
            {"path": "WINMAN\\data\\tally.dat", "size": 2048576, "mtime": 1717200000.0},
            ...
        ]
    """

def snapshot_to_dict(files: list[dict]) -> dict[str, tuple[int, float]]:
    """
    Convert walk result to {relative_path: (size, mtime)}.
    O(1) lookup for diff operations.
    """

def diff_snapshots(
    before: dict[str, tuple[int, float]],
    after: dict[str, tuple[int, float]],
) -> dict:
    """
    Compare two snapshots.
    
    Returns:
        {
            "added": [paths present in after but not before],
            "removed": [paths present in before but not after],
            "modified": [paths where (size, mtime) changed],
            "unchanged": [paths where (size, mtime) identical]
        }
    
    Algorithm: set operations on keys + tuple comparison on intersection.
    O(n) where n = number of files.
    """
```

**When it's called in flow.py**:
```
lan_before_snapshot = get_lan_snapshot()     # Optional: for diff report
run_lan_sync()
lan_after_snapshot = walk_lan_destination()  # Always: feeds DB
diff = diff_snapshots(lan_before, lan_after) # Optional: for report

# Feed DB from filesystem truth
for path, (size, mtime) in lan_after.items():
    db.upsert_file_entry(path, size, mtime, status="lan_synced")

# Purge deleted files from DB
db.delete_entries(diff["removed"])
```

---

### 4.7 `core/cloud_preflight.py`

**Purpose**: Run `rclone check --one-way` as a dry-run before the actual sync. Fast — compares metadata only.

**Dependencies**: `subprocess`, `tempfile`

**Public API**:

```python
def run_cloud_dry_run(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,          # Temp rclone config file
) -> dict:
    """
    Runs: rclone check source GCS:bucket/fy_prefix --one-way --fast-list
    
    Exit 0 = Everything matches. Source and GCS are in sync.
    Exit 1 = Differences found. Expected (new files since last run).
    Exit 2+ = Error (config, auth, network).
    
    Returns:
        {
            "ok": bool,          # True if check ran without errors
            "matched": bool,     # True if exit_code == 0
            "exit_code": int,
            "error": str | None
        }
    
    Purpose: catch auth failures, bucket-not-found, config errors 
    BEFORE the multi-hour sync attempt.
    """
```

---

### 4.8 `core/cloud_sync.py`

**Purpose**: Execute `rclone sync` to mirror source → GCS.

**Dependencies**: `subprocess`, `tempfile`

**Reference**: `AAM_BACKUP_V2/core/rclone.py` — proven exit classification, temp config pattern.

**Public API**:

```python
def write_temp_rclone_config(gcs_key_path: str, bucket: str, location: str, project_number: str) -> str:
    """
    Creates a temp file with:
    
    [aam_gcs]
    type = google cloud storage
    service_account_file = {resolved key path}
    project_number = 920173882190
    object_acl =
    bucket_acl =
    bucket_policy_only = true
    location = asia-south1
    storage_class = COLDLINE
    
    Returns the temp file path (caller must clean up).
    """

def classify_rclone_exit(code: int) -> str:
    """
    Exit codes (from rclone docs):
        0 → CLOUD_COMPLETE   (all files synced successfully)
        1 → CLOUD_FAILED     (uncategorised error)
        2 → CLOUD_FAILED     (syntax/usage error)
        3 → CLOUD_FAILED     (directory not found)
        4 → CLOUD_PARTIAL    (file not found — transient)
        5 → CLOUD_PARTIAL    (temporary error — network)
        6 → CLOUD_PARTIAL    (less serious — partial transfer)
        7 → CLOUD_FAILED     (fatal — auth, bucket, critical)
        8 → CLOUD_FAILED     (transfer limit exceeded)
        9 → CLOUD_COMPLETE   (no files to transfer)
       10 → CLOUD_PARTIAL    (duration limit hit)
    """

def build_rclone_sync_command(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
    bwlimit: str = "10M",
    retries: int = 3,
) -> list[str]:
    """
    Flags:
        rclone sync source aam_gcs:bucket/fy_prefix
        --config {config_path}
        --fast-list                 GCS recursive ListObjects API
        --gcs-no-check-bucket       Skip bucket existence check
        --gcs-storage-class COLDLINE
        --modify-window 1s          GCS has 1s metadata precision
        --bwlimit 10M               Protect source server bandwidth
        --transfers 4
        --checkers 16
        --retries 3
        --retries-sleep 30s
        --track-renames             Avoid re-upload on rename
        --header-upload x-goog-meta-backup-run:{run_id}
        --no-traverse               List source only (not dest) for transfers
        --use-json-log
        --log-file {temp}/cloud_{run_id}.jsonl
        --log-level INFO
        --stats 60s
    
    No --backup-dir flag — GCS Object Versioning handles recovery.
    No filter-from file — V1 excludes nothing (explicit is better).
    """

def run_cloud_sync(
    source: str,
    bucket: str,
    fy_prefix: str,
    gcs_key_path: str,
    location: str,
    project_number: str,
    bwlimit: str,
    retries: int,
    timeout: int,
    run_id: str,
) -> dict:
    """
    1. write_temp_rclone_config()
    2. build_rclone_sync_command()
    3. subprocess.run(timeout)
    4. classify_rclone_exit()
    5. Clean up temp config in finally
    
    Returns:
        {
            "status": "CLOUD_COMPLETE" | "CLOUD_PARTIAL" | "CLOUD_FAILED",
            "exit_code": int,
            "error": str | None
        }
    """
```

---

### 4.9 `core/cloud_verify.py`

**Purpose**: Post-sync integrity verification. `rclone check --one-way` compares source → GCS. Exit 0 = identical.

**Dependencies**: `subprocess`

```python
def verify_cloud_integrity(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
) -> dict:
    """
    Runs: rclone check source GCS:bucket/fy --one-way --fast-list
    
    Returns:
        {
            "verified": bool,    # True if exit_code == 0
            "exit_code": int,
            "error": str | None
        }
    """
```

---

### 4.10 `core/cloud_reporter.py`

**Purpose**: Gather GCS state for reporting. Every function calls one rclone subcommand. Zero custom logic.

**Dependencies**: `subprocess`, `json`

```python
def get_cloud_size(bucket: str, fy_prefix: str, config_path: str) -> dict:
    """
    rclone size GCS:bucket/fy_prefix --json
    
    Returns: {"count": int, "bytes": int, "sizeless": str}
    
    Instant — GCS returns pre-computed sizes.
    """

def get_cloud_manifest(bucket: str, fy_prefix: str, config_path: str) -> list[dict]:
    """
    rclone lsjson GCS:bucket/fy_prefix -R
    
    Returns: [{Path, Size, MimeType, ModTime, IsDir}, ...]
    Files only (IsDir=False filtered out).
    """

def get_cloud_diff(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
) -> dict:
    """
    rclone check source GCS:bucket/fy --combined {temp_file} --fast-list
    
    Parses the combined output:
        + path  → added
        - path  → removed in cloud  
        * path  → modified
        = path  → unchanged
    
    Returns: {"added": [...], "removed": [...], "modified": [...], "unchanged": [...]}
    
    CLEANUP: temp file deleted in finally block. Must use tempfile for filename.
    """
```

---

### 4.11 `core/hashing.py`

**Purpose**: MD5 checksums compatible with `rclone hashsum md5`.

**Dependencies**: `hashlib`, `pathlib`. **Zero external packages** (no xxhash).

```python
PENDING_CHECKSUM = "pending"

def compute_md5(file_path: str | Path) -> str:
    """
    Uses hashlib.file_digest (Python 3.11+) for efficient streaming.
    Returns hex digest string.
    """

def verify_checksum(file_path: str | Path, expected: str) -> bool:
    """
    Returns True if checksum matches. 
    Skips if expected is PENDING_CHECKSUM.
    """
```

---

### 4.12 `core/fy_router.py`

**Purpose**: Compute the fiscal year folder prefix based on IST date. Auto-rollover on April 1.

**Dependencies**: `datetime`, `zoneinfo` (Python 3.9+ stdlib)

```python
def get_fy_prefix(today: date | None = None) -> str:
    """
    If today is None, use current IST date.
    
    Logic:
      - Get IST date (Asia/Kolkata timezone)
      - If month >= 4: FY is {year}-{year+1 % 100}  (e.g., FY25-26)
      - If month < 4:  FY is {year-1}-{year % 100}   (e.g., FY24-25)
    
    Examples:
      May 25, 2026  → "FY26-27"
      March 15, 2026 → "FY25-26"
      April 1, 2026 → "FY26-27"  (rollover happens)
    
    Returns: string like "FY25-26"
    """
```

**Integration in flow**:
```python
fy_prefix = get_fy_prefix()
# Pass to rclone: dest = f"aam_gcs:{bucket}/{fy_prefix}/"
# GCS auto-creates FY26-27/ on first upload after April 1
```

---

### 4.13 `core/report.py`

**Purpose**: Compose and send email reports. Reads from ManifestDB run_history.

**Dependencies**: `smtplib`, `email.mime`, `json`. Reads from `manifest.py` (SQLite queries).

```python
def send_failure_alert(config: NotificationConfig, error: str, run_data: dict) -> None:
    """
    Sends immediate email on backup failure.
    Subject: "Backup Failed — {firm_name}"
    Body: error message + run details.
    """

def send_weekly_report(db: ManifestDB, config: NotificationConfig) -> None:
    """
    Queries: SELECT * FROM run_history WHERE started_at >= DATE('now', '-7 days')
    Aggregates: total files, total bytes, success rate, failures.
    Sends formatted HTML email with summary table.
    """

def send_monthly_report(db: ManifestDB, config: NotificationConfig) -> None:
    """
    Queries: SELECT * FROM run_history WHERE started_at >= DATE('now', '-30 days')
    Aggregates same as weekly but over 30 days.
    """
```

**Triggering**: Weekly/monthly reports triggered by a separate Prefect schedule (Monday morning, 1st of month). NOT embedded in backup flow. A separate scheduled deployment:
```
@deployment(name="weekly-report", schedule={"cron": "0 8 * * MON"})
@flow
def weekly_report_flow(config_path: str):
    config = load_config(config_path)
    db = ManifestDB(config.paths.database_path)
    send_weekly_report(db, config.notifications)
```

---

### 4.14 `core/logging.py`

**Purpose**: Structured logging. Minimal — just configures loguru.

```python
import sys
from pathlib import Path
from loguru import logger

def configure(log_dir: str | Path) -> None:
    """
    Remove default handler (stderr).
    Add rotating file sink:
        {log_dir}/backup_{time:YYYY-MM-DD}.log
        rotation="1 day"
        retention="30 days"
        level="DEBUG"
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}"
    
    Also add stderr handler at INFO level for flow run visibility.
    """
```

---

## 5. Flow Orchestrator (`flow.py`)

### Algorithm — Cloud Mode

```
1. CONFIG:  load_config(config_path)
2. HEALTH:  check_source_drive(), check_binary("rclone")
3. FY:      fy_prefix = get_fy_prefix()
4. PREF:    result = run_cloud_dry_run(source, bucket, fy_prefix)
            if not result.ok → ABORT + send alert
5. SYNC:    result = run_cloud_sync(source, bucket, fy_prefix, ...)
            if result.status == CLOUD_FAILED → ABORT + send alert
6. VERIFY:  result = verify_cloud_integrity(source, bucket, fy_prefix)
7. REPORT:  size = get_cloud_size()
            manifest = get_cloud_manifest()
            diff = get_cloud_diff()
8. DB:      for file in manifest:
                db.upsert_file_entry(file.Path, file.Size, ...)
            db.insert_run(run_data)
            db.delete_entries(diff.removed)
9. ALERT:   if any failure → send_failure_alert()
```

### Algorithm — LAN Mode

```
1. CONFIG:  load_config(config_path)
2. HEALTH:  check_source_drive(), check_binary("robocopy")
3. WOL:     if config.wol.enabled:
                ensure_server_online(config)
4. PREF:    result = run_lan_dry_run(source, dest)
            if not result.ok → ABORT + send alert
5. DIFF:    lan_before = walk_lan_destination()     # Optional snapshot
6. SYNC:    result = run_lan_sync(source, dest)
            if result.status == LAN_FAILED → ABORT + send alert
7. MANIFEST: lan_after = walk_lan_destination()
            for file in lan_after:
                db.upsert_file_entry(file.path, file.size, file.mtime)
                db.mark_lan_synced(file.path)
            diff = diff_snapshots(lan_before, lan_after)   # Optional
            db.delete_entries(diff.removed)
8. DB:      db.insert_run(run_data)
9. SHUTDOWN: if config.lan.shutdown_after_backup:
                shutdown_server(config.wol.server_ip)
10. ALERT:  if any failure → send_failure_alert()
```

### Pseudo-code: flow.py

```python
from prefect import flow, task
from prefect.cron import CronSchedule

from core.health import pre_backup_health
from core.logging import configure
from core.fy_router import get_fy_prefix
from core.wol import ensure_server_online
from core.shutdown import shutdown_server
from core.lan_preflight import run_lan_dry_run
from core.lan_sync import run_lan_sync
from core.lan_manifest import walk_lan_destination, snapshot_to_dict, diff_snapshots
from core.cloud_preflight import run_cloud_dry_run
from core.cloud_sync import write_temp_rclone_config, run_cloud_sync, classify_rclone_exit
from core.cloud_verify import verify_cloud_integrity
from core.cloud_reporter import get_cloud_size, get_cloud_manifest, get_cloud_diff
from core.manifest import ManifestDB
from core.report import send_failure_alert
from models.config import load_config, AppConfig


@task(name="cloud-backup", retries=2, retry_delay_seconds=300)
def cloud_backup_task(config: AppConfig):
    """Execute Cloud-side backup: preflight → sync → verify → report."""
    run_id = str(uuid.uuid4())
    db = ManifestDB(config.paths.database_path)

    try:
        # Preflight
        dry_run = run_cloud_dry_run(
            config.paths.source_drive,
            config.cloud.bucket,
            get_fy_prefix(),
            # temp config generated inside
        )
        if not dry_run["ok"]:
            raise RuntimeError(f"Cloud preflight failed: {dry_run['error']}")

        # Sync
        result = run_cloud_sync(
            source=config.paths.source_drive,
            bucket=config.cloud.bucket,
            fy_prefix=get_fy_prefix(),
            gcs_key_path=config.paths.gcs_key_path,
            location=config.cloud.location,
            project_number=config.cloud.project_number,
            bwlimit=config.cloud.bandwidth_limit,
            retries=config.cloud.retry_count,
            timeout=config.cloud.subprocess_timeout_seconds,
            run_id=run_id,
        )

        # Verify
        verify = verify_cloud_integrity(
            config.paths.source_drive,
            config.cloud.bucket,
            get_fy_prefix(),
            # config path from temp
        )

        # Report
        size = get_cloud_size(config.cloud.bucket, get_fy_prefix(), ...)
        manifest = get_cloud_manifest(config.cloud.bucket, get_fy_prefix(), ...)
        diff = get_cloud_diff(config.paths.source_drive, config.cloud.bucket, get_fy_prefix(), ...)

        # Update DB
        for file in manifest:
            db.upsert_file_entry(
                relative_path=file["Path"],
                file_size=file["Size"],
                mtime=file.get("ModTime", 0),
                cloud_status="synced",
            )
        db.delete_entries(diff["removed"])
        db.insert_run({
            "run_id": run_id,
            "mode": "cloud",
            "started_at": "UTC now",
            "ended_at": "UTC now",
            "status": result["status"],
            "exit_code": result["exit_code"],
            "files_copied": size["count"],
            "bytes_copied": size["bytes"],
        })

        return result, verify, size, diff

    except Exception as e:
        send_failure_alert(config.notifications, str(e), {"run_id": run_id, "mode": "cloud"})
        raise
    finally:
        db.close()


@task(name="lan-backup", retries=1, retry_delay_seconds=600)
def lan_backup_task(config: AppConfig):
    """Execute LAN-side backup: WoL → preflight → sync → manifest → shutdown."""
    run_id = str(uuid.uuid4())
    db = ManifestDB(config.paths.database_path)

    try:
        # WoL
        if config.wol.enabled:
            ensure_server_online(config)

        # Preflight
        dry_run = run_lan_dry_run(
            config.paths.source_drive,
            config.paths.lan_destination,
        )
        if not dry_run["ok"]:
            raise RuntimeError(f"LAN preflight failed: {dry_run['error']}")

        # Optional: before snapshot
        lan_before = snapshot_to_dict(walk_lan_destination(config.paths.lan_destination))

        # Sync
        result = run_lan_sync(
            config.paths.source_drive,
            config.paths.lan_destination,
            config.lan,
        )

        # Manifest
        lan_after = walk_lan_destination(config.paths.lan_destination)
        for entry in lan_after:
            db.upsert_file_entry(
                relative_path=entry["path"],
                file_size=entry["size"],
                mtime=entry["mtime"],
                lan_status="synced",
            )

        # Diff
        after_dict = snapshot_to_dict(lan_after)
        diff = diff_snapshots(lan_before, after_dict)
        db.delete_entries(diff["removed"])

        # Run history
        db.insert_run({
            "run_id": run_id,
            "mode": "lan",
            "started_at": "UTC now",
            "ended_at": "UTC now",
            "status": result["status"],
            "exit_code": result["exit_code"],
            "files_copied": result.get("files_copied", 0),
            "bytes_copied": result.get("bytes_copied", 0),
            "files_failed": result.get("files_failed", 0),
        })

        return result, diff

    except Exception as e:
        send_failure_alert(config.notifications, str(e), {"run_id": run_id, "mode": "lan"})
        raise
    finally:
        db.close()
        # Shutdown regardless of success/failure
        if config.lan.shutdown_after_backup and config.wol.enabled:
            try:
                shutdown_server(config.wol.server_ip)
            except Exception:
                pass  # Shutdown failure is non-critical


@flow(name="aam-backup", log_prints=True)
def backup(config_path: str = "config.yaml", mode: str = "all"):
    """
    Entry point for AAM Backup Automation.
    
    Modes:
        cloud   — Run only cloud backup
        lan     — Run only LAN backup (includes WoL + shutdown)
        all     — Run both sequentially (cloud first, then LAN)
    """
    config = load_config(config_path)
    configure(config.paths.log_directory)
    pre_backup_health(config, mode)

    errors = []

    if mode in ("cloud", "all") and config.cloud.enabled:
        try:
            cloud_backup_task(config)
        except Exception as e:
            errors.append(f"Cloud: {e}")

    if mode in ("lan", "all") and config.lan.enabled:
        try:
            lan_backup_task(config)
        except Exception as e:
            errors.append(f"LAN: {e}")

    if errors:
        raise RuntimeError(f"Backup completed with errors: {'; '.join(errors)}")
```

---

## 6. Prefect Deployment (`deploy/serve.py`)

```python
from prefect import serve
from flow import backup

# Cloud: daily at 6 PM IST
cloud_deployment = backup.to_deployment(
    name="backup-cloud",
    parameters={"config_path": "config.yaml", "mode": "cloud"},
    schedule={"cron": "0 18 * * *", "timezone": "Asia/Kolkata"},
)

# LAN: daily at 1 AM IST
lan_deployment = backup.to_deployment(
    name="backup-lan",
    parameters={"config_path": "config.yaml", "mode": "lan"},
    schedule={"cron": "0 1 * * *", "timezone": "Asia/Kolkata"},
)

# Report: weekly Monday 8 AM IST
report_deployment = weekly_report_flow.to_deployment(
    name="weekly-report",
    parameters={"config_path": "config.yaml"},
    schedule={"cron": "0 8 * * MON", "timezone": "Asia/Kolkata"},
)

if __name__ == "__main__":
    serve(cloud_deployment, lan_deployment, report_deployment)
```

Single `python deploy/serve.py` manages all three deployments. One process to monitor.

---

## 7. GCS Bucket Configuration

### Bucket Identity

| Property | Value |
|----------|-------|
| Name | `aam-backup-demo-innovizta` |
| Project ID | `aam-demo-gcs` |
| Project Number | `920173882190` |
| Region | `asia-south1` (Mumbai) |

### Security & Access

| Property | Value | Enforced in rclone config |
|----------|-------|---------------------------|
| Bucket-level access | Enabled (uniform) | `bucket_policy_only = true` |
| Object ACLs | Disabled (empty) | `object_acl =` |
| Bucket ACLs | Disabled (empty) | `bucket_acl =` |

### Object Features

| Feature | Status | Notes |
|---------|--------|-------|
| Object Versioning | Enabled | Noncurrent versions retained on update/delete |
| Live Storage Class | Standard | Uploaded objects default |
| Validation Storage Class | COLDLINE | Configured for lifecycle transitions |
| Encryption | Google-managed | Default, no additional config needed |
| Object Retention/Holds | None | Not configured |
| Soft Delete Policy | 0 seconds | Expected, mitigates surcharges |

### Lifecycle

| Rule | Action |
|------|--------|
| Noncurrent version → COLDLINE | At ~90 days (or accumulation count) |
| Noncurrent version → Delete | At ~90 days (OR lifecycle expiration) |

**Net effect**: Deleted/overwritten files are recoverable via version history for ~90 days. No `--backup-dir` flag needed — GCS versioning IS the safety net.

---

## 8. Robocopy Flag Reference

### LanSync Flags (with `/MIR`)

| Flag | Purpose | Required |
|------|---------|----------|
| `/MIR` | Mirror source → destination (deletes extras) | Yes |
| `/Z` | Restartable mode (resume interrupted copies) | Yes |
| `/XJ` | Exclude junction points (prevents infinite loops) | Yes |
| `/MT:8` | 8 thread multi-threaded copy | Yes |
| `/R:n` | Retry count on failure | Config |
| `/W:n` | Wait seconds between retries | Config |
| `/V` | Verbose — log every file | Yes |
| `/TS` | Include source timestamps in log | Yes |
| `/FP` | Include full path in log | Yes |
| `/BYTES` | Show file sizes in bytes | Yes |
| `/NJH` | No Job Header | Yes |
| `/NJS` | No Job Summary | Yes |
| `/NDL` | No Directory List | Yes |
| `/NP` | No Progress percentage | Yes |
| `/LOG:path` | Write output to file | Yes |
| `/XD` | Exclude directory | "System Volume Information" |

### LanPreflight Flags (with `/L`)

| Flag | Purpose |
|------|---------|
| `/L` | List-only — simulate without copying |
| `/MIR` | Same mirror logic as real run |
| `/XJ` | Same junction exclusion |
| `/NJH` / `/NJS` / `/NP` | Minimal output |

---

## 9. Rclone Flag Reference

### CloudSync Flags

| Flag | Purpose |
|------|---------|
| `sync` | Make destination match source (includes deletions) |
| `--config` | Temp config file path |
| `--fast-list` | Use GCS recursive ListObjects API |
| `--gcs-no-check-bucket` | Skip bucket existence check |
| `--gcs-storage-class` | Set to COLDLINE |
| `--modify-window 1s` | Match GCS 1-second metadata precision |
| `--bwlimit 10M` | Bandwidth cap |
| `--transfers 4` | Concurrent transfers |
| `--checkers 16` | Concurrent listing threads |
| `--retries 3` | Per-file retry count |
| `--retries-sleep 30s` | Wait between retries |
| `--track-renames` | Avoid re-upload on rename |
| `--header-upload` | x-goog-meta-backup-run metadata |
| `--no-traverse` | List source only for transfers |
| `--use-json-log` | Structured JSON log |
| `--log-file` | Write to file |
| `--stats 60s` | Progress every 60 seconds |

### CloudVerify Flags

| Flag | Purpose |
|------|---------|
| `check source dest` | Compare source vs destination |
| `--one-way` | Only check source → dest (not dest → source) |
| `--fast-list` | Recursive API call |
| `--config` | Temp config file |

### CloudDryRun Flags (same as verify)

| Flag | Purpose |
|------|---------|
| `check source dest --one-way` | Metadata comparison |
| `--fast-list` | Recursive API call |

### CloudReporter Flags

| Command | Purpose |
|---------|---------|
| `rclone size dest --json` | Total count + bytes |
| `rclone lsjson dest -R` | Full file listing with metadata |
| `rclone check source dest --combined file.txt` | Per-file diff (+/-/=/=) |

---

## 10. Error Handling Matrix

| Failure Point | Classification | Action | Retry? |
|---------------|----------------|--------|--------|
| Config invalid | RuntimeError | Abort immediately | No |
| Source drive missing | RuntimeError | Abort immediately | No |
| rclone/robocopy missing | RuntimeError | Abort immediately | No |
| Cloud dry-run fails | RuntimeError | Abort cloud | Retries via Prefect task |
| LAN dry-run fails | RuntimeError | Abort LAN | No |
| WoL timeout | WolTimeout | Abort LAN | No (server offline) |
| Robocopy LAN_FAILED | LAN_FAILED | Alert + record | No (retries in subprocess) |
| Robocopy LAN_PARTIAL | LAN_PARTIAL | Alert + record + continue | Yes (Prefect task retry) |
| Rclone CLOUD_FAILED (1-3, 7-8) | CLOUD_FAILED | Alert + record | Yes (Prefect task retry) |
| Rclone CLOUD_PARTIAL (4-6, 10) | CLOUD_PARTIAL | Alert + record + continue | Yes (Prefect task retry) |
| Shutdown fails | Warning only | Log + continue | No (non-critical) |
| ManifestDB error | Root exception | Alert + abort | No |

---

## 11. Data Flow: Deletion Tracking

```
BEFORE SYNC                     AFTER SYNC
─────────                       ──────────
ManifestDB:                     ManifestDB:
  a.txt → synced                  a.txt → synced     (unchanged)
  b.txt → synced                  b.txt → DELETED     (/MIR or sync removed it)
  c.txt → synced                  c.txt → synced     (unchanged)
                                  d.txt → synced     (new file)

How deletions are found:

LAN:
  lan_before = {a.txt, b.txt, c.txt}      # Walk before sync
  lan_after  = {a.txt, c.txt, d.txt}       # Walk after sync
  diff.removed = {b.txt}                    # Set difference
  db.delete_entries(["b.txt"])             # Purge from DB
  db.upsert_file_entry("d.txt", ...)       # Add new file

CLOUD:
  diff = get_cloud_diff()                  # rclone check --combined: - b.txt
  diff.removed = ["b.txt"]
  db.delete_entries(["b.txt"])             # Purge from DB
```

**Guarantee**: ManifestDB always reflects actual destination state. No stale entries.

---

## 12. Test Strategy

### Unit Tests (per module)

| Module | Test | Assertions |
|--------|------|------------|
| `health.py` | Missing drive | RuntimeError raised |
| `health.py` | Empty drive | RuntimeError raised |
| `health.py` | Missing binary | RuntimeError raised |
| `wol.py` | Magic packet format | Correct MAC bytes |
| `wol.py` | SMB unreachable | Returns False quickly |
| `fy_router.py` | May 2026 | Returns "FY26-27" |
| `fy_router.py` | March 2026 | Returns "FY25-26" |
| `fy_router.py` | April 1 2026 | Returns "FY26-27" |
| `lan_preflight.py` | Dry run exit 0 | Returns ok=True |
| `lan_preflight.py` | Dry run exit 16 | Returns ok=False |
| `lan_sync.py` | Exit 0 → COMPLETE | Bitmask classification |
| `lan_sync.py` | Exit 8 → PARTIAL | Bitmask classification |
| `lan_sync.py` | Exit 16 → FAILED | Bitmask classification |
| `cloud_sync.py` | Exit 0 → COMPLETE | Classification |
| `cloud_sync.py` | Exit 5 → PARTIAL | Classification (retryable) |
| `cloud_sync.py` | Exit 7 → FAILED | Classification (fatal) |
| `lan_manifest.py` | Walk test directory | File count matches |
| `lan_manifest.py` | Diff snapshots | Added/removed/modified correct |
| `cloud_reporter.py` | size command | Parses JSON correctly |
| `cloud_reporter.py` | lsjson output | Filters dirs, returns files |
| `cloud_reporter.py` | check --combined | Parses +/-/*/= correctly |
| `hashing.py` | Compute MD5 | Matches `rclone hashsum md5` |
| `hashing.py` | Pending check | Skips verification |
| `manifest.py` | Upsert + query | Round-trip correct |
| `manifest.py` | Delete entries | Entries removed |
| `manifest.py` | Run history insert | Query returns correct fields |

### Integration Tests

| Test | Coverage |
|------|----------|
| `flow.py --mode cloud` with mock rclone | Preflight → Sync → Verify → DB → Report |
| `flow.py --mode lan` with mock robocopy | WoL → Preflight → Sync → Manifest → Shutdown |
| Cloud fails, LAN still runs | Cloud error does not block LAN in `all` mode |
| LAN fails, shutdown still fires | Shutdown runs in finally block |
| Config disabled gate | LAN disabled → LAN section skipped |
| Config disabled gate | Cloud disabled → Cloud section skipped |
| Snapshot diff correctness | Before/after with known file changes |

---

## 13. Implementation Order

### Phase A: Foundation (no dependencies)

| # | File | Lines | Verify |
|---|------|-------|--------|
| 1 | `aam_backup_automation_V1/` directory | — | Scaffold |
| 2 | `pyproject.toml` | ~20 | `uv sync` succeeds |
| 3 | `models/config.py` | ~80 | Pydantic validates sample config |
| 4 | `config.yaml` | ~60 | Parsed by models/config.py |
| 5 | `core/fy_router.py` | ~20 | Test: May 2026 → FY26-27 |
| 6 | `core/hashing.py` | ~15 | Test: matches rclone hashsum md5 |
| 7 | `core/logging.py` | ~15 | Test: log file created |

### Phase B: Database

| # | File | Lines | Verify |
|---|------|-------|--------|
| 8 | `core/manifest.py` | ~120 | Test: upsert, delete, run_history CRUD |

### Phase C: Health + WoL + Shutdown

| # | File | Lines | Verify |
|---|------|-------|--------|
| 9 | `core/health.py` | ~30 | Test: missing drive raises |
| 10 | `core/wol.py` | ~70 | Test: SMB check, magic packet format |
| 11 | `core/shutdown.py` | ~25 | Test: command format |

### Phase D: LAN Pipeline

| # | File | Lines | Verify |
|---|------|-------|--------|
| 12 | `core/lan_preflight.py` | ~25 | Test: /L dry run |
| 13 | `core/lan_sync.py` | ~80 | Test: exit code classification |
| 14 | `core/lan_manifest.py` | ~55 | Test: walk + diff |

### Phase E: Cloud Pipeline

| # | File | Lines | Verify |
|---|------|-------|--------|
| 15 | `core/cloud_preflight.py` | ~25 | Test: dry-run exits |
| 16 | `core/cloud_sync.py` | ~90 | Test: temp config + exit classification |
| 17 | `core/cloud_verify.py` | ~20 | Test: verify exit codes |
| 18 | `core/cloud_reporter.py` | ~70 | Test: size/lsjson/diff |

### Phase F: Reporting

| # | File | Lines | Verify |
|---|------|-------|--------|
| 19 | `core/report.py` | ~80 | Test: email composition |

### Phase G: Orchestration

| # | File | Lines | Verify |
|---|------|-------|--------|
| 20 | `flow.py` | ~200 | Integration test both modes |
| 21 | `deploy/serve.py` | ~25 | Deployments registered |

---

## 14. What Gets Left Behind

All files in `AAM_BACKUP_V2/` remain untouched. The new code lives in `aam_backup_automation_V1/`. No imports cross the boundary. No shared state.

Old code serves as reference only during implementation — read it, understand the proven logic, write it fresh.

---

## 15. Verification Checklist (Pre-Deployment)

- [ ] `pyproject.toml` has all dependencies and `uv sync` succeeds
- [ ] `config.yaml` validates against `models/config.py` without errors
- [ ] `fy_router.get_fy_prefix()` returns correct FY for current IST date
- [ ] `hashing.compute_md5()` produces same hash as `rclone hashsum md5`
- [ ] `manifest.insert_run()` and `manifest.get_runs_since()` return matching data
- [ ] `manifest.delete_entries()` removes rows and doesn't cascade incorrectly
- [ ] `health.pre_backup_health()` raises RuntimeError when source drive missing
- [ ] `health.pre_backup_health()` raises RuntimeError when source drive empty (0 files)
- [ ] `wol.ensure_server_online()` returns immediately when `wol.enabled=False`
- [ ] `wol.ensure_server_online()` sends magic packet and waits correctly
- [ ] `shutdown.shutdown_server()` emits correct Windows shutdown command
- [ ] `lan_preflight.run_lan_dry_run()` with valid UNC → returns `ok=True`
- [ ] `lan_preflight.run_lan_dry_run()` with invalid UNC → returns `ok=False`
- [ ] `lan_sync.classify_exit_code(0)` → `LAN_COMPLETE`
- [ ] `lan_sync.classify_exit_code(8)` → `LAN_PARTIAL`
- [ ] `lan_sync.classify_exit_code(16)` → `LAN_FAILED`
- [ ] `lan_manifest.walk_lan_destination()` returns correct file count on test share
- [ ] `lan_manifest.diff_snapshots()` detects added/removed/modified/unchanged
- [ ] `cloud_preflight.run_cloud_dry_run()` with valid config → `ok=True`
- [ ] `cloud_preflight.run_cloud_dry_run()` with invalid bucket → `ok=False`
- [ ] `cloud_sync.classify_rclone_exit(0)` → `CLOUD_COMPLETE`
- [ ] `cloud_sync.classify_rclone_exit(5)` → `CLOUD_PARTIAL`
- [ ] `cloud_sync.classify_rclone_exit(7)` → `CLOUD_FAILED`
- [ ] `cloud_verify.verify_cloud_integrity()` returns `verified=True` for matching state
- [ ] `cloud_reporter.get_cloud_size()` returns `{count, bytes, sizeless}`
- [ ] `cloud_reporter.get_cloud_diff()` parses +/-/*/= correctly
- [ ] `flow.py --mode cloud` exercises full cloud pipeline end-to-end
- [ ] `flow.py --mode lan` exercises full LAN pipeline end-to-end
- [ ] `flow.py --mode all` runs cloud, then LAN, catches errors independently
- [ ] Cloud failure does not prevent LAN execution
- [ ] LAN failure does not prevent shutdown
- [ ] `lan.enabled=False` in config → LAN section skipped cleanly
- [ ] `cloud.enabled=False` in config → Cloud section skipped cleanly
- [ ] Temp files (rclone config, diff output) cleaned up in finally blocks
- [ ] ManifestDB reflects actual state after sync (no stale entries)
- [ ] Weekly report query returns correct aggregation
- [ ] Failure email sends with correct error context
- [ ] `deploy/serve.py` registers three deployments with correct schedules
