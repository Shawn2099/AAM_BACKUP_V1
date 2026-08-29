# AAM Backup Automation V1 — Comprehensive Fix Implementation Plan

## Background

This plan addresses all confirmed bugs, dead code, schema redundancies, Windows Server character encoding traps, and startup lifecycle issues identified in the audit across both `core/` and non-core codebase files. 

The software is our flagship 24×7 automated backup solution running on **Windows Server 2016** with **Python 3.12** and **Prefect 3**. Every fix follows enterprise standards: zero hacks, vetted standard libraries, strict type/error containment, and 100% backward compatibility.

All impact analysis was conducted via GitNexus on repository `AAM_BACKUP_V1`.

---

## Complete Scope of Fixes

| ID | Category | Component | File | Issue & Senior Dev Standard Solution |
|---|---|---|---|---|
| **A1** | 🐛 Bug | Time Utils | [`pyproject.toml`](file:///C:/Users/Shawn%20A/Desktop/bk/pyproject.toml) & [`core/time_utils.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/time_utils.py) | Add `cron-descriptor>=1.4.5` dependency. Use `ExpressionDescriptor(cron.strip(), options).get_description()` as the **primary** engine with 24h format and timezone suffix. Two-tier exception handling: `(MissingFieldException, FormatException)` logged as `WARNING`, and generic `Exception` logged as `ERROR` fallback. |
| **A2** | 🐛 Bug | Hashing | [`core/hashing.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/hashing.py) | `hashlib.md5()` crashes on Windows Server FIPS mode. Use `hashlib.md5(usedforsecurity=False)` with standard 64KB streaming; drop 3.11 version branching. |
| **A3** | 🐛 Bug | LAN Preflight | [`pyproject.toml`](file:///C:/Users/Shawn%20A/Desktop/bk/pyproject.toml) & [`core/lan_preflight.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/lan_preflight.py) | Add `tenacity>=8.5.0` dependency. Clean UNC host extraction (`[p for p in clean.lstrip("\\").split("\\") if p]`). Probe SMB port 445 via `socket.create_connection` decorated with `@retry(stop=stop_after_attempt(4), wait=wait_fixed(3), retry=retry_if_result(lambda ok: not ok), retry_error_callback=lambda rs: False)` to survive slow NAS spin-ups without raising `RetryError`. Use strict `canary_file.is_file()` and `try/except OSError` mapping to `HealthError`. |
| **A4** | 🐛 Bug | Subprocess UTF-8 | [`core/cloud_reporter.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/cloud_reporter.py), [`core/cloud_sync.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/cloud_sync.py), [`core/cloud_verify.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/cloud_verify.py), [`core/cloud_preflight.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/cloud_preflight.py) | Windows Server defaults to `cp1252`, crashing on non-ASCII filenames in subprocess output. Specify `encoding="utf-8", errors="replace"` in `subprocess.run()`. Remove dead `json.JSONDecodeError` from subprocess catch. |
| **A5** | 🐛 Bug | Startup Lifecycle | [`launch.py`](file:///C:/Users/Shawn%20A/Desktop/bk/launch.py) | `UnboundLocalError` on `_cfg` causes `_reconcile_disabled_legs` to silently fail on every boot. Load `_cfg = load_config(CONFIG_PATH)` at top of `main()`. |
| **B1** | ⚠️ Dead Code | Manifest DB | [`core/manifest.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/manifest.py) | `mark_lan_synced` and `mark_cloud_synced` have 0 production callers. Delete methods and cleanup test files. |
| **B2** | ⚠️ Dead Code | Manifest DB | [`core/manifest.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/manifest.py) | `update_checksums` has 0 production callers. Delete method and update test files (including `tests/test_scen_branch_g.py:743`). |
| **B3** | ⚠️ Dead Code | Migration | [`core/fy_router.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/fy_router.py) | 4-line re-export shim. Migrate all 5 callers to `core.time_utils`, update monkeypatch in `test_rt_06_flow_pipeline.py`, delete `test_fy_router.py` and `fy_router.py`. |
| **C1** | 📝 Schema | Manifest DDL | [`core/manifest.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/manifest.py) | Duplicate `run_id` UNIQUE index in DDL. `run_id TEXT NOT NULL UNIQUE` in table definition already creates the primary B-tree autoindex. Remove redundant `CREATE UNIQUE INDEX` line. |
| **C2** | 📝 Concurrency | Manifest DB | [`core/manifest.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/manifest.py) | Wrap read+update in `with conn:` inside `with self._lock:` in `prune_stale_synced` for atomic transactions with automatic rollback on error. Add regression test asserting surviving partially-synced rows. |

---

## Detailed Code Modifications

---

### A1 — Industry Standard Cron Formatting with Two-Tier Protection (`pyproject.toml` & `core/time_utils.py`)

#### 1. Add dependency to [`pyproject.toml`](file:///C:/Users/Shawn%20A/Desktop/bk/pyproject.toml)
```diff
 dependencies = [
     "prefect==3.7.2",
+    "cron-descriptor>=1.4.5",
+    "tenacity>=8.5.0",
     "pydantic==2.13.4",
     ...
 ]
```

#### 2. Update [`core/time_utils.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/time_utils.py)
```diff
+from cron_descriptor import ExpressionDescriptor, Options
+from cron_descriptor.Exception import FormatException, MissingFieldException
+from loguru import logger

 def cron_to_human(cron: str, tz: str | None = None) -> str:
-    """Convert a 5-field cron expression to a human-readable string."""
-    parts = cron.strip().split()
-    ...
-    return f"Daily at {int(hour):02d}:{int(minute):02d} {tz_short}"
+    """Convert a 5-field cron expression to a human-readable string.
+
+    Uses ExpressionDescriptor as the primary engine with 24-hour time formatting.
+    Two-tier exception handling ensures the status dashboard never crashes on malformed input.
+    """
+    if not cron or not isinstance(cron, str):
+        return str(cron or "")
+    try:
+        options = Options()
+        options.use_24hour_time_format = True
+        desc = ExpressionDescriptor(cron.strip(), options).get_description()
+        tz_short = str(tz or "").split("/")[-1] if "/" in str(tz or "") else str(tz or "")
+        return f"{desc} ({tz_short})" if tz_short else desc
+    except (MissingFieldException, FormatException) as e:
+        logger.warning(f"cron_descriptor failed to parse cron expression {cron!r}: {e}")
+        return cron
+    except Exception as e:
+        logger.error(f"cron_to_human: unexpected error parsing {cron!r}, falling back to raw: {e}")
+        return cron
```

---

### A2 — FIPS-Compliant MD5 Hashing (`core/hashing.py`)

**File:** [`core/hashing.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/hashing.py)

```diff
 def compute_md5(file_path: str | Path) -> str:
-    """Compute MD5 digest for a file using streaming (Python 3.11+ file_digest or fallback).
+    """Compute MD5 digest for a file using 64KB streaming reads.
+
+    Uses usedforsecurity=False to satisfy FIPS-compliant OpenSSL policies on
+    Windows Server 2016 — MD5 is strictly used as an integrity fingerprint.
     """
-    if hasattr(hashlib, "file_digest"):
-        with open(file_path, "rb") as f:
-            return hashlib.file_digest(f, "md5").hexdigest()
-    else:
-        md5_hash = hashlib.md5()
-        with open(file_path, "rb") as f:
-            for chunk in iter(lambda: f.read(65536), b""):
-                md5_hash.update(chunk)
-        return md5_hash.hexdigest()
+    md5_hash = hashlib.md5(usedforsecurity=False)
+    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()
```

---

### A3 — Retrying Socket Probe & Strict Canary Check (`core/lan_preflight.py`)

**File:** [`core/lan_preflight.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/lan_preflight.py)

```diff
+import socket
+from tenacity import retry, retry_if_result, stop_after_attempt, wait_fixed
+
+def _extract_unc_host(path_str: str) -> str | None:
+    """Extract host/IP from a UNC path (e.g. '\\\\192.168.10.10\\share' or '//nas/share').
+
+    Returns None for local drive paths (e.g. 'D:\\...') or relative paths.
+    """
+    clean = str(path_str).replace("/", "\\")
+    if not clean.startswith(r"\\"):
+        return None
+    parts = [p for p in clean.lstrip("\\").split("\\") if p]
+    return parts[0].strip() if parts else None
+
+
+@retry(
+    stop=stop_after_attempt(4),
+    wait=wait_fixed(3),
+    retry=retry_if_result(lambda ok: not ok),
+    retry_error_callback=lambda retry_state: False,
+)
+def _is_smb_reachable(host: str, port: int = 445, timeout: float = 2.0) -> bool:
+    """Probe SMB port 445 with up to 4 attempts (~15s budget) to survive slow NAS spin-ups.
+
+    Uses retry_error_callback to return False upon exhaustion instead of raising RetryError.
+    """
+    try:
+        with socket.create_connection((host, port), timeout=timeout):
+            return True
+    except (OSError, socket.timeout, TimeoutError):
+        return False
+
 def run_lan_dry_run(source: str, dest: str, timeout: int = 300) -> dict:
+    # 1. Fast SMB Reachability Probe for UNC destinations (bypasses 45s OS hang)
+    host = _extract_unc_host(dest)
+    if host and not _is_smb_reachable(host, port=445, timeout=2.0):
+        msg = (
+            f"Cannot reach LAN host '{host}' on SMB port 445 within 2.0s. "
+            "Verify the backup server/NAS is powered on, Wake-on-LAN succeeded, "
+            "and network connectivity is operational."
+        )
+        logger.error(msg)
+        raise HealthError(msg)
+
+    # 2. Strict Canary File Verification (.is_file() instead of .exists())
     dest_path = Path(dest)
     canary_file = dest_path / ".AAM_TARGET_MOUNTED"
-    if not canary_file.exists():
+    try:
+        canary_valid = canary_file.is_file()
+    except OSError as e:
+        msg = f"Cannot access LAN destination '{dest}': {e}"
+        logger.error(msg)
+        raise HealthError(msg) from e
+
+    if not canary_valid:
         msg = (
-            f"Canary file {canary_file} missing — refusing to mirror into an "
+            f"Canary file {canary_file} missing or is not a regular file — refusing to mirror into an "
             "unverified destination. Recovery: verify the FY share is mounted, "
             f"then create the canary:  cmd /c type nul > \"{canary_file}\""
         )
         logger.error(msg)
         raise HealthError(msg)
```

---

### A4 — Windows UTF-8 Subprocess Output Decoding Across Core Subprocess Runners

**Files Modified:**
- [`core/cloud_reporter.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/cloud_reporter.py) (lines 44, 94, 163):
  - Replace `text=True` with `text=True, encoding="utf-8", errors="replace"`.
  - Remove dead `json.JSONDecodeError` from `subprocess.run` exception handler.
- [`core/cloud_sync.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/cloud_sync.py) (line 212):
  - Add `encoding="utf-8", errors="replace"` to `subprocess.run`.
- [`core/cloud_verify.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/cloud_verify.py) (line 62):
  - Add `encoding="utf-8", errors="replace"` to `subprocess.run`.
- [`core/cloud_preflight.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/cloud_preflight.py) (line 115):
  - Add `encoding="utf-8", errors="replace"` to `subprocess.run`.

---

### A5 — Fix Startup `UnboundLocalError` in Deployment Reconciliation (`launch.py`)

**File:** [`launch.py`](file:///C:/Users/Shawn%20A/Desktop/bk/launch.py)

```diff
 def main():
     print("=" * 50)
     print("  AAM Backup Automation V1 — Launch")
     print("=" * 50)
 
+    from models.config import CONFIG_PATH, load_config
+    _cfg = load_config(CONFIG_PATH)
+
     # Wait for Prefect API...
     print("[launch] Waiting for Prefect API server...")
     ...
     _ensure_concurrency_limit()
     _cancel_orphaned_runs()
 
     try:
         _reconciliation = _reconcile_disabled_legs(_cfg)
         for dep_name, outcome in _reconciliation.items():
             print(f"[launch] Deployment {dep_name}: {outcome}")
     except Exception as exc:
         print(f"[launch] Deployment reconciliation FAILED (non-fatal): {exc}")
 
     print("[launch] Starting backup scheduler (main thread)...")
-    from models.config import CONFIG_PATH
-    from models.config import load_config as _lc
-    _cfg = _lc(CONFIG_PATH)
     print(f"[launch] Dashboard: http://{_cfg.dashboard.bind_address}:{_cfg.dashboard.port}")
```

---

### B1 & B2 — Dead Manifest Methods & Test Cleanup (`core/manifest.py` & tests)

**Files Modified:**
- [`core/manifest.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/manifest.py): Delete `mark_lan_synced` (lines 344–360), `mark_cloud_synced` (lines 362–378), and `update_checksums` (lines 396–408).
- [`tests/test_manifest.py`](file:///C:/Users/Shawn%20A/Desktop/bk/tests/test_manifest.py): Remove test methods covering deleted functions.
- [`tests/test_manifest_comprehensive.py`](file:///C:/Users/Shawn%20A/Desktop/bk/tests/test_manifest_comprehensive.py): Remove test blocks for `mark_*_synced` and `update_checksums`.
- [`tests/test_manifest_edge_cases.py`](file:///C:/Users/Shawn%20A/Desktop/bk/tests/test_manifest_edge_cases.py): Remove references to `mark_lan_synced` and `mark_cloud_synced`.
- [`tests/test_scen_branch_g.py`](file:///C:/Users/Shawn%20A/Desktop/bk/tests/test_scen_branch_g.py): Update `test_DB_17_checksums` (line 743) so it does not call deleted `db.update_checksums()`.

---

### B3 — Complete `fy_router.py` Migration

**Files Modified:**
- [`flow.py:30`](file:///C:/Users/Shawn%20A/Desktop/bk/flow.py#L30): Change `from core.fy_router import get_fy_prefix` → `from core.time_utils import get_fy_prefix`.
- [`core/__init__.py:3`](file:///C:/Users/Shawn%20A/Desktop/bk/core/__init__.py#L3): Change `from core.fy_router import get_fy_prefix` → `from core.time_utils import get_fy_prefix`.
- [`tests/test_workflows.py:13`](file:///C:/Users/Shawn%20A/Desktop/bk/tests/test_workflows.py#L13): Change import to `core.time_utils`.
- [`tests/test_rt_06_flow_pipeline.py:97-122`](file:///C:/Users/Shawn%20A/Desktop/bk/tests/test_rt_06_flow_pipeline.py#L97-L122): Update monkeypatch from `core.fy_router.get_fy_prefix` to `core.time_utils.get_fy_prefix`.
- [`tests/test_fy_router.py`](file:///C:/Users/Shawn%20A/Desktop/bk/tests/test_fy_router.py): **Delete file**.
- [`core/fy_router.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/fy_router.py): **Delete file**.

---

### C1 — Duplicate DDL Index Removal (`core/manifest.py`)

**File:** [`core/manifest.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/manifest.py) — line 61

```diff
 CREATE INDEX IF NOT EXISTS idx_run_history_started_at ON run_history(started_at);
 CREATE INDEX IF NOT EXISTS idx_run_history_mode ON run_history(mode);
-CREATE UNIQUE INDEX IF NOT EXISTS idx_run_history_run_id ON run_history(run_id);
```

---

### C2 — SQLite Atomic Transaction Context Manager (`core/manifest.py`)

**File:** [`core/manifest.py`](file:///C:/Users/Shawn%20A/Desktop/bk/core/manifest.py) — lines 459–493

```diff
     def prune_stale_synced(self, mode: str, active_paths: set[str]) -> int:
         if mode not in ("cloud", "lan"):
             raise ValueError(f"mode must be 'cloud' or 'lan', got {mode!r}")
         status_field = f"{mode}_status"
         ts_field = f"{mode}_last_synced_at"
-        db_paths = self.get_synced_paths(mode)  # acquires + releases lock
-        stale_paths = [p for p in db_paths if p not in active_paths]
-        if not stale_paths:
-            return 0
-        with self._lock:
-            conn = self._get_conn()
-            conn.executemany(...)
-            conn.execute(...)
-            conn.commit()
-        return len(stale_paths)
+        # Inline read under single lock + connection transaction context for atomic rollback
+        with self._lock:
+            conn = self._get_conn()
+            with conn:
+                rows = conn.execute(
+                    f"SELECT relative_path FROM file_entries WHERE {status_field} = 'synced'"
+                ).fetchall()
+                db_paths = {r["relative_path"] for r in rows}
+                stale_paths = [p for p in db_paths if p not in active_paths]
+                if not stale_paths:
+                    return 0
+                conn.executemany(
+                    f"UPDATE file_entries SET {status_field} = NULL, "
+                    f"{ts_field} = NULL WHERE relative_path = ?",
+                    [(path,) for path in stale_paths],
+                )
+                conn.execute(
+                    "DELETE FROM file_entries "
+                    "WHERE lan_status IS NULL AND cloud_status IS NULL"
+                )
+        return len(stale_paths)
```

---

## Sequential Execution Order

1. **Step 1:** Add `cron-descriptor` and `tenacity` to `pyproject.toml`, update `core/time_utils.py`, and update test expectations in `test_ui.py`, `test_scen_branch_h.py`, `test_batch3_fixes.py` (Fix A1).
2. **Step 2:** Fix A2 (`core/hashing.py`).
3. **Step 3:** Fix A3 with `[p for p in clean.lstrip("\\").split("\\") if p]` + tenacity retry on `_is_smb_reachable` with `retry_error_callback` + `.is_file()` (`core/lan_preflight.py`).
4. **Step 4:** Fix A4 with `encoding="utf-8", errors="replace"` across all core rclone subprocess runners (`core/cloud_reporter.py`, `core/cloud_sync.py`, `core/cloud_verify.py`, `core/cloud_preflight.py`).
5. **Step 5:** Fix A5 (`launch.py`).
6. **Step 6:** Fix C1 (`core/manifest.py` DDL).
7. **Step 7:** Fix C2 (`core/manifest.py` atomic `with conn:` in `prune_stale_synced`) + add regression test for partially-synced row preservation.
8. **Step 8:** Fix B1 & B2 (`core/manifest.py` dead code + test cleanups including `test_scen_branch_g.py`).
9. **Step 9:** Fix B3 (`flow.py`, `core/__init__.py`, `test_rt_06_flow_pipeline.py`, delete `fy_router.py`).

---

## Complete Verification Plan

### Automated Test Suite Execution
```powershell
uv add cron-descriptor tenacity
python -m pytest tests/ -x -q --tb=short
```

Targeted suites after each milestone:
- **A1:** `tests/test_ui.py`, `tests/test_scen_branch_h.py`, `tests/test_batch3_fixes.py`
- **A2:** `tests/test_hashing.py`
- **A3:** `tests/test_lan_preflight.py`, `tests/test_lan_preflight_comprehensive.py`, `tests/test_p1_exc_unify.py` (Verify closed-port check returns `HealthError` directly without `RetryError`)
- **A4:** `tests/test_cloud_reporter.py`, `tests/test_cloud_reporter_comprehensive.py`, `tests/test_cloud_sync.py`
- **A5:** `tests/test_p2_sched_pause.py`
- **C2:** Regression test for `file_entries` partial status survival.
- **B3:** `tests/test_rt_06_flow_pipeline.py`, `tests/test_workflows.py`
- **Full Suite:** All 50+ test files passing with 0 failures.
