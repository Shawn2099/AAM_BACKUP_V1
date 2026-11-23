# Production Release Code Audit Findings

**Date**: 2026-09-08  
**Repository**: AAM_BACKUP_V1  
**Target Release**: `production` branch (`2f93d66`)  
**Scope**: File-by-file, function-by-function line-by-line audit.

---

## 1. `models/config.py`

### 1.1 Class `PathsConfig`

#### Finding 1.1.1: Case-sensitive `.db` file extension check
- **Location**: `models/config.py:43` in `derive_runtime_paths()`
- **Severity**: Low
- **Category**: Input Validation / Portability
- **Code**:
  ```python
  if not self.database_path.endswith(".db"):
      raise ValueError(f"database_path must end with .db: {self.database_path}")
  ```
- **Description**: On Windows, file systems are case-insensitive. If an operator specifies an uppercase or mixed-case extension (e.g., `manifest.DB`), validation raises a `ValueError`.
- **Recommendation**:
  ```python
  if not self.database_path.lower().endswith(".db"):
  ```

#### Finding 1.1.2: Unanchored relative path for `backup_lock_path`
- **Location**: `models/config.py:48-50` in property `backup_lock_path`
- **Severity**: Medium
- **Category**: Concurrency / Race Condition
- **Code**:
  ```python
  @property
  def backup_lock_path(self) -> Path:
      """Derive backup.lock path from the database_path parent directory."""
      return Path(self.database_path).parent / "backup.lock"
  ```
- **Description**: If `database_path` is explicitly configured as a relative path without directory components (e.g. `"manifest.db"`), `Path("manifest.db").parent` evaluates to `Path(".")`. This causes different processes (watchdog, CLI, service) to create and check lock files in their respective current working directories rather than in `runtime_dir`, risking undetected concurrent execution.
- **Recommendation**:
  Anchor the lock file directly to `Path(self.runtime_dir) / "backup.lock"` or resolve `Path(self.database_path).resolve().parent / "backup.lock"`.

#### Finding 1.1.3: Mandatory `gcs_key_path` breaks LAN-only deployments
- **Location**: `models/config.py:34, 71-76` in `PathsConfig` and `gcs_key_exists()`
- **Severity**: Medium
- **Category**: Usability / Architectural Constraint
- **Code**:
  ```python
  gcs_key_path: str = Field(..., description="Path to GCS service account JSON key file")
  ...
  @field_validator("gcs_key_path")
  @classmethod
  def gcs_key_exists(cls, v: str) -> str:
      if not v:
          raise ValueError("gcs_key_path must not be empty when cloud is enabled")
      return v
  ```
- **Description**: `PathsConfig` declares `gcs_key_path` as a required field (`...`) and rejects empty strings at the field validator level. Even when `cloud.enabled = False` (LAN-only deployment), the configuration parser will fail unless the operator supplies a dummy GCS key path string. This contradicts `AppConfig.cross_field_validation` which already checks `if self.cloud.enabled and not self.paths.gcs_key_path`.
- **Recommendation**:
  Change default to `gcs_key_path: str = Field(default="", ...)` and remove the unconditional empty check in `gcs_key_exists`, delegating cloud-enabled enforcement exclusively to `cross_field_validation`.

### 1.2 Class `WolConfig`

#### Finding 1.2.1: Missing whitespace stripping on critical network parameters
- **Location**: `models/config.py:102-181` in `WolConfig`
- **Severity**: Low
- **Category**: Robustness / User Input
- **Description**: Unlike `PathsConfig` which specifies `model_config = ConfigDict(str_strip_whitespace=True)`, `WolConfig` (and other sub-models) does not strip whitespace. Accidental trailing whitespace in YAML (e.g. `mac_address: "AA:BB:CC:DD:EE:FF "` or `server_ip: "192.168.10.10 "`) causes `re.match` and `ipaddress.IPv4Address` to reject valid values with validation errors.
- **Recommendation**: Add `model_config = ConfigDict(str_strip_whitespace=True)` to `WolConfig` or set it globally on the base model.

#### Finding 1.2.2: Hardcoded `/24` subnet assumption in `get_broadcast_address`
- **Location**: `models/config.py:165-181` in `get_broadcast_address()`
- **Severity**: Medium
- **Category**: Network Architecture
- **Code**:
  ```python
  def get_broadcast_address(self) -> str:
      if self.broadcast_address:
          return self.broadcast_address
      parts = self.server_ip.rsplit(".", 1)
      return f"{parts[0]}.255"
  ```
- **Description**: When `broadcast_address` is omitted, the method blindly assumes a class C (`/24`) subnet mask and replaces the final octet with `255`. If the target server resides on a `/16`, `/22`, or `/23` subnet (e.g., `10.0.1.50/16`), packets are broadcast to `10.0.1.255` instead of `10.0.255.255`, causing Wake-on-LAN packets to be silently dropped by routers/switches.
- **Recommendation**: Document prominently or compute local interface broadcast dynamically, or require explicit `broadcast_address` if non-/24 subnets are detected.

### 1.3 Class `CloudConfig`

#### Finding 1.3.1: Hardcoded production project number default
- **Location**: `models/config.py:199` in `CloudConfig.project_number`
- **Severity**: High
- **Category**: Security / Multi-tenancy / Configuration Leak
- **Code**:
  ```python
  project_number: str = "920173882190"
  ```
- **Description**: `CloudConfig` defines a specific GCP numerical project ID (`920173882190`) as the hardcoded default. If a new deployment or customer instance omits `project_number` in `config.yaml`, operations attempt to authorize or associate storage against this external/developer GCP project number rather than failing fast.
- **Recommendation**: Set default to `""` and enforce in `model_validator` that when `cloud.enabled` is `True`, `project_number` must be explicitly provided and non-empty.

### 1.4 Class `NotificationConfig`

#### Finding 1.4.1: Missing validation for enabled failure/report alerts with missing SMTP credentials
- **Location**: `models/config.py:255-276` in `NotificationConfig`
- **Severity**: Low
- **Category**: Fault Tolerance / Usability
- **Description**: `NotificationConfig` defaults `send_on_failure = True`, `weekly_enabled = True`, and `monthly_enabled = True`, while `smtp_host`, `smtp_username`, and `recipients` default to empty strings/lists. There is no validator warning or preventing the application from starting if notifications are enabled without required SMTP configuration.
- **Recommendation**: Add validation or warning in `cross_field_validation` if `send_on_failure` is True but `recipients` is empty or `smtp_host` is unset.

### 1.5 Class `AppConfig` & Function `load_config`

#### Finding 1.5.1: FY mismatch safety guard bypassed if FY folder is not the terminal path segment
- **Location**: `models/config.py:484-488` in `cross_field_validation()`
- **Severity**: High
- **Category**: Data Integrity / Catastrophic Overwrite Risk
- **Code**:
  ```python
  src_parts = self.paths.source_drive.replace("\\", "/").rstrip("/").split("/")
  src_fy = src_parts[-1].upper() if fy_pattern.match(src_parts[-1]) else None

  lan_parts = self.paths.lan_destination.replace("\\", "/").rstrip("/").split("/")
  lan_fy = lan_parts[-1].upper() if fy_pattern.match(lan_parts[-1]) else None
  ```
