# core/ — Production Audit Report

> Line-by-line, function-by-function audit of every file in `core/`.
> Findings are noted here. No changes are made without explicit user approval.

---

## `core/__init__.py` ✅ CLEAN

- 8 lines. Package-level imports of `get_fy_prefix`, `compute_md5`, `verify_checksum`, `PENDING_CHECKSUM`, `configure_logging`.
- `__all__` matches exactly what is imported. No extras, nothing missing.
- **No issues.**

---

## `core/backup_repository.py` ✅ CLEAN (1 minor note)

### `record_sync_results()`
- Normalizes both rclone (`Path/Size/ModTime`) and os.walk (`path/size/mtime`) entry formats into a single shape before bulk upsert. The `is not None` guard on `Size` is correct — avoids treating `Size=0` as falsy.
- Active path set uses `.replace("\\", "/")` for Windows UNC path consistency before pruning. Matches what `bulk_upsert_synced` stores.
- **No issues.**

### `record_run_history()`
- All params are keyword-only (`*` separator) — prevents argument order bugs.
- Broad `except Exception` is intentional and correct — function is called from `finally` blocks in `flow.py` and must never raise.
- WAL checkpoint after `insert_run` is good practice.

**📝 Minor note (no fix needed unless you want it):**
> If `wal_checkpoint()` fails *after* a successful `insert_run()`, the error log says `"Failed to record run history"` — slightly inaccurate because the run *was* recorded; only the WAL flush failed. Data is not lost (WAL checkpoints on next run). Log message could confuse an operator during triage.

---

<!-- FILES TO AUDIT -->
## `core/cloud_preflight.py` ✅ CLEAN

### `run_cloud_dry_run()`
- **Probe A (source drive):** `Path.exists()` → `iterdir()` one call. Empty drive is a warning (not a failure) — by design, actual fail-close on empty source is in `health.py`. `OSError` on read returns fail dict.
- **Probe B (GCS auth):** `temp_rclone_config` context manager guarantees cleanup. `--gcs-no-check-bucket` is intentionally **absent** to force a real `storage.buckets.get` IAM check. `--max-depth 0` = one API call, no traversal.
- All three subprocess failure modes covered: `TimeoutExpired`, `FileNotFoundError`, `OSError`.
- Full stderr logged on non-zero exit — not truncated.
- **No issues.**

---

<!-- cloud_preflight.py -->
<!-- cloud_reporter.py -->
## `core/cloud_reporter.py` — 1 BUG

### `CloudReporterError` class
- Correct use of domain exception to distinguish "failed listing" from "empty bucket." ✅

### `_base_args()`
- `--gcs-no-check-bucket` correct here (bucket already verified by preflight). `--fast-list` reduces API calls. ✅

### `get_cloud_size()`
- **🐛 BUG (line 45):** `json.JSONDecodeError` is in the subprocess `except` tuple — dead code. `subprocess.run()` cannot raise this. The actual JSON parse is in its own `try/except` 13 lines later. Creates false impression of coverage at the subprocess level.
- Non-zero exit sets `data = None` and returns error dict without parsing stdout. Correct.
- `.get("count", 0)` / `.get("bytes", 0)` handle malformed-but-valid JSON without KeyError. ✅

### `get_cloud_manifest()`
- All failure modes (`TimeoutExpired`, `FileNotFoundError`, `OSError`, non-zero exit, bad JSON) raise `CloudReporterError` — never returns `[]` on failure (M6 fix). ✅
- Filters directory entries with `not f.get("IsDir")`. ✅

### `get_cloud_diff()`
- `mkstemp` + `os.close(fd)` before rclone write — correct Windows handle pattern. ✅
- Exit code boundary: 0/1 = valid result, 2+ = error. Correct.
- `--combined` format parsing (`+/-/*/=` prefix + `line[2:]`) is correct. ✅
- `finally` block always cleans up temp file. ✅

---


<!-- cloud_sync.py -->
## `core/cloud_sync.py` ✅ CLEAN (1 minor note)

### `scan_rclone_log_for_errors()`
- `(log_text or "")` handles None input. Unknown JSON log levels default to 0 (never treated as errors). Non-JSON lines checked for plaintext fatal markers. Returns last 10 bad lines — bounded. ✅

### `classify_rclone_exit()`
- Unknown exit codes default to `CLOUD_FAILED` — fail-closed. ✅

### `resolve_max_duration_seconds()`
- Three cases handled: explicit override, `0` = disable, auto = timeout minus 300s margin. If margin would go negative, cap is disabled rather than sending a negative value. ✅

### `build_rclone_sync_command()`
- Every flag has a rationale comment. `--error-on-no-transfer` intentional (enables exit-9 reclassification). `--check-first` separates stat and upload phases for HDD head efficiency. ✅
- **📝 Minor (not a bug):** `if max_duration_seconds:` (line 156) is falsy for `0`. Safe in practice because `resolve_max_duration_seconds` converts `0` → `None`. But `if max_duration_seconds is not None:` would be more explicit.

### `run_cloud_sync()`
- `mkstemp` + `os.close` before rclone write — correct Windows handle pattern. ✅
- stderr written to file (not pipe) — prevents pipe buffer deadlock on large error output. ✅
- Exit-9 reclassification: reads stderr log, scans for ERROR/CRITICAL JSON lines or fatal plaintext markers before trusting `CLOUD_NO_CHANGES_COMPLETE`. ✅
- `stderr_path = None` initialized before `mkstemp` — `finally` guard `if stderr_path:` is safe. ✅
- Temp stderr log always cleaned up in `finally`. ✅

---


<!-- cloud_verify.py -->
## `core/cloud_verify.py` ✅ CLEAN (1 minor observation)

### `verify_cloud_integrity()`
- Named constants `_EXIT_VERIFIED = 0`, `_EXIT_MISMATCH = 1` — no magic numbers. ✅
- `--one-way` correct: only checks source→GCS, not reverse. ✅
- `--size-only` avoids 2+ hour MD5 re-hash of 500GB HDD. ✅
- `--modify-window 2s` handles NTFS mtime granularity. ✅
- Distinguishes exit 1 (mismatch) from exit 2+ (error) in log label. Full stderr logged. ✅
- Returns consistent `{"verified": bool, "exit_code": int, "error": str|None}` in all paths. ✅
- All three subprocess exceptions handled: `TimeoutExpired`, `FileNotFoundError`, `OSError`. ✅
- **📝 Minor observation:** No `--retries` flag (unlike `cloud_preflight.py` which uses `--retries 2`). A single transient network blip during verify would fail immediately. Likely intentional (network is stable post-sync). Confirm this is deliberate.

### `_build_error_message()`
- Clean three-way split: `None` on success, specific message on mismatch, generic on error. ✅

---


<!-- fy_rollover.py -->
## `core/fy_rollover.py` — 3 minor issues

### Module header & imports (lines 1–22)
- All imports are used. `FY_PATTERN` compiled at module level (once). ✅

### `_resolve_gcloud()` (lines 29–60)
- Resolution order (bundled → all-users SDK → per-user SDK → system PATH) is correct.
- **📝 Minor — Duplicate env-var candidates (lines 51–52):** `%LOCALAPPDATA%` and `%USERPROFILE%\AppData\Local` resolve to the **same directory** on every standard Windows installation. Candidate 5 is a dead fallback; it adds one extra `stat` call per invocation for no benefit.
- **📝 Minor — Redundant `path.exists()` (line 56):** `Path.is_file()` already returns `False` for non-existent paths. The preceding `path.exists()` call is an extra, unnecessary `stat` syscall per candidate.

### `_fy_name()` (lines 67–73)
- Cross-platform separator normalization before split is correct. ✅
- `re.IGNORECASE` on `FY_PATTERN` then `name.upper()` return is consistent (accepts `fy26-27`, normalizes to `FY26-27`). ✅

