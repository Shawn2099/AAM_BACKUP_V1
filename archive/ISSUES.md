# AAM Backup Automation V1 — Open Issues
> Generated: 2026-06-21 | Hardware target: 2C/4T CPU, 128GB RAM, HDD, 500GB data, ~10GB/day churn

---

## 🔴 HIGH — Fix Before Production

---

### ISSUE-001 — Leaked MockDB SQLite Files in Project Root

**File:** Project root directory (`/`)
**Severity:** High
**Type:** Test isolation failure / disk pollution

**Problem:**
Dozens of SQLite database files named after MagicMock objects are being created in the project root during test runs. Example filenames:
```
<MagicMock name='load_config().paths.database_path' id='125470110705104'>
```
These are real SQLite files (~53KB each) written to disk because `database_path` is
not being redirected to a temp directory in the test fixtures. Over time these will
accumulate and pollute the production working directory.

**Root Cause:**
The `ManifestDB` constructor receives an unpatched `MagicMock` string as the path,
which Python's `sqlite3` module silently accepts and creates as a literal filename.

**Fix:**
Add a `conftest.py` fixture that provides a `tmp_path`-based `database_path` for all
tests that instantiate `ManifestDB` or call `load_config`. Alternatively, patch
`ManifestDB.__init__` to use `:memory:` in tests.

**Cleanup required now:**
```bash
# Run from project root to delete all leaked mock DB files
find . -maxdepth 1 -name "<MagicMock*" -delete
```

---

### ISSUE-002 — `cloud_verify.py` Missing `--check-first` Flag

**File:** `core/cloud_verify.py` (line 36–43)
**Severity:** High
**Type:** HDD I/O thrashing

**Problem:**
`rclone check` in `verify_cloud_integrity()` runs without `--check-first`. This means
rclone interleaves random filesystem stat/hash calls with sequential data reads across
the full 500GB source, causing HDD read-head thrashing during the verify phase.

The same fix was applied to `cloud_sync.py` (where `--check-first` was added) but was
not carried through to the verify step. The verify step reads the entire 500GB source
on every run, making this the highest-I/O operation in the whole pipeline.

**Current command:**
```python
cmd = [
    rclone_exe, "check",
    source, dest,
    "--one-way",
    "--fast-list",
    "--config", config_path,
    "--gcs-no-check-bucket",
]
```

**Fix:**
```python
cmd = [
    rclone_exe, "check",
    source, dest,
    "--one-way",
    "--fast-list",
    "--check-first",        # <-- ADD THIS
    "--config", config_path,
    "--gcs-no-check-bucket",
]
```

---

### ISSUE-003 — Stale `vacuum_freelist_threshold` Defaults in `flow.py`

**File:** `flow.py` (line 187 and line 271)
**Severity:** High
**Type:** Configuration drift / silent regression

**Problem:**
The function signatures for `cloud_record_task` and `lan_record_task` both have
hardcoded default values of `vacuum_freelist_threshold: int = 1000`. These were
set before the project-wide threshold was increased to `10000` to reduce HDD write
cycles. If these tasks are ever called without an explicit argument, they silently
revert to the old aggressive vacuum behavior.

**Current (stale):**
```python
# flow.py line 187
def cloud_record_task(..., vacuum_freelist_threshold: int = 1000):

# flow.py line 271
def lan_record_task(..., vacuum_freelist_threshold: int = 1000):
```

**Fix:**
```python
def cloud_record_task(..., vacuum_freelist_threshold: int = 10000):
def lan_record_task(...,   vacuum_freelist_threshold: int = 10000):
```

Also update `_record_run()` at line 582 which has the same stale default:
```python
def _record_run(..., vacuum_freelist_threshold: int = 1000):  # → 10000
```

---

## 🟡 MEDIUM — Improvements

---

### ISSUE-004 — `cloud_verify.py` Re-Hashes All 500GB Every Night (Inefficient)

**File:** `core/cloud_verify.py`
**Severity:** Medium
**Type:** Performance / unnecessary HDD wear

**Problem:**
`rclone check` without `--size-only` computes MD5 checksums for every file on the
source HDD on every nightly run. For 500GB of data at ~80MB/s this is a full 1.5–2
hour read regardless of how much data actually changed (only ~10GB/day churn).

Since `rclone sync` already verifies file integrity during transfer, the post-sync
verify step is doing redundant work on 490GB of unchanged files every single night.

**Recommended fix:**
Use `--size-only` for the nightly verify (fast, metadata-only, ~seconds). Only fall
back to full MD5 verify on explicit request or weekly schedule.

```python
# Fast nightly verify (no HDD read — uses cached metadata)
"--size-only",

# Full cryptographic verify (weekly, or on-demand only)
# "--checksum",  # enable only for scheduled full integrity check
```

**Impact:** Reduces nightly verify from ~2 hours of HDD reads to ~30 seconds.

---

### ISSUE-005 — No Sanity Check on LAN Snapshot Before Sync

**File:** `flow.py` → `lan_snapshot_before_task()` (line 231–237)
**Severity:** Medium
**Type:** Silent failure / data loss risk