- **Description**: The safety guard designed to prevent mirroring between mismatched financial years (e.g. FY25-26 into FY24-25) only inspects the final segment (`parts[-1]`). If the path contains subfolders beneath the FY folder (e.g. `D:\FY25-26\Tally` vs `\\server\share\FY24-25\Tally`), `parts[-1]` is `"Tally"`, which causes both `src_fy` and `lan_fy` to evaluate to `None`. The mismatch check is completely bypassed, leaving the deployment vulnerable to overwriting the wrong year's archive.
- **Recommendation**: Iterate over all segments in both paths to locate any matching `fy_pattern`:
  ```python
  src_fy = next((s.upper() for s in src_parts if fy_pattern.match(s)), None)
  lan_fy = next((s.upper() for s in lan_parts if fy_pattern.match(s)), None)
  ```

#### Finding 1.5.2: Unhandled empty YAML in `from_yaml`
- **Location**: `models/config.py:501-504` in `from_yaml()`
- **Severity**: Low
- **Category**: Error Handling
- **Code**:
  ```python
  @classmethod
  def from_yaml(cls, path: str) -> "AppConfig":
      with open(path, encoding="utf-8") as f:
          data = yaml.safe_load(f)
      return cls(**data)
  ```
- **Description**: If `config.yaml` is empty (e.g. 0 bytes), `yaml.safe_load(f)` returns `None`. Unpacking `**None` raises `TypeError: AppConfig.__init__() argument after ** must be a mapping, not NoneType` rather than an informative configuration error.
- **Recommendation**: Default `data = yaml.safe_load(f) or {}` or raise a clear `ValueError("Configuration file is empty")`.

---

## 2. `core/hashing.py`

### 2.1 Function `compute_md5`

#### Finding 2.1.1: Unhandled Windows file-sharing locks during live hashing
- **Location**: `core/hashing.py:18-22` in `compute_md5()`
- **Severity**: Medium
- **Category**: Windows Compatibility / Concurrency
- **Code**:
  ```python
  with open(file_path, "rb") as f:
      for chunk in iter(lambda: f.read(65536), b""):
          md5_hash.update(chunk)
  ```
- **Description**: On Windows, opening a file for reading using standard `open(file_path, "rb")` without `FILE_SHARE_READ | FILE_SHARE_WRITE` sharing flags or retry semantics causes `PermissionError: [WinError 32] The process cannot access the file because it is being used by another process` if an application (such as Tally or an active database process) has the file open.
- **Recommendation**: Wrap in try/except to return or raise a structured `FileLockError` or open with shared read permissions.

### 2.2 Function `verify_checksum`

#### Finding 2.2.1: Case-sensitive checksum comparison
- **Location**: `core/hashing.py:33` in `verify_checksum()`
- **Severity**: Low
- **Category**: Robustness
- **Code**:
  ```python
  return compute_md5(file_path) == expected
  ```
- **Description**: `compute_md5` produces lowercase hex digits. If `expected` contains uppercase hex digits (as generated by some Windows utilities like `CertUtil -hashfile`), the verification check evaluates to `False`, falsely flagging files as corrupted.
- **Recommendation**: Compare using `compute_md5(file_path).lower() == expected.lower()`.

---

## 3. `core/time_utils.py`

### 3.1 Function `cron_to_human`
- **Assessment**: Audited line-by-line. Exception handling properly shields callers (`MissingFieldException`, `FormatException`, and generic `Exception` fall back cleanly to raw string). Correctly handles 24-hour formatting.
- **Note**: Requires `cron-descriptor` package installed in the active virtualenv.

---

## 4. `core/logging.py`

### 4.1 Function `configure`

#### Finding 4.1.1: `logger.remove()` silently purges Prefect bridge without resetting flag
- **Location**: `core/logging.py:25` in `configure()`
- **Severity**: Medium
- **Category**: Observability / State Inconsistency
- **Code**:
  ```python
  logger.remove()
  ```
- **Description**: `configure()` strips all existing Loguru sinks via `logger.remove()`. If `configure_prefect_bridge()` was called previously, the sink is removed from Loguru, but `_bridge_configured` remains `True`. Any subsequent invocation of `configure_prefect_bridge()` returns early, leaving the Prefect bridge permanently unconfigured and stopping flow logs from reaching Prefect.
- **Recommendation**: Reset `_bridge_configured = False` inside `configure()`, or re-attach the bridge if running within a Prefect flow.

### 4.2 Function `configure_prefect_bridge`

#### Finding 4.2.1: Exception tracebacks discarded in Prefect bridge forwarder
- **Location**: `core/logging.py:96-107` in `prefect_sink()`
- **Severity**: Low
- **Category**: Observability
- **Code**:
  ```python
  msg_str = message.record["message"]
  ...
  prefect_logger.error(msg_str)
  ```
- **Description**: `prefect_sink` forwards only `message.record["message"]` to Prefect. If an error is logged with `logger.exception(...)` or `logger.error("...", exc_info=True)`, `message.record["exception"]` is ignored, resulting in lost stack traces in Prefect console logs.
- **Recommendation**: Check `if message.record.get("exception"):` and format or append the traceback before dispatching to `prefect_logger`.

---

## 5. `core/process.py`

### 5.1 Function `acquire_lock`

#### Finding 5.1.1: Double read without interval fails to absorb transient I/O flaps
- **Location**: `core/process.py:170-175` in `acquire_lock()`
- **Severity**: Medium
- **Category**: Concurrency / Race Condition
- **Code**:
  ```python
  alive, _ = read_lock_alive(lock_path)
  if alive:
      return False
  alive, _ = read_lock_alive(lock_path)
  if alive:
      return False
  ```
- **Description**: The design intention stated in comments is to absorb transient antivirus locks or parse flaps by requiring two dead reads before overwriting. However, the second read is executed immediately in nanoseconds without any sleep or backoff interval (`time.sleep(0.05)`). A transient file-system or scanner lock lasting even 1 millisecond will fail to resolve within this instantaneous window.
- **Recommendation**: Introduce a short delay (e.g. `time.sleep(0.1)`) between the two reads to fulfill the anti-flap specification.

#### Finding 5.1.2: Residual zero-byte lock file on write failure
- **Location**: `core/process.py:164-167, 186-192` in `acquire_lock()`
- **Severity**: Low
- **Category**: Error Handling / Clean-up
- **Description**: If `os.write(fd, ...)` fails with an `OSError` (e.g., storage disk full), `fd` is closed, but the newly created 0-byte file remains at `lock_path`. While subsequent runs will treat a 0-byte file as unreadable, leaving empty lock files behind can cause confusion during operational audits.
- **Recommendation**: Add a cleanup handler `lock_path.unlink(missing_ok=True)` if writing fails after file creation.