### `_parent_path()` (lines 76–91)
- UNC path reassembly (`"\\\\".join(parent_parts[2:])`) resolves correctly — Python `"\\\\"` is the two-char string `\\`, so the join produces `server\share` → prepend `\\` → `\\server\share`. ✅
- Windows and POSIX paths handled separately and correctly. ✅

### `detect_rollover()` (lines 100–117)
- Falls back from source_drive to lan_destination for FY detection. Correct opt-out design. ✅
- Returns `False` with a `logger.warning` when no FY folder found — non-blocking, well-documented. ✅

### `run_final_backup()` (lines 120–193)
- Exit codes 0 and 9 accepted as cloud OK — 9 = `CLOUD_NO_CHANGES_COMPLETE`, correct. ✅
- Narrow `except (OSError, subprocess.SubprocessError, RuntimeError)` — config attribute errors propagate loudly. ✅
- **📝 Minor — `ImportError` gap (line 160):** `from core.wol import ensure_server_online` is a lazy import inside the outer `try` block, but the `except` clause only catches `(OSError, subprocess.SubprocessError, RuntimeError)`. An `ImportError` from this line would propagate uncaught to `rollover()`. In production the module always exists, but it's an inconsistency: the `from core.shutdown import shutdown_server` import on line 182 has the same gap.
- LAN `classify_lan_exit` accepting `LAN_PARTIAL` as OK is intentional per design. ✅
- Shutdown step in a separate inner `try/except (OSError, RuntimeError)` — non-blocking. ✅

### `create_new_fy_folders()` (lines 196–230)
- Source folder: no `try/except` — local disk failure hard-stops the rollover. Correct. ✅
- LAN folder: `OSError` caught, `ACTION REQUIRED` message logged, rollover continues. ✅
- Canary `.AAM_TARGET_MOUNTED` created on new LAN folder — matches what `lan_preflight.py` checks. ✅

### `update_config_yaml()` (lines 233–272)
- `ruamel.yaml` round-trip mode preserves comments. ✅
- `mkstemp + os.fdopen + os.replace` = atomic write, never leaves a half-written config. ✅
- **📝 Minor — Unguarded key access (lines 252–253):** `cfg["paths"]["source_drive"]` and `cfg["paths"]["lan_destination"]` use direct dict access. If these keys were absent (e.g., severely malformed yaml), the result is a raw `KeyError` propagating to `rollover()` instead of a clean `RolloverError`. In practice safe because the config was just loaded and validated, but the contract is implicit not explicit.
- `except Exception: os.unlink(tmp_path); raise` — temp file cleaned on any failure. ✅

### `run_archive_transition()` (lines 275–392)
- `os.environ.copy()` + inject `GOOGLE_APPLICATION_CREDENTIALS` — stateless auth, doesn't pollute parent process environment. ✅
- Auth step skipped if key file not found (ambient auth path). ✅
- `--recursive` on bare prefix avoids PowerShell glob expansion. ✅
- Entire function is non-blocking: all exception paths return `False` and log `WARNING`. ✅

### `rollover()` (lines 395–465)
- `load_config` lazily imported — avoids circular import at module level. ✅
- Operation order: final backup → archive transition → folder creation → config update. **Config is written last** — a crash before it leaves the old config intact so the next run retries cleanly. ✅
- `archive_ok` is informational only (used in final log message); it does not gate the config write. ✅
- `required = []` pattern: `RolloverError` raised only if an *enabled* pipeline failed — disabled pipelines do not block rollover. ✅


<!-- fy_router.py -->
## `core/fy_router.py` — ⚠️ DEAD SHIM (still imported — migration incomplete)

- 4 lines. Pure re-export shim: `from core.time_utils import get_fy_prefix  # noqa: F401`.
- Docstring says "backward compatibility" — the intent is that callers migrate to `core.time_utils` directly.
- **⚠️ Issue — Migration is incomplete:** `flow.py` (line 30), `core/__init__.py` (line 3), `tests/test_workflows.py` (line 13), and `tests/test_fy_router.py` (line 5) all still import from `core.fy_router` instead of `core.time_utils`. The shim is therefore not a temporary compat layer that can be removed — it is still load-bearing.
- `# noqa: F401` is used correctly to suppress the "imported but unused" warning on a re-export. ✅
- **No logic bugs.** Issue is a process/migration one: either complete the migration (update all callers to import from `core.time_utils`) or remove the "backward compatibility" label and treat `fy_router.py` as a permanent thin re-export alias.


<!-- hashing.py -->
## `core/hashing.py` — ⚠️ 1 BUG, 1 DEAD CODE finding

### Module header & imports (lines 1–6)
- `hashlib`, `pathlib.Path` — both used. ✅
- `PENDING_CHECKSUM = "pending"` — sentinel constant exported via `core/__init__.py`. ✅

### `compute_md5()` (lines 9–23)
- `hasattr(hashlib, "file_digest")` runtime branch — correctly detects Python ≥ 3.11 vs fallback. ✅
- 3.11+ path: `hashlib.file_digest(f, "md5")` — C-level streaming, correct and efficient. ✅
- Legacy path: `iter(lambda: f.read(65536), b"")` — 64 KB chunk sentinel loop, memory-safe. ✅
- **🐛 BUG — FIPS system crash (line 19):** `hashlib.md5()` without `usedforsecurity=False` raises `ValueError` on FIPS-compliant OS configurations (some hardened Windows Server deployments disable MD5 for security-sensitive use). Since this is backup integrity (not a security hash), `hashlib.md5(usedforsecurity=False)` is the correct fix. The 3.11+ branch (`hashlib.file_digest`) also calls `hashlib.new("md5")` internally and has the same exposure. This would be a hard crash on a FIPS server — not a silent failure.

### `verify_checksum()` (lines 26–34)
- Returns `False` for `PENDING_CHECKSUM` — prevents false positives for uncatalogued files. ✅
- No exception handling — `FileNotFoundError` propagates to caller if file doesn't exist. Docstring does not document this. Callers must know to handle it. Minor doc gap.

### ⚠️ DEAD CODE — Neither function is called in production
- `compute_md5` and `verify_checksum` are only imported in `core/__init__.py` and called from `tests/test_hashing.py`. **No production code in `flow.py`, `ui.py`, or any other `core/` module calls them.** The entire MD5 checksum infrastructure is built but never wired into any backup pipeline. Files get `md5_checksum = 'pending'` written to the DB by `bulk_upsert_synced` and it appears to stay `'pending'` forever — the background computation step that would call `compute_md5` and `update_checksums` does not exist in any currently active code path.


<!-- health.py -->
## `core/health.py` — 2 minor issues

### Module header & imports (lines 1–12)
- All imports used. `pendulum`, `http.client`, `shutil`, `email.utils.parsedate_to_datetime`, `resolve_binary` — correct. ✅

### `HealthError` (lines 14–15)
- Domain exception used as the single identity for preflight failures (imported by `lan_preflight.py`). ✅

### `check_source_drive()` (lines 18–65)
- `any(source.iterdir())` — stops after first entry, memory-safe for large directories. ✅
- `PermissionError` caught separately from `OSError` — produces distinct, more informative error message. ✅
- Empty source → fail-closed by design (M8). Detailed rationale in comment. ✅
- `shutil.disk_usage` failure is a `logger.warning` + skip (not a hard fail) — correct; space check is advisory. ✅

### `check_binary_exists()` (lines 68–70)
- One-liner wrapping `resolve_binary`. Clean delegation. ✅

### `check_gcs_key()` (lines 73–80)
- Checks `exists()` then `st_size == 0` — two distinct failure modes, two distinct messages. ✅
- Does not validate JSON format — intentional, format errors surface at rclone/gcloud runtime. ✅

