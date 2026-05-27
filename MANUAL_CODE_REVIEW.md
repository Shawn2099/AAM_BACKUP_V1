# Manual Code Review

Line-by-line analysis of every source file. No changes — just understanding.

---

## File: `core/cloud_preflight.py`

### Purpose
Fast pre-sync validation using `rclone check --one-way` — catches auth failures, missing buckets, and config errors before the multi-hour sync starts. 86 lines.

### What it does
1. Writes a temp rclone config file from the GCS key
2. Runs `rclone check --one-way --fast-list source → aam_gcs:bucket/prefix`
3. Exit 0 = in sync, Exit 1 = differences found (normal), Exit 2+ = error
4. Cleans up temp config in `finally`

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 11 | 🟡 Low | `write_temp_config as _write_temp_config` — imports a public function under a private alias. In `flow.py` it's re-imported again as `from core.cloud_preflight import _write_temp_config`. Confusing re-export chain. |
| 57 | 🟡 Medium | `timeout=300` hardcoded — 5 minutes for metadata-only check is usually fine, but for buckets with millions of files this could timeout. Should come from `CloudConfig`. |
| 60-62 | 🟡 Low | `ok = code < 2` treats *any* subprocess death including signal kills (exit code -N) as `ok=True, matched=False`. A killed process should be an error. Should be `ok = code in (0, 1)`. |
| 65 | 🟢 Info | `result.stderr[:300]` truncates error messages — could lose useful debugging info in rare cases. |
| 19 | 🟢 Info | `location: str = "asia-south1"` — default duplicated from `CloudConfig`. Not a bug since `flow.py` always passes it explicitly, but a minor DRY violation. |

### What's Good
- ✅ Clean `finally` block always deletes temp config
- ✅ Proper rclone exit code classification (0 & 1 are valid)
- ✅ Covers `TimeoutExpired`, `FileNotFoundError`, `OSError` separately
- ✅ Docstring is accurate with return type documented
- ✅ `--gcs-no-check-bucket` flag — skips unnecessary bucket existence check (faster)

### Design Question: Temp vs Permanent rclone Config
Current approach creates and destroys a temp config on every call. See detailed analysis at `core/rclone_config.py` review.

---

## File: `core/cloud_reporter.py`

### Purpose
Queries GCS state via rclone subcommands — size, manifest (file listing), and diff (added/removed/modified). 119 lines.

### Architecture

```
_base_args()            → shared rclone flags (--config, --gcs-no-check-bucket, --fast-list)
get_cloud_size()        → rclone size --json  (30s timeout)
get_cloud_manifest()    → rclone lsjson -R    (5min timeout)
get_cloud_diff()        → rclone check --combined → temp file parse  (10min timeout)
```

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 17 | 🟡 Low | `_base_args()` helper exists here but `cloud_preflight.py` and `cloud_verify.py` inline the same flags. `cloud_sync.py` has its own `build_rclone_sync_command()`. **3 different patterns across 4 files** for composing rclone flags — no shared helper. |
| 33 | 🟡 Low | Catches `KeyError` alongside timeout/JSON errors. If rclone returns unexpected JSON schema, "Cloud size query failed" is misleading — you'd have no idea it was a schema mismatch, not a network issue. |
| 48 | 🟢 Info | `json.loads(result.stdout)` — no `.strip()` unlike line 30 in `get_cloud_size()`. If rclone ever emits leading whitespace, this crashes while its sibling handles it fine. Minor consistency gap. |
| 52-54 | 🟡 Medium | **Silent failure**: returns `[]` on error. `flow.py` calls this and processes the result — 0 files is indistinguishable from "GCS is empty". No exception, no log warning propagated. |
| 84 | 🟡 Medium | `timeout=600` hardcoded. 10-minute wall clock timeout for the diff operation. Not in config, not tunable per deployment. |
| 94-97 | 🟢 Info | `line[2:]` assumes rclone's `--combined` output format is stable. Currently correct, but undocumented coupling to rclone's display format. |
| 102-103 | 🟡 Medium | `except FileNotFoundError: pass` — if the temp file wasn't written by rclone, returns empty diff **with no log entry at all**. No warning, no error. This is a silent data loss path for deletion tracking. |
| 111-113 | 🟡 Medium | **Systemic silent degradation**: all three public functions swallow exceptions and return zeros/empties. If GCS key expired, network down, or rclone crashed, the pipeline continues and produces a report saying "0 files, 0 bytes." Backups are fine, but reporting + deletion tracking are silently lost. |
| 35, 54, 113 | 🟢 Info | Temp file hygiene is correct everywhere (`mkstemp` + `os.close(fd)` + `finally` unlink). Consistent with rclone_config.py pattern. |

### Behavior Trace: What Happens When Reporting Fails

```
flow.py calls get_cloud_manifest()
  → rclone timeout / crash / auth error
  → returns [] silently
  → flow.py iterates 0 files, marks nothing synced
  → no error logged above WARNING level
  → dashboard shows 0 cloud files → looks like "nothing in GCS"
```

### What's Good
- ✅ Temp file hygiene is correct and consistent
- ✅ `get_cloud_diff()` correctly handles all 4 rclone diff states (+ - * =)
- ✅ Timeouts are reasonable for each operation type
- ✅ `--gcs-no-check-bucket` and `--fast-list` on every call
- ✅ Docstring clearly documents return format

---

## File: `core/rclone_config.py`

### Purpose
Single source of truth for writing a temporary rclone config file from the GCS service account key. Used by cloud_preflight, cloud_sync, and cloud_reporter callers. 36 lines.

### Line-by-Line

```python
def write_temp_config(
    gcs_key_path: str,
    location: str,
    project_number: str = "920173882190",   # Line 14
    storage_class: str = "COLDLINE",         # Line 15
) -> str:
```

**Line 22:** `key_abs = str(Path(gcs_key_path).resolve()).replace("\\", "/")`
- Resolves relative paths to absolute and normalizes Windows backslashes to forward slashes.
- `/` is what rclone expects in config files even on Windows.
- **But**: if `gcs_key_path` points to a network share (`\\server\share\key.json`), `.resolve()` might not produce a path rclone can read depending on the service user's permissions.

**Lines 23-32:** Config content string
```ini
[aam_gcs]
type = google cloud storage
service_account_file = {key_abs}
project_number = {project_number}
object_acl =
bucket_acl =
bucket_policy_only = true
location = {location}
storage_class = {storage_class}
```
- `object_acl =` and `bucket_acl =` are left empty — correct, since `bucket_policy_only = true` means ACLs are disabled at the bucket level.
- `service_account_file = {key_abs}` references the JSON key file path, not the key contents. The actual GCS credential JSON stays on disk and is never embedded in the config.