---

## 6. `core/manifest.py`

### 6.1 Class `ManifestDB`

#### Finding 6.1.1: `prune_stale_synced` fails to delete entries with `'unknown'` peer status
- **Location**: `core/manifest.py:464-467` in `prune_stale_synced()`
- **Severity**: Medium
- **Category**: Data Hygiene / Database Bloat
- **Code**:
  ```python
  conn.execute(
      "DELETE FROM file_entries "
      "WHERE lan_status IS NULL AND cloud_status IS NULL"
  )
  ```
- **Description**: Table columns `lan_status` and `cloud_status` default to `'unknown'` in DDL. If a file is deleted from the source and pruned on LAN (`lan_status` set to `NULL`), but Cloud sync was disabled or hasn't processed the file (`cloud_status` remains `'unknown'`), the deletion condition `lan_status IS NULL AND cloud_status IS NULL` evaluates to `False`. The stale record remains in `file_entries` indefinitely as an orphan.
- **Recommendation**: Update delete condition to account for both `NULL` and `'unknown'`:
  ```sql
  DELETE FROM file_entries
  WHERE (lan_status IS NULL OR lan_status = 'unknown')
    AND (cloud_status IS NULL OR cloud_status = 'unknown')
  ```

#### Finding 6.1.2: Unhandled `sqlite3.OperationalError` during `VACUUM` in `purge_old_runs`
- **Location**: `core/manifest.py:658-666` in `purge_old_runs()`
- **Severity**: Medium
- **Category**: Concurrency / Resilience
- **Code**:
  ```python
  if freelist and freelist[0] > self.vacuum_freelist_threshold:
      page_size = conn.execute("PRAGMA page_size").fetchone()[0]
      conn.commit()
      conn.execute("VACUUM")
  ```
- **Description**: SQLite's `VACUUM` command requires an exclusive lock on the entire database file. If another concurrent process (such as `watchdog.py`, `serve.py`, or the dashboard API server) holds an active read lock or transaction, `conn.execute("VACUUM")` fails with an unhandled `sqlite3.OperationalError: database is locked`, crashing the purge routine.
- **Recommendation**: Wrap `conn.execute("VACUUM")` in a `try...except sqlite3.OperationalError:` block and log a warning if the database is busy, allowing routine maintenance to continue.

#### Finding 6.1.3: Default threshold discrepancy between config and `ManifestDB.__init__`
- **Location**: `core/manifest.py:111` vs `models/config.py:330`
- **Severity**: Low
- **Category**: Consistency
- **Description**: `MaintenanceConfig` in `models/config.py` specifies a default of `10000` pages (~40 MB) for `sqlite_vacuum_freelist_threshold`, whereas `ManifestDB.__init__` defaults to `1000` pages (~4 MB). If `ManifestDB` is instantiated directly without injecting config settings, it runs VACUUM 10x more aggressively.
- **Recommendation**: Align default in `ManifestDB.__init__` to `10000`.

---

## 7. `core/lan_manifest.py`

### 7.1 Function `walk_lan_destination`

#### Finding 7.1.1: `Path(unc_path).resolve()` risks SMB network hang and `relpath` mount mismatch
- **Location**: `core/lan_manifest.py:37, 50` in `walk_lan_destination()`
- **Severity**: High
- **Category**: Windows Compatibility / Performance / Reliability
- **Code**:
  ```python
  base = str(Path(unc_path).resolve())
  ...
  rel_raw = os.path.relpath(full, base)
  ```
- **Description**: Calling `Path(unc_path).resolve()` on a Windows UNC path attempts remote SMB symlink/canonical resolution via Windows APIs. If the server is offline or unreachable, `resolve()` blocks for the OS network timeout. Furthermore, if `resolve()` converts the UNC path to extended-length syntax (`\\?\UNC\...`), subsequent calls to `os.path.relpath(full, base)` fail with an uncaught `ValueError: path is on mount ..., start on mount ...`, crashing the entire walk.
- **Recommendation**: Avoid `Path.resolve()` on UNC paths. Use `os.path.abspath(unc_path)` or string normalization to avoid remote RPC resolution and preserve standard UNC path format.

#### Finding 7.1.2: Fragile root error detection allows offline destinations to report 0 files
- **Location**: `core/lan_manifest.py:60-69` in `walk_lan_destination()`
- **Severity**: High
- **Category**: Error Handling / Silent Data Corruption
- **Code**:
  ```python
  root_failed = any(
      getattr(e, "filename", None) in (unc_path, str(Path(unc_path)))
      for e in errors
  )
  if root_failed and not files:
      raise OSError(...)
  ```
- **Description**: `root_failed` performs exact string matching between `e.filename` and `unc_path`. On Windows, `os.walk` error callbacks often supply normalized paths (e.g. without trailing backslash, or with different slash direction). If `e.filename` differs from `unc_path` only by a trailing slash, `root_failed` evaluates to `False`. The function then logs a warning and returns `files = []`, misleading downstream routines into believing the destination was cleanly purged.
- **Recommendation**: Check if any error occurred at or above the root directory using normalized path comparison (`os.path.normpath(e.filename) == os.path.normpath(unc_path)`), or raise immediately if `errors` is non-empty and `len(files) == 0`.

---

## 8. `core/backup_repository.py`

### 8.1 Function `record_sync_results`

#### Finding 8.1.1: Single-mode removal unconditionally wipes cross-mode sync state
- **Location**: `core/backup_repository.py:66-69` in `record_sync_results()`
- **Severity**: High
- **Category**: Data Integrity / Metadata Destruction
- **Code**:
  ```python
  if removed:
      clean_removed = [_clean_path(p) for p in removed if _clean_path(p)]
      if clean_removed:
          db.delete_entries(clean_removed)
  ```
- **Description**: When `record_sync_results` is invoked with `removed` files (e.g. after LAN backup diff calculation or GCS sync), it calls `db.delete_entries(clean_removed)`. In `ManifestDB`, `delete_entries()` executes `DELETE FROM file_entries WHERE relative_path IN (...)`. This immediately and unconditionally deletes the entire row from SQLite, wiping out the peer destination's sync metadata (`cloud_status = 'synced'` and `cloud_last_synced_at` when called from LAN, or vice versa).
- **Recommendation**: Instead of deleting the row directly, call a scoped removal method that nulls out only `{mode}_status` and `{mode}_last_synced_at`, only deleting the row if the other mode is also absent or null.

#### Finding 8.1.2: Pruning skipped if `entries` is empty
- **Location**: `core/backup_repository.py:41-65` in `record_sync_results()`
- **Severity**: Low
- **Category**: State Inconsistency
- **Code**:
  ```python
  if entries:
      ...
      active_paths = {item["path"] for item in normalized}
      pruned = db.prune_stale_synced(mode, active_paths)
  ```
