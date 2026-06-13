# Line-by-Line Review of core/ Directory

## Summary

Reviewed all 13 files in `core/` directory. Found **3 critical issues**, **4 medium issues**, and **2 minor issues**.

---

## CRITICAL Issues

### 1. `core/cloud_sync.py` — Wrong Exit Code Classifications (Lines 24-35)

**Location:** `classify_rclone_exit()` function

**Current code:**
```python
mapping = {
    6: "CLOUD_PARTIAL",   # WRONG
    9: "CLOUD_COMPLETE",  # WRONG
}
```

**Should be:**
```python
mapping = {
    6: "CLOUD_FAILED",    # NoRetry errors — retries won't help
    9: "CLOUD_PARTIAL",   # No files transferred (with --error-on-no-transfer)
}
```

**Evidence:** From rclone official docs:
- Exit 6: "Less serious errors (like 461 errors from dropbox) (NoRetry errors)"
- Exit 9: "Operation successful, but no files transferred (Requires --error-on-no-transfer)"

**Impact:** Backup status incorrectly reported. Exit 6 (non-retryable) classified as retryable, exit 9 (no work done) classified as complete success.

---

### 2. `core/cloud_sync.py` — Docstring Also Wrong (Lines 24-35)

The docstring has the same incorrect classifications as the mapping. Both must be fixed together.

---

## MEDIUM Issues

### 3. `core/cloud_preflight.py` — Hardcoded Timeout (Line 30)

**Location:** `run_cloud_dry_run()` function

```python
timeout=300,
```

**Issue:** Timeout is hardcoded at 300 seconds. Should be a parameter for consistency with `cloud_verify.py` which accepts `timeout` as parameter.

---

### 4. `core/cloud_verify.py` — Hardcoded Timeout (Line 17)

**Location:** `verify_cloud_integrity()` function

```python
timeout: int = 600,
```

**Issue:** While this accepts a parameter, the default 600s is hardcoded. Consider making it configurable via config.

---

### 5. `core/logging.py` — Complex Prefect Bridge Logic (Lines 60-95)

**Location:** `configure_prefect_bridge()` function

**Issue:** The `_get_prefect_logger()` function uses two separate flags (`_cached_logger` and `_logger_initialized`) to track state. This is overly complex and could be simplified to just check `_cached_logger is not None`.

---

### 6. `core/manifest.py` — Unused Schema Version (Line 11)

**Location:** Module level

```python
SCHEMA_VERSION = 1
```

**Issue:** Defined but never used in any code. Either use it for schema migration or remove it.

---

## MINOR Issues

### 7. `core/rclone_config.py` — No Input Sanitization (Line 25)

**Location:** `write_temp_config()` function

```python
content = f"""[aam_gcs]
type = google cloud storage
service_account_file = {key_abs}
...
location = {location}
storage_class = {storage_class}
"""
```

**Issue:** No escaping of special characters in values. If `location` or `storage_class` contain special chars, the config file would be malformed.

---

### 8. `core/report.py` — SMTP Credentials Check (Line 25)

**Location:** `_send_email()` function

```python
if not config.smtp_username or not config.smtp_password:
    logger.warning("SMTP credentials not set — skipping")
    return False
```

**Issue:** Only checks for truthy values. Doesn't handle empty strings explicitly (though `not ""` is falsy, so this works).

---

## Files Reviewed

| File | Lines | Issues |
|------|-------|--------|
| `__init__.py` | 8 | 0 |
| `cloud_preflight.py` | 83 | 1 (timeout) |
| `cloud_sync.py` | 157 | 2 (exit codes) |
| `cloud_verify.py` | 78 | 1 (timeout) |
| `hashing.py` | 28 | 0 |
| `lan_sync.py` | 144 | 0 |
| `logging.py` | 95 | 1 (complexity) |
| `manifest.py` | 519 | 1 (unused var) |
| `process.py` | 31 | 0 |
| `rclone_config.py` | 51 | 1 (sanitization) |
| `report.py` | 200 | 1 (minor) |
| `shutdown.py` | 51 | 0 |
| **Total** | **1,445** | **8** |

---

## Recommendations

1. **Fix exit codes in cloud_sync.py** — This is the most critical issue affecting backup status reporting
2. **Add timeout parameter to cloud_preflight.py** — For consistency with other functions
3. **Simplify logging.py Prefect bridge** — Reduce complexity
4. **Remove unused SCHEMA_VERSION** — Or implement schema migration properly