**Problem:**
`lan_snapshot_before_task()` performs a recursive walk of the NAS destination.
If the NAS path is unavailable (WoL failed, SMB not mounted, permission denied),
`walk_lan_destination()` silently returns an empty list. The before-snapshot is
then `{}` (empty dict).

After `robocopy /MIR` runs successfully, `diff_snapshots({}, after_dict)` will
classify ALL files as "added" and none as "removed". The diff is meaningless and
the run history in the database will show completely wrong statistics.

**Worse:** If the NAS has a partial mount, robocopy `/MIR` against an empty
apparent destination could mirror 0 files and treat the NAS as in-sync.

**Fix (V1 Architecture - Canary File):**
Implement a "Canary File" pattern to validate the NAS mount before syncing:
1. When `core/fy_rollover.py` creates the new LAN folder on April 1st, it must immediately write an empty file named `.AAM_TARGET_MOUNTED` into it.
2. In `core/lan_preflight.py`, explicitly check for the existence of `Path(config.paths.lan_destination) / ".AAM_TARGET_MOUNTED"`.
3. If the file is missing, immediately raise a `HealthError` and abort the backup to prevent `robocopy /MIR` from deleting the destination or filling up the local drive due to a phantom mount.

This is robust against network drops and seamlessly handles the natural 0GB -> 500GB growth throughout the fiscal year.

---

## 🟢 LOW — Nice to Have

---

### ISSUE-006 — `cloud_verify.py` Default Timeout Not Aligned with Config

**File:** `core/cloud_verify.py` (line 15)
**Severity:** Low
**Type:** Documentation / defensive coding

**Problem:**
The function signature has `timeout: int = 600` as its default, but `config.yaml`
now sets `verify_timeout_seconds: 14400`. The function default is a dead code path
(the caller always passes the config value), but it creates a misleading impression
that 600 seconds is acceptable for this operation.

**Fix:**
Update the function default to match the config default:
```python
def verify_cloud_integrity(..., timeout: int = 14400) -> dict:
```

---

### ISSUE-007 — `mydatabase.db` Artifact in Project Root

**File:** `mydatabase.db` (project root)
**Severity:** Low
**Type:** Hygiene

**Problem:**
A stray `mydatabase.db` SQLite file exists in the project root. This appears to be
a development/debug artifact and should be removed and added to `.gitignore`.

**Fix:**
```bash
rm mydatabase.db
echo "mydatabase.db" >> .gitignore
```

---

### ISSUE-008 — Hardcoded Timeout in `shutdown.py` Violates Config-Driven Rule

**File:** `core/shutdown.py` (line 33)
**Severity:** Medium
**Type:** Architectural Violation

**Problem:**
The `shutdown.exe` subprocess call has a hardcoded `timeout=30`. The architecture rule states that all timeouts should be driven by `config.yaml` and validated in `models/config.py`.

**Fix:**
Add `shutdown_timeout_seconds` to `LanConfig` in `models/config.py` and pass it to `shutdown_server` in `flow.py`.

---

### ISSUE-009 — Hardcoded Timeout in `report.py` Violates Config-Driven Rule

**File:** `core/report.py` (line 43, 45)
**Severity:** Medium
**Type:** Architectural Violation

**Problem:**
The SMTP connection functions `smtplib.SMTP_SSL` and `smtplib.SMTP` use a hardcoded `timeout=30`. This violates the config-driven rule.

**Fix:**
Add `smtp_timeout_seconds` to `NotificationConfig` in `models/config.py` and pass it to the SMTP calls.

---

## Summary

| ID | Severity | File | Description |
|---|---|---|---|
| ISSUE-001 | 🔴 HIGH | project root | Leaked MockDB SQLite files from test runs |
| ISSUE-002 | 🔴 HIGH | `core/cloud_verify.py` | Missing `--check-first` flag causes HDD thrashing |
| ISSUE-003 | 🔴 HIGH | `flow.py` | Stale `vacuum_freelist_threshold=1000` in 3 function defaults |
| ISSUE-004 | 🟡 MEDIUM | `core/cloud_verify.py` | Re-hashes 500GB nightly; should use `--size-only` |
| ISSUE-005 | 🟡 MEDIUM | `flow.py` | No sanity check on LAN before-snapshot (silent empty mount) |
| ISSUE-008 | 🟡 MEDIUM | `core/shutdown.py` | Hardcoded `timeout=30` violates config-driven rule |
| ISSUE-009 | 🟡 MEDIUM | `core/report.py` | Hardcoded `timeout=30` violates config-driven rule |
| ISSUE-006 | 🟢 LOW | `core/cloud_verify.py` | Function default timeout (600s) misaligned with config (14400s) |
| ISSUE-007 | 🟢 LOW | project root | Stray `mydatabase.db` development artifact |

---

*Fix order recommendation: ISSUE-003 → ISSUE-002 → ISSUE-001 cleanup → ISSUE-004 → ISSUE-005 → ISSUE-008 → ISSUE-009*