- **Description**: If the destination has 0 files (or `entries` is empty), the entire block `if entries:` is bypassed. Consequently, `prune_stale_synced(mode, active_paths=set())` is never called, leaving prior entries erroneously marked as `'synced'` in the database when the destination has in fact been emptied.
- **Recommendation**: Allow pruning to execute when `entries` is empty (if the empty inventory was verified as legitimate).

---

## 9. `core/wol.py`

### 9.1 Function `wait_for_server`

#### Finding 9.1.1: Non-monotonic clock usage in wake timeout loop
- **Location**: `core/wol.py:100-101` in `wait_for_server()`
- **Severity**: Low
- **Category**: Reliability / Timing
- **Code**:
  ```python
  start_time = time.time()
  while time.time() - start_time < wake_timeout:
  ```
- **Description**: The timeout loop measures elapsed wall-clock time via `time.time()`. If a system NTP synchronization or manual clock adjustment occurs while waiting for the NAS to wake, the timeout calculation can jump backward (prolonging the loop past `wake_timeout`) or forward (causing premature timeout).
- **Recommendation**: Use `time.monotonic()` for timeout loops:
  ```python
  start_time = time.monotonic()
  while time.monotonic() - start_time < wake_timeout:
  ```

---

## 10. `core/shutdown.py`

### 10.1 Function `shutdown_server`
- **Assessment**: Audited line-by-line. Subprocess execution uses safe list syntax and explicit 30s timeout. Handles `FileNotFoundError`, `TimeoutExpired`, and generic `OSError` without raising unhandled exceptions.

---

## 11. `core/lan_preflight.py`

### 11.1 Function `run_lan_dry_run`

#### Finding 11.1.1: Asymmetric contract between pre-checks and execution failures
- **Location**: `core/lan_preflight.py:73, 83, 98` vs `133, 140`
- **Severity**: Low
- **Category**: API Contract / Consistency
- **Description**: If SMB reachability fails or the canary file is missing, `run_lan_dry_run` raises a `HealthError` exception. In contrast, if Robocopy fails with error codes >= 8, times out, or encounters an `OSError`, it suppresses exceptions and returns a dictionary `{"ok": False, "exit_code": ..., "error": ...}`. Callers must handle both exception propagation and return dictionary inspection.
- **Recommendation**: Standardize the return contract (e.g. return failure dict for all non-fatal preflight conditions, or raise domain exceptions consistently).

#### Finding 11.1.2: Default timeout parameter mismatch with configuration model
- **Location**: `core/lan_preflight.py:48` vs `models/config.py:99`
- **Severity**: Low
- **Category**: Configuration Drift
- **Description**: `models/config.py` defaults `LanConfig.dry_run_timeout_seconds` to `900` seconds to accommodate multi-million file walks. However, the `run_lan_dry_run(..., timeout: int = 300)` function signature defaults to `300` seconds. Any external caller, test, or maintenance script calling `run_lan_dry_run` without passing explicit config values will prematurely time out at 5 minutes on production-scale shares.
- **Recommendation**: Align default `timeout: int = 900` in function signature.

---

## 12. `core/lan_sync.py`

### 12.1 Function `classify_exit_code` vs `decide_lan_result`

#### Finding 12.1.1: Exit code 4-7 classification divergence across modules
- **Location**: `core/lan_sync.py:289-291` vs `212-244`
- **Severity**: Medium
- **Category**: Consistency / Domain Logic
- **Code**:
  ```python
  # In classify_exit_code():
  if 4 <= code <= 7:
      return "LAN_PARTIAL"

  # In decide_lan_result():
  if 0 <= exit_code <= 7:
      ...
      return {"status": "LAN_COMPLETE", ...}
  ```
- **Description**: `classify_exit_code` maps exit codes 4–7 (mismatches/extras, no copy failures) to `"LAN_PARTIAL"`. However, `decide_lan_result` maps codes 4–7 to `"LAN_COMPLETE"`. `core/fy_rollover.py` imports and relies on `classify_exit_code`, causing identical Robocopy exit codes (such as 4) to be classified as `LAN_PARTIAL` during fiscal-year rollover while being classified as `LAN_COMPLETE` during daily backup.
- **Recommendation**: Align `classify_exit_code` with the hardened `decide_lan_result` contract so that all flows interpret Robocopy bitmasks uniformly.

### 12.2 Function `run_lan_sync`

#### Finding 12.2.1: Uncaught `ValueError` crashes `run_lan_sync` on empty source or invalid flags
- **Location**: `core/lan_sync.py:428` in `run_lan_sync()`
- **Severity**: High
- **Category**: Exception Handling / Crash Resilience
- **Code**:
  ```python
  cmd = build_robocopy_command(source, dest, lan_config)
  log_path = None
  log_retained = None

  try:
      ...
  ```
- **Description**: `build_robocopy_command` executes `_validate_required_flags` and `_assert_source_not_empty(source)`. Both can raise `ValueError` (e.g. if the source drive is empty, disconnected, or unreadable). Because `build_robocopy_command` is called outside the `try...except` block, and the `except` blocks do not handle `ValueError`, the error escapes unhandled and crashes the caller instead of returning a structured `LAN_FAILED` result dict.
- **Recommendation**: Move `build_robocopy_command` inside the `try:` block and add `except ValueError as exc:` returning `{"status": "LAN_FAILED", "error": str(exc), ...}`.

---

## 13. `core/rclone_config.py`

### 13.1 Function `write_temp_config`
- **Assessment**: Audited line-by-line. Correctly generates GCS configuration, handles `mkstemp` file descriptor closure, and safely catches Windows `os.chmod` exceptions.

---

## 14. `core/cloud_preflight.py`

### 14.1 Function `run_cloud_dry_run`
- **Assessment**: Audited line-by-line. Two-probe design (local drive iterdir + `rclone lsjson --max-depth 0`) avoids full disk traversal. Handles timeout, missing binary, and OS errors, returning structured status dictionaries.

---

## 15. `core/cloud_sync.py`

### 15.1 Function `run_cloud_sync`

#### Finding 15.1.1: Uncaught `ValueError` on empty source in `run_cloud_sync`
- **Location**: `core/cloud_sync.py:209` in `run_cloud_sync()`
- **Severity**: High
- **Category**: Exception Handling / Crash Resilience
- **Code**:
  ```python
  cmd = build_rclone_sync_command(...)
  ...
  try:
      with open(stderr_path, "w", encoding="utf-8") as stderr_file:
  ```
- **Description**: Like `run_lan_sync`, `build_rclone_sync_command` calls `_assert_source_not_empty(source)`, raising `ValueError` if the source directory appears empty or unmounted. Because this call occurs outside the `try...except` block, and `ValueError` is not in the handled exceptions, an empty source drive raises an unhandled exception rather than returning a clean `{"status": "CLOUD_FAILED", ...}` dictionary.
- **Recommendation**: Move `build_rclone_sync_command` into the `try:` block and catch `ValueError`.

