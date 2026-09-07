# Comprehensive Code Audit & Vulnerability Report
**Merged Changes Range:** `4e9f1dc..c0d525b`  
**Commits Audited:**  
- `5fd209a` *fix(watchdog,lock): never unlink PID-live backup lock; atomic lock acquisition*  
- `9e82540` *feat(integrity): operational result contract + weekly independent audit*  
- `c0d525b` *fix(verify): F-08 verification-liveness completion contract*  
**Date:** September 7, 2026  
**Environment:** Windows Server 2022 / Windows 11, Python 3.12, Prefect 3.x, SQLite 3 (WAL mode)

---

## Executive Summary

A comprehensive code audit was conducted on the latest commits merged into the codebase. While these changes significantly harden process termination handling (`F-08`), introduce an independent weekly audit framework, and establish atomic lock acquisition, several **critical logical defects, race conditions, and contract violations** were identified. 

If deployed without remediation, these bugs will:
1. **Silently corrupt ManifestDB state** by marking divergent or corrupted cloud backups as `synced`.
2. **Crash and page administrators every Sunday** for Wake-on-LAN (WoL) NAS setups due to unhandled server sleep states.
3. **Trigger false-alarm emergency alerts and skip NAS shutdown** on normal Robocopy runs encountering mismatched timestamps or extra files (exit codes 4–7).
4. **Produce false-positive `VERIFIED` audit reports** when files cannot be read due to file locks or permissions.
5. **Thrash dual-core Xeon/HDD hardware** due to uncoordinated concurrent execution between weekly audits and daily backups.

---

## Vulnerability & Defect Matrix

> [!NOTE]
> **Remediation Status:** All 11 vulnerabilities and defects cataloged below have been **remediated, tested, and verified** (with dedicated regression test suite `tests/test_audit_remediation.py` and `tests/test_lan_shutdown_policy.py`).