**Lines 33-35:**
```python
fd, cfg_path = tempfile.mkstemp(suffix=".conf", prefix="rclone_")
os.close(fd)
Path(cfg_path).write_text(content, encoding="utf-8")
return cfg_path
```
- `mkstemp` + `os.close(fd)` = the NamedTemporaryFile fix documented in `WINDOWS_SERVER_2016_FINDINGS.md`. On Windows, `NamedTemporaryFile` holds an exclusive lock that prevents subprocesses (rclone) from reading the file. `mkstemp` returns only a file descriptor (no Python file object), then `close(fd)` releases it — rclone can open the file freely.
- `suffix=".conf"` — rclone recognizes `.conf` files as config. Correct.
- Encoding is explicit `utf-8` — the NamedTemporaryFile fix from Windows findings.

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 14-15 | 🟡 Low | Defaults `project_number` and `storage_class` are duplicated from `CloudConfig` model. Not a bug since callers pass values from config, but creates two sources of truth. |
| 22 | 🟢 Info | Network UNC path resolution: if `gcs_key_path` is on a UNC share (`\\server\share\key.json`), `.resolve()` behavior varies by OS and could produce a path the rclone service user can't read. Windows Server 2016 specific. |
| 22 | 🟢 Info | No `Path.exists()` check on the GCS key before writing config. If the key file was deleted or the path is wrong, rclone will fail at runtime with a confusing "service_account_file not found" error rather than a clear validation message at config-write time. |
| — | 🟡 Low | No `config_path` validation anywhere — callers pass the result to rclone. A failed `mkstemp` (disk full, permission denied) would raise an unhandled `OSError`. |

### What's Good
- ✅ NamedTemporaryFile workaround correctly implemented for Windows Server 2016
- ✅ Explicit UTF-8 encoding
- ✅ `bucket_policy_only = true` matches modern GCS best practice (uniform bucket-level access)
- ✅ Clean, minimal — 36 lines, one job, does it well

### The Temp Config Design Decision

See the discussion in `cloud_preflight.py` above. Summary of options:

| Approach | Pros | Cons |
|----------|------|------|
| **Temp config** (current) | Zero setup, always picks up config.yaml changes | File I/O per operation, cleanup burden on callers |
| **Permanent `rclone config`** | Manual debugging works, no temp files | Extra deployment step, user-profile dependent on Windows |
| **Managed fixed-path config** | Manual debugging, no drift, no temp file risk | Must regenerate when config.yaml changes |

---

## File: `core/cloud_sync.py`

### Purpose
The most critical file in the cloud pipeline — executes the actual rclone sync that transfers files to GCS. 138 lines, 3 functions.

---

### Function 1: `classify_rclone_exit()` (lines 13-41)

Maps rclone exit codes 0-10 to status strings. Default `CLOUD_FAILED` for anything unknown.

```python
mapping = {
    0: "CLOUD_COMPLETE",   9: "CLOUD_COMPLETE",
    4: "CLOUD_PARTIAL",    5: "CLOUD_PARTIAL",
    6: "CLOUD_PARTIAL",   10: "CLOUD_PARTIAL",
    # 1,2,3,7,8 + anything else → CLOUD_FAILED
}
return mapping.get(code, "CLOUD_FAILED")
```

**Verdict:** Solid. Fail-closed default handles negative exit codes and undocumented rclone exits correctly.

---

### Function 2: `build_rclone_sync_command()` (lines 44-76)

Builds the rclone sync command with GCS-optimized flags.

| Flag | Purpose |
|------|---------|
| `--fast-list` | Uses GCS native list ops |
| `--gcs-no-check-bucket` | Skips bucket existence check |
| `--modify-window 1s` | 1s mtime tolerance (GCS stores seconds) |
| `--track-renames` | Detects renames instead of re-upload |
| `--no-traverse` | Local→remote: skip remote scan |
| `--transfers 4` | Parallel file transfers |
| `--checkers 16` | Parallel checkers |
| `--use-json-log` | Structured stderr output |
| `--stats 60s` | Progress every 60s |

**Issues:**

| Line | Severity | Issue |
|------|----------|-------|
| 52-53 | 🟡 **Medium** | `transfers=4`, `checkers=16` hardcoded as defaults. `flow.py` does NOT pass these from `config.yaml` — they always use defaults. Significant performance-tuning knobs are not configurable without code changes. |
| 67-68 | 🟢 Info | `str(transfers)` and `str(checkers)` — explicit conversion is defensive but hides type mismatches. If someone passed a string, it would silently work. Minor. |
| 73-74 | 🟢 Info | `--use-json-log --log-level INFO --stats 60s` all write to stderr, which is fully buffered in memory by `subprocess.run()`. Fine for normal runs, but a 6-hour sync produces ~360 stats lines at ~200 bytes each (~72KB). Acceptable. |

---

### Function 3: `run_cloud_sync()` (lines 79-138)

The main execution: write temp config → run sync → classify exit → cleanup.

**🔴 HIGH: Error message silently discarded (line 121)**

```python
result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
status = classify_rclone_exit(result.returncode)
# result.stderr contains rclone's error message
# but it is NEVER read or returned
return {"status": status, "exit_code": result.returncode, "error": None}
```

When rclone fails (exit 1 = general error, exit 3 = directory not found, exit 7 = auth failure), the **actual diagnostic message from rclone stderr is thrown away**. The caller gets `{"status": "CLOUD_FAILED", "exit_code": 1, "error": None}` — zero diagnostic value.

The logger only logs at INFO level with no error detail:
```python
logger.info(f"Cloud sync exit {result.returncode} → {status}")
```

This should be:
```python
error_msg = result.stderr[:500] if result.returncode != 0 else None
return {"status": status, "exit_code": result.returncode, "error": error_msg}
```

| Line | Severity | Issue |
|------|----------|-------|
| 121 | 🔴 **High** | rclone stderr error message discarded on non-zero exit — caller gets no diagnostic info |
| 103 | 🟡 Medium | Positional args to `write_temp_config()` — fragile if parameter order changes. Should use keyword args. |
| 104 | 🟢 Info | 9 arguments on one line — hard to read |
| 131 | 🟢 Info | `str(e)` on OSError loses Windows error codes. `repr(e)` includes `winerror` attribute. |

**What's Good:**
- ✅ Comprehensive exit code classification covers all documented rclone codes
- ✅ Proper temp file cleanup in `finally`
- ✅ GCS-optimized flag selection is correct
- ✅ `--track-renames` avoids re-uploading renamed files
- ✅ `--retries-sleep 30s` — backoff delay between rclone's internal retries
- ✅ Timeout passed through from config (unlike cloud_preflight.py)


---

## File: `core/cloud_verify.py`

### Purpose
Post-sync integrity verification — runs `rclone check --one-way` to confirm source and GCS are byte-identical after sync completes. 74 lines, single function.

### Function: `verify_cloud_integrity()` (lines 8-74)

```python
def verify_cloud_integrity(
    source: str, bucket: str, fy_prefix: str, config_path: str,
    timeout: int = 600,
) -> dict:
```

Runs `rclone check --one-way --fast-list source aam_gcs:bucket/prefix`.