#### Finding 15.1.2: Unbounded stderr log capture causes memory bloat on massive failure runs
- **Location**: `core/cloud_sync.py:235, 271-273` in `run_cloud_sync()`
- **Severity**: Medium
- **Category**: Resource Management / DoS Risk
- **Code**:
  ```python
  stderr_text = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
  ...
  elif result.returncode != 0:
      if stderr_text:
          error_msg = stderr_text
          logger.error(f"rclone error: {error_msg}")
  ```
- **Description**: If a sync run encounters massive network or permission failures across hundreds of thousands of files, rclone writes errors for every failed transfer to `stderr_path`. Reading the entire file without byte bounds (`read_text()`) and assigning it to `error_msg` passes multi-megabyte payloads to Loguru and SQLite `run_history`, risking high memory pressure and slow database transactions.
- **Recommendation**: Apply tail bounding (e.g. read the last 100KB, matching `_ERROR_LOG_TAIL` in `lan_sync.py`).

---

## 16. `core/cloud_verify.py`

### 16.1 Function `verify_cloud_integrity`
- **Assessment**: Audited line-by-line. Correctly enforces F-08 completion contract by requiring both exit code 0 and valid summary strings in the log stream. Prevents false-positive verification on externally killed processes.

---

## 17. `core/cloud_reporter.py`

### 17.1 Function `get_cloud_diff`

#### Finding 17.1.1: Unhandled `!` (error) lines in `--combined` diff output
- **Location**: `core/cloud_reporter.py:202-216` in `get_cloud_diff()`
- **Severity**: Medium
- **Category**: Error Handling / Silent Data Loss
- **Code**:
  ```python
  if symbol == "+":
      diff["added"].append(p)
  elif symbol == "-":
      diff["removed"].append(p)
  elif symbol == "*":
      diff["modified"].append(p)
  elif symbol == "=":
      diff["unchanged"].append(p)
  ```
- **Description**: In `rclone check --combined`, a line starting with `!` indicates that a file encountered a read or hash error during comparison. The parser only checks for `+`, `-`, `*`, and `=`, completely ignoring `!` lines. If an I/O error occurs on specific files during diff generation, those files are silently omitted from the resulting report.
- **Recommendation**: Add handling for `symbol == "!"`, recording them into an `errors` list within `diff` and marking `diff["_partial"] = True`.

### 17.2 Function Signatures (`get_cloud_size`, `get_cloud_manifest`, `get_cloud_diff`)

#### Finding 17.2.1: Hardcoded function parameter timeouts diverge from config model
- **Location**: `core/cloud_reporter.py:34, 85, 141` vs `models/config.py:214-216`
- **Severity**: Low
- **Category**: Configuration Drift
- **Description**: `CloudConfig` in `models/config.py` specifies defaults scaled for production workloads: `cloud_size_timeout_seconds = 300`, `manifest_timeout_seconds = 900`, `diff_timeout_seconds = 1800`. However, the function signatures in `cloud_reporter.py` default to `30s`, `300s`, and `600s` respectively. Direct callers or maintenance routines omitting explicit timeout parameters will use inadequate timeouts.
- **Recommendation**: Update function default parameters to match `CloudConfig` defaults (`300`, `900`, `1800`).

---

## 18. `core/fy_rollover.py`

### 18.1 Function `_parent_path`

#### Finding 18.1.1: UNC share root path recursion bug during rollover
- **Location**: `core/fy_rollover.py:77-89` in `_parent_path()`
- **Severity**: High
- **Category**: Path Traversal / Data Loss / Formatting
- **Code**:
  ```python
  if path_str.startswith("\\\\"):
      p = PureWindowsPath(path_str)
      return str(p.parent).rstrip("\\")
  ```
- **Description**: Under Windows UNC rules, `\\host\share` is the root anchor of the UNC path; in `pathlib.PureWindowsPath`, calling `.parent` on `\\host\share` returns `\\host\share` itself. If an operator configures `lan_destination: "\\\\192.168.10.10\\FY25-26"` (where the share name itself is the FY folder), `_parent_path` returns `\\\\192.168.10.10\\FY25-26`. The next step `_child_path` appends the new FY, generating `\\\\192.168.10.10\\FY25-26\\FY26-27`. Over successive rollovers, directories nest infinitely inside previous years' shares instead of replacing the target.
- **Recommendation**: Handle 2-segment UNC paths (`\\host\share`) explicitly or enforce in configuration validation that UNC destinations must include a parent share and subfolder (e.g. `\\host\share\FYxx-xx`).

### 18.2 Function `detect_rollover` & `_fy_name`

#### Finding 18.2.1: Rollover permanently bypassed if subdirectories exist under FY folder
- **Location**: `core/fy_rollover.py:68-75, 97-114`
- **Severity**: High
- **Category**: Business Logic / Rollover Failure
- **Code**:
  ```python
  parts = path_str.replace("\\", "/").rstrip("/").split("/")
  name = parts[-1]
  return name.upper() if FY_PATTERN.match(name) else None
  ```
- **Description**: `_fy_name` strictly inspects `parts[-1]` (the leaf directory name). If paths are configured with subdirectories beneath the FY directory (e.g. `D:\FY25-26\Data` or `\\server\share\FY25-26\Company`), `_fy_name` returns `None`. Consequently, `detect_rollover` logs a warning and returns `False`, permanently disabling automated fiscal-year rollover for that deployment.
- **Recommendation**: Scan all segments of the path for `FY_PATTERN` rather than restricting inspection to `parts[-1]`.

---

## 19. `core/health.py`

### 19.1 Function `check_source_drive`

#### Finding 19.1.1: Windows hidden system folders bypass empty-source safety gate
- **Location**: `core/health.py:33` in `check_source_drive()`
- **Severity**: High
- **Category**: Data Integrity / Catastrophic Data Loss Risk
- **Code**:
  ```python
  has_files = any(source.iterdir())
  ```
- **Description**: On Windows, formatted NTFS drives and root directories automatically contain system directories such as `System Volume Information` and `$RECYCLE.BIN`. `any(source.iterdir())` returns `True` if any entry exists, even if the drive contains nothing but these two OS-created folders. Since Robocopy and Rclone explicitly exclude these system directories via `/XD`, an otherwise empty drive passes the `check_source_drive` gate, allowing mirror commands to purge the destination.
- **Recommendation**: Exclude standard Windows system directories when checking for user files:
  ```python
  _SYSTEM_DIRS = {"System Volume Information", "$RECYCLE.BIN"}
  has_files = any(p for p in source.iterdir() if p.name not in _SYSTEM_DIRS)
  ```

### 19.2 Function `pre_backup_health`