### `check_clock_skew()` (lines 83–123)
- HEAD request to `www.googleapis.com/` — minimal (no auth, no body). ✅
- `conn.close()` before parsing — does not hold connection open during computation. ✅
- **📝 Minor — Cross-library datetime subtraction (line 108):** `local_utc` is a `pendulum.DateTime`; `google_time` is a stdlib `datetime.datetime` with tzinfo from `parsedate_to_datetime`. Pendulum handles this, but it's an implicit cross-library operation. `pendulum.instance(google_time)` would make the conversion explicit and self-documenting.
- `OSError` and `ValueError` both return `(True, "")` — permissive skip (can't reach Google = no skew data = don't block backup). ✅

### `pre_backup_health()` (lines 126–175)
- `valid_modes` guard at the top — raises `HealthError` on invalid mode string, not `ValueError`. Consistent with the domain exception pattern. ✅
- Cloud mode: checks `rclone` binary, GCS key, clock skew. All raise `HealthError` on failure. ✅
- **📝 Minor — Silent GCS key skip (line 159):** `if gcs_key_path:` means the key check is silently skipped when `gcs_key_path` is `None` or an empty string, even in cloud mode. The config model should prevent this, but there's no assertion here to confirm that assumption. A falsy key path in cloud mode is a latent misconfiguration that would fail later at rclone runtime with a less helpful error.
- LAN mode: checks `robocopy` binary only. ✅
- Final `logger.info` confirms all checks passed. ✅


<!-- lan_manifest.py -->
## `core/lan_manifest.py` ✅ CLEAN (1 design note)

### Module header & imports (lines 1–10)
- `os`, `pathlib.Path`, `loguru.logger` — all used. ✅

### `walk_lan_destination()` (lines 13–74)
- `base = str(Path(unc_path).resolve())` — resolves UNC path for `os.path.relpath`. On Windows, `resolve()` on a UNC path returns it unchanged (no drive mapping needed). ✅
- `_on_walk_error` closure collects all errors without stopping the walk. ✅
- Per-file `os.stat()` in `try/except OSError` — silently skips locked/deleted files mid-walk. ✅
- **📝 Design note — Root-failure condition (lines 57–66):** Raises `OSError` only when `root_failed AND not files`. If the root has a permission error but at least one file was found (partial subtree accessible), the error is downgraded to a warning. This is intentional per the docstring's three-tier failure semantics, but it means a root-level error is silently promoted to "partial inventory" when any file is accessible. Operators should be aware that a `logger.warning` here can mask a root-level enumeration failure.
- Non-root errors logged with count + first error message. ✅

### `snapshot_to_dict()` (lines 77–79)**
- `{f["path"]: (f["size"], f["mtime"])}` — O(1) lookup dict. ✅
- Duplicate paths from `walk_lan_destination` would silently produce last-wins. Not possible given `os.walk` + `os.path.relpath` semantics, but contract is implicit.

### `diff_snapshots()` (lines 82–112)**
- Set intersection/difference for added/removed, element-wise tuple compare for modified/unchanged. O(n). ✅
- All four output lists are `sorted()` — deterministic order. ✅
- `before[p] != after[p]` compares `(size, mtime)` tuples — either field changing = modified. Correct. ✅


<!-- lan_preflight.py -->
## `core/lan_preflight.py` — 1 BUG, 1 minor style issue

### Module header & imports (lines 1–13)
- All imports used. ✅
- **📝 Minor — Missing blank line before function:** PEP 8 requires two blank lines between the import block and the first function definition. `run_lan_dry_run` starts on line 15 with only one blank line after the imports (line 14 is missing). Style issue only.

### `run_lan_dry_run()` (lines 15–96)

**Canary check (lines 31–50):**
- `canary_file = Path(dest) / ".AAM_TARGET_MOUNTED"` — guard against mirroring into an unverified destination. ✅
- Missing canary → `HealthError` with exact recovery command in the message. ✅
- **🐛 BUG — Unhandled `OSError` from `canary_file.exists()` (line 33):** If the UNC share is completely offline, `Path.exists()` raises `OSError` (e.g., "network path not found"). This `OSError` is **not caught** before line 65's `try` block — it propagates raw to the caller instead of returning `{"ok": False, "exit_code": -1, "error": ...}` like every other failure path. The caller (`flow.py`) likely expects either a return dict or a `HealthError`, not a bare `OSError`. The `OSError` handler at line 94 only covers `subprocess.run`, not the canary check.

**Command build (lines 52–61):**
- `resolve_binary("robocopy") or "robocopy"` — correct fallback. ✅
- `/L /MIR /XJ /NJH /NJS /NP` — list-only mirror, no junctions, minimal output. ✅
- `/XF .AAM_TARGET_MOUNTED /XD "System Volume Information" "$RECYCLE.BIN"` — correct exclusions. ✅

**Subprocess execution (lines 65–86):**
- `capture_output=True, text=True` — correct. Robocopy writes errors to stdout, not stderr; both captured. ✅
- `code < 8` = OK — robocopy exit codes 0–7 are all success variants. ✅
- `{"ok": bool, "exit_code": int, "error": str|None}` — consistent shape on all code paths. ✅

**Exception handling (lines 88–96):**
- `TimeoutExpired`, `FileNotFoundError`, `OSError` all caught from `subprocess.run`. ✅
- Gap: canary-check `OSError` (before `subprocess.run`) escapes all handlers (see BUG above).


<!-- lan_sync.py -->
## `core/lan_sync.py` — 1 stale docstring

### Module header & imports (lines 1–16)
- All imports used. ✅

### Module-level constants (lines 18–43)
- `_ERROR_LOG_TAIL = 100_000` and `_ANOMALY_LOG_TAIL = 100_000` — both 100 KB. ✅
- Regexes compiled at module level (once). ✅
- **📝 Stale docstring:** `run_lan_sync` docstring (line 259) says `anomaly_details` is "up to 5KB". The actual code passes `_ANOMALY_LOG_TAIL = 100_000` (100 KB) to `_read_log_tail`. The docstring is wrong — the constant is correct.

### `_validate_required_flags()` (lines 31–34)
- Rejects `/NC` / `-NC` (case-insensitive) — raises `ValueError`. Correct guard. ✅

### `_summary_files_row_values()` (lines 45–63)
- Positional parse of the `Files :` summary row. Fails closed on any non-numeric token. ✅
- Strips thousands separators (`,` and `.`) before `isdigit()` check. ✅
- Returns `None` on parse failure — callers fall back to bit-floor. ✅

### `failed_file_count()` (lines 66–82)
- `bit3_floor`: if bit 3 is set in exit code, floor is at least 1 (never falsely reports 0 failures). ✅
- `values[4]` = FAILED column (positional: Total Copied Skipped Mismatch **FAILED** Extras). ✅
- `max(parsed_failed, bit3_floor)` — contradictory signals resolve loud. ✅

### `count_failed_lines()` (lines 85–93)
- Counts `** FAILED:` markers for diagnostic use alongside `failed_file_count`. ✅

### `_read_log_tail()` (lines 96–120)
- `f.seek(size - max_bytes)` — tail-only read, memory-efficient for large logs (F15). ✅
- `lstrip("\ufffd")` — cleans up mid-sequence UTF-8 cut artifact. ✅
- `OSError` returns fallback string, never raises. ✅

### `classify_exit_code()` (lines 123–164)
- Checks bit 4 (fatal) before bit 3 (copy errors) — correct priority; code 24 (bits 3+4) → `LAN_FAILED`. ✅
- Negative/unexpected codes → `LAN_FAILED` (fail-closed). ✅

### `cleanup_orphaned_robocopy_logs()` (lines 174–197)
- `import time as _time` lazily inside function — harmless, `time` is always available.
- Age-gated cutoff prevents touching a live run's log file. ✅
- All `OSError` paths caught; function never raises (best-effort). ✅

### `build_robocopy_command()` (lines 200–243)
- All flags have rationale comments. `/NJS` removal documented (P1-COUNT). ✅
- `_validate_required_flags` called on the constructed list before returning. ✅