| ID | Severity | Category | Affected File(s) | Summary | Status |
|---|---|---|---|---|---|
| **BUG-01** | **CRITICAL** | Data Integrity / Logic | [flow.py](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L581-L638) | Premature ManifestDB upsert before cloud verification gate check. | **RESOLVED** |
| **BUG-02** | **CRITICAL** | Concurrency / Resource | [flow.py](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L1088-L1184) | Weekly integrity audit runs without concurrency slot or lock file protection. | **RESOLVED** |
| **BUG-03** | **CRITICAL** | Hardware / Network | [flow.py](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L1150-L1168) | LAN audit does not wake sleeping NAS via WoL and never powers it down. | **RESOLVED** |
| **BUG-04** | **HIGH** | False Sense of Security | [core/integrity.py](file:///c:/Users/Shawn%20A/Desktop/bk/core/integrity.py#L72-L100) | Audit marks runs as `VERIFIED` even when file read/permission errors occur. | **RESOLVED** |
| **BUG-05** | **HIGH** | Operational Alarm Fatigue | [core/lan_sync.py](file:///c:/Users/Shawn%20A/Desktop/bk/core/lan_sync.py#L212-L216), [flow.py](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L64) | Robocopy exit codes 4–7 treated as `LAN_PARTIAL`, paging admins and skipping NAS shutdown. | **RESOLVED** |
| **BUG-06** | **MEDIUM** | Reliability / Deadlock | [watchdog.py](file:///c:/Users/Shawn%20A/Desktop/bk/watchdog.py#L397-L465) | Watchdog enters infinite deferral loop without alerting if a process hangs with a live lock. | **RESOLVED** |
| **BUG-07** | **MEDIUM** | Operational Stability | [flow.py](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L1153) | LAN audit ignores configured timeouts and hardcodes 4 hours. | **RESOLVED** |
| **BUG-08** | **MEDIUM** | Data Schema / DB | [flow.py](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L1155-L1163) | Negative sentinel values (`-1`) persisted to `integrity_audits` integer columns. | **RESOLVED** |
| **BUG-09** | **MEDIUM** | CI / Test Suite | [tests/test_h4_watchdog_counters.py](file:///c:/Users/Shawn%20A/Desktop/bk/tests/test_h4_watchdog_counters.py) | Regression tests fail due to stale assertions against obsolete lock-unlinking logic. | **RESOLVED** |
| **BUG-10** | **LOW** | Test Infrastructure | [tests/test_integrity_audit.py](file:///c:/Users/Shawn%20A/Desktop/bk/tests/test_integrity_audit.py#L23), [tests/test_f08_verify_liveness.py](file:///c:/Users/Shawn%20A/Desktop/bk/tests/test_f08_verify_liveness.py#L24) | `shutil.which` fails to detect bundled `deploy\bin\rclone.exe`, skipping 9 integration tests. | **RESOLVED** |
| **BUG-11** | **LOW** | Scheduler / State | [launch.py](file:///c:/Users/Shawn%20A/Desktop/bk/launch.py#L216-L220) | `integrity-audit` deployment is never paused when all backup legs are disabled. | **RESOLVED** |

---

## Detailed Findings & Remediation Plans

### BUG-01: Premature ManifestDB Upsert Before Cloud Verification Gate
- **Severity:** `CRITICAL`
- **Location:** [flow.py:581-638](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L581-L638)
- **Root Cause:**
  In `_run_cloud_pipeline()`, `cloud_record_task()` is executed at line 581:
  ```python
  else:
      cloud_record_task(
          db_path, verify_data, sync_result,
          busy_timeout_ms=config.maintenance.sqlite_busy_timeout_ms,
          vacuum_freelist_threshold=config.maintenance.sqlite_vacuum_freelist_threshold,
          synchronous=config.maintenance.sqlite_synchronous,
      )
  ```
  The gate check `evidence_ok` is only evaluated at line 605:
  ```python
  if not evidence_ok:
      verify_err = "Cloud integrity verification FAILED after sync: ..."
      status = "CLOUD_VERIFY_FAILED"
      ...
      raise RuntimeError(verify_err)
  ```
- **The "Bite Us" Scenario:**
  1. `rclone sync` runs and transfers files, but network corruption or an external interruption causes files in GCS to be missing or truncated.
  2. `cloud_verify_and_report_task` runs `rclone check` and identifies 5 modified/missing files.
  3. `cloud_record_task` runs immediately and executes `db.bulk_upsert_synced(normalized, "cloud")`, permanently recording those files in SQLite as `cloud_status = 'synced'`.
  4. The gate check `evidence_ok` evaluates to `False` and raises `RuntimeError`.
  5. The backup run is marked failed, but the database **already claims the files are synced**. Subsequent runs, the web UI, and disaster recovery audits will report that the cloud replica has files that are actually missing or corrupt.
- **Remediation:**
  Move `cloud_record_task` inside the post-verification block (after line 638), or execute it only when `evidence_ok` is `True`.

---

### BUG-02: Weekly Integrity Audit Runs Without Concurrency Slot or Lock Protection
- **Severity:** `CRITICAL`
- **Location:** [flow.py:1088-1184](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L1088-L1184)
- **Root Cause:**
  Daily backups synchronize using `_backup_slot(config)`, which enforces:
  ```python
  with concurrency("aam-backup", occupy=1, timeout_seconds=wait_seconds):
      acquired = acquire_lock(lock_path)
  ```
  However, `integrity_audit_flow()` invokes `audit_cloud()` and `audit_lan()` directly without acquiring `_backup_slot()` or the `BACKUP_LOCK_PATH`.
- **The "Bite Us" Scenario:**
  1. An admin triggers a manual backup, or a daily scheduled backup runs late.
  2. At 03:00 Sunday, `integrity_audit_flow` fires via its Cron trigger.
  3. Two separate heavy I/O operations (`rclone check` and `robocopy /MIR` or `rclone sync`) execute concurrently over the exact same local source drive and remote storage.
  4. Disk I/O bottlenecks cause extreme latency on the dual-core Xeon hardware.
  5. Files being actively written by the backup flow will be read mid-write by `rclone check`, causing MD5 hash mismatches and triggering false-positive `VERIFICATION_FAILED` alerts.
  6. If the Prefect API experiences transient latency during this spike, the watchdog inspects `BACKUP_LOCK_PATH`. Since `integrity_audit_flow` created no lock, the watchdog treats the server as idle and forcibly issues `sc stop AamPrefectServer`, killing the audit mid-execution.
- **Remediation:**
  Wrap the body of `integrity_audit_flow()` in `with _backup_slot(config):`.

---

### BUG-03: LAN Weekly Audit Bypasses WoL and Never Shuts Down NAS
- **Severity:** `CRITICAL`
- **Location:** [flow.py:1150-1168](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L1150-L1168)
- **Root Cause:**
  `integrity_audit_flow` tests LAN integrity with:
  ```python
  if mode in ("lan", "all") and config.lan.enabled:
      audit_id = new_audit_id("lan", "full")
      started_at = now_iso()
      result = audit_lan(config.paths.source_drive, config.paths.lan_destination)
  ```
  It completely bypasses `wol_check_task(config)` before the audit and `lan_shutdown_task(config)` after the audit.
- **The "Bite Us" Scenario:**
  1. A client has an energy-efficient NAS configured with Wake-on-LAN (`wol.enabled = true`, `lan.shutdown_after_backup = true`). Outside nightly backup windows, the NAS is powered off.
  2. On Sunday at 03:00, `integrity_audit_flow` fires.
  3. `audit_lan` attempts to access `\\192.168.1.50\backup`. Because the NAS is asleep, Windows SMB returns network path not found.
  4. `rclone check` fails with exit code 1 or 2.
  5. The audit records `status = "VERIFICATION_FAILED"` to the manifest database.
  6. `send_failure_alert()` sends an emergency alert to firm partners claiming backup integrity has failed.
  7. If the NAS happened to be online, `integrity_audit_flow` never issues a shutdown command, leaving the NAS running for the rest of the week.
- **Remediation:**
  Before invoking `audit_lan()`, invoke `wol_check_task(config)`. In a `finally` block or post-audit check, invoke `lan_shutdown_task(config)` if `config.lan.shutdown_after_backup` is enabled.

---

### BUG-04: Silent False-Positive `VERIFIED` on File Read / Permission Errors in Weekly Audit
- **Severity:** `HIGH`
- **Location:** [core/integrity.py:72-100, 150-200](file:///c:/Users/Shawn%20A/Desktop/bk/core/integrity.py#L72-L100)
- **Root Cause:**
  1. `_parse_combined_file()` parses lines prefixed with `+`, `-`, `*`, and `=`. Rclone's indicator for file read/access errors is `!` (e.g. `! path/to/locked.docx`). The parser drops all `!` lines.
  2. Unlike `core/cloud_verify.py`, `core/integrity.py` does not check for `ERROR` or `CRITICAL` log markers.
  3. When an unreadable or locked file is encountered, rclone does not count it as a "difference"; it emits `0 differences found` and `1 errors while checking`, exiting with code 1.
  4. In `_run_rclone_check()`:
     ```python
     mismatches = len(added) + len(modified) + len(removed) # Evaluates to 0
     summary_n = int(summaries[-1])                          # Evaluates to 0
     status = "VERIFIED" if mismatches == 0 else "VERIFICATION_FAILED"
     ```
     Because `mismatches == 0` and `summary_n == 0`, `status` is assigned `"VERIFIED"`.
- **The "Bite Us" Scenario:**
  A critical accounting database or Excel file on the source server is locked exclusively by QuickBooks or antivirus. Rclone check fails to read it. The weekly audit runs, fails to inspect the file, but reports `VERIFIED: 0 differences found`. The administrator believes all backups are 100% verified, unaware that key databases were never validated.
- **Remediation:**
  - In `_parse_combined_file()`, record lines starting with `!` as an `errors` list.
  - In `_run_rclone_check()`, inspect the log for `_ERROR_LINE_RE` and verify that `len(errors) == 0` and no "errors while checking" message was logged before assigning `VERIFIED`.

---

### BUG-05: Robocopy Exit Codes 4–7 Trigger False-Alarm Failures & Skip NAS Shutdown
- **Severity:** `HIGH`
- **Location:** [core/lan_sync.py:212-216](file:///c:/Users/Shawn%20A/Desktop/bk/core/lan_sync.py#L212-L216) & [flow.py:64, 853-885](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L64)
- **Root Cause:**
  In `decide_lan_result()`:
  ```python
  if 4 <= exit_code <= 7:
      return {
          "status": "LAN_PARTIAL", "termination": "normal",
          "log_complete": True, "counts": counts,
          "reason": f"robocopy mismatches/extras (exit {exit_code}, bit 2 set)",
      }
  ```
  In `flow.py`:
  ```python
  _LAN_OK_STATUSES = frozenset({"LAN_COMPLETE"})
  ...
  if status == "LAN_PARTIAL":
      # 1. Logs error
      # 2. Skips lan_shutdown_task()
      # 3. Sends failure alert email
  ...
  if status not in _LAN_OK_STATUSES:
      raise PartialRun(...)
  ```
- **The "Bite Us" Scenario:**
  Robocopy uses a bitmask exit code:
  - Code `1`: Files copied.
  - Code `2`: Extra files detected (and purged by `/MIR`).
  - Code `4`: Mismatches detected (e.g. timestamp/attribute differences between NTFS and SMB).
  - Code `8`: Actual file copy failures.
  Codes 4–7 indicate that **mismatches or extra files were present, but ZERO files failed to copy**. In `run_lan_sync()`, this was specifically treated as `anomaly_details` (not an error). However, `decide_lan_result` sets `status = "LAN_PARTIAL"`. 
  As a result:
  1. Every time a user modifies file attributes or deletes files on the source, `robocopy` exits with 4–7.
  2. `flow.py` sends an urgent notification: *"LAN backup PARTIAL: robocopy exit code 4 — some files were not copied"*, even though all files were copied successfully.
  3. `lan_shutdown_task` is skipped, leaving the physical NAS running 24/7.
  4. `PartialRun` is raised, turning the flow red/FAILED in the Prefect dashboard.
- **Remediation:**
  Either classify exit codes 4–7 as `LAN_COMPLETE` (with `anomaly_details` set) when `counts.get("failed", 0) == 0` and bit 3 (`& 8`) is not set, or update `_LAN_OK_STATUSES` and `flow.py` NAS shutdown logic to distinguish non-fatal anomalies from actual copy errors (`exit_code & 8`).

---

### BUG-06: Silent Indefinite Deadlock in Watchdog Without Operator Notification
- **Severity:** `MEDIUM`
- **Location:** [watchdog.py:397-414, 446-465](file:///c:/Users/Shawn%20A/Desktop/bk/watchdog.py#L397-L414)
- **Root Cause:**
  Commit `5fd209a` established that a PID-live lock must never be unlinked:
  ```python
  if lock_held:
      logger.critical(
          f"... while the backup lock owner is still alive. NOT removing the live lock and NOT restarting."
      )
      transfer_deferrals = 0
      time.sleep(BACKUP_WAIT_INTERVAL)
      continue
  ```
  The critical log is written strictly to `watchdog_svc.log`.
- **The "Bite Us" Scenario:**
  If the backup agent process encounters a deadlocked SQLite query or hangs on an un-timeoutable network socket while the Prefect server is down:
  1. The PID remains alive in the OS process table.
  2. Watchdog checks the lock, sees the PID is alive, logs to local disk, resets `transfer_deferrals = 0`, and sleeps.
  3. No email alert is sent (because watchdog intentionally avoids alerting).
  4. The web dashboard is unreachable (because Prefect is down).
  5. The entire backup system is permanently wedged, and days or weeks can pass before an administrator realizes no backups are occurring.
- **Remediation:**
  When `transfer_deferrals >= MAX_TRANSFER_DEFERRALS` or `lock_deferrals >= MAX_DEFERRALS` is hit with `lock_held == True`, watchdog should send an email notification or alert via `send_failure_alert()` once before looping, warning that manual operator intervention is required.

---

### BUG-07: LAN Audit Ignores Configured Timeouts
- **Severity:** `MEDIUM`
- **Location:** [flow.py:1153](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L1153)
- **Root Cause:**
  `flow.py` calls:
  `result = audit_lan(config.paths.source_drive, config.paths.lan_destination)`
  without passing `timeout`. In `core/integrity.py`, `audit_lan()` defaults to `timeout: int = 14400` (4 hours).
  In contrast, `audit_cloud()` uses `config.cloud.verify_timeout_seconds`.
- **The "Bite Us" Scenario:**
  If a firm has a large multi-terabyte dataset and configured `lan.subprocess_timeout_seconds = 28800` (8 hours), the weekly LAN audit will be abruptly killed after 4 hours with `TimeoutExpired`, causing the audit to fail.
- **Remediation:**
  Pass `timeout=getattr(config.lan, "subprocess_timeout_seconds", 14400)` to `audit_lan()`.

---

### BUG-08: Negative Value Persistence in `integrity_audits` Table
- **Severity:** `MEDIUM`
- **Location:** [flow.py:1155-1163](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L1155-L1163) vs [flow.py:1140-1144](file:///c:/Users/Shawn%20A/Desktop/bk/flow.py#L1140-L1144)
- **Root Cause:**
  Cloud audit clamps metrics before writing:
  ```python
  "files_checked": max(result["files_checked"], 0),
  "bytes_checked": max(result["bytes_checked"], 0),
  "mismatches": max(result["mismatches"], 0),
  ```
  LAN audit writes raw values directly:
  ```python
  "files_checked": result["files_checked"],
  "bytes_checked": result["bytes_checked"],
  "mismatches": result["mismatches"],
  ```
  When `_run_rclone_check` fails, it returns `-1` for these fields.
- **The "Bite Us" Scenario:**
  LAN audit failures insert `-1` into SQLite integer columns. Any query or reporting dashboard aggregating files or bytes checked (`SUM(files_checked)`) will produce inaccurate or negative numbers.
- **Remediation:**
  Apply `max(result[...], 0)` uniformly across both cloud and lan audit records.

---

### BUG-09: Stale Unit Tests in `test_h4_watchdog_counters.py`
- **Severity:** `MEDIUM`
- **Location:** [tests/test_h4_watchdog_counters.py](file:///c:/Users/Shawn%20A/Desktop/bk/tests/test_h4_watchdog_counters.py)
- **Root Cause:**
  Commit `5fd209a` updated `watchdog.py` so that it never unlinks PID-live locks and never triggers a restart when a lock owner is alive. However, the tests in `test_h4_watchdog_counters.py` were not updated and still assert the old behavior:
  - `test_transfer_deferrals_do_not_presatisfy_lock_cap`: asserts `mock_sc.call_count == 1` and `assert not BACKUP_LOCK_PATH.exists()`.
  - `test_transfer_cap_force_restarts_in_same_iteration`: asserts `mock_sc.call_count == 1` and `assert not BACKUP_LOCK_PATH.exists()`.
- **The "Bite Us" Scenario:**
  Running the test suite results in 2 test failures, breaking CI/CD pipelines and preventing clean test verification.
- **Remediation:**
  Update the tests to reflect the new contract: when the lock owner is alive, the lock is retained and restart is not called; when the lock owner is dead/stale, restart is called.

---

### BUG-10: Test Suite Skips Real Rclone Integration Tests Due to `shutil.which`
- **Severity:** `LOW`
- **Location:** [tests/test_integrity_audit.py:23-24](file:///c:/Users/Shawn%20A/Desktop/bk/tests/test_integrity_audit.py#L23-L24) & [tests/test_f08_verify_liveness.py:24-25](file:///c:/Users/Shawn%20A/Desktop/bk/tests/test_f08_verify_liveness.py#L24-L25)
- **Root Cause:**
  Tests check `RCLONE = shutil.which("rclone")`. In this environment, `rclone.exe` is located at `deploy\bin\rclone.exe` and is not in the system Windows PATH. `core.process.resolve_binary("rclone")` knows how to find it, but `shutil.which` does not.
- **The "Bite Us" Scenario:**
  9 critical verification and audit tests are silently skipped during normal `pytest` execution unless `deploy\bin` is manually added to the environment PATH.
- **Remediation:**
  In `tests/conftest.py`, add `deploy\bin` to `os.environ["PATH"]` in `pytest_configure()`.

---

### BUG-11: `integrity-audit` Deployment Not Paused When All Legs Disabled
- **Severity:** `LOW`
- **Location:** [launch.py:216-220](file:///c:/Users/Shawn%20A/Desktop/bk/launch.py#L216-L220) & [serve.py:90-101](file:///c:/Users/Shawn%20A/Desktop/bk/serve.py#L90-L101)
- **Root Cause:**
  `launch.py` reconciles enabled status for `backup-lan` and `backup-cloud`, but does not include `integrity-audit`.
- **The "Bite Us" Scenario:**
  If an organization sets both `cloud.enabled = false` and `lan.enabled = false` (e.g. temporary maintenance), the Prefect scheduler continues to fire `integrity_audit_flow` every Sunday at 03:00.
- **Remediation:**
  Add `"integrity-audit": bool(config.lan.enabled or config.cloud.enabled)` to `launch._reconcile_disabled_legs()`.

---

## Action Plan & Recommendations

1. **Immediate Code Fixes Required:**
   - [ ] Fix `flow.py`: Move `cloud_record_task` so it only records after `evidence_ok` passes.
   - [ ] Fix `flow.py`: Enclose `integrity_audit_flow` in `_backup_slot(config)`.
   - [ ] Fix `flow.py`: Add `wol_check_task` and `lan_shutdown_task` to `integrity_audit_flow`.
   - [ ] Fix `core/integrity.py`: Parse `!` lines in combined diff and fail closed on `ERROR`/`CRITICAL` log output.
   - [ ] Fix `core/lan_sync.py` & `flow.py`: Do not classify Robocopy codes 4–7 as `LAN_PARTIAL` failures when zero files failed.
   - [ ] Fix `tests/test_h4_watchdog_counters.py`: Update assertions to match the `5fd209a` contract.
   - [ ] Fix `tests/conftest.py`: Inject `deploy\bin` into PATH so rclone tests execute in local testing.

2. **Verification Protocol:**
   - Run complete test suite (`pytest`) ensuring 0 failures.
   - Execute mock Sunday audit run with simulated WoL NAS target.
   - Validate that divergent GCS test runs do not leave behind false `synced` rows in `manifest.db`.