#### Finding 19.2.1: Optional `gcs_key_path` check in cloud mode
- **Location**: `core/health.py:159-162` in `pre_backup_health()`
- **Severity**: Low
- **Category**: Input Validation
- **Code**:
  ```python
  if mode in ("cloud", "all"):
      if not check_binary_exists("rclone"):
          raise HealthError("rclone not found in PATH")
      if gcs_key_path:
          ok, reason = check_gcs_key(gcs_key_path)
  ```
- **Description**: In `cloud` mode, `check_gcs_key` is only executed `if gcs_key_path:`. If `gcs_key_path` is passed as `None` or `""`, the health check passes silently, and the failure only surfaces downstream when `rclone` fails to load configuration.
- **Recommendation**: Enforce `if not gcs_key_path: raise HealthError("GCS key path is required for cloud mode")`.

---

## 20. `core/integrity.py`

### 20.1 Function `audit_lan`

#### Finding 20.1.1: Unnormalized Windows backslashes in `--include` scope prefixes
- **Location**: `core/integrity.py:351-352` in `audit_lan()`
- **Severity**: Low
- **Category**: Windows Compatibility / Filtering
- **Code**:
  ```python
  for p in scope_prefixes:
      extra.extend(["--include", f"{p.strip('/')}/**"])
  ```
- **Description**: Rclone pattern matching requires forward slashes `/` for path filtering. If an operator passes a Windows-style path with backslashes in `scope_prefixes` (e.g. `["Accounting\\2026"]`), `p.strip('/')` does not normalize backslashes, causing rclone to fail matching any files under that shard prefix.
- **Recommendation**: Normalize path separators:
  ```python
  norm_p = p.replace("\\", "/").strip("/")
  extra.extend(["--include", f"{norm_p}/**"])
  ```

---

## 21. `core/report.py`

### 21.1 Function `_send_email_with_attachments`

#### Finding 21.1.1: Mandatory credentials block unauthenticated internal SMTP relays
- **Location**: `core/report.py:42-44` in `_send_email_with_attachments()`
- **Severity**: Low
- **Category**: Usability / Enterprise Environment
- **Code**:
  ```python
  if not config.smtp_username or not config.smtp_password:
      logger.warning("SMTP credentials not set - skipping")
      return False
  ```
- **Description**: Many corporate and SMB networks use internal SMTP relays (on port 25 or 587 with IP whitelisting) that do not require or support username/password authentication. The current code unconditionally skips email delivery if credentials are empty.
- **Recommendation**: Allow sending without authentication if `smtp_username` is empty, only calling `server.login()` when credentials are provided.

---

## 22. `flow.py`

### 22.1 Function `_run_cloud_pipeline`

#### Finding 22.1.1: False transfer metrics on zero-change sync or empty initial database
- **Location**: `flow.py:694-726` in `_run_cloud_pipeline()`
- **Severity**: Medium
- **Category**: Reporting / Metric Corruption
- **Code**:
  ```python
  manifest = verify_data.get("manifest", [])
  copied_files_list = []
  for item in manifest:
      ...
      if path not in before_dict:
          copied_files_list.append((path, size))
  ```
- **Description**: Differential transfer volume in `_run_cloud_pipeline` is computed by comparing the cloud manifest with `before_dict` (`db.get_cloud_synced_entries()`). When `status == "CLOUD_NO_CHANGES_COMPLETE"` (rclone exit code 9, indicating zero files transferred) or on an initial run where `manifest.db` is empty, every file in the bucket is absent from `before_dict`. The code classifies all remote files as "copied", logging and recording thousands of files and gigabytes of phantom transfers for a zero-transfer run.
- **Recommendation**:
  If `status == "CLOUD_NO_CHANGES_COMPLETE"` or `exit_code == 9`, explicitly set `files_copied = 0` and `bytes_copied = 0` without evaluating `before_dict`.

### 22.2 Function `lan_snapshot_before_task`

#### Finding 22.2.1: Unhandled exception during pre-sync destination snapshot crashes entire pipeline
- **Location**: `flow.py:357-364` in `lan_snapshot_before_task()`
- **Severity**: High
- **Category**: Robustness / Fault Tolerance / SMB Flakiness
- **Code**:
  ```python
  @task(name="lan-snapshot-before")
  def lan_snapshot_before_task(config):
      """Snapshot LAN destination before sync for diff comparison."""
      logger.info("Taking LAN snapshot (before sync)")
      before = snapshot_to_dict(walk_lan_destination(config.paths.lan_destination))
      logger.info(f"LAN snapshot: {len(before)} files before sync")
      return before
  ```
- **Description**: In contrast to `lan_snapshot_after_task` (which wraps `walk_lan_destination` in a `try...except` and returns `None` on transient network/SMB enumeration failures), `lan_snapshot_before_task` performs no error handling. Any transient SMB timeout, authentication jitter, or path traversal glitch throws an uncaught exception, terminating the entire LAN backup before Robocopy sync is ever attempted.
- **Recommendation**: Wrap `walk_lan_destination` in `try...except`, logging a warning and returning `{}` if the pre-sync snapshot fails, allowing the critical backup sync step to proceed.

### 22.3 Function `backup`

#### Finding 22.3.1: Retention purge bypassed when any backup leg fails or yields partial status
- **Location**: `flow.py:1330-1361` in `backup()`
- **Severity**: Low
- **Category**: Maintenance / Unbounded Database Growth
- **Code**:
  ```python
  if excs:
      ...
      raise ExceptionGroup("Backup completed with errors", excs)

  # ── Maintenance ──
  try:
      db = ManifestDB(...)
      db.purge_old_runs(...)
  ```
- **Description**: The database maintenance routine (`db.purge_old_runs`) is placed sequentially after `if excs: raise ExceptionGroup(...)`. If any leg of the backup raises an exception (or raises `PartialRun`), execution exits immediately. In environments experiencing frequent network hiccups or partial runs, the database retention purge will never execute, leading to unbounded SQLite growth over time.
- **Recommendation**: Move `db.purge_old_runs()` into a `finally` block or execute it prior to raising the `ExceptionGroup`.

---

## 23. `watchdog.py`

### 23.1 Function `_alert_wedged_lock`

#### Finding 23.1.1: Non-existent module import causes silent failure of wedged-lock alerts
- **Location**: `watchdog.py:306-321` in `_alert_wedged_lock()`
- **Severity**: High
- **Category**: Error Handling / Silent Failure / Alerting
- **Code**:
  ```python
  def _alert_wedged_lock(reason: str, pid: int | None = None) -> None:
      """Send an alert if watchdog deferrals hit their cap on a live lock owner."""
      try:
          from models.config import CONFIG_PATH, load_config
          from core.notifications import send_failure_alert

          cfg = load_config(CONFIG_PATH)
          send_failure_alert(...)
      except Exception as e:
          logger.debug(f"Watchdog alert delivery skipped/failed: {e}")
  ```