### `run_lan_sync()` (lines 246–372)
- `log_path = None` before `try` → `finally` guard is always safe. ✅
- `mkstemp + os.close` before robocopy — correct Windows handle release pattern. ✅
- `stdout=DEVNULL, stderr=DEVNULL` — robocopy writes via `/LOG:path`, not pipe. ✅
- `status == "LAN_FAILED" or (result.returncode & 8)` — correct: catches `LAN_PARTIAL` from bit-3 in the error-capture branch, not just `LAN_FAILED`. ✅
- `4 <= result.returncode <= 7` anomaly branch: sync completed, captures log tail for diagnostics, does NOT set `error` (no alert triggered). ✅
- All three exception paths return consistent 5-key dicts (`status`, `exit_code`, `error`, `anomaly_details`, `files_failed`). ✅
- `finally`: temp log unlinked on every path. ✅


<!-- logging.py -->
## `core/logging.py` ✅ CLEAN (1 design note)

### Module header & imports (lines 1–11)
- `sys`, `pathlib.Path`, `loguru.logger` — all used. `LOG_FORMAT` constant used in both `logger.add` calls. ✅

### `configure()` (lines 14–42)
- `logger.remove()` before adding sinks — correct Loguru setup pattern. ✅
- `log_dir.mkdir(parents=True, exist_ok=True)` — idempotent directory creation. ✅
- `enqueue=True` on file sink — async, thread-safe writes. ✅
- `retention=f"{log_retention_days} days"` — correct Loguru string format. ✅
- **📝 Design note — `configure()` removes the Prefect bridge:** If `configure()` is ever called *after* `configure_prefect_bridge()`, `logger.remove()` silently removes the bridge sink while `_bridge_configured` stays `True`, permanently preventing it from being re-added. In production the call order is always `configure()` → `configure_prefect_bridge()`, so this is safe — but the ordering constraint is implicit, not enforced.

### `_bridge_configured = False` (line 45)
- Module-level idempotency flag. Works correctly because `configure_prefect_bridge` guards with `if _bridge_configured: return`. ✅

### `configure_prefect_bridge()` (lines 48–112)
- Lazy import of `prefect.context` — only pulled in when actually called (not at module import time). ✅
- `_logger_cache = {}` and `_CACHE_MAX = 128` created inside function scope, captured by closures. Safe because the `_bridge_configured` guard ensures these are only ever created once. ✅
- **`_get_prefect_logger()` closure:**
  - `TaskRunContext` checked before `FlowRunContext` — correct (task is more specific than flow). ✅
  - Cache key `(context_type, run_id)` — per-run isolation, fixes F10 stale-logger bug. ✅
  - `_logger_cache.pop(next(iter(_logger_cache)))` — O(1) FIFO eviction of oldest entry (insertion-ordered dict). ✅
  - `get_run_logger()` failure returns `None` gracefully. ✅
- **`prefect_sink()` closure:**
  - Forwards `INFO/WARNING/ERROR/CRITICAL` to Prefect; everything else goes to `debug`. ✅
  - `except Exception:` swallows all bridge failures — correct; a broken bridge must never kill the backup. ✅
- `logger.add(prefect_sink, level="INFO")` — bridge sink added at INFO level. ✅
- `_bridge_configured = True` set after successful `logger.add`. ✅


<!-- manifest.py -->
## `core/manifest.py` — 2 dead code findings, 1 duplicate index, 1 TOCTOU note

### Module header & imports (lines 1–19)
- All imports used. `SCHEMA_VERSION = 1` — migration gate. ✅

### DDL string (lines 21–69)
- WAL mode, synchronous=NORMAL, foreign_keys=ON — correct pragmas. ✅
- `relative_path COLLATE NOCASE UNIQUE` — Windows file-system semantics. ✅
- **⚠️ Duplicate index on `run_id` (line 61):** `run_id TEXT NOT NULL UNIQUE` in the column definition *already* creates an implicit index. `CREATE UNIQUE INDEX IF NOT EXISTS idx_run_history_run_id ON run_history(run_id)` creates a **second** index on the same column. SQLite maintains both separately — wasted storage and a marginally slower write on every `insert_run`. One of the two should be removed.
- `INSERT OR IGNORE INTO db_meta` — idempotent on DDL re-execution. ✅

### `ManifestDB.__init__()` (lines 85–113)
- `synchronous` validated against `("normal", "full")` — raises `ValueError` on bad value. ✅
- Lazy connection (`self._conn = None`). Thread safety via `self._lock`. ✅

### `_get_conn()` (lines 116–161)
- Pre-migration dedup of `run_id` rows before UNIQUE index is applied. Wrapped in `try/except` — skipped on error. ✅
- `executescript(DDL)` — idempotent. ✅
- `conn.close(); raise` on migration failure — never caches a bad connection. ✅

### `_migrate_legacy_schema()` (lines 163–212)
- `BEGIN IMMEDIATE` makes DDL + version stamp atomic. ✅
- Retry loop (3×) for `SQLITE_BUSY` with exponential sleep. ✅
- `raise ManifestSchemaError` on permanent failure — loud, startup fails. ✅

### `upsert_file_entry()` (lines 225–277)
- Path normalized before insert. ✅
- `COALESCE` on md5/status — never overwrites real values with `None`. ✅
- Timestamp CASE: only updates `*_last_synced_at` on first-time transition to 'synced'. ✅

### `bulk_upsert_synced()` (lines 279–342)
- `mode` validated before being interpolated into SQL — no injection risk. ✅
- Chunked at 100 rows to stay under SQLite 999-variable limit. ✅

### `mark_lan_synced()` / `mark_cloud_synced()` (lines 344–378)
- **⚠️ DEAD CODE — Known since prior audit H7 (AUDIT_REPORT.md line 54), still not removed.** Neither method has any production caller. Both were replaced by `bulk_upsert_synced`. Referenced in prior audit as "remove or document," but the prior audit's fix list was not acted on.

### `delete_entries()` (lines 380–394)
- Chunked at 500 (1 param/row) — under the 999 limit. ✅
- `",".join("?" for _ in chunk)` — parameterized, no injection risk. ✅

### `update_checksums()` (lines 396–408)
- Correct `executemany`. ✅
- **⚠️ DEAD CODE** — No production caller. Part of the MD5 checksum pipeline that was built but never wired in (see `hashing.py` audit).

### `get_entry()` / `file_count()` / `get_cloud_synced_entries()` / `get_synced_paths()` (lines 410–457)
- All normalize paths before lookup. ✅
- `file_count`: allowlist check on `status_field` before string interpolation — prevents injection. ✅

### `prune_stale_synced()` (lines 459–493)
- **📝 TOCTOU gap (lines 477–481):** `get_synced_paths(mode)` acquires then releases `self._lock`, then the method re-acquires it for the update. Between these two lock acquisitions another writer could modify the DB. In practice this is a single-writer deployment so no real race — but the logical read-then-write is not atomic. A single `with self._lock` wrapping both operations would eliminate the gap.
- `DELETE WHERE lan_status IS NULL AND cloud_status IS NULL` — prunes fully orphaned entries. ✅

### `insert_run()` (lines 497–534)
- Required key validation upfront. ✅
- `ON CONFLICT(run_id) DO UPDATE` — upsert for run finalization. ✅
- `COALESCE` on `extended_metrics` — doesn't overwrite existing metrics with NULL. ✅

### `get_runs_since()` / `last_run()` / `last_successful_run()` / `get_recent_runs()` (lines 536–588)
- `LIKE '%_COMPLETE'` — catches all `*_COMPLETE` statuses. ✅
- `ORDER BY started_at DESC LIMIT 1` on single-run queries. ✅

### `wal_checkpoint()` / `purge_old_runs()` (lines 592–627)
- `PRAGMA wal_checkpoint(TRUNCATE)` — correct. ✅
- `PRAGMA optimize + ANALYZE` before freelist check — correct maintenance order. ✅
- `conn.commit()` before `VACUUM` — required (VACUUM cannot run inside a transaction). ✅
- Conditional VACUUM (only when freelist exceeds threshold). ✅


