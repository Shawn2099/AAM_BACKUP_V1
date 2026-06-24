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

---
---

# TEST FILE 2: `tests/test_rt_02_cloud_sync.py`
# Cloud Sync — Real Rclone against Real GCS Bucket

## Purpose
Verify that `core/cloud_sync.py`, `core/cloud_verify.py`, `core/cloud_preflight.py`,
and `core/cloud_reporter.py` work correctly against the real GCS bucket. Uses the
`E2E_TEST_FY` prefix in the bucket — NEVER the real `FY26-27` prefix.

## Environment Setup (run once at module start)
1. Create `E2E_TEST_SOURCE\` folder on local source drive parent.
2. Populate with a known set of files (sizes recorded for later verification).
3. Note the `gcs_key_path`, `bucket`, `project_number`, `location`, `storage_class` from config.

## Teardown (always)
- `shutil.rmtree(source_test_dir(), ignore_errors=True)` — clean local files.
- Run `rclone purge aam_gcs:BUCKET/E2E_TEST_FY --config ...` to remove cloud files.

---

## Test Cases

### CLOUD-01: Golden Path — Files Appear in GCS
**What it does:**
- Creates 3 known files in `E2E_TEST_SOURCE\` and records their sizes.
- Calls `run_cloud_sync(source, bucket, "E2E_TEST_FY", ...)`.

**Assertions:**
1. `sync_result["status"]` is `"CLOUD_COMPLETE"` (exit code 0).
2. `sync_result["error"]` is `None`.
3. Calls `rclone lsjson aam_gcs:BUCKET/E2E_TEST_FY --config ...` and verifies all 3 files appear in the JSON output.
4. The `Size` field in the rclone JSON matches the local file sizes exactly.

**Log assertions:**
- Log contains `"Cloud sync exit 0 → CLOUD_COMPLETE"`.

---

### CLOUD-02: Idempotency — Second Run Transfers Zero New Bytes
**What it does:**
- Runs CLOUD-01 (initial upload).
- Runs `run_cloud_sync()` again without changing any source files.

**Assertions:**
1. Second `sync_result["status"]` is `"CLOUD_COMPLETE"` or `"CLOUD_NO_CHANGES_COMPLETE"` (exit code 0 or 9).
2. Rclone does not re-upload — verify by timing the second run (should be < 5 seconds for small test set).

**Why this matters:** Verifies rclone's `--check-first` and `--modify-window 2s` flags correctly identify unchanged NTFS files. A bug here means the system re-uploads hundreds of GB every night.

---

### CLOUD-03: Modified File Is Re-Uploaded
**What it does:**
- Runs CLOUD-01.
- Overwrites one source file with different `os.urandom()` content (same name, different size).
- Runs `run_cloud_sync()` again.
- Fetches the GCS file list and checks the modified file's size.

**Assertions:**
1. The file's `Size` in GCS matches the NEW local file size (not the old one).
2. `sync_result["status"]` is `"CLOUD_COMPLETE"`.

---

### CLOUD-04: Bandwidth Limiting Is Actually Enforced
**What it does:**
- Creates a 20 MB test file.
- Records wall-clock time `t0 = time.time()`.
- Calls `run_cloud_sync()` with `bwlimit="1M"` (1 megabyte/sec limit).
- Records `elapsed = time.time() - t0`.

**Assertions:**
1. `elapsed >= 15.0` seconds (20 MB ÷ 1 MB/s = 20s, allow 25% tolerance).
2. `sync_result["status"]` is `"CLOUD_COMPLETE"`.

**Why this matters:** If bandwidth limiting is broken, a production sync could saturate the firm's internet connection during business hours.

---

### CLOUD-05: Verify Cloud Integrity — Post-Sync Check
**What it does:**
- Runs CLOUD-01 to populate GCS.
- Builds a temp rclone config via `temp_rclone_config(...)`.
- Calls `verify_cloud_integrity(source, bucket, "E2E_TEST_FY", cfg_path)`.

**Assertions:**
1. `verify_result["verified"]` is `True`.
2. `verify_result["exit_code"]` is `0`.

---

### CLOUD-06: Verify Fails on Tampered GCS File (Deliberate Mismatch)
**What it does:**
- Runs CLOUD-01.
- Adds a new file to `E2E_TEST_SOURCE\` WITHOUT re-running sync.
- Calls `verify_cloud_integrity()` — source now has a file GCS doesn't.

**Assertions:**
1. `verify_result["verified"]` is `False`.
2. `verify_result["exit_code"]` is `1` (rclone mismatch code).
3. `verify_result["error"]` contains `"Integrity mismatch"` — a human-readable message.

**Log assertions:**
- Log contains `"Cloud verify mismatch"` — not a generic Python exception.

---

### CLOUD-07: GCS Reporter — get_cloud_size Returns Accurate Count
**What it does:**
- Runs CLOUD-01 with exactly 3 known files.
- Calls `get_cloud_size(bucket, "E2E_TEST_FY", cfg_path)`.

**Assertions:**
1. `size_result["count"]` is `3`.
2. `size_result["bytes"]` matches the sum of the 3 file sizes (within 1% rounding tolerance).

---

### CLOUD-08: GCS Reporter — get_cloud_manifest Returns File List
**What it does:**
- Runs CLOUD-01 with 3 known files.
- Calls `get_cloud_manifest(bucket, "E2E_TEST_FY", cfg_path)`.

**Assertions:**
1. Result is a list of dicts.
2. Length is exactly 3.
3. Each dict has a `"Path"` key and a `"Size"` key.
4. The `"Path"` values match the relative paths of the uploaded files.

---

### CLOUD-09: Preflight Fails on Bad GCS Credentials
**What it does:**
- Creates a fake, corrupt GCS key JSON file at a temp path.
- Calls `run_cloud_dry_run(source, bucket, "E2E_TEST_FY", fake_key_path, ...)`.

**Assertions:**
1. `preflight_result["ok"]` is `False`.
2. `preflight_result["error"]` contains the word `"auth"` or `"credential"` or the key file path — something an operator can act on.

**Log assertions:**
- Log does NOT contain a raw Python traceback as the error message to the user — it should be a structured rclone error.

---

### CLOUD-10: Preflight Succeeds on Valid Config
**What it does:**
- Calls `run_cloud_dry_run()` with real credentials and real bucket.

**Assertions:**
1. `preflight_result["ok"]` is `True`.
2. Runs within 10 seconds (it's a single `rclone lsjson --max-depth 0` call — any longer indicates a network issue).

---

## Edge Cases the Coder Must Handle
- Always use GCS prefix `E2E_TEST_FY` — never `FY26-27` or empty string.
- The teardown `rclone purge` must run even if tests fail — wrap in `try/finally` at module scope using `pytest` fixtures with `scope="module"`.
- `get_cloud_manifest()` may return keys as `"Path"` (capital P) or `"path"` depending on rclone version — handle both in assertions.
- `CLOUD_NO_CHANGES_COMPLETE` (exit 9) is a success status, not a failure — treat it as passing in idempotency test.

---
---

# TEST FILE 3: `tests/test_rt_03_watchdog.py`
# Watchdog Lock Logic — Real Process IDs on Real Windows

## Purpose
Verify `core/process.py` (lock file protocol) and `watchdog.py`'s decision logic
work correctly with real OS process states. No mocks — use real PIDs.

---

## Test Cases

### WD-01: Lock Write and Read Round-Trip
**What it does:**
- Creates a temp `backup.lock` path.
- Calls `write_lock(lock_path)` — writes the real current PID and creation time.
- Calls `read_lock_alive(lock_path)`.

**Assertions:**
1. `alive` is `True`.
2. `pid` equals `os.getpid()`.
3. Lock file content format is `"PID:create_time"` — verify by reading the raw text.

---

### WD-02: Stale Lock — Process No Longer Exists
**What it does:**
- Spawns a real subprocess (`subprocess.Popen(["ping", "127.0.0.1", "-n", "1"])`).
- Captures its PID and records its create_time via `psutil.Process(pid).create_time()`.
- Waits for the process to exit (`proc.wait()`).
- Manually writes a lock file with the dead PID and its former create_time.
- Calls `read_lock_alive(lock_path)`.

**Assertions:**
1. `alive` is `False` — the PID is gone.
2. `pid` equals the dead PID (we can read it from the stale file).

**Why this matters:** If stale locks are treated as alive, the watchdog will never restart Prefect after a crash.

---

### WD-03: PID Reuse — Same PID, Different Process
**What it does:**
- Spawns a short-lived subprocess and captures its PID + create_time.
- Waits for it to die.
- Spawns a NEW subprocess. On Windows, PIDs are recycled rapidly in short tests.
- If the new process happens to get the same PID (rare but possible), the create_time will be different.
- Alternatively: manually write a lock file with our own PID but with a fake create_time (`0.0`).
- Calls `read_lock_alive(lock_path)`.

**Assertions:**
1. `alive` is `False` — same PID, wrong create_time → PID reuse detected.

**Why this matters:** This is the exact antivirus / stale-zombie scenario. Without create_time validation, the watchdog would incorrectly honor a stale lock.

---

### WD-04: AV-Locked File — Fail-Safe to Alive
**What it does:**
- Creates a lock file and places a real Windows exclusive lock on it using:
  `fd = os.open(path, os.O_RDWR); msvcrt.locking(fd, msvcrt.LK_NBLCK, file_size)`
- Calls `read_lock_alive(lock_path)` while the lock is held.

**Assertions:**
1. `alive` is `True` — the fail-safe behavior is triggered.
2. `pid` is `-1` — the sentinel value for AV-locked files.

**Teardown:** Always release with `msvcrt.locking(fd, msvcrt.LK_UNLCK, ...)` in `finally`.

**Why this matters:** Antivirus locking a file mid-backup is real. The fail-safe prevents the watchdog from restarting Prefect mid-transfer.

---

### WD-05: Watchdog `_transfer_process_running()` Detects Real Robocopy
**What it does:**
- Spawns a real, long-running robocopy process:
  `subprocess.Popen(["robocopy", source, dest, "/MIR", "/W:5", "/R:0"])`.
- Imports and calls `watchdog._transfer_process_running()` while robocopy is running.
- Terminates the robocopy process.
- Calls `_transfer_process_running()` again.

**Assertions:**
1. While robocopy is running: returns `True`.
2. After robocopy is killed: returns `False`.

---

### WD-06: Lock File Atomic Write — No Partial Read Possible
**What it does:**
- In a thread, calls `write_lock()` in a tight loop 100 times.
- In the main thread, reads the lock file 100 times concurrently.
- Parses each read — any read must either succeed or find the file temporarily absent (during `os.replace`).

**Assertions:**
1. No read ever returns a partially-written value (e.g., just `"123"` without the `:create_time` part when the full format was being written).
2. All reads either return a valid `"PID:create_time"` string or raise `FileNotFoundError` (during the replace window).

---

---

# TEST FILE 4: `tests/test_rt_04_manifest_db.py`
# ManifestDB — Real SQLite on Real Disk

## Purpose
Verify that `core/manifest.py` correctly persists, retrieves, and maintains
data under real disk I/O conditions. Tests use a real `.db` file in a temp directory.

---

## Test Cases

### DB-01: Fresh Database — DDL and Schema Created Correctly
**What it does:**
- Creates a `ManifestDB` pointing to a fresh temp path.
- Opens it (triggers DDL execution).

**Assertions:**
1. File exists on disk.
2. `PRAGMA journal_mode` returns `"wal"` — WAL mode is active.
3. Tables `file_entries`, `run_history`, `db_meta` all exist.
4. `db_meta` contains `schema_version = '1'`.

---

### DB-02: Upsert and Retrieve a File Entry
**What it does:**
- Calls `db.upsert_file_entry("folder/test.txt", 1024, 1234567890.0, lan_status="synced")`.
- Calls `db.get_entry("folder/test.txt")`.

**Assertions:**
1. Entry is not `None`.
2. `entry["file_size"]` is `1024`.
3. `entry["lan_status"]` is `"synced"`.
4. `entry["lan_last_synced_at"]` is not `None`.

---

### DB-03: Bulk Upsert — 10,000 Entries in One Transaction
**What it does:**
- Creates a list of 10,000 fake file entries (path, size, mtime).
- Calls `db.bulk_upsert_synced(entries, "cloud")`.
- Times the operation.

**Assertions:**
1. `db.file_count("cloud_status")` returns `10000`.
2. The operation completes in under 10 seconds (validates WAL + transaction batching performance).

**Why this matters:** A firm with 500GB of small files can have 100K+ entries. Slow bulk inserts make the pipeline take hours.

---

### DB-04: Run History — Insert and Retrieve
**What it does:**
- Calls `db.insert_run({run_id, mode, started_at, status, exit_code, ...})`.
- Calls `db.last_run("cloud")`.

**Assertions:**
1. `last_run["run_id"]` matches what was inserted.
2. `last_run["status"]` matches.
3. `last_run["exit_code"]` matches.

---

### DB-05: Duplicate Run ID — Upsert Overwrites Correctly
**What it does:**
- Inserts a run with `status="LAN_SKIPPED"`.
- Inserts the SAME `run_id` with `status="LAN_COMPLETE"`.
- Retrieves with `last_run()`.

**Assertions:**
1. Only ONE entry exists for that `run_id` (no duplicate rows).
2. `status` is `"LAN_COMPLETE"` (the second write wins).

---

### DB-06: `purge_old_runs` Deletes Old Entries Only
**What it does:**
- Inserts 5 runs with timestamps 200 days ago.
- Inserts 5 runs with today's timestamp.
- Calls `db.purge_old_runs(retention_days=90)`.

**Assertions:**
1. The 5 old runs are deleted.
2. The 5 recent runs still exist.
3. `file_entries` table is unaffected — purge only cleans `run_history`.

---

### DB-07: `prune_stale_synced` Cleans Ghost Entries
**What it does:**
- Inserts 10 file entries all with `cloud_status="synced"`.
- Simulates a deletion by only providing 7 paths as `active_paths`.
- Calls `db.prune_stale_synced("cloud", active_paths=set_of_7_paths)`.

**Assertions:**
1. Return value is `3` (3 stale paths removed).
2. The 3 deleted paths have `cloud_status=NULL`.
3. Or if `lan_status` is also NULL, the entire row is deleted.

---

### DB-08: WAL Mode Survives Abrupt Process Kill (Crash Recovery)
**What it does:**
- Inserts 100 entries in a loop using a background thread.
- Kills the thread mid-operation using `thread.join(timeout=0.1)` without giving it time to commit.
- Opens a NEW `ManifestDB` connection to the same file.
- Reads `file_entries`.

**Assertions:**
1. The new connection opens without error.
2. The database is not corrupt — `PRAGMA integrity_check` returns `"ok"`.
3. Only complete transactions are visible — no half-written rows.

---
---

# TEST FILE 5: `tests/test_rt_05_fy_rollover.py`
# Fiscal Year Rollover — Real Config Rewrite on Real Disk

## Purpose
Verify `core/fy_rollover.py` correctly detects, executes, and atomically
commits a fiscal year rollover without risking data loss or config corruption.

## IMPORTANT — Config Safety
Tests in this file must NEVER write to the real `config.yaml`.
Instead, each test copies `config.yaml` to a temp path and passes the temp path
to `rollover(config_path=temp_path)`.

---

## Test Cases

### FY-01: `detect_rollover()` Returns False When FY Is Current
**What it does:**
- Reads current `config.yaml` `source_drive` path (e.g., `E:\FY26-27`).
- Calls `detect_rollover(source_drive, lan_destination)` when the system FY matches the path suffix.

**Assertions:**
1. Returns `False` — no rollover needed.

---

### FY-02: `detect_rollover()` Returns True When FY Is Stale
**What it does:**
- Constructs fake paths pointing to an old FY: `E:\FY24-25` and `\\NAS\lan_backup\FY24-25`.
- Calls `detect_rollover(fake_source, fake_dest)`.

**Assertions:**
1. Returns `True` — the path FY suffix doesn't match the computed current FY.

---

### FY-03: `create_new_fy_folders()` Creates Real Folders on Disk
**What it does:**
- Calls `create_new_fy_folders(source_parent, nas_parent, "FY_E2E_TEST")`.

**Assertions:**
1. `E2E_TEST_SOURCE\FY_E2E_TEST\` folder exists on the local drive.
2. `E2E_TEST_DEST\FY_E2E_TEST\` folder exists on the NAS (or logs a clear warning if NAS is offline).
3. `.AAM_TARGET_MOUNTED` canary file exists inside the NAS folder.

**Teardown:** Delete both test FY folders.

---

### FY-04: `update_config_yaml()` Atomically Rewrites Config
**What it does:**
- Copies the real `config.yaml` to a temp file.
- Calls `update_config_yaml(temp_config_path, source_parent, nas_parent, "FY_E2E_TEST")`.
- Reads the temp config back.

**Assertions:**
1. `config["paths"]["source_drive"]` ends with `\FY_E2E_TEST`.
2. `config["paths"]["lan_destination"]` ends with `\FY_E2E_TEST`.
3. All other keys in the YAML are preserved exactly (comments, other sections).
4. The original real `config.yaml` is UNCHANGED.

---

### FY-05: Config Rewrite Is Atomic — Crash Mid-Write Leaves Original Intact
**What it does:**
- Copies `config.yaml` to a temp path.
- Patches `os.replace` to raise `OSError` after the temp file is written.
- Calls `update_config_yaml()` and catches the error.

**Assertions:**
1. The temp config file is unchanged (original content preserved).
2. The temp scratch file (`.config_rollover_*.yaml`) is cleaned up — not left on disk.

---

### FY-06: `run_archive_transition()` Actually Calls gcloud CLI
**What it does:**
- Calls `run_archive_transition(bucket, "E2E_TEST_FY", gcs_key_path)`.

**Assertions:**
1. Returns `True` on success.
2. Log contains `"archive transition succeeded"`.
3. (Verify via `rclone lsjson` that the objects in the test prefix still exist — archive doesn't delete them.)

**OR if gcloud is not installed:**
1. Returns `False`.
2. Log contains `"gcloud CLI not found"` with the specific searched paths.
3. Log does NOT raise a Python exception — it degrades gracefully.

---

### FY-07: Full Rollover on Temp Config — End-to-End
**What it does:**
- Copies `config.yaml` to a temp path.
- Sets `paths.source_drive` to `E:\FY_E2E_OLD` and `paths.lan_destination` to `\\NAS\FY_E2E_OLD`.
- Creates `E:\FY_E2E_OLD\` folder with some dummy files.
- Creates `\\NAS\FY_E2E_OLD\` folder with `.AAM_TARGET_MOUNTED`.
- Calls `rollover(config_path=temp_config_path)`.

**Assertions:**
1. Returns `True`.
2. Temp config's `source_drive` now points to the new FY.
3. New FY folder created on local drive.
4. Log contains `"FY rollover complete"`.
5. Real `config.yaml` is UNCHANGED.

**Teardown:** Delete all `FY_E2E_*` folders from local drive and NAS.

---
---

# TEST FILE 6: `tests/test_rt_06_flow_pipeline.py`
# Full End-to-End Pipeline — Health → Preflight → Sync → Verify → DB Record

## Purpose
Test the complete `_run_cloud_pipeline()` and `_run_lan_pipeline()` internal
orchestrators from `flow.py`. This is the closest thing to a full production run.
Import the private functions directly: `from flow import _run_cloud_pipeline, _run_lan_pipeline`.

## Environment Setup
- Create `E2E_TEST_SOURCE\` with 5 known files.
- Place canary on NAS `E2E_TEST_DEST\`.
- Create a fresh temp SQLite DB path for recording.
- Temporarily patch `config.paths.source_drive` and `config.paths.lan_destination`
  to point to the E2E test folders using `object.__setattr__` or a `dataclasses.replace()`.
  Never modify `config.yaml`.

---

## Test Cases

### PIPE-01: Cloud Pipeline — Golden Path, All Steps Record to DB
**What it does:**
- Calls `_run_cloud_pipeline(config, run_id="e2e-cloud-test", started_at=now_iso())`.

**Assertions:**
1. Returns `{"status": "CLOUD_COMPLETE", "exit_code": 0}`.
2. Opens the temp DB and calls `db.last_run("cloud")` — run is recorded.
3. `last_run["files_copied"]` is `> 0` (the 5 test files were transferred).
4. `last_run["bytes_copied"]` is `> 0`.
5. `last_run["status"]` is `"CLOUD_COMPLETE"`.
6. `last_run["exit_code"]` is `0`.
7. `last_run["error_message"]` is `None`.
8. `db.file_count("cloud_status")` is `5` (all files recorded as synced).

---

### PIPE-02: LAN Pipeline — Golden Path, Diff Recorded to DB
**What it does:**
- Calls `_run_lan_pipeline(config, run_id="e2e-lan-test", started_at=now_iso())`.

**Assertions:**
1. Returns `{"status": "LAN_COMPLETE", "exit_code": 1}`.
2. `db.last_run("lan")["status"]` is `"LAN_COMPLETE"`.
3. `db.last_run("lan")["files_copied"]` is `5`.
4. Physical files exist on NAS under `E2E_TEST_DEST\`.
5. `db.file_count("lan_status")` is `5`.

---

### PIPE-03: Cloud Pipeline — Health Check Fails Fast, DB Records the Error
**What it does:**
- Temporarily makes the source path inaccessible by renaming `E2E_TEST_SOURCE\` to `E2E_TEST_SOURCE_HIDDEN\`.
- Calls `_run_cloud_pipeline(config, ...)`.

**Assertions:**
1. Raises an exception (health check fails).
2. `db.last_run("cloud")["status"]` is `"CLOUD_SKIPPED"` (pipeline never reached sync).
3. `db.last_run("cloud")["error_message"]` contains `"Source drive"` — not a Python traceback.

**Teardown:** Rename `E2E_TEST_SOURCE_HIDDEN\` back.

---

### PIPE-04: LAN Pipeline — Canary Missing, DB Records the Error
**What it does:**
- Removes `.AAM_TARGET_MOUNTED` from `E2E_TEST_DEST\`.
- Calls `_run_lan_pipeline(config, ...)`.

**Assertions:**
1. Raises an exception.
2. `db.last_run("lan")["status"]` is `"LAN_SKIPPED"`.
3. `db.last_run("lan")["error_message"]` contains `"Canary"` or the NAS path — actionable.

**Teardown:** Restore the canary file.

---

### PIPE-05: Backup Lock Written and Released
**What it does:**
- In a thread, calls `backup(config_path=temp_config, mode="cloud")` (the main Prefect flow).
- Immediately in the main thread: sleeps 2 seconds then reads the lock file.

**Assertions:**
1. While backup runs: lock file exists and `read_lock_alive(lock_path)` returns `(True, pid)`.
2. After backup completes: lock file is deleted — `Path(lock_path).exists()` is `False`.

**Why this matters:** If the lock is not released, the watchdog will defer restarts indefinitely after a normal backup finishes.

---

### PIPE-06: Concurrent Backup — Second Invocation Blocked by Concurrency Slot
**What it does:**
- In two threads, simultaneously call `backup(config_path=temp_config, mode="cloud")`.

**Assertions:**
1. Only one backup runs at a time (Prefect `concurrency("aam-backup", occupy=1)` slot).
2. The second call either waits or raises a timeout — it does NOT run both simultaneously.

**Note:** If Prefect server is not running in the test environment, the concurrency slot won't work — document this and skip this test if Prefect API is unavailable.

---

---

# TEST FILE 7: `tests/test_rt_07_log_quality.py`
# Log Quality — Every Failure Produces Actionable Output

## Purpose
This is the hardest and most important test file. It doesn't just check if the
system survives failures — it checks that the **log messages are useful**.
A log that says "Error 5" or "OSError: [WinError 5]" with no context is useless
at 2 AM when an admin is trying to fix a broken backup.

## Evaluation Criteria
Each test triggers a REAL failure on REAL hardware, then inspects the log output.
A log message is considered **actionable** if it contains:
1. **What failed** — the specific path, process, or resource.
2. **Why it failed** — the error code or reason (not just the exception class).
3. **What to do** — either explicit guidance or enough info to diagnose.

## How to Capture Logs
Use `loguru`'s `add()` with a `StringIO` sink:
```python
from io import StringIO
import sys
from loguru import logger