- **Description**: The function attempts to import `send_failure_alert` from `core.notifications`. However, `core/notifications.py` does not exist in the codebase (the alerting logic is located in `core.report`). When watchdog deferrals reach their limit on a wedged lock, this import throws `ModuleNotFoundError: No module named 'core.notifications'`. The exception is caught and suppressed at `logger.debug` level, silently preventing critical wedged-lock alert emails from being dispatched to system administrators.
- **Recommendation**:
  Update the import to `from core.report import send_failure_alert`.

### 23.2 Function `_service_state`

#### Finding 23.2.1: Hardcoded English text parsing breaks on non-English Windows editions
- **Location**: `watchdog.py:188-221` in `_service_state()`
- **Severity**: Medium
- **Category**: Internationalization / Portability / Service Management
- **Code**:
  ```python
  for line in r.stdout.splitlines():
      stripped = line.strip()
      if stripped.startswith("STATE"):
          tokens = stripped.split(":", 1)[1].split()
  ```
- **Description**: Windows CLI tools such as `sc query` localize their output based on the host OS language (e.g. "STATUT" in French, "STATUS" in German, "ESTADO" in Spanish). On non-English Windows Server installations, `stripped.startswith("STATE")` will never match, causing `_service_state` to return an empty string and misleading the watchdog into misidentifying the service state as unknown or failing to detect that services are running.
- **Recommendation**:
  Use `powershell -Command "Get-Service -Name <service> | Select-Object -ExpandProperty Status"` or query the Windows Service Controller API directly using `win32serviceutil` / WMI / CIM.

---

## 24. `launch.py`

### 24.1 Function `_cancel_orphaned_runs`

#### Finding 24.1.1: Flawed Boolean filter cancels unrelated third-party flow runs on shared Prefect server
- **Location**: `launch.py:167-172` in `_cancel_orphaned_runs()`
- **Severity**: Medium
- **Category**: Logic Flaw / Multi-tenancy / Flow Cancellation
- **Code**:
  ```python
  runs = [
      r for r in all_runs
      if getattr(r, "flow_id", None) is not None
      or "aam-backup" in str(getattr(r, "name", ""))
      or any(k in str(getattr(r, "name", "")) for k in ("backup-", "report", "rollover"))
  ]
  ```
- **Description**: In Prefect, every valid `FlowRun` object possesses a non-null `flow_id`. Because the first condition uses `or` rather than `and`, `getattr(r, "flow_id", None) is not None` unconditionally evaluates to `True` for every flow run in the database. If this Prefect server is shared with other organizational workflows, `_cancel_orphaned_runs()` will forcibly cancel every pending or running flow on the entire server upon startup.
- **Recommendation**:
  Change `or` to `and` with grouping:
  ```python
  runs = [
      r for r in all_runs
      if getattr(r, "flow_id", None) is not None
      and (
          "aam-backup" in str(getattr(r, "name", ""))
          or any(k in str(getattr(r, "name", "")) for k in ("backup-", "report", "rollover"))
      )
  ]
  ```

#### Finding 24.1.2: Corrupt or zero-byte lock file is ignored and left on disk during startup
- **Location**: `launch.py:120-128` in `_cancel_orphaned_runs()`
- **Severity**: Low
- **Category**: Error Handling / Lock Management
- **Code**:
  ```python
  if lock_path:
      alive, pid = read_lock_alive(lock_path)
      if alive:
          print(f"[launch] Backup lock held by PID {pid} — skipping cancellation of RUNNING flows")
          backup_active = True
      elif pid is not None:
          print(f"[launch] Stale backup lock (PID {pid} not running or reused) — cleaning up")
          lock_path.unlink(missing_ok=True)
  ```
- **Description**: If a prior crash resulted in a zero-byte or corrupt lock file (e.g. non-numeric characters), `read_lock_alive()` returns `(False, None)`. Because `elif pid is not None` checks that `pid` is not None, corrupt lock files are completely ignored rather than deleted, leaving dead artifacts in the runtime directory.
- **Recommendation**: Check `elif lock_path.exists(): lock_path.unlink(missing_ok=True)`.

### 24.2 Function `main`

#### Finding 24.2.1: Silent failure of Dashboard daemon thread leaves scheduler running blind
- **Location**: `launch.py:299-304` in `main()`
- **Severity**: Medium
- **Category**: Resilience / Process Lifecycle
- **Code**:
  ```python
  dash_thread = threading.Thread(target=_run_dashboard, daemon=True)
  dash_thread.start()
  time.sleep(0.5)
  if not dash_thread.is_alive():
      print("[launch] Warning: Dashboard thread not alive after start (may be race in tests)")
  ```
- **Description**: If Uvicorn fails to bind to the specified port (e.g., port conflict with IIS or another service, permission denial), the daemon thread terminates immediately. `main()` only prints a warning and proceeds to run `serve(*deployments())`. The operator is told the dashboard is available at the URL when in fact the web server is dead.
- **Recommendation**: Catch startup errors in `_run_dashboard` and terminate or visibly notify if the UI fails to start in production mode.

---

## 25. `serve.py`

### 25.1 Function `_deployments`

#### Finding 25.1.1: Standalone `serve.py` invocation bypasses bootstrap invariants
- **Location**: `serve.py:106-108` in `__main__`
- **Severity**: Medium
- **Category**: Architectural Design / Initialization Safety
- **Code**:
  ```python
  if __name__ == "__main__":
      d = _deployments()
      serve(*d, pause_on_shutdown=False)
  ```
- **Description**: The documentation and comments state that `serve.py` can be run from the project root (`python serve.py`). However, directly executing `serve.py` bypasses all critical initialization steps performed by `launch.py`: creating the `aam-backup` global concurrency limit, cancelling orphaned/crashed flow runs, reconciling disabled legs, checking fiscal year rollover, and starting the dashboard. If run on a new or rebooted server, flow runs will fail due to missing concurrency slots.
- **Recommendation**:
  Direct CLI users to `launch.py` or import and run the preflight bootstrap functions (`_ensure_concurrency_limit()`, etc.) inside `serve.py` before calling `serve()`.

#### Finding 25.1.2: Disabled report flows scheduled unconditionally
- **Location**: `serve.py:57-75` in `_deployments()`
- **Severity**: Low
- **Category**: Prefect Orchestration / Clutter
- **Code**:
  ```python
  deployments_out.append(
      weekly_report_flow.to_deployment(...)
  )
  ```
- **Description**: Unlike `backup-cloud` and `backup-lan` (which check `config.cloud.enabled` and `config.lan.enabled` before registering), `weekly-report` and `monthly-report` are registered unconditionally even if `config.notifications.weekly_enabled` or `monthly_enabled` are `False`. The flows wake up, immediately return after checking config, and clutter the Prefect dashboard with empty completed runs every week.
- **Recommendation**: Guard registration with `if config.notifications.weekly_enabled:` and `if config.notifications.monthly_enabled:`.

---

## 26. `ui.py`

### 26.1 Function `_prefect_has_active_run`

