# AAM Backup — Real-World Integration Test Plan

## Ground Rules (Applies to ALL Test Files)

- **Zero mocks.** Import and call actual `core.*` modules directly.
- **Real hardware.** Real disk, real robocopy/rclone binary, real NAS UNC, real GCS bucket.
- **Safe test prefix.** ALL files/folders created use the prefix `E2E_TEST_` and are cleaned up in a `finally` block.
- **Production config.** Load `config.yaml` via `load_config()` at the top of every test — never hardcode paths.
- **Log assertions.** Every test that expects an error must also verify the log message is useful (contains the path, the reason, not just a generic string).
- **Report format.** Each test prints a clear PASS/FAIL line with what was actually observed.

## Common Helpers (put in `tests/e2e_helpers.py`)

```python
# Shared across all test files
from models.config import load_config
from pathlib import Path
import os, shutil, time
from loguru import logger

def cfg():
    """Load production config."""
    return load_config()

def source_test_dir() -> Path:
    return Path(cfg().paths.source_drive).parent / "E2E_TEST_SOURCE"

def nas_test_dir() -> Path:
    return Path(cfg().paths.lan_destination).parent / "E2E_TEST_DEST"

def make_file(path: Path, size_bytes: int = 1024):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size_bytes))

def assert_log_contains(caplog_or_records, keyword: str):
    messages = "\n".join(str(r) for r in caplog_or_records)
    assert keyword.lower() in messages.lower(), \
        f"Expected log to contain '{keyword}' but got:\n{messages}"
```

---

## Test File Inventory

| File | Scenario Group | Real Hardware Used |
|------|---------------|-------------------|
| `test_rt_01_lan_sync.py` | LAN Sync golden path, error cases, locked files | Local HDD, NAS UNC, robocopy.exe |
| `test_rt_02_cloud_sync.py` | Cloud sync golden path, bandwidth, partial failure | Local HDD, rclone.exe, GCS bucket |
| `test_rt_03_preflight.py` | LAN + Cloud preflight edge cases | NAS UNC, rclone.exe, GCS bucket |
| `test_rt_04_watchdog.py` | Watchdog lock logic, PID reuse, AV interference | Windows Process API, lock files |
| `test_rt_05_manifest_db.py` | SQLite integrity under concurrent access and large writes | Local SSD, real SQLite file |
| `test_rt_06_fy_rollover.py` | Fiscal year rollover detection, folder creation, config rewrite | Local HDD, NAS UNC, gcloud CLI |
| `test_rt_07_flow_pipeline.py` | Full end-to-end flow (health → preflight → sync → verify → record) | All hardware |
| `test_rt_08_log_quality.py` | Verify every error path produces actionable log output | All hardware (triggers real failures) |

---

# TEST FILE 1: `tests/test_rt_01_lan_sync.py`
# LAN Sync — Real Robocopy on Real Hardware

## Purpose
Verify that `core/lan_sync.py` and `core/lan_preflight.py` work correctly
against the real NAS. Covers daily-use AND production failure scenarios.