buf = StringIO()
handler_id = logger.add(buf, format="{level} | {message}", level="DEBUG")
# ... run code ...
logger.remove(handler_id)
log_output = buf.getvalue()
```

---

## Test Cases

### LOG-01: Source Drive Missing → Log Contains Path
**What it does:**
- Calls `pre_backup_health(source_path="/nonexistent/path", mode="cloud", ...)`.

**Log assertion:**
- Log contains `/nonexistent/path` (the actual path that failed).
- Log contains the word `"not accessible"` or `"not found"`.
- Does NOT just say `"Health check failed"`.

---

### LOG-02: GCS Key Missing → Log Contains Key Path
**What it does:**
- Calls `run_cloud_dry_run(..., gcs_key_path="/fake/key.json")`.

**Log assertion:**
- Log contains `/fake/key.json`.
- Log contains `"not found"` or `"not exist"`.

---

### LOG-03: Canary Missing → Log Contains Full NAS Path
**What it does:**
- Removes `.AAM_TARGET_MOUNTED` from a test NAS folder.
- Calls `run_lan_dry_run(source, nas_test_dir())`.

**Log assertion:**
- Log contains the full NAS path including `\\10.10.186.231\...`.
- Log contains `"Canary"` or `"unmounted"`.
- Log does NOT just say `"LAN preflight failed"`.

---

### LOG-04: Robocopy Locked File → Log Contains Robocopy Log Tail
**What it does:**
- Runs the locked file scenario (same as LAN-04).
- Inspects `sync_result["error"]`.

**Assertion:**
- `sync_result["error"]` is not `None`.
- `len(sync_result["error"]) > 50` — it's actual robocopy output, not an empty string.
- The robocopy log tail contains the filename that was locked.

---

### LOG-05: Watchdog Stale Lock → Log Contains PID and Action Taken
**What it does:**
- Creates a stale lock file with a dead PID.
- Calls `read_lock_alive(lock_path)` and then simulates what the watchdog main loop would log.

**Log assertion:**
- If the watchdog logs the stale lock, it must contain the dead PID value.
- Log must say something like `"stale"` or `"PID not alive"` — not just `"restarting"`.

---

### LOG-06: Cloud Verify Mismatch → Log Contains File Count Discrepancy
**What it does:**
- Runs CLOUD-06 (adds a file locally, skips re-upload, runs verify).
- Inspects the log output.

**Log assertion:**
- Log contains `"mismatch"` and the rclone exit code.
- Log contains enough stderr output to identify which file is missing.

---

### LOG-07: FY Rollover Config Write Failure → Log Contains Temp File Path
**What it does:**
- Patches `os.replace` to raise `OSError("disk full")`.
- Calls `update_config_yaml(temp_path, ...)`.

**Log assertion:**
- Exception propagates correctly (no silent swallow).
- The temp scratch file path is mentioned in the error context.

---

### LOG-08: rclone Not Found → Log Contains Searched Paths, Not Python ImportError
**What it does:**
- Temporarily renames the rclone binary (e.g., `rclone.exe` → `rclone.exe.bak`).
- Calls `run_cloud_sync(...)`.

**Log assertion:**
- Log contains `"rclone not found"` (from `FileNotFoundError` handler in `cloud_sync.py`).
- Does NOT surface a raw Python traceback as the user-visible error.

**Teardown:** Rename `rclone.exe.bak` back.

---

---

# TEST FILE 8: `tests/test_rt_08_health_check.py`
# Pre-Backup Health Checks — Real System State

## Purpose
Verify `core/health.py` `pre_backup_health()` checks work against the real system.

---

## Test Cases

### HC-01: Source Drive Check — Drive Exists and Has Files
**Assertions:** `check_source_drive(config.paths.source_drive)` returns `(True, "")`.

### HC-02: Source Drive Check — Empty Drive Fails
**What it does:** Creates an empty temp directory, calls `check_source_drive(empty_dir)`.
**Assertions:** Returns `(False, reason)` where `reason` contains `"empty"`.

### HC-03: Binary Check — Robocopy and Rclone Found
**Assertions:**
1. `check_binary_exists("robocopy")` returns `True`.
2. `check_binary_exists("rclone")` returns `True`.

### HC-04: GCS Key Check — Real Key Exists and Is Valid JSON
**Assertions:**
1. `check_gcs_key(config.paths.gcs_key_path)` returns `(True, "")`.
2. Opens the key file and verifies it parses as valid JSON with a `"type"` field.

### HC-05: Clock Skew Check — System Clock Is Sane
**Assertions:**
1. `check_clock_skew(max_skew_seconds=600)` returns `(True, "")`.
2. Elapsed time for the HTTP call is under 5 seconds.

### HC-06: `pre_backup_health()` Raises on Source Missing
**What it does:** Renames source folder temporarily, calls `pre_backup_health(bad_path, "cloud", ...)`.
**Assertions:** Raises `HealthError` with the path in the message.