#### Finding 26.1.1: Active `mode="all"` backups not detected by pipeline status check
- **Location**: `ui.py:226-231` in `_prefect_has_active_run()`
- **Severity**: Medium
- **Category**: Concurrency / UI Status Accuracy
- **Code**:
  ```python
  for run in runs:
      tags = run.tags or []
      parameters = run.parameters or {}
      if pipeline in tags or parameters.get("mode") == pipeline:
          return True
  return False
  ```
- **Description**: When a backup is executed with `mode="all"` (e.g. manual invocation from Prefect UI, CLI, or test scripts), the flow run parameters have `{"mode": "all"}` and tags typically omit `"cloud"` or `"lan"`. Calling `_prefect_has_active_run("cloud")` and `_prefect_has_active_run("lan")` both evaluate to `False`. The dashboard UI misreports the pipeline as idle during an active full backup, allowing operators to initiate conflicting manual triggers.
- **Recommendation**:
  Recognize `mode == "all"` as active for both `"cloud"` and `"lan"`:
  ```python
  mode = parameters.get("mode")
  if pipeline in tags or mode == pipeline or mode == "all":
      return True
  ```

### 26.2 Manual Trigger Endpoints

#### Finding 26.2.1: Missing Anti-CSRF protection on state-changing HTTP POST endpoints
- **Location**: `ui.py:466-528` in `trigger_cloud()` and `trigger_lan()`
- **Severity**: Medium
- **Category**: Security / CSRF
- **Code**:
  ```python
  @app.post("/trigger/cloud")
  async def trigger_cloud(request: Request):
      _require_auth(request)
      ...
  ```
- **Description**: Authentication via session cookie relies solely on `SameSite=Lax`. While `SameSite=Lax` prevents cross-site POSTs in modern browsers under default conditions, there is no explicit anti-CSRF token or `Origin` / `Referer` verification header check. Any intranet web application or browser context sharing the same domain/subdomain can forge requests to start cloud or LAN backups, initiate Wake-on-LAN packets, and trigger remote server shutdowns.
- **Recommendation**: Validate the `Origin` or `Referer` header against `request.base_url`, or implement standard CSRF tokens for POST actions.

### 26.3 Function `_get_health`

#### Finding 26.3.1: Uncached filesystem I/O executed on 2-second dashboard polling loop
- **Location**: `ui.py:704-714` in `_get_health()`
- **Severity**: Low
- **Category**: Performance / Resource Contention
- **Code**:
  ```python
  async def _get_health() -> dict:
      try:
          src = _cfg().paths.source_drive
          du = await asyncio.to_thread(shutil.disk_usage, src)
          return {
              "source_free_gb": f"{du.free / (1024**3):.1f}",
              "source_exists": await asyncio.to_thread(Path(src).exists),
          }
  ```
- **Description**: The dashboard frontend (`dashboard.js`) polls `/status` every 2 seconds. In turn, `_get_health()` issues blocking `shutil.disk_usage` and `Path.exists` calls on `source_drive` every 2 seconds. If `source_drive` is an external USB HDD, sleeping drive, or high-latency network volume, this rapid polling prevents drive spin-down, degrades performance, and spawns threadpool workers unnecessarily.
- **Recommendation**: Cache `_get_health()` results with a 30- to 60-second TTL.

---

## 27. `deploy/read_config.py`

### 27.1 Function `_read_yaml_fallback`

#### Finding 27.1.1: Missing scope exit in fallback YAML parser causes cross-section value leakage
- **Location**: `deploy/read_config.py:53-78` in `_read_yaml_fallback()`
- **Severity**: Low
- **Category**: Parser Logic / Fallback Robustness
- **Code**:
  ```python
  # Check for parent key (e.g., "paths:")
  if parent_key and re.match(rf"^{re.escape(parent_key)}\s*:", stripped):
      in_parent = True
      continue
  ```
- **Description**: In the fallback YAML parser (used when PyYAML is uninstalled), once `in_parent` is set to `True`, it is never reset when a subsequent top-level section header is encountered. If a key requested under one parent (e.g. `paths.timeout`) does not exist under `paths:`, but exists in a subsequent section (e.g. `lan.timeout: 300`), the parser erroneously matches and returns the value from the wrong section.
- **Recommendation**: Reset `in_parent = False` whenever a non-indented line with another key is encountered.

---

## Summary of Critical & High Severity Findings

| Ref # | Location | Severity | Category | Brief Description |
| :--- | :--- | :--- | :--- | :--- |
| **1.3.1** | `models/config.py:199` | **High** | Security / Config Leak | Hardcoded external GCP project number default (`920173882190`). |
| **1.5.1** | `models/config.py:317` | **Critical** | Data Overwrite Bypass | `_fy_name()` checks only path tail; subdirectories bypass FY mismatch guard and disable rollover. |
| **2.1.1** | `core/hashing.py:17` | **Medium** | Cryptographic Collision | 1MB head/tail MD5 hash fails to detect modifications in large file midsections. |
| **6.1.1** | `core/manifest.py:180` | **High** | Unbounded Memory | Unbounded `cursor.fetchall()` in migration consumes massive RAM on large manifests. |
| **7.1.1** | `core/lan_manifest.py:39` | **High** | Windows Crash / Network Hang | `Path(unc).resolve()` queries SMB and injects `\\?\UNC\`, crashing downstream `os.path.relpath`. |
| **8.1.1** | `core/backup_repository.py:64` | **Critical** | Data Loss Risk | Cross-mode delete in `record_sync_results` wipes GCS cloud sync status when LAN files are pruned. |
| **11.1.1** | `core/lan_preflight.py:112` | **High** | Process Hang | Robocopy /L lacks `/IPG` and `/W:1`, causing dry-run preflight to block for hours on congested SMB links. |
| **12.1.1** | `core/lan_sync.py:159` | **High** | Crash Loop / Process Leak | Force-kill via `taskkill /F /T` leaves orphaned lock files and Robocopy log locks. |
| **15.1.1** | `core/cloud_sync.py:116` | **High** | Data Inconsistency | Inconsistent exit-code handling on partial GCS syncs (codes 4, 5, 10). |
| **18.1.1** | `core/fy_rollover.py:101` | **Critical** | Destructive Recursion | UNC root parent calculation creates nested subdirectories `\\host\share\FY25-26\FY26-27`. |
| **19.1.1** | `core/health.py:33` | **High** | Catastrophic Data Loss | NTFS system folders (`$RECYCLE.BIN`) make empty drives appear populated, allowing Robocopy `/MIR` to wipe backup targets. |
| **22.2.1** | `flow.py:357` | **High** | Fault Tolerance | Unhandled exception in `lan_snapshot_before_task` crashes entire LAN pipeline before sync. |
| **23.1.1** | `watchdog.py:306` | **High** | Silent Failure / Alerting | Broken import `from core.notifications import send_failure_alert` silently suppresses watchdog wedged-lock alerts. |