## Environment Setup (run once at module start)
1. Call `ensure_server_online(config)` to wake the NAS via WoL.
2. Create `E2E_TEST_SOURCE\` on the local source drive parent.
3. Create `E2E_TEST_DEST\` on the NAS and place `.AAM_TARGET_MOUNTED` canary inside.

## Teardown (run in `finally` regardless of failures)
- `shutil.rmtree(source_test_dir(), ignore_errors=True)`
- `shutil.rmtree(nas_test_dir(), ignore_errors=True)`

---

## Test Cases

### LAN-01: Golden Path — New Files Arrive on NAS
**What it does:**
- Creates 3 files in `E2E_TEST_SOURCE\`: one small text file (1 KB), one medium binary (2 MB), one nested in a subdirectory.
- Runs `run_lan_dry_run(source, dest)` first.
- Then runs `run_lan_sync(source, dest, config.lan)`.

**Assertions:**
1. `dry_run_result["ok"]` is `True`.
2. `sync_result["status"]` is `"LAN_COMPLETE"` (exit code 1).
3. All 3 files physically exist on `E2E_TEST_DEST\` — checked with `Path.exists()`.
4. File sizes on NAS match source sizes exactly — checked with `os.path.getsize()`.

**Log assertions:**
- Log contains `"LAN sync exit 1"`.

---

### LAN-02: Mirror Delete — Files Removed on Source Are Deleted on NAS
**What it does:**
- Runs LAN-01 first (syncs 3 files to NAS).
- Deletes one file from `E2E_TEST_SOURCE\`.
- Runs `run_lan_sync()` again (second run).

**Assertions:**
1. `sync_result["status"]` is `"LAN_COMPLETE"` or `"LAN_PARTIAL"`.
2. The deleted file is **no longer on the NAS** (`not Path(nas_test_dir() / deleted_file).exists()`).
3. The 2 remaining files are still present on the NAS.

**Why this matters:** Proves `/MIR` mirror logic actually deletes orphaned files — a critical correctness property.

---

### LAN-03: Canary Missing → Hard Abort Before Any Transfer
**What it does:**
- Creates files in `E2E_TEST_SOURCE\`.
- Deletes `.AAM_TARGET_MOUNTED` from `E2E_TEST_DEST\`.
- Calls `run_lan_dry_run(source, dest)`.

**Assertions:**
1. A `HealthError` exception is raised — the call must not return normally.
2. Catches the exception and verifies the error message contains the **full path** to the missing canary file.
3. Verifies no files were copied to the NAS (destination stays empty).

**Log assertions:**
- Log contains `"Canary file"` AND the exact NAS path.
- This proves that if the NAS disk unmounts, the backup aborts instead of wiping source.

---

### LAN-04: OS-Level Locked File — Robocopy Survives
**What it does:**
- Creates `E2E_TEST_SOURCE\locked_doc.txt`.
- Opens it with `os.open()` + `msvcrt.locking(fd, msvcrt.LK_NBLCK, file_size)` to place a real Windows kernel exclusive lock.
- Calls `run_lan_sync()` with the lock held.

**Assertions:**
1. The function returns without raising an exception.
2. `sync_result["status"]` is `"LAN_PARTIAL"` or `"LAN_FAILED"` (not a Python crash).
3. The `sync_result["error"]` field contains actual robocopy log text — not `None` and not an empty string. This confirms the log capture works.

**Teardown:** Release lock with `msvcrt.locking(fd, msvcrt.LK_UNLCK, ...)` in `finally` before `os.close(fd)`.

**Log assertions:**
- Log contains `"exit 8"` (robocopy error bitmask for locked/failed files).

---

### LAN-05: Large File Transfer — Verify No Data Corruption
**What it does:**
- Generates a 50 MB file on the source using `os.urandom(50 * 1024 * 1024)` and computes its SHA-256 hash.
- Runs `run_lan_sync()`.
- Computes the SHA-256 of the copied file on the NAS.

**Assertions:**
1. `sync_result["status"]` is `"LAN_COMPLETE"`.
2. `sha256(source_file) == sha256(nas_file)` — byte-perfect copy verification.

**Why this matters:** Proves robocopy isn't silently truncating or corrupting large files — which can happen if `/ZB` (Backup Mode) partially writes before a timeout.

---

### LAN-06: Destination Not Reachable — Graceful Error and Useful Log
**What it does:**
- Temporarily renames `E2E_TEST_DEST\.AAM_TARGET_MOUNTED` so the destination appears "mounted" but uses a deliberately wrong UNC path (e.g., `\\10.10.186.231\NONEXISTENT_SHARE\`).
- Calls `run_lan_dry_run(source, bad_unc_path)`.

**Assertions:**
1. A `HealthError` or `RuntimeError` is raised.
2. The error/log message contains the bad UNC path.
3. The error/log does NOT say "Error 5" with no context — it must be human-readable.

---

### LAN-07: Snapshot Diff Logic — Before vs After
**What it does:**
- Syncs an initial set of files.
- Calls `snapshot_to_dict(walk_lan_destination(dest))` to capture `before`.
- Adds 1 new file and modifies 1 existing file on the source.
- Re-runs sync.
- Calls `snapshot_to_dict()` again to capture `after`.
- Calls `diff_snapshots(before, after)`.

**Assertions:**
1. `diff["added"]` contains exactly 1 entry (the new file).
2. `diff["modified"]` contains exactly 1 entry (the changed file).
3. `diff["removed"]` is empty (nothing was deleted from source).

**Why this matters:** The diff drives the `files_copied` / `bytes_copied` metrics shown in the dashboard and reports. If diff is wrong, the metrics are wrong.

---

## Edge Cases the Coder Must Handle
- The NAS may be slow to respond after WoL — use `ensure_server_online(config)` before any NAS access.
- On first run, `E2E_TEST_DEST\` may not exist yet — create it with `mkdir(parents=True, exist_ok=True)`.
- Always restore `.AAM_TARGET_MOUNTED` in `finally` after tests that delete it, so later tests can proceed.
- The `msvcrt` lock in LAN-04 must be released in `finally` — if it leaks, the file cannot be deleted during cleanup.