<!-- process.py -->
## `core/process.py` ✅ CLEAN (1 minor note)

### Module header & imports (lines 1–20)
- `os`, `shutil`, `tempfile`, `pathlib.Path`, `psutil` — all used. ✅

### `_get_create_time()` (lines 22–35)
- Catches `(NoSuchProcess, AccessDenied, ProcessLookupError, ValueError, OverflowError)` — G6 coverage for corrupted lock files with negative/huge PIDs. ✅
- `AccessDenied` on Windows system processes correctly maps to `None` (= stale). ✅

### `write_lock()` (lines 40–68)
- `fd = -1` after `os.close(fd)` — prevents double-close in `except BaseException`. ✅
- `except BaseException:` — cleans up temp file even on `KeyboardInterrupt` / `SystemExit`. ✅
- `mkstemp + os.replace` — atomic write (never a partial lock file visible to readers). ✅
- Fallback to bare PID string if own process `create_time` is None — lock is still written. ✅

### `read_lock_alive()` (lines 71–133)
- `PermissionError → (True, -1)` — fail-safe (antivirus lock = treat as live process). ✅
- New format (`pid:create_time`): splits on first `:` only — safe against decimal separators. ✅
- `abs(current_ct - written_ct) < 0.1` — 100ms float tolerance. ✅
- PID reuse correctly detected: same PID but different creation time = stale. ✅
- Legacy bare-PID format supported as fallback. ✅

### `pid_alive()` (lines 138–140)
- Backward-compat alias for tests. Correct. ✅

### `resolve_binary()` (lines 145–158)
- Priority: `deploy/bin/<name>` → `deploy/bin/<name>.exe` → `shutil.which`. ✅
- **📝 Minor — No `.cmd` check:** `fy_rollover.py:_resolve_gcloud` checks for `.cmd` files (gcloud on Windows is a `.cmd` batch script), but `resolve_binary` only checks bare name and `.exe`. In practice `rclone` and `robocopy` are `.exe` binaries so no functional gap, but the approach is asymmetric with `_resolve_gcloud`.


<!-- rclone_config.py -->
## `core/rclone_config.py` ✅ CLEAN (1 minor note)

### Module header & imports (lines 1–9)
- `os`, `tempfile`, `contextlib.contextmanager`, `pathlib.Path` — all used. ✅

### `write_temp_config()` (lines 12–47)
- `.strip()` on all three string inputs — removes accidental whitespace. ✅
- `valid_storage_classes` allowlist with `.upper()` comparison — case-insensitive validation. ✅
- Empty string `""` in the valid set is intentional: `storage_class =` in the config = inherit bucket-level default. ✅
- `Path(gcs_key_path).resolve()` → `.replace("\\", "/")` — absolute path with forward slashes for rclone config. ✅
- `mkstemp + os.close(fd)` before `write_text` — releases Windows file handle before rclone opens it. ✅
- **📝 Minor — `project_number` unvalidated:** Stripped but not checked for numeric format. An empty or non-numeric `project_number` would cause a rclone error at runtime rather than at config-write time. Config model should enforce this upstream.

### `temp_rclone_config()` (lines 50–60)
- `@contextmanager` — clean resource-management pattern. ✅
- `finally: Path(path).unlink()` — always deletes temp file. `except OSError: pass` — best-effort. ✅
- `*args, **kwargs` passthrough — correct delegation to `write_temp_config`. ✅


<!-- report.py -->
## `core/report.py` — 2 minor issues, 1 inefficiency

### Module header & imports (lines 1–23)
- All imports used. `humanize` used for `naturalsize` in the HTML report. ✅

### Module-level constants (lines 25–27)
- `_SMTP_MAX_ATTEMPTS`, `_SMTP_BASE_DELAY_SECONDS`, `_SMTP_JITTER_FACTOR` — all used in retry logic. ✅

### `_send_email_with_attachments()` (lines 30–105)
- MIME nesting: `MIMEMultipart("mixed")` → `MIMEMultipart("alternative")` → `MIMEText("html")` — correct for HTML email with attachments. ✅
- Port 465 → `SMTP_SSL`; all others → `SMTP + starttls()`. ✅
- Permanent SMTP errors (`AuthenticationError`, `RecipientsRefused`, `SenderRefused`) → immediate `return False`, never retried. ✅
- Exponential backoff with ±20% jitter. `finally: server.quit()` always closes connection. ✅
- **📝 Minor — Stale type annotation (line 34):** `attachments: list[dict] = None` — the default is `None` but the type says `list[dict]`. Should be `list[dict] | None = None`. Minor annotation inaccuracy.
- Falls through to `return False` on line 105 after all retries exhausted. ✅

### `send_failure_alert()` (lines 108–167)
- `html.escape()` applied to every user-controlled string in the HTML body. ✅
- Error truncated at 1000 chars inline; full error attached as `failure_details.txt`. ✅
- `exit_code` guarded with `if exit_code is not None else "-"`. ✅

### `_csv_safe()` (lines 175–188)
- Prepends `'` to cells starting with `=`, `+`, `-`, `@`, `\t`, `\r` — G5 CSV injection prevention. ✅
- Only applied to free-text columns; numeric columns bypass it. ✅

### `_generate_csv_data()` (lines 191–220)
- All runs included (not capped at 10 like the HTML body). ✅
- `_csv_safe` applied to `error_message` and `extended_metrics` only. ✅
- Returns `bytes` for email attachment. ✅

### `generate_report_html()` (lines 223–322)
- `"NO_CHANGES"` counted before `"_COMPLETE"` — correct because `CLOUD_NO_CHANGES_COMPLETE` matches both. ✅
- `failures = total - successes - no_changes - partials - skipped` — unknown statuses land in failures. Fail-loud for new unhandled statuses. ✅
- `html.escape()` applied to all user-provided strings in the row loop. ✅
- **📝 Minor — `row_count` is redundant (lines 267, 283):** Incremented once per loop iteration over `runs[:10]`. It always equals `min(len(runs), 10)`. The same value is available as `len(rows_list)` or computed from `total`. Not a bug — just a pointless counter.
- **📝 Minor — `html.escape(status)` on a hardcoded string (line 282):** `_status_display()` returns only hardcoded strings (`"No Changes"`, `"Completed"`, etc.) with no HTML-special characters. The `escape()` call is harmless but unnecessary.

### `send_summary_report()` (lines 325–356)
- **📝 Inefficiency — Double DB query (lines 337, 235):** `db.get_runs_since(days)` is called on line 337 to check for runs, then called again inside `generate_report_html` on line 235. Two identical queries for the same data. The first result could be passed into `generate_report_html` to avoid the second query.
- CSV attachment always contains ALL runs; HTML shows last 10. ✅

### `send_weekly_report()` / `send_monthly_report()` (lines 359–374)
- Thin wrappers. `days=7` / `days=30`. ✅


<!-- shutdown.py -->
## `core/shutdown.py` ✅ CLEAN (1 minor note)

### Module header & imports (lines 1–8)
- `subprocess`, `loguru.logger` — both used. ✅

### `shutdown_server()` (lines 11–50)
- `["shutdown", "/s", "/m", f"\\\\{server_ip}", "/t", "300", "/f"]` — correct remote shutdown command. 5-minute delay gives staff time to cancel with `shutdown /a`. ✅
- `capture_output=True, text=True, timeout=30` — captures output, 30s timeout on the command invocation itself (not the server shutdown). ✅
- `result.stderr.strip() or f"exit code {result.returncode}"` — fallback if stderr empty. ✅
- All three exception paths (`FileNotFoundError`, `TimeoutExpired`, `OSError`) return consistent `{"shutdown_initiated": False, "server_ip": str, "error": str}` dicts. ✅
- **📝 Minor — No `server_ip` validation:** An empty or malformed IP is passed directly to `shutdown /m \\`. The command would fail with an OS error message (captured and logged), but the error text would be cryptic. Config model should enforce a valid IPv4 format upstream.