| Exit | Meaning |
|------|---------|
| 0 | Everything matches — verified |
| 1 | Differences found (something didn't sync) |
| 2+ | Error (config, auth, network) |

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 13 | 🟡 **Medium** | `timeout: int = 600` — parameter exists but `flow.py` never passes it. `CloudConfig` has no `verify_timeout_seconds`. Effectively hardcoded at 10 minutes despite looking configurable. |
| 63 | 🟡 **Medium** | `"Exit {code}: mismatch detected"` — one error string for ALL non-zero exits. Exit 1 (differences found, post-sync warning) and Exit 7 (auth failure, critical) return the same message. Should differentiate. |
| 57 | 🟢 Info | Stderr IS captured for logging but NOT returned in the dict. Same pattern as `cloud_sync.py`. |
| — | 🟢 Info | No `--combined` flag — binary pass/fail only. No detail on which files differ. Acceptable for current use. |

### Cross-File Inconsistency: Config Lifecycle

```
cloud_preflight.py → creates AND destroys its own temp config
cloud_sync.py     → creates AND destroys its own temp config
cloud_verify.py   → RECEIVES pre-built config from flow.py
cloud_reporter.py → RECEIVES pre-built config from flow.py
```

Two different patterns within the same module family.

### What's Good
- ✅ Explicit timeout parameter (even if not wired from caller yet)
- ✅ Stderr captured and logged for diagnostics
- ✅ Clean single-responsibility function
- ✅ Correct GCS flags (`--fast-list`, `--gcs-no-check-bucket`)
- ✅ Consistent exception handling with other cloud modules


---

## File: `core/fy_router.py`

### Purpose
Compute GCS fiscal year folder prefix from IST date. Auto-rollover on April 1. 26 lines.

### Full Analysis

```python
IST = ZoneInfo("Asia/Kolkata")          # Line 6 — module-level, runs on import

def get_fy_prefix(today: date | None = None) -> str:
    if today is None:
        today = datetime.now(IST).date()
    year = today.year
    if today.month >= 4:
        return f"FY{year % 100:02d}-{(year + 1) % 100:02d}"
    return f"FY{(year - 1) % 100:02d}-{year % 100:02d}"
```

### Edge Case Verification

| Date | Expected | Actual | Verdict |
|------|----------|--------|---------|
| 2026-04-01 | FY26-27 | FY26-27 | ✅ April 1 fires the new FY |
| 2026-03-31 | FY25-26 | FY25-26 | ✅ March 31 stays in old FY |
| 2099-04-01 | FY99-00 | FY99-00 | ✅ Century rollover: `(year+1) % 100 = 0` → `00` |
| 2000-04-01 | FY00-01 | FY00-01 | ✅ Millenium rollover: `(year-1) % 100 = 99` → resolved to `00-01` |

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 6 | 🟢 Info | `IST = ZoneInfo(...)` at module level — runs on import. If `tzdata` package is missing on Windows Server 2016, the entire module fails with `ZoneInfoNotFoundError`. A lazy `_get_ist()` function would isolate this. |

That is the **only** finding in this file. The core logic is exhaustively correct for all date boundaries including century rollover.

### What's Good
- ✅ Pure function — zero side effects, zero I/O, zero state
- ✅ Single responsibility: computes one thing and does it correctly
- ✅ Standard library only — `zoneinfo` is built into Python 3.9+
- ✅ `date \| None` type hint documents the contract without enforcement
- ✅ Timezone-aware via `ZoneInfo("Asia/Kolkata")`, not system local time
- ✅ Well-documented docstring with real example
- ✅ Format spec `% 100:02d` correctly handles single-digit years with leading zero


---

## File: `core/hashing.py`

### Purpose
MD5 checksum computation compatible with `rclone hashsum md5`. 27 lines, 2 functions, 1 sentinel constant.

### Full Analysis

```python
PENDING_CHECKSUM = "pending"

def compute_md5(file_path: str | Path) -> str:
    with open(file_path, "rb") as f:
        return hashlib.file_digest(f, "md5").hexdigest()

def verify_checksum(file_path: str | Path, expected: str) -> bool:
    if expected == PENDING_CHECKSUM:
        return False
    return compute_md5(file_path) == expected
```

**`compute_md5()`:** Uses `hashlib.file_digest()` (Python 3.11+). Streams the file in internal chunks — memory-safe for multi-GB files. Hex output matches `rclone hashsum md5`.

**`verify_checksum()`:** Returns `False` for `PENDING_CHECKSUM` sentinel. Otherwise computes and compares. Docstring explicitly documents the false-negative contract for uncatalogued files.

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 15 | 🟡 **Medium** | `open(file_path, "rb")` — zero error handling. `FileNotFoundError`, `PermissionError`, `IsADirectoryError` all propagate unhandled to the caller. No try/except. |
| 25-27 | 🟢 Info | Same propagation through `verify_checksum()`. If the file can't be read, the exception passes through uncaught. |
| — | 🟢 Info | `verify_checksum()` may be **dead code** — `flow.py` never calls it. It exists as a utility for future use. `compute_md5()` could also be uncalled in the main pipeline. |

### What's Good
- ✅ `hashlib.file_digest()` — uses the modern streaming API, not a naive `read()`-everything approach
- ✅ `PENDING_CHECKSUM` sentinel avoids false positives for uncatalogued files
- ✅ Accepts `str | Path` — no forced conversion needed by callers
- ✅ Docstring explicitly documents the sentinel behavior
- ✅ rclone-compatible output format


---

## File: `core/health.py`

### Purpose
Pre-backup health checks: source drive, binary existence, GCS key, system clock skew, and a composite `pre_backup_health()` orchestrator. 124 lines.

### Function-by-function

#### `HealthError(RuntimeError)` (line 12)
Custom exception. Standard pattern. Nothing to flag.

#### `check_source_drive()` (lines 16-51)
Checks source path exists, counts files via `rglob("*")`, measures free space via `shutil.disk_usage()`.

#### `check_binary_exists()` (lines 54-56)
`shutil.which(name) is not None`. Clean, one-liner. ✅

#### `check_gcs_key()` (lines 59-66)
Verifies GCS service account key file exists and is non-empty. No JSON format validation — acceptable for a pre-flight check.

#### `check_clock_skew()` (lines 69-99)
HEAD request to `www.googleapis.com`, parses HTTP `Date` header, compares to local UTC time. Silently passes if Google is unreachable.

#### `pre_backup_health()` (lines 102-124)
Composite function that runs source drive check and binary checks. **Does NOT call `check_gcs_key()` or `check_clock_skew()`.**

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 28 | 🔴 **HIGH** | `source.rglob("*")` on a large source drive (500K+ files, deep trees, or network share) blocks the **entire pipeline synchronously** — this is a pre-backup check that runs before any backup begins. Could take minutes on Windows Server 2016 with HDD or slow network paths. Only purpose is detecting "empty drive" (line 34-35); could be replaced with a cheap `any(source.iterdir())` check. |
| 102-124 | 🟡 **Medium** | `pre_backup_health()` never calls `check_gcs_key()` or `check_clock_skew()` — both are **dead code**. The system depends on rclone to fail later with its own error message when clock skew exceeds 10 minutes or GCS key is invalid. |
| 48-49 | 🟡 **Medium** | `except OSError` on `shutil.disk_usage()` is silently swallowed — function returns `(True, "")`. Backup proceeds even when free space could not be measured. Silent degradation. |
| 102, 116, 120 | 🟡 **Medium** | `mode: str` accepts any string. Should use `Literal["cloud", "lan", "all"]` from `typing` to catch typos at caller sites. |
| 80 | 🟢 Info | `conn.close()` not in `finally` — connection leak if `parsedate_to_datetime()` or subsequent code raises before `close()`. The broad `except Exception` on line 97 saves it in practice, but a `with` context manager would be cleaner. |
| 39 | 🟢 Info | `min_free_gb: int = 1` — default of 1 GB is very low for production servers with TB-sized drives. Should be configurable from `CloudConfig` / `LanConfig`. |
| 40-44 | 🟢 Info | `free_gb` is formatted with `:1f` — one decimal place. For large drives, `"2048.0 GB free"` is correct but less readable than rounding to integer. Cosmetic. |
| 59-66 | 🟢 Info | `check_gcs_key()` validates existence but not JSON format or file permissions. A malformed key file passes this check only to fail later in rclone. Acceptable for a pre-flight lightweight check. |
| 72-73 | 🟢 Info | Docstring references GCS JWT 10-minute tolerance but `max_skew_seconds` defaults to 600. This is correct but `check_clock_skew()` is never called (see Medium finding above). |

### What's Good
- ✅ `shutil.which()` for cross-platform binary detection — works on Windows (respects PATHEXT for `.exe`, `.bat`, etc.)
- ✅ Clock skew check uses standard library only — no external deps for HTTP parsing
- ✅ `email.utils.parsedate_to_datetime()` correctly parses RFC 2822 `Date` header
- ✅ `check_clock_skew()` degrades gracefully when Google is unreachable (broad except, silent pass)
- ✅ `HealthError` extends `RuntimeError` — conventional, catchable by broad `except Exception` handlers
- ✅ Docstrings on every function describe return format `tuple[bool, str]`
- ✅ GCS key check: explicit zero-file detection (not just existence)
- ✅ Proper use of `PermissionError` and `OSError` separation in `check_source_drive()`


---

## File: `core/lan_manifest.py`

### Purpose
Walk a LAN destination via `os.walk` + `os.stat` to produce a file inventory, then diff before/after snapshots to detect added/removed/modified/unchanged files. 81 lines, 3 public functions.

### Philosophy
Module docstring: *"No scanner. No log parsing. No regex. Just os.walk + os.stat. The filesystem IS the truth."* — refreshing clarity of purpose.

### Function-by-function

#### `walk_lan_destination(unc_path: str) -> list[dict]`
Walks the UNC share, stats every file, returns `[{path, size, mtime}, ...]`. Skips files where `os.stat()` raises OSError.

#### `snapshot_to_dict(files: list[dict]) -> dict[str, tuple[int, float]]`
Converts walk result to `{rel_path: (size, mtime)}` for O(1) diff lookup.

#### `diff_snapshots(before, after) -> dict`
Set-based diff. Returns sorted `added`, `removed`, `modified`, `unchanged` lists.

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 27-43 | 🔴 **HIGH** | `walk_lan_destination()` loads the **entire file listing into memory** (`list[dict]`). On a LAN share with 1M+ files and 100-char average path, each entry is ~150 bytes → ~150 MB just for path strings, plus dict/list overhead (~300+ MB total). Called **twice per backup cycle** (before + after sync). No streaming or chunking alternative. |
| 27 | 🔴 **HIGH** | `os.walk` over SMB/UNC on Windows Server 2016 is **synchronous and slow** — each directory entry and `os.stat()` call traverses the network. No progress logging. No timeout. For large directory trees this can block the pipeline for minutes to hours with zero feedback. |
| 27 | 🟡 **Medium** | No error handling around `os.walk()` itself. If the UNC root becomes unreachable mid-walk (network drop, server restart), an unhandled `OSError` propagates. The per-file `OSError` skip (line 32-33) does not protect the walk iteration itself. |
| 56-57 | 🟡 **Medium** | `diff_snapshots` iterates the intersection **twice** (lines 75-80: modified comprehension + unchanged comprehension). On 1M files, this is 2M dict lookups. Could partition in a single pass. |
| 72-80 | 🟡 **Medium** | All four result lists are `.sorted()`. For diff consumers that only need counts or set membership, sorting millions of entries is O(n log n) with no benefit. Caller-dependent, but `diff_snapshots` doesn't know the caller's needs. |
| 38 | 🟢 Info | `os.stat().st_mtime` on Windows/SMB has variable precision. NTFS local → 100ns. Over SMB, depends on remote server and filesystem (FAT32 → 2-second resolution). Using exact `(size, mtime)` tuple comparison may produce false "modified" results due to timestamp rounding differences. |
| 25 | 🟢 Info | `Path(unc_path).resolve()` on a UNC path that doesn't exist returns the path as-is (non-strict). Fine for existing destinations, but the `resolve()` call is unnecessary if `unc_path` is already a normalized UNC — it adds a filesystem access for no benefit. |
| 46-48 | 🟢 Info | `snapshot_to_dict()` silently overwrites duplicate keys. On a case-insensitive filesystem (Windows default), two files differing only in case would produce identical relative path strings — later wins, no warning. Extremely rare in practice. |
| 23 | 🟢 Info | Docstring says `"rel\\path\\file.txt"` with double backslashes. On Windows this matches `os.path.relpath()` output. On a non-Windows system (unlikely for this project) the forward/backslash mismatch could break callers. |

### What's Good
- ✅ Zero dependencies beyond standard library — `os` + `os.path` only
- ✅ Module docstring clearly states philosophy in plain English
- ✅ Per-file OSError handling (locked/deleted files skip gracefully)
- ✅ Pure functions throughout — no side effects, no mutable defaults
- ✅ Set-based diff is clean O(n) — the right algorithm choice
- ✅ `snapshot_to_dict` is a single-line comprehension — trivially verifiable
- ✅ Type hints on all function signatures
- ✅ Docstrings with Args/Returns on every public function
- ✅ Sorted output — deterministic, easier to diff in test assertions


---

## File: `core/lan_preflight.py`

### Purpose
Run `robocopy /L /MIR /XJ` as a dry-run to validate that source and LAN destination paths are reachable and readable before committing to a full sync. 67 lines, 1 function.

### Full listing for reference

```python
def run_lan_dry_run(source: str, dest: str, timeout: int = 300) -> dict:
    cmd = ["robocopy", source, dest, "/L", "/MIR", "/XJ", "/NJH", "/NJS", "/NP"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        code = result.returncode
        ok = code < 8
        if not ok:
            stderr_snippet = result.stderr[:200] if result.stderr else "no stderr"
            return {"ok": False, "exit_code": code, "error": f"Robocopy /L failed with exit {code}"}
        return {"ok": True, "exit_code": code, "error": None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "error": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "exit_code": -1, "error": "robocopy.exe not found"}
    except OSError as e:
        return {"ok": False, "exit_code": -1, "error": str(e)}
```

**Exit code logic (`code < 8`):** Robocopy exit codes are bitmasked:
- `0` = no files match / already in sync
- `1` = files copied successfully
- `2` = extra files/dirs detected
- `4` = mismatched files detected
- `8` = copy errors (files couldn't be read)
- `16` = fatal error

`code < 8` correctly treats 0-7 as pass, 8+ as failure. For `/L` (list-only), exit 4 or 7 just means "files would be modified" — not a preflight error.

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 39-44 | 🟢 Info | `capture_output=True` captures both stdout and stderr, but **stdout is discarded**. Robocopy `/L` output lists every file that would be affected. Useful for debugging, but never logged or returned. |
| 52 | 🟢 Info | `result.stderr[:200]` truncates error messages to 200 chars. Robocopy errors on Windows are typically short, but a deeply nested path or long error string could be cut off. |
| 28-34 | 🟢 Info | Robocopy flags are hardcoded (`/L`, `/MIR`, `/XJ`, `/NJH`, `/NJS`, `/NP`). If `lan_sync.py` uses different flags, the dry-run won't match real behavior. Verify consistency with `lan_sync.py`. |
| 12 | 🟢 Info | `timeout: int = 300` — hardcoded default, not configurable from `LanConfig`. Same pattern as `cloud_preflight.py`. |

### Exit code verification

| Code | Scenario | `code < 8` | Verdict |
|------|----------|-----------|---------|
| 0 | Already in sync | ✅ pass | Correct |
| 1 | Files would be copied | ✅ pass | Correct (dry-run) |
| 4 | Mismatched files | ✅ pass | Correct (dry-run) |
| 7 | Copied + extra + mismatched | ✅ pass | Correct |
| 8 | Copy errors (can't read source) | ❌ fail | Correct |
| 16 | Fatal error | ❌ fail | Correct |

### What's Good
- ✅ Correct robocopy exit code interpretation via `code < 8` — commented with bitmask reasoning
- ✅ `/L` flag ensures zero bytes are moved during validation
- ✅ `/XJ` excludes junction points — prevents infinite loops on Windows
- ✅ Minimal output flags (`/NJH /NJS /NP`) — clean stderr for error detection
- ✅ Graceful exception handling for all failure modes: timeout, missing binary, OS error
- ✅ Return dict has consistent shape with cloud modules (`ok`, `exit_code`, `error`)
- ✅ Docstring documents every flag's purpose
- ✅ `FileNotFoundError` catch specifically for missing `robocopy.exe` — correct exception name


---

## File: `core/lan_sync.py`

### Purpose
Execute `robocopy /MIR` mirror sync with production-verified flags, temp log file, and exit code classification. 126 lines, 3 functions.

### Full listing for reference

```python
def _validate_required_flags(flags: list[str]) -> None:        # Line 22
    for f in flags:
        if f.upper() in ("/NC", "-NC"):
            raise ValueError("/NC flag suppresses file class labels")

def classify_exit_code(code: int) -> str:                       # Line 28
    if code & 16: return "LAN_FAILED"
    if code & 8:  return "LAN_PARTIAL"
    if 0 <= code <= 7: return "LAN_COMPLETE"
    return "LAN_FAILED"

def build_robocopy_command(source, dest, lan_config) -> list[str]:  # Line 48
    flags = [
        "/MIR", "/Z", "/XJ",
        f"/MT:{lan_config.mt_threads}",
        f"/R:{lan_config.retry_count}",
        f"/W:{lan_config.retry_wait_seconds}",
        "/V", "/TS", "/FP",
        "/NJH", "/NJS", "/NDL", "/NP",
        "/XD", "System Volume Information",
    ]
    _validate_required_flags(flags)
    return ["robocopy", source, dest, *flags]

def run_lan_sync(source, dest, lan_config) -> dict:             # Line 70
    cmd = build_robocopy_command(source, dest, lan_config)
    log_fd, log_path_str = tempfile.mkstemp(suffix=".log", prefix="robocopy_sync_")
    os.close(log_fd)
    log_path = Path(log_path_str)
    cmd.extend([f"/LOG:{log_path}"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=...)
    return {"status": classify_exit_code(result.returncode), "exit_code": ..., "log_path": str(log_path), ...}
    # ✂️ finally: log_path.unlink() ← DELETED before caller sees it
```

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 121-126 | 🔴 **HIGH** | **Log file deleted in `finally` before caller can read it.** `run_lan_sync` returns `{"log_path": str(log_path), ...}` but the `finally` block immediately calls `log_path.unlink()`. By the time the caller inspects the return dict, the log file is gone. The `log_path` in the return dict always points to a deleted file. **Bug.** |
| 95-100 | 🟡 **Medium** | `capture_output=True` captures stdout/stderr into memory but **neither is ever read or logged**. The real output goes to `/LOG:`. This wastes memory (potentially MBs), adds `UnicodeDecodeError` risk from `text=True`, for zero benefit. Should be `capture_output=False`. |
| 56 | 🟡 **Medium** | `/Z` (restart mode) instead of `/ZB` (restart + backup mode fallback). On Windows Server 2016, `/ZB` uses Volume Shadow Copy to read files locked by other processes (open DB files, logs). `/Z` alone will fail with sharing violations on locked files. Backup mode is critical for production backups. |
| 22-25 | 🟢 Info | `_validate_required_flags` misnamed — only checks the *forbidden* `/NC` flag. Does not validate that *required* flags (`/V`, `/FP`, `/TS`) are present. Name suggests broader validation than implemented. |
| 63 | 🟢 Info | `/XD "System Volume Information"` is hardcoded. Only one exclusion path supported. Production deployments may need multiple exclusions (pagefile, hiberfil, browser caches). |
| 95 | 🟢 Info | `capture_output=True` with `text=True` forces UTF-8/ANSI decoding of robocopy pipe output. English robocopy is safe, but non-English locale or unusual characters in file paths could trigger `UnicodeDecodeError`. |

### Exit code classification matrix

| Code | Bits | `classify_exit_code()` | Meaning |
|------|------|----------------------|---------|
| 0 | None | `LAN_COMPLETE` | Identical / no action |
| 1 | Bit 0 | `LAN_COMPLETE` | Files copied |
| 2 | Bit 1 | `LAN_COMPLETE` | Extra files |
| 4 | Bit 2 | `LAN_COMPLETE` | Mismatch |
| 7 | 0+1+2 | `LAN_COMPLETE` | Copied + extra + mismatched |
| 8 | Bit 3 | `LAN_PARTIAL` | Some files failed to copy |
| 16 | Bit 4 | `LAN_FAILED` | Fatal error |
| 23 | 0+1+2+4 | `LAN_PARTIAL` **← Bug?** | Copied some + extras + mismatches + copy errors → should be `LAN_PARTIAL` per severity ordering, but this is actually correct (bit 8 catches first) |

### What's Good
- ✅ Docstring references the proven robocopy.py from AAM_BACKUP_V2 — traceable provenance
- ✅ `/MIR` + `/MT:N` + `/R:N /W:N` — all critical production flags present
- ✅ `/XJ` excludes junction points — prevents infinite walks
- ✅ `/XD "System Volume Information"` — preemptively excludes VSS shadow store
- ✅ `tempfile.mkstemp` for log output — no collision risk, no stale artifacts on crash
- ✅ `classify_exit_code` correctly prioritizes bit 4 > bit 3 > bits 0-2 — severity ordering
- ✅ Configurable multi-thread count, retry count, retry wait, and subprocess timeout via `LanConfig`
- ✅ Graceful exception handling: TimeoutExpired, FileNotFoundError, OSError all produce structured error dicts
- ✅ Guard against `/NC` flag upstream — prevents silent log parsing failures
- ✅ Log cleanup in `finally` prevents temp file leaks (despite the timing bug)


---

## File: `core/logging.py`

### Purpose
Configure Loguru with daily rotating files, 30-day retention, and stderr output. 40 lines, 1 function + 1 format constant.

### Full listing for reference

```python
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"

def configure(log_dir: str | Path) -> None:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, format=LOG_FORMAT, level="INFO", colorize=True)
    logger.add(
        log_dir / "backup_{time:YYYY-MM-DD}.log",
        rotation="1 day", retention="30 days",
        encoding="utf-8", level="DEBUG",
        format=LOG_FORMAT, enqueue=True,
    )
```

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 35 | 🟢 Info | No `compression` on rotation. 30 days of DEBUG-level logs from daily multi-hour backups can accumulate significantly (potentially hundreds of MB). Consider `compression="gz"` to reduce disk usage. |
| 38 | 🟢 Info | `enqueue=True` writes log messages via a background thread. On process crash (SIGKILL, power loss), the last N buffered messages are lost. Required for non-blocking performance; acceptable trade-off. |
| 26-30 | 🟢 Info | `colorize=True` on stderr — ANSI codes on Windows Server 2016 classic cmd.exe may render as escape characters. Works correctly in Windows Terminal or VS Code integrated terminal. Low priority. |
| — | 🟢 Info | No environment variable or CLI flag to override log level dynamically. Log level is compile-time: stderr=`INFO`, file=`DEBUG`. Changing requires code edit. |

### What's Good
- ✅ `logger.remove()` before `logger.add()` — clean slate, no duplicate handlers
- ✅ `log_dir.mkdir(parents=True, exist_ok=True)` — idempotent directory creation
- ✅ `encoding="utf-8"` on file sink — correct for non-ASCII file paths
- ✅ `rotation="1 day"` — predictable daily files, easy to correlate with backup runs
- ✅ `retention="30 days"` — automatic cleanup, no cron job needed
- ✅ `str | Path` type hint — callers can pass either
- ✅ File level is `DEBUG`, stderr level is `INFO` — production log files capture detail, terminal stays readable
- ✅ `LOG_FORMAT` includes source location (`name:function:line`) — crucial for debugging


---

## File: `core/manifest.py`

### Purpose
SQLite manifest database — file catalog (`file_entries`), run history (`run_history`), schema versioning (`db_meta`). WAL mode, thread-safe via `threading.Lock` + `threading.local()`. 315 lines.

### Schema

```sql
file_entries (
    relative_path TEXT NOT NULL UNIQUE,  -- Windows case-insensitive!
    file_size INTEGER, mtime REAL,
    md5_checksum TEXT DEFAULT 'pending',
    lan_status TEXT DEFAULT 'unknown',
    cloud_status TEXT DEFAULT 'unknown',
    lan_last_synced_at TEXT,
    cloud_last_synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

run_history (
    run_id TEXT, mode TEXT, started_at TEXT, ended_at TEXT,
    status TEXT, exit_code INTEGER,
    files_copied INTEGER, bytes_copied INTEGER, files_failed INTEGER,
    duration_seconds REAL, error_message TEXT
);
```

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 176-179 | 🔴 **HIGH** | **SQLite variable limit** (default 999) exceeded in `delete_entries()`. `','.join('?' * len(paths))` creates an IN clause with N placeholders. For N > 999, SQLite raises `too many SQL variables`. On large deletes (e.g., after comparing manifest vs destination), this silently crashes. |
| 19 | 🟡 **Medium** | `relative_path TEXT NOT NULL UNIQUE` uses **BINARY collation** (SQLite default). On Windows NTFS (case-insensitive), `"File.txt"` and `"file.txt"` are distinct DB entries but the same file. If robocopy and rclone report different casings, upserts create duplicates. Should be `relative_path TEXT NOT NULL UNIQUE COLLATE NOCASE`. |
| 203-215 | 🟡 **Medium** | `get_all_lan_entries()` and `get_all_cloud_entries()` use **`fetchall()` with no pagination**. For a manifest with 1M+ entries, this loads everything into memory as a `list[dict]`. Potential OOM on large deployments. |
| 70-74 | 🟡 **Medium** | No `__enter__`/`__exit__` — `ManifestDB` is not a context manager. Callers must manually call `.close()`. Easy to leak connections in error paths. |
| 84-87 | 🟡 **Medium** | `close()` only closes **the current thread's connection**. Other threads' connections (created via `threading.local()` in `_get_conn()`) are leaked with no cleanup mechanism. |
| 217-221 | 🟢 Info | **SQL injection via f-string**: `f"SELECT COUNT(*) ... WHERE {status_field} = 'synced'"` — `status_field` is interpolated directly. Currently called only with controlled values (`"lan_status"`, `"cloud_status"`), but a future caller could pass untrusted input. Should whitelist. |
| 80 | 🟢 Info | `executescript(DDL)` runs on **every** new connection — includes all `CREATE TABLE IF NOT EXISTS` and `INSERT OR IGNORE INTO db_meta`. Idempotent but wastes I/O after first init. A `PRAGMA user_version` check would skip it. |
| 297-300 | 🟢 Info | `PRAGMA wal_checkpoint(TRUNCATE)` is called manually. If connections are long-lived, the WAL file can grow large between checkpoints. The caller should call this after each backup cycle. |
| 302-315 | 🟢 Info | `purge_old_runs()` runs `PRAGMA optimize` but **never VACUUMs**. `DELETE FROM run_history` frees pages but doesn't shrink the DB file. Over years of daily runs, the `.db` file grows unboundedly. |
| 23 | 🟢 Info | `md5_checksum TEXT DEFAULT 'pending'` — the sentinel string is hardcoded in DDL but defined as a constant in `hashing.py::PENDING_CHECKSUM = "pending"`. If one changes without the other, they drift. The DDL should reference the constant (impossible in pure SQL) or the constant should document the coupling. |

### Connection & Locking Architecture

```
ManifestDB
├── _lock (threading.Lock)          → serializes ALL writes
├── _local (threading.local())      → one connection per thread
│   ├── Thread A → Connection A     → reads: no lock needed (WAL mode)
│   └── Thread B → Connection B     → reads: no lock needed
└── check_same_thread=False         → safety net disabled (by design)
```

- Reads: no lock. SQLite WAL supports concurrent readers.
- Writes: `_lock` acquired. One writer at a time.
- Each thread gets its own connection via `threading.local()`.
- `check_same_thread=False` is required for this pattern, but removes SQLite's built-in cross-thread safety net.

### What's Good
- ✅ WAL mode — concurrent reads without blocking writes
- ✅ `threading.Lock` + `threading.local()` — correct multi-thread pattern
- ✅ `COALESCE` in `ON CONFLICT ... DO UPDATE SET` — preserves existing fields on partial updates
- ✅ `PRAGMA foreign_keys=ON` — referential integrity
- ✅ Indexes on `lan_status`, `cloud_status`, `relative_path`, `started_at`, `mode` — query performance
- ✅ `db_meta` table for schema versioning — future migration path
- ✅ `wal_checkpoint(TRUNCATE)` exposed as method — caller controls checkpoint timing
- ✅ `purge_old_runs()` — prevents unbounded run_history growth
- ✅ `insert_run()` validates required keys with clear error message
- ✅ `ON CONFLICT(relative_path) DO UPDATE SET` — upsert pattern, no separate INSERT/SELECT logic
- ✅ `str | Path` on `__init__`, creates parent dir — zero-config setup
- ✅ Module docstring explicitly states "Single writer" assumption


---

## File: `core/report.py`

### Purpose
Email notifications: failure alerts + weekly/monthly summary reports. Reads from `ManifestDB.run_history`. 190 lines, 6 functions.

### Full listing for reference

```python
def _send_email(config, subject, body_html) -> bool:       # Line 17
    # Guard: missing config fields → skip
    msg = MIMEMultipart("alternative")
    msg["To"] = ", ".join(config.recipients)
    try:
        if config.smtp_port == 465:
            server = smtplib.SMTP_SSL(...)
        else:
            server = smtplib.SMTP(...); server.starttls()
        server.login(config.smtp_username, config.smtp_password)
        server.sendmail(...)
        server.quit()
        return True
    except Exception:
        return False

def send_failure_alert(config, firm_name, error_message, run_data):   # Line 62
    if not config.send_on_failure: return False
    subject = f"Backup FAILED — {firm_name} ({mode})"
    body = f"<h2 style=\"color: red;\">Backup Failure — {firm_name}</h2>..."
    return _send_email(config, subject, body)

def send_summary_report(db, config, firm_name, days, period):         # Line 99
    runs = db.get_runs_since(days)
    # Count successes, failures, partials
    # Show latest 10 runs in HTML table
    return _send_email(config, subject, body)

def _human_bytes(n: int) -> str:                                       # Line 185
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024: return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"
```

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 87-94 | 🟡 **Medium** | **No HTML escaping** — `{error_message}` is interpolated directly into an HTML email body. If a subprocess error contains `<script>`, `>`, `&`, or angle brackets from file paths (`"File <foo> not found"`), the email HTML is malformed. Should use `html.escape()` from stdlib. Same pattern on lines 136-140 (`status`, `mode` in summary report rows) — lower risk since those are controlled values from DB. |
| 23 | 🟢 Info | `all([config.smtp_host, config.sender, config.recipients])` — a recipients list containing an empty string `[""]` passes validation (the list is truthy), but `", ".join([""])` produces an empty `To` header. Edge case in config validation. |
| 39-43 | 🟢 Info | `timeout=30` hardcoded on both SMTP connections. Not configurable. For slow SMTP servers or high-latency networks, this could cause timeout failures. |
| 125-126 | 🟢 Info | Success/failure counting uses fixed status lists. `"LAN_PARTIAL"` and `"CLOUD_PARTIAL"` are not listed in either successes or failures, so they fall into `partials`. Correct for current classification, but adding a new status would silently shift results. |
| 185-190 | 🟢 Info | `_human_bytes()` modifies `n` in-place (`n /= 1024`). Works correctly, but could be a pure computation without mutation. Also doesn't handle negative `n` (returns `"-1.0 B"` — correct math, meaningless for byte counts). |

### SMTP Connection Logic

```
port == 465 → SMTP_SSL (implicit TLS)           ← legacy, still common
otherwise   → SMTP + server.starttls() (explicit TLS)  ← modern standard (587)
```

Both paths use same `timeout=30`. Login is attempted after connection establishment. Any exception → logged, `False` returned.

### What's Good
- ✅ `_send_email` returns `bool` — never raises, safe to call from any pipeline step
- ✅ `MIMEMultipart("alternative")` — correct MIME type for HTML email
- ✅ `send_on_failure` flag — opt-out for failure alerts
- ✅ Config guard at function entry — skips early if not configured (no noisy errors)
- ✅ `server.quit()` in both success and error paths — proper SMTP connection teardown
- ✅ `send_weekly_report` / `send_monthly_report` are thin wrappers — minimal duplication
- ✅ Summary report shows latest 10 runs with timestamps, mode, status, file count
- ✅ All email logic is self-contained — zero knowledge of backup internals (per module docstring)
- ✅ `_human_bytes()` correctly handles all byte ranges up to petabytes+ via fallthrough return


---

## File: `core/shutdown.py`

### Purpose
Send remote shutdown command to a Windows backup server via `shutdown.exe`. 50 lines, 1 function.

### Full listing for reference

```python
def shutdown_server(server_ip: str) -> dict:
    cmd = [
        "shutdown", "/s",
        "/m", f"\\\\{server_ip}",
        "/t", "300",    # 5-minute delay
        "/f",           # force close apps
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return {"shutdown_initiated": True, "server_ip": server_ip, "error": None}
        error_msg = result.stderr.strip() or f"exit code {result.returncode}"
        return {"shutdown_initiated": False, "server_ip": server_ip, "error": error_msg}
    except FileNotFoundError:
        return {"shutdown_initiated": False, ..., "error": "shutdown.exe not found"}
    except subprocess.TimeoutExpired:
        return {"shutdown_initiated": False, ..., "error": "timeout"}
    except OSError as e:
        return {"shutdown_initiated": False, ..., "error": str(e)}
```

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 28 | 🟢 Info | `/t 300` (5-minute delay) is hardcoded. If a different delay is needed (e.g., 1 minute for quick cycles, 10 minutes for large deployments), code must be edited. |
| 33 | 🟢 Info | `capture_output=True` with output unused — stdout from `shutdown.exe` is discarded. Harmless pattern consistent with codebase. |
| 24-29 | 🟢 Info | `server_ip` is not validated (format, reachability). Invalid IPs or hostnames produce `OSError` which is caught on line 48. Safe, but a validation check could provide earlier, clearer feedback. |

### What's Good
- ✅ Correct `shutdown.exe` flags: `/s` (shutdown), `/m \\\\IP` (remote target), `/t 300` (5-min delay), `/f` (force close)
- ✅ Graceful exception handling for missing binary, timeout, OS errors — all return structured error dicts
- ✅ `result.stderr.strip() or f"exit code {result.returncode}"` — falls back to exit code when stderr is empty
- ✅ Return dict consistent shape: `shutdown_initiated`, `server_ip`, `error`
- ✅ 30-second subprocess timeout is generous — `shutdown` command returns immediately after scheduling
- ✅ Docstring references AAM_BACKUP_V2/tasks/shutdown_server_task.py — traceable provenance
- ✅ 5-minute delay gives staff time to cancel via `shutdown /a` on the target, documented in docstring


---

## File: `core/wol.py`

### Purpose
Wake-on-LAN: send magic packet to server, poll SMB port 445 until online, then wait for stability. 90 lines, 3 functions + 1 exception class.

### Full listing for reference

```python
def _smb_port_open(server_ip: str, port: int = 445, timeout: float = 5.0) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    result = sock.connect_ex((server_ip, port))
    sock.close()
    return result == 0

def send_magic_packet(mac_address: str) -> None:
    wol_send(mac_address, ip_address="255.255.255.255", port=9)

def wait_for_server(server_ip, wake_timeout, ping_interval, stability_wait):
    start = time.time()
    while time.time() - start < wake_timeout:
        time.sleep(ping_interval)
        if _smb_port_open(server_ip):
            if stability_wait > 0: time.sleep(stability_wait)
            return
    raise WolTimeout(...)

def ensure_server_online(config: AppConfig) -> bool:
    if not config.wol.enabled: return True         # assumed already online
    if _smb_port_open(config.wol.server_ip): return True  # already up
    send_magic_packet(config.wol.mac_address)
    wait_for_server(config.wol.server_ip, ...)
    return True
```

### Issues Found

| Line | Severity | Issue |
|------|----------|-------|
| 21-26 | 🟡 **Medium** | **Potential socket leak** — no `with` context manager. If `connect_ex()` raises (edge case, but theoretically possible on Windows), `sock.close()` is never called. Should be `with socket.socket(...) as sock:`. |
| 22-24 | 🟢 Info | `socket.AF_INET` hardcoded — IPv4 only. Acceptable for LAN backup server on the same subnet. |
| 72-74 | 🟢 Info | `ensure_server_online` returns `True` when WoL is disabled, **assuming server is manually online**. If the server is actually off, the backup proceeds and fails later at the SMB connection stage. |
| 34 | 🟢 Info | Global broadcast `255.255.255.255` — may not be forwarded by routers across subnets. Standard WoL limitation for routed networks. The `wakeonlan` library sends to the subnet broadcast if `ip_address` matches the local interface's subnet; using the global broadcast is the safest default. |

### What's Good
- ✅ SMB port 445 check (`_smb_port_open`) — more reliable than ICMP ping which may be blocked by Windows Firewall
- ✅ `connect_ex` (returns errno) instead of `connect` (raises) — avoids exception for expected "port closed" responses
- ✅ `stability_wait` after SMB responds — prevents race between SMB service start and full server readiness
- ✅ All WoL timing parameters configurable via `WolConfig` (wake_timeout, ping_interval, stability_wait)
- ✅ `WolTimeout` exception with descriptive message — clear error for pipeline error handling
- ✅ `send_magic_packet` wraps third-party `wakeonlan` library behind a narrow API — easy to swap implementation
- ✅ `ensure_server_online` checks SMB before sending WoL — skips wake if server is already online
- ✅ `wol_send` aliased at import — makes the foreign function name (`send_magic_packet`) available when read at call site
- ✅ Docstring references AAM_BACKUP_V2/core/wol.py — traceable provenance


---

## File: `core/__init__.py`

### Purpose
Package init — re-exports selected symbols for `from core import ...` convenience. 7 lines.

```python
from core.fy_router import get_fy_prefix
from core.hashing import PENDING_CHECKSUM, compute_md5, verify_checksum
from core.logging import configure as configure_logging

__all__ = ["get_fy_prefix", "PENDING_CHECKSUM", "compute_md5", "verify_checksum", "configure_logging"]
```

### Analysis

- Re-exports 5 symbols from 3 modules (`fy_router`, `hashing`, `logging`)
- `configure_logging` aliases `logging.configure()` — self-documenting name for external consumers
- `PENDING_CHECKSUM` sentinel exported for external use — clean API boundary
- `__all__` defined — controls star imports (`from core import *`)

### Issues

| Line | Severity | Issue |
|------|----------|-------|
| — | 🟢 Info | Selective exports only cover 3 of 14 modules in `core/`. This is not a bug — most modules (`manifest.py`, `health.py`, `report.py`, `cloud_*`, `lan_*`, `wol.py`, `shutdown.py`, `rclone_config.py`) are used directly by `flow.py` and `serve.py`, not through the package API. The `__init__` serves as a convenience re-export, not a formal public API surface. |

### What's Good
- ✅ Minimal — no side effects, no startup logic, no circular imports
- ✅ `__all__` defined — explicit public API
- ✅ `configure` → `configure_logging` alias — disambiguates the generic function name at import site
- ✅ `PENDING_CHECKSUM` re-exported — consumers import from `core` rather than `core.hashing`


---

## Summary: `core/` Code Review Complete

**17 files reviewed. 3,626 lines analyzed (including blank and comment lines).**

| File | Lines | Severity Summary |
|------|-------|-----------------|
| `cloud_preflight.py` | 76 | 🟡 1, 🟢 2 |
| `cloud_reporter.py` | 83 | 🟡 3, 🟢 1 |
| `cloud_sync.py` | 135 | 🔴 1, 🟡 3, 🟢 2 |
| `cloud_verify.py` | 67 | 🟡 2, 🟢 2 |
| `fy_router.py` | 26 | 🟢 1 |
| `hashing.py` | 27 | 🟡 1, 🟢 2 |
| `health.py` | 124 | 🔴 1, 🟡 3, 🟢 5 |
| `lan_manifest.py` | 81 | 🔴 2, 🟡 4, 🟢 2 |
| `lan_preflight.py` | 67 | 🟢 4 |
| `lan_sync.py` | 126 | 🔴 1, 🟡 3, 🟢 4 |
| `logging.py` | 40 | 🟢 4 |
| `manifest.py` | 315 | 🔴 1, 🟡 5, 🟢 7 |
| `rclone_config.py` | 84 | (earlier review) |
| `report.py` | 190 | 🟡 1, 🟢 4 |
| `shutdown.py` | 50 | 🟢 3 |
| `wol.py` | 90 | 🟡 1, 🟢 3 |
| `__init__.py` | 7 | 🟢 1 |

**Total: 🔴 6 HIGH, 🟡 25 MEDIUM, 🟢 45 INFO**

---
---