# Architecture Review — AAM Backup Automation V1

Reviewed: 2026-05-29

---

## Verdict

**Overall architecture is sound.** Clean dependency graph (no circular deps), good layering, consistent error handling, well-separated core modules.

---

## Strengths

- **Clean dependency graph** — no circular dependencies, clear layer boundaries (launch → serve → flow → core → models)
- **Excellent config model** — Pydantic v2 with thorough validation, cross-field checks, type safety
- **Idempotent DB design** — each invocation opens/closes its own connection, WAL mode, no contention
- **Consistent error handling** — core modules return error dicts, orchestrator records runs in `finally`, failure alerts via Prefect hooks
- **Concurrency guard** — global Prefect concurrency limit (`aam-backup` = 1) prevents overlapping backups
- **Core modules are single-purpose** — clean separation, each file does one thing

---

## Fix Now (Code Quality)

### 1. Duplicate rclone temp config writes in `cloud_backup_task()`

**File:** `flow.py` lines 121-131 and 142-153

Same config written and cleaned up twice with identical parameters. Should merge into one shared temp config with a single `try/finally`.

```python
# Current: two separate writes with identical params
verify_config = write_temp_config(gcs_key_path, location, project_number, storage_class)
try:
    verify_result = verify_cloud_integrity(...)
finally:
    Path(verify_config).unlink()

report_config = write_temp_config(gcs_key_path, location, project_number, storage_class)  # duplicate
try:
    size = get_cloud_size(...)
    manifest = get_cloud_manifest(...)
    cloud_diff = get_cloud_diff(...)
finally:
    Path(report_config).unlink()

# Fix: one write, shared across verify + report
config_path = write_temp_config(gcs_key_path, location, project_number, storage_class)
try:
    verify_result = verify_cloud_integrity(..., config_path=config_path)
    size = get_cloud_size(..., config_path)
    manifest = get_cloud_manifest(..., config_path)
    cloud_diff = get_cloud_diff(..., config_path)
finally:
    Path(config_path).unlink()
```

### 2. Confusing import aliases in `flow.py`

**File:** `flow.py` lines 121 and 142

Deferred imports aren't needed here — `rclone_config` has no dependency on `flow.py`. Move to top-level import.

```python
# Current (inside function body):
from core.rclone_config import write_temp_config          # line 121
from core.rclone_config import write_temp_config as w_config  # line 142 — confusing alias

# Fix: top-level import, single name
from core.rclone_config import write_temp_config
```

### 3. Hardcoded `"config.yaml"` in 4 files

**Files:** `flow.py` (2x), `serve.py` (1x), `launch.py` (3x), `ui.py` (1x)

Should be a single constant. Could go in `models/config.py` or a shared `constants.py`.

```python
# Current: scattered across files
load_config("config.yaml")

# Fix: single constant
CONFIG_PATH = "config.yaml"  # in models/config.py
load_config(CONFIG_PATH)
```

---

## Defer (Bigger Refactors)

### 4. `flow.py` duplicates DB write logic between cloud/lan tasks

Each task has ~30 lines of identical DB interaction code:
- `db.upsert_file_entry()` (per file)
- `db.mark_cloud_synced()` / `db.mark_lan_synced()` (bulk)
- `db.delete_entries()` (removed files)
- `db.insert_run()` (run history)

**Recommendation:** Extract to `core/backup_repository.py` with methods like:
- `record_sync_results(db, mode, manifest, diff)`
- `record_run_history(db, mode, run_id, started_at, ...)`

### 5. Per-file DB upserts — performance bottleneck for large inventories

**File:** `core/manifest.py` — `upsert_file_entry()` does individual `INSERT/UPDATE` + `commit()` per file.

For 100K files, this generates 100K individual SQL statements. `mark_cloud_synced()` and `mark_lan_synced()` already use `executemany()` (efficient).

**Recommendation:** Add bulk method:
```python
def upsert_file_entries(self, entries: list[dict]):
    """Bulk upsert using executemany()."""
    with self._lock:
        conn = self._get_conn()
        now = _utcnow()
        conn.executemany(
            """INSERT INTO file_entries (...) VALUES (...)
               ON CONFLICT(relative_path) DO UPDATE SET ...""",
            [(e["path"], e["size"], e["mtime"], now, now) for e in entries],
        )
        conn.commit()
```

### 6. `ui.py` is a monolith (820 lines)

Contains:
- Session management (in-memory store)
- Rate limiting (in-memory)
- Authentication middleware
- 8 API routes
- Lock file management
- Prefect API queries
- 500+ lines of inline HTML/CSS/JS templates

**Recommendation:** Split into:
- `ui.py` — FastAPI routes + auth + rate limiting
- `templates/dashboard.html` — HTML/CSS/JS template
- Or use FastAPI's `Jinja2Templates`

### 7. Test gaps

| Module | Status | Risk |
|--------|--------|------|
| `flow.py` | **Not tested** | HIGH — orchestration logic, mode routing, retry application, ExceptionGroup |
| `core/lan_manifest.py` | **Not tested** | MEDIUM — pure Python `diff_snapshots()`, `snapshot_to_dict()` |
| `core/rclone_config.py` | **Not tested** | LOW — temp file writer, simple |
| `core/wol.py` | **Not tested** | MEDIUM — mockable (socket + wakeonlan) |
| `core/cloud_preflight.py` | **Not tested** | LOW — subprocess wrapper |
| `core/cloud_verify.py` | **Not tested** | LOW — subprocess wrapper |
| `core/cloud_reporter.py` | **Not tested** | LOW — subprocess wrapper |
| `core/lan_preflight.py` | **Not tested** | LOW — subprocess wrapper |
| `core/shutdown.py` | **Not tested** | LOW — subprocess wrapper |
| `core/logging.py` | **Not tested** | LOW — config/setup |

**Recommendation:** Prioritize `flow.py` tests (mode routing, retry behavior, error aggregation) and `lan_manifest.py` tests (pure Python, no mocking needed).

---

## Dependency Graph

```
launch.py ──► serve.py ──► flow.py ──► core/* ──► models/config.py
    │                        │
    ▼                        ▼
  ui.py                  core/* (leaf modules)
```

**No circular dependencies.** All core modules point "upward" toward `models/config.py` and never back toward `flow.py` or `ui.py`.

---

## Data Flow Summary

```
config.yaml → AppConfig (Pydantic validation)
    ├── flow.py: passes config to tasks
    │   ├── cloud_backup_task → health → preflight → sync → verify → report → ManifestDB
    │   └── lan_backup_task   → health → WoL → preflight → sync → manifest → ManifestDB → shutdown
    ├── serve.py: reads config.schedule for cron
    ├── launch.py: reads config.dashboard for bind/port
    └── ui.py: reads config for status display + triggers deployments via Prefect SDK
```

---

## Database Access

- **Writers:** `cloud_backup_task`, `lan_backup_task` (via `ManifestDB`), `backup()` flow (purge)
- **Readers:** `ui.py` (dashboard), `report.py` (weekly/monthly summaries)
- **Contention:** None. Concurrency limit ensures one backup at a time. Dashboard only reads. Each consumer creates its own `ManifestDB` instance.