<!-- time_utils.py -->
## `core/time_utils.py` — 1 BUG, 2 minor issues

### Module header & imports (lines 1–21)
- `from datetime import date`, `import pendulum` — both used. ✅
- `IST = pendulum.timezone("Asia/Kolkata")` — module-level constant. India has no DST, so this timezone offset is always `+05:30`. ✅

### `now_iso()` (lines 28–34)
- `pendulum.now(IST).isoformat()` — always timezone-aware with explicit `+05:30` offset. ✅

### `now_formatted()` (lines 37–42)
- `pendulum.now(IST).format(fmt)` — pendulum token format. Default `"YYYY-MM-DD HH:mm z"` produces `"2026-05-30 19:52 IST"`. ✅

### `cutoff_iso()` (lines 49–54)
- `pendulum.now(IST).subtract(days=days).isoformat()` — correct. ✅

### `get_fy_prefix()` (lines 61–78)**
- April 1 FY start. `% 100` truncation handles 2099–2100 rollover correctly (produces `FY99-00` then `FY00-01`). ✅
- `today` parameter enables testing with a specific date. ✅

### `cron_to_human()` (lines 85–112)
- `len(parts) != 5` → returns raw cron string as fallback. ✅
- **🐛 BUG — `ValueError` on non-numeric `hour`/`minute` (lines 100, 110, 112):** `int(hour)` and `int(minute)` are called unconditionally. A cron expression with step syntax (`*/15 * * * *`) or ranges (`0-30 8 * * *`) would cause `int("*/15")` to raise `ValueError`. The `len(parts) != 5` guard only checks field count, not field values. The function should either add `try/except ValueError` around the `int()` calls (returning the raw cron on failure) or validate that `hour` and `minute` are digit strings before converting.
- **📝 Minor — `month` field unused (line 91):** `minute, hour, dom, month, dow = parts` — `month` is unpacked but never read. A cron like `0 8 1 6 *` ("June 1st at 8am") is displayed as `"1st of month at 08:00 IST"`, silently ignoring the month constraint. All current project crons use `*` for month, so this doesn't cause incorrect output today — but it's an incomplete implementation.
- **📝 Minor — Redundant condition in ordinal suffix (line 107):** `(4 <= d <= 20) or d in (11, 13)` — `11` and `13` are already within the `4–20` range. The `d in (11, 13)` check is always already covered by the range check. Dead condition. (F14 fix comment mentions this was a correction; the fix was correct but left a trivially redundant sub-condition.)


<!-- wol.py -->
## `core/wol.py` ✅ CLEAN (2 minor notes)

### Module header & imports (lines 1–12)
- `socket`, `time`, `loguru.logger`, `wakeonlan.send_magic_packet`, `AppConfig` — all used. ✅

### `WolTimeout` (lines 15–16)
- Domain exception. Raised by `wait_for_server`, expected by callers. ✅

### `_smb_port_open()` (lines 19–34)
- Port range validation (`0 <= port <= 65535`) before `connect_ex`. ✅
- `socket` used as context manager — auto-closed on exit. ✅
- `connect_ex` returns error code, never raises on connection refusal. `OSError` caught for lower-level network failures. ✅

### `_send_magic_packet()` (lines 37–84)
- `max(1, int(repeat))` — at least one round even if config sets `repeat=0`. ✅
- `subnet_broadcast != "255.255.255.255"` guard — avoids sending the packet twice to the same address. ✅
- Per-round `except Exception` — one bad round logs a warning, the loop continues. ✅
- `if attempt < rounds: time.sleep(interval)` — no sleep after the last round. ✅
- **📝 Minor — Three blank lines (lines 84–89):** PEP 8 requires two blank lines between top-level definitions. The three blank lines between `_send_magic_packet` and `wait_for_server` is a style deviation.

### `wait_for_server()` (lines 89–112)
- `while time.time() - start_time < wake_timeout:` — correct wall-clock polling. ✅
- Stability wait after SMB probe succeeds — prevents connecting before the NAS share is fully ready. ✅
- `raise WolTimeout(...)` on expiry. ✅
- **📝 Minor — SMB port hardcoded in caller:** `_smb_port_open(server_ip)` uses default `port=445`. `wait_for_server` doesn't expose a port parameter, so the probe port is implicitly fixed at 445. Not a problem in practice (SMB is always 445), but the implicit dependency isn't visible in the function signature.

### `ensure_server_online()` (lines 115–144)
- WoL disabled → returns `True` immediately (assumes server always on). ✅
- Pre-check before sending WoL packet — avoids unnecessary wake broadcast if server is already up. ✅
- `config.wol.get_broadcast_address()` — delegated to config model. ✅
- `WolTimeout` propagates to caller unchanged. ✅

---

## Audit Summary

| File | Status | Key Findings |
|------|--------|-------------|
| `__init__.py` | ✅ CLEAN | — |
| `backup_repository.py` | ✅ CLEAN | Minor: misleading log on WAL failure after successful insert |
| `cloud_preflight.py` | ✅ CLEAN | — |
| `cloud_reporter.py` | 🐛 1 BUG | `json.JSONDecodeError` in subprocess `except` — dead code |
| `cloud_sync.py` | ✅ CLEAN | Minor: `if max_duration_seconds:` falsy for 0 |
| `cloud_verify.py` | ✅ CLEAN | Minor: no `--retries` flag |
| `fy_rollover.py` | ⚠️ 3 minor | Duplicate LOCALAPPDATA candidates; redundant `path.exists()`; unguarded key access in YAML update |
| `fy_router.py` | ⚠️ INCOMPLETE | Migration not finished — 4 callers still import from `fy_router` not `time_utils` |
| `hashing.py` | 🐛 1 BUG + ⚠️ DEAD CODE | FIPS crash on `hashlib.md5()`; entire MD5 pipeline is built but never called in production |
| `health.py` | ⚠️ 2 minor | Cross-library datetime subtraction; silent GCS key skip when `gcs_key_path` is falsy |
| `lan_manifest.py` | ✅ CLEAN | Design note: root-level error silently downgraded to partial-inventory warning |
| `lan_preflight.py` | 🐛 1 BUG | `canary_file.exists()` `OSError` not caught — share offline raises raw OSError instead of structured dict |
| `lan_sync.py` | 📝 1 stale docstring | `anomaly_details` docstring says "5KB" but constant is 100KB |
| `logging.py` | ✅ CLEAN | Design note: `configure()` order constraint is implicit, not enforced |
| `manifest.py` | ⚠️ 4 findings | Duplicate `run_id` index; `mark_lan_synced`/`mark_cloud_synced` dead (known H7, not fixed); `update_checksums` dead; TOCTOU gap in `prune_stale_synced` |
| `process.py` | ✅ CLEAN | Minor: no `.cmd` extension check in `resolve_binary` |
| `rclone_config.py` | ✅ CLEAN | Minor: `project_number` unvalidated |
| `report.py` | ⚠️ 2 minor + 1 inefficiency | Stale type annotation; redundant `row_count` counter; double DB query in `send_summary_report` |
| `shutdown.py` | ✅ CLEAN | Minor: no `server_ip` format validation |
| `time_utils.py` | 🐛 1 BUG + 2 minor | `cron_to_human` crashes on step/range cron syntax; `month` field unused; redundant ordinal suffix condition |
| `wol.py` | ✅ CLEAN | Minor: 3 blank lines (PEP 8); SMB port 445 hardcoded |

---

# Non-Core Files — Production Audit Report

> Line-by-line, function-by-function audit of all files outside `core/`.

---

## `models/__init__.py` ✅ CLEAN

- 6 lines.
- Exports `AppConfig`, `load_config`.
- `__all__` matches exports exactly.
- **No issues.**

---

## `models/config.py` ✅ CLEAN (2 minor notes)

