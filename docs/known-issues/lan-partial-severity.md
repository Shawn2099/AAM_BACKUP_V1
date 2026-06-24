# LAN_PARTIAL Severity Classification — Known Issue

**Created:** 2026-06-23
**Status:** Resolved
**Priority:** Low — functional, but over-reports

---

## Summary

`lan_sync.py:classify_exit_code()` treats all `LAN_PARTIAL` states the same, but exit codes 4-7 (anomalies) and 8-15 (copy errors) have different severity. This causes false alerts for non-critical anomalies.

---

## Affected File

**`core/lan_sync.py`** — lines 116-121

```python
error_msg = None
if status in ("LAN_FAILED", "LAN_PARTIAL"):
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        error_msg = log_text[-100000:] if len(log_text) > 100000 else log_text
    except OSError:
        error_msg = f"robocopy exit {result.returncode} (log unreadable)"
```

---

## Robocopy Exit Code Reference

Exit codes are bitmask-based combinations:

| Exit | Bitmask | Meaning | Severity |
|------|---------|---------|----------|
| 0 | 0000 | No files copied, no failure | None |
| 1 | 0001 | Files copied successfully | None |
| 2 | 0010 | Extra files on destination | Informational |
| 3 | 0011 | Copied + extras | Informational |
| 4 | 0100 | Mismatches detected | Informational |
| 5 | 0101 | Copied + mismatches | Informational |
| 6 | 0110 | Extras + mismatches | Informational |
| 7 | 0111 | Copied + extras + mismatches | Informational |
| 8 | 1000 | Some files failed to copy | **Error** |
| 9 | 1001 | Copied + some failed | **Error** |
| 10 | 1010 | Extras + some failed | **Error** |
| 11 | 1011 | Copied + extras + some failed | **Error** |
| 12 | 1100 | Mismatches + some failed | **Error** |
| 13 | 1101 | Copied + mismatches + some failed | **Error** |
| 14 | 1110 | Extras + mismatches + some failed | **Error** |
| 15 | 1111 | All flags + some failed | **Error** |
| 16+ | xxxx | Fatal error | **Fatal** |

---

## Current Behavior

### classify_exit_code() mapping

```python
if code & 16:       # Bit 4 → LAN_FAILED
if code & 8:        # Bit 3 → LAN_PARTIAL
if code in (0,1,2,3): → LAN_COMPLETE
if 4 <= code <= 7:  → LAN_PARTIAL
default:            → LAN_FAILED
```

### Result dict

| Exit Code | Status | error field |
|-----------|--------|-------------|
| 0-3 | LAN_COMPLETE | None |
| 4-7 | LAN_PARTIAL | log_tail (100K chars) |
| 8-15 | LAN_PARTIAL | log_tail (100K chars) |
| 16+ | LAN_FAILED | log_tail (100K chars) |

---

## The Problem

Exit codes 4-7 and 8-15 both return `LAN_PARTIAL` with a populated `error` field. But:

- **Codes 4-7 (anomalies):** Sync completed. All files processed. Some mismatches or extras detected. Informational — investigate later, not critical.

- **Codes 8-15 (copy errors):** Sync partially failed. Some files couldn't be copied. Backup is incomplete. Needs immediate attention.

Current code treats both as "error", causing:
1. False alerts for non-critical anomalies
2. Operator fatigue (ignoring real failures)
3. No way to distinguish severity from return dict

---

## Recommended Fix

```python
error_msg = None
if status == "LAN_FAILED":
    # Fatal — always capture log
    error_msg = log_tail
elif result.returncode & 8:
    # Copy errors (codes 8-15) — real failure, capture log
    error_msg = log_tail
elif 4 <= result.returncode <= 7:
    # Anomalies only (codes 4-7) — log it, but don't set error
    logger.warning(
        f"LAN sync anomalies detected (exit {result.returncode}) — "
        f"review log for details"
    )
```

### After fix

| Exit Code | Status | error field | Alert? |
|-----------|--------|-------------|--------|
| 0-3 | LAN_COMPLETE | None | No |
| 4-7 | LAN_PARTIAL | None | No (logged) |
| 8-15 | LAN_PARTIAL | log_tail | Yes |
| 16+ | LAN_FAILED | log_tail | Yes |

---

## Trade-offs

| Aspect | Current | After Fix |
|--------|---------|-----------|
| False alerts | Yes (codes 4-7) | No |
| Anomaly visibility | In error field | In logs only |
| Operator attention | Over-reported | Correctly reported |
| Complexity | Simple | Slightly more logic |

---

## When to Address

- **Before production deployment** if alerting is wired to `error` field
- **After deployment** if alerts are based on `status` field only
- **Not urgent** if operators already know to check both fields

---

## Related Code

- `core/lan_sync.py:classify_exit_code()` — exit code classification
- `core/lan_sync.py:run_lan_sync()` — sync execution and error handling
- `tests/test_lan_sync.py` — exit code classification tests
- `flow.py:lan_sync_task` — Prefect task that calls run_lan_sync

---

## Testing

Current tests cover all exit codes but don't verify the `error` field behavior for codes 4-7 vs 8-15. When fixing, add tests:

```python
def test_exit_4_anomalies_no_error_field():
    """Code 4 (mismatches) — sync completed, error should be None."""
    ...

def test_exit_8_copy_errors_has_error_field():
    """Code 8 (copy errors) — sync failed, error should contain log."""
    ...
```