### Module Header & Constants (lines 1–20)
- `_DEFAULT_RUNTIME_DIR` defaults to `C:\BackupAgent` or environment variable `AAM_RUNTIME_DIR`. ✅

### `PathsConfig` (lines 22–78)
- `derive_runtime_paths`: auto-populates `logs/` and `manifest.db` inside `runtime_dir` if empty. Requires `.db` suffix. ✅
- Derived properties `backup_lock_path` and `prefect_home` computed accurately. ✅
- Validators: `source_drive` non-empty, `lan_destination` UNC path validation (`^\\\\.+\\`), `gcs_key_path` non-empty. ✅

### `LanConfig` (lines 79–93)
- Pydantic bounds on all numeric fields (`retry_count` 1–10, `dry_run_timeout_seconds` 60–7200, `mt_threads` 1–128). ✅

### `WolConfig` (lines 94–174)
- MAC format validation (`valid_mac`) with conditional enforcement (`_mac_required_when_enabled`). ✅
- IPv4 validation with `ipaddress.IPv4Address`. ✅
- `get_broadcast_address()`: returns explicit broadcast address or auto-derives `/24` subnet broadcast (`x.x.x.255`). ✅

### `CloudConfig` (lines 175–246)
- GCS bucket naming regex `^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$` enforced when `enabled=True`. ✅
- `storage_class` allowlist validation with upper-casing. ✅
- `bandwidth_limit` regex validation (`^\d+[kMG]$`). ✅
- **📝 Minor note:** `project_number` is a plain string without numeric regex validation (e.g. `^\d+$`).

### `NotificationConfig` (lines 247–280)
- SMTP port range 1–65535 validated. ✅
- `__repr__` masks `smtp_password` as `'***'`. ✅

### `MaintenanceConfig` & `HealthConfig` (lines 281–361)
- Concurrency, retention, SQLite busy timeout (1–120s), clock skew, and rollover timeout settings fully bounded. ✅

### `DashboardConfig` (lines 363–380)
- Enforces `api_key` when `auth_enabled=True`.
- `__repr__` masks `api_key` as `'***'`. ✅

### `ScheduleConfig` (lines 382–430)
- Validates 5-part cron syntax.
- Dynamically validates all crons and `timezone` against Prefect's `CronSchedule` schema to catch invalid schedules at startup rather than during deployment registration. ✅

### `AppConfig` (lines 431–498)
- Cross-field validation: UNC path for LAN, GCS key for Cloud, at least one destination enabled. ✅
- **Data Loss Prevention Guard:** Detects and blocks 4-digit FY folder names (`FY2025-2026`) and rejects mismatched FY folders between source and LAN destination (`src_fy != lan_fy`). ✅
- `from_yaml` and `load_config`: UTF-8 YAML loader. ✅
- **📝 Minor note:** If `config.yaml` is completely empty (0 bytes), `yaml.safe_load(f)` returns `None`, producing `TypeError: __init__() argument after ** must be a mapping, not NoneType` rather than a config validation error. (Trivial edge case).

---

## `collect_config_data.py` ✅ CLEAN (1 minor note)

### Module Header & stdout (lines 1–14)
- `sys.stdout.reconfigure(encoding='utf-8')` — prevents UnicodeEncodeError on Windows cmd when printing emojis/checkmarks. ✅

### `get_network_info()` (lines 15–37)
- Scans `psutil.net_if_addrs()` and `net_if_stats()`.
- Filters down interfaces, loopback (`127.`), and APIPA (`169.254.`).
- Normalizes MAC to colon-separated uppercase (`AA:BB:CC:DD:EE:FF`). ✅

### `list_drives()` (lines 39–44)
- Queries `psutil.disk_partitions(all=False)`. ✅

### `is_firewall_open()` (lines 49–56)
- Queries Windows Defender Firewall via PowerShell `Get-NetFirewallPortFilter`.
- Gracefully returns `False` on failure or non-elevated prompt. ✅

### `verify_with_pydantic()` (lines 57–77)
- Merges candidate snippets into `config.yaml` and validates against `AppConfig`. ✅
- **📝 Minor note:** If `config.yaml` doesn't exist yet, `open(CONFIG_PATH)` raises `FileNotFoundError` (caught and logged as validation failure). Could fall back to `config.example.yaml` if `config.yaml` is missing.

### `main()` (lines 78–172)
- Interactive CLI generator that outputs formatted YAML snippets. Includes `input("Press Enter to exit...")` to prevent window dismissal on Windows GUI double-click. ✅

---

## `launch.py` — 🐛 1 BUG (UnboundLocalError on startup)

### Header & Environment Setup (lines 1–24)
- Sets `os.environ["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"` before any Prefect client imports. ✅

### `_run_dashboard()` (lines 26–37)
- In-thread config load and `uvicorn.run` invocation. Runs as daemon thread. ✅

### `_check_prefect_api()` (lines 39–51)
- Probes `http://127.0.0.1:4200/api/health` with 5s timeout. Returns boolean. ✅

### `_ensure_concurrency_limit()` (lines 53–86)
- Upserts global concurrency limit (`aam-backup` = 1) and tag concurrency limit via Prefect API client. ✅

### `_cancel_orphaned_runs()` (lines 88–161)
- Stale lock detection via `read_lock_alive()`.
- If a backup is actively running (live PID held in lock), skips cancelling `RUNNING` flow runs and cancels only `PENDING` runs.
- Cancels orphaned runs with `Cancelled` state on service restart. ✅

### `_reconcile_disabled_legs()` (lines 163–225)
- Server-side deployment state reconciliation: pauses disabled backup deployments and resumes enabled ones.
- Event loop safety: detects running loop and runs on a dedicated worker thread via `ThreadPoolExecutor` if needed. ✅

### `main()` (lines 227–328)
- Prefect API wait loop: polls every 10s up to 300s.
- **🐛 BUG — `UnboundLocalError` on `_cfg` (lines 286, 300):**
  On line 286, `_reconcile_disabled_legs(_cfg)` is called using the variable `_cfg`. However, `_cfg = _lc(CONFIG_PATH)` is only executed on line 300 (14 lines later). Because Python treats `_cfg` as a local variable throughout `main()`, accessing it on line 286 raises `UnboundLocalError: cannot access local variable '_cfg' where it is not associated with a value`. The enclosing `try/except Exception` catches this and logs `[launch] Deployment reconciliation FAILED (non-fatal): ...`, silently failing deployment reconciliation on every startup.
  *Fix:* Move `_cfg = load_config(CONFIG_PATH)` to the top of `main()` before calling `_reconcile_disabled_legs(_cfg)`.
- `serve(*deployments(), pause_on_shutdown=False)`: starts Prefect runner on main thread with clean Ctrl+C trap. ✅

---

## `serve.py` ✅ CLEAN

### Module Header & Imports (lines 1–15)
- Prefect 3 schedules (`Cron`) and flow imports. ✅

### `_deployments()` & `deployments()` (lines 16–90)
- Reads timezone and cron expressions from `config.yaml`.
- Conditional registration: `backup-cloud` and `backup-lan` are only registered if enabled in config.
- Always registers `weekly-report`, `monthly-report`, and `rollover-check` (daily rollover check ensures April 1 boundary is caught on 24x7 servers without requiring a service reboot).
- Returns tuple of configured `Deployment` objects. ✅

### Standalone execution (lines 93–96)
- `serve(*d, pause_on_shutdown=False)` allows direct invocation. ✅

---

## `ui.py` ✅ CLEAN (Protected by Fix A1)

### Rate Limiting & Auth (lines 39–119)
- In-memory thread-safe rate limiter with 5-minute sliding window (5 triggers, 10 logins, 10 reports). ✅
- Session tokens generated via cryptographic `secrets.token_hex(32)` with 24-hour TTL.
- API key verification uses `hmac.compare_digest` (constant-time comparison against timing attacks).
- Browser requests redirect to `/login` (303), API calls receive JSON 401. ✅

### Thread-Safe Config & DB Lifecycle (lines 120–176)
- `_CFG_LOCK` (`threading.RLock`) protects concurrent config reloads and DB connection caching across Uvicorn threadpool workers.
- Automatically detects database path changes (e.g. on FY rollover) and safely evicts/reconnects `_DB_INSTANCE`. ✅

### Pipeline State & H3 Tri-State Contract (lines 179–249)
- `_prefect_has_active_run()` queries Prefect API for `RUNNING` and `PENDING` states.
- Tri-state return: `True` (active), `False` (idle), `None` (API unreachable).
- Fail-closed for triggers (HTTP 503 when API is unreachable to prevent double-runs); fail-open for UI badge polling. ✅

### Dashboard & Status Endpoints (lines 258–443)
- `GET /`: template rendering with flash alerts, schedule summaries, and Starlette version compatibility fallback.
- `GET /status`: returns recent 25 runs (error messages safely truncated at 2000 chars), file counts, and tri-state status fields.
- `GET /health`: unauthenticated lightweight probe for uptime monitors; uses `asyncio.to_thread` for non-blocking disk checks. ✅

### Trigger & Report Endpoints (lines 445–633)
- Manual triggers (`/trigger/cloud`, `/trigger/lan`): rate limited, fail-closed against offline API, inline `await arun_deployment()`.
- Downloadable reports (`/report/weekly`, `/report/monthly`): attachment headers with sanitized firm names.
- Manual email triggers (`/trigger/report/*/email`): sends immediately to configured recipients, returns structured JSON status. ✅

### Helpers (lines 636–675)
- Disk usage querying uses `asyncio.to_thread(shutil.disk_usage, src)` to avoid blocking the async event loop on slow storage. ✅

---

## `watchdog.py` ✅ CLEAN

### Architecture & Constants (lines 1–83)
- External liveness watchdog monitoring both `AamPrefectServer` (HTTP `/api/health`) and `AamBackupAgent` (Windows SCM state).
- 5-minute failure threshold (`FAILURE_THRESHOLD=5`, `CHECK_INTERVAL_SECONDS=60`). ✅

### Configuration & Path Resolution (lines 85–114)
- `_resolve_paths()` reads lock and log paths from `config.yaml`, with safe fallbacks to `C:\BackupAgent`. ✅

### Two-Tier Backup Detection (lines 117–178)
- **Signal A (Active Transfer):** `_transfer_process_running()` checks `psutil` for live `rclone.exe` / `robocopy.exe` processes. Deferral safety cap: 8 hours (`MAX_TRANSFER_DEFERRALS=240 × 120s`). Prevents killing long-running multi-hour backups while still recovering from zombie transfers.
- **Signal B (Lock Only):** `_is_backup_running()` reads PID and creation timestamp via `read_lock_alive()`. Deferral safety cap: 30 minutes (`MAX_DEFERRALS=15 × 120s`) to clean up stale locks from PID reuse.
- G6 exception safety: catches all exceptions on corrupted lock files without crashing. ✅

### Windows SCM Management & Circuit Breaker (lines 182–293)
- `_service_state()` parses SCM state (`RUNNING`, `START_PENDING`, `STOPPED`, etc.) to distinguish transitioning states from genuine stoppages.
- Sliding 1-hour circuit breaker (`_start_allowed`): caps automatic service restarts to 3 per hour per service, preventing flapping on hard crash-loops. Logs `CRITICAL` when exhausted. ✅

### Main Monitoring Loop (lines 308–483)
- Separate transfer-deferral and lock-deferral counters prevent transfer deferrals from prematurely triggering stale-lock deletions (H4).
- Safe recovery: when Prefect server is hung, verifies no active backup is running before issuing `sc stop AamPrefectServer`.
- 120s post-restart cooldown allows Prefect server full boot time before health checks resume. ✅

---

## `flow.py` ✅ CLEAN (1 migration note)

### Imports & Concurrency Serialization (lines 1–117)
- `_stable_run_id()`: binds run IDs to Prefect `flow_run.id` so task retries maintain manifest identity. ✅
- `_backup_slot()`: global concurrency serialization (`occupy=1` on `aam-backup` limit) + PID lockfile lifecycle inside a single context manager. Enforces slot holder == lock holder (F13). Supports `PREFECT_TEST_MODE=1` for isolated testing. ✅
- **📝 Migration note (line 30):** `from core.fy_router import get_fy_prefix` — part of Fix B3 (update import to `core.time_utils`).

### Granular Task Architecture (lines 123–468)
- `health_check_task`: pre-flight health validation (clock skew, source drive, GCS key). Fails fast. ✅
- `cloud_preflight_task` & `cloud_sync_task`: rclone sync and dry-run execution. ✅
- `cloud_verify_and_report_task`: GCS integrity verification (`rclone check`) with M6 error containment (manifest query failures do not mask as empty buckets). ✅
- `cloud_record_task`: database persistence for cloud sync entries. ✅
- `wol_check_task`, `lan_preflight_task`, `lan_sync_task`: LAN backup pipeline with Wake-on-LAN and robocopy `/MIR`. ✅
- `lan_snapshot_before_task` & `lan_snapshot_after_task`: destination inventory snapshots. Post-sync walk errors (G14) gracefully degrade without corrupting sync status. ✅
- `lan_record_task`: records snapshot diffs to ManifestDB. ✅
- `lan_shutdown_task`: issues remote NAS shutdown only after complete backups. ✅
- `cloud_publish_artifact_task` & `lan_publish_artifact_task`: publishes formatted Markdown run metrics to Prefect Console UI. ✅

### Pipeline Orchestration (lines 474–777)
- `_run_cloud_pipeline()`: sequentially executes cloud tasks with retry policies. Enforces strict verification contract (F1: `CLOUD_VERIFY_FAILED` on mismatch with alert email). F2 phase-aware terminal status recording. Monotonic duration clock (F16) immune to NTP adjustments. ✅
- `_run_lan_pipeline()`: sequentially executes LAN tasks. F3 safety guard: NAS is powered down only on `LAN_COMPLETE`; on `LAN_PARTIAL` (exit 4–15), shutdown is skipped and an alert email is dispatched. ✅
- `_record_run()`: atomic execution history insertion into `ManifestDB`. ✅

### Maintenance, Reports, and Flow Entrypoint (lines 840–1025)
- `weekly_report_flow` & `monthly_report_flow`: scheduled email summary reports. ✅
- `rollover_check_flow`: daily scheduled FY rollover check (G10). Idempotent on all days, triggers archival and path updates on April 1 boundary. Dispatches alert email if blocked. ✅
- `backup()`: main entrypoint for `cloud`, `lan`, and `all` modes. Orphaned robocopy log cleanup (G8), sequential pipeline execution, `ExceptionGroup` error aggregation, and automatic DB retention pruning (`purge_old_runs`). ✅

---

## Non-Core Audit Summary

| File | Status | Key Findings |
|------|--------|-------------|
| `models/__init__.py` | ✅ CLEAN | Package exports match `__all__`. |
| `models/config.py` | ✅ CLEAN | 2 minor notes: unvalidated `project_number` string; empty YAML edge case. |
| `collect_config_data.py` | ✅ CLEAN | 1 minor note: missing `config.yaml` fallback on initial setup. |
| `launch.py` | 🐛 1 BUG | `UnboundLocalError` on `_cfg` in `main()` causes `_reconcile_disabled_legs` to fail silently on every startup. |
| `serve.py` | ✅ CLEAN | Clean deployment definitions for Prefect 3. |
| `ui.py` | ✅ CLEAN | Rate limiting, session security, tri-state API error handling, and thread-safe DB lifecycle. (Protected by Fix A1). |
| `watchdog.py` | ✅ CLEAN | 2-tier backup active detection, circuit-breaker auto-restarts, defensive against corrupted locks and SCM transitions. |
| `flow.py` | ✅ CLEAN | 1 migration note: line 30 imports from `fy_router` instead of `time_utils`. |







