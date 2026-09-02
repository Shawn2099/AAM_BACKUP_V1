# Deep Code Review — Slow, Line-by-Line, File-by-File

> Generated with CodeRabbit CLI v0.7.5 + manual line-by-line analysis
> Auth: Shawn2099 (Free plan — not assigned seat)
> Date: 2026-09-02
> Branch: development
> Index: AAM_BACKUP_V1 (4173 symbols, 9904 rels)
> Scope: 1 file at a time, 1 function at a time, line-by-line
> Method: `coderabbit review --agent` + GitNexus impact/query + manual deep read

## How This Document Is Built
- Each file gets: header (lines, purpose), CodeRabbit findings (grouped Critical/Warning/Info), Manual findings (function-by-function, line refs `file:line`), Positive notes, Fix recommendations.
- CodeRabbit is run per-diff where possible; Free plan seat not assigned → API may limit depth — manual review fills gaps.
- Findings are appended incrementally — do not overwrite previous sections.

## File Inventory (git ls-files, filtered)
Total tracked: 210 files. Production sources (priority order):
1. `core/backup_repository.py`
2. `core/cloud_preflight.py`
3. `core/cloud_reporter.py`
4. `core/cloud_sync.py`
5. `core/cloud_verify.py`
6. `core/fy_rollover.py`
7. `core/hashing.py`
8. `core/health.py`
9. `core/lan_manifest.py`
10. `core/lan_preflight.py`
11. `core/lan_sync.py`
12. `core/logging.py`
13. `core/manifest.py`
14. `core/process.py`
15. `core/rclone_config.py`
16. `core/report.py`
17. `core/shutdown.py`
18. `core/time_utils.py`
19. `core/wol.py`
20. `models/config.py`
21. `flow.py`
22. `serve.py`
23. `ui.py`
24. `launch.py`
25. `watchdog.py`
26. `collect_config_data.py`
27. `deploy/*.ps1,*.bat,*.py`
28. `static/js/*.js`, `templates/*.html`

---

## 01 — `core/backup_repository.py` — 95 lines

**Purpose:** DB write façade for backup results — normalizes rclone/walk dicts, bulk upserts, prunes stale, deletes removed, records run history with WAL checkpoint.

**CodeRabbit (`coderabbit review --agent --base main` and `-t uncommitted`):**
- No findings — file unchanged vs `main`, no uncommitted diff. CodeRabbit only reviews diffs, so no API findings for this file. Manual review below fills gap.

**GitNexus blast radius:** `record_sync_results` used by `flow.py:cloud_record_task` and `flow.py:lan_record_task` (2 prod callers, 22 test callers). Downstream `ManifestDB.bulk_upsert_synced`, `prune_stale_synced`, `delete_entries`. 6 affected processes (cloud/lan record_task flows). Change risk: MEDIUM — touches manifest correctness for both backup modes.

### Manual line-by-line

**Module header `core/backup_repository.py:1-10`**
- Good docstring, single responsibility. Imports minimal (`loguru`, `ManifestDB`). No issue.

**`record_sync_results` `core/backup_repository.py:12-51`**
- `core/backup_repository.py:12-18` Signature: `db: ManifestDB, mode: str, entries: list[dict], removed: list[str]|None` — `mode` unvalidated (any string accepted). Caller could pass typo `"clodu"` and DB would create that bucket. Recommend `Literal["cloud","lan"]` or assert.
- `core/backup_repository.py:20-21` Comment says rclone vs walk formats — accurate but incomplete: also handles mixed formats per test `test_records_mixed_format_entries`.
- `core/backup_repository.py:29-37` Normalization:
  ```python
  "path": e.get("Path") if e.get("Path") is not None else e.get("path", "")
  ```
  - Correctly prefers capitalized key but falls back. Edge: if `e={"Path": ""}` (empty string, not None) it keeps `""` rather than falling back — intentional but undocumented. If rclone returns `Path: ""` for corrupt entry, you store `""` and later `active_paths` contains `""`. Prune logic would then treat `""` as active. Low severity, but should coalesce `or` not just `is not None` if empty should fallback.
  - `size`/`mtime` fallback to 0: for mtime 0 is falsy epoch — caller may want to preserve 0 vs missing distinction; bulk_upsert likely treats 0 as valid. OK.
  - No `TypeError` guard: if `entries` contains non-dict (e.g., `None`), `e.get` will throw `AttributeError` and bubble. No try/except — will crash `cloud_record_task` if rclone lsjson returns malformed element. Consider filtering or wrapping.
- `core/backup_repository.py:30-37` List comp builds `normalized` without validating `path` non-empty. An entry with `path=""` would still upsert — DB may insert empty path row. Should filter `if e.get("Path") or e.get("path")`.
- `core/backup_repository.py:38` `db.bulk_upsert_synced(normalized, mode)` — no error handling; if bulk fails, exception propagates to caller. That's correct for transactional semantics, but docstring doesn't mention it can raise. Add `Raises` note.
- `core/backup_repository.py:42-48` Self-healing prune:
  - `active_paths = {item["path"].replace("\\", "/") ...}` — correct Windows fix (comment explains UNC backslashes). Good.
  - But `item["path"]` may be `""` → `""` in set → `prune_stale_synced(mode, {""})` would never prune `""` row and would prune everything else incorrectly if `""` was only active path. Filtering empties would fix.
  - `pruned = db.prune_stale_synced(mode, active_paths)` — if `entries` is empty list, this block doesn't run (guarded by `if entries:`), so prune skipped for empty sync. That's intentional for empty backup? But stale entries would then accumulate. The guard means empty successful run never heals. Should prune even on empty? Currently `if entries:` prevents prune when cloud has 0 files — stale rows remain forever. Consider separate `if mode and ...` prune regardless.
  - Logger `if pruned:` only logs when >0 — good, avoids noise.
- `core/backup_repository.py:50-51` `if removed: db.delete_entries(removed)` — if `removed=[]` no-op (correct). No path normalization for `removed` — if caller passes backslashes, `delete_entries` may miss. Symmetry with `active_paths` normalization missing. Should normalize removed too: `[p.replace("\\","/") for p in removed]`.
- Overall function never raises for `removed` case either; delete errors would propagate.

**`record_run_history` `core/backup_repository.py:54-95`**
- `core/backup_repository.py:54-69` Signature uses keyword-only `*` — good, prevents positional misuse. Defaults for metrics are sensible. Return type `bool` never raises — documented.
- `core/backup_repository.py:75-90` `db.insert_run({...})` dict construction: includes `extended_metrics: str|None` — no validation that it's valid JSON string; caller could pass arbitrary string and DB stores it. OK.
- `core/backup_repository.py:91` `db.wal_checkpoint()` after insert — correct, ensures WAL flushed. But if `insert_run` succeeds and `wal_checkpoint()` fails, function still returns False? Actually it goes to except, logs error, returns False — but the run row is already inserted. That's partial success reported as failure. Caller may think record failed and retry, creating duplicate run_id? `run_id` uniqueness not enforced in Python — DB may throw on duplicate. Worth noting that checkpoint failure should not mask successful insert; could log warning not error return False.
- `core/backup_repository.py:93-95` Bare `except Exception` — correct for finally-block safety, but swallows DB errors without re-raising. Logged with `logger.error(f"Failed...{e}")` — includes exception str but no traceback. Should use `logger.exception` for stack.
- No input validation: `run_id` empty string would insert bad row; `status` not validated against allowed set (`success`, `failed`, etc.). Could add assert or enum.

**Info / Style**
- No type for `db` import at runtime — `ManifestDB` imported unconditionally; if `core/manifest.py` has heavy import cost, lazy could help but fine.
- File is 95 lines — no dead code.

**Positives**
- Clear separation from `flow.py`, centralizes DB writes, well-tested (22 tests), handles Windows path normalization correctly, safe `record_run_history` for finally blocks.

**Recommendations (grouped)**
- **Warning:** Validate `mode` as `Literal["cloud","lan"]` — `core/backup_repository.py:14`.
- **Warning:** Normalize `removed` paths to forward slashes like `active_paths` — `core/backup_repository.py:50`.
- **Warning:** Filter empty `path` entries before upsert/prune — `core/backup_repository.py:30-45` (avoid `""` poison).
- **Warning:** Consider pruning even when `entries` empty, or document why not — `core/backup_repository.py:29`.
- **Info:** Use `logger.exception` not `logger.error` for run history failure — `core/backup_repository.py:94`.
- **Info:** Guard `e` type before `.get`, or wrap normalization in try — `core/backup_repository.py:31-34`.
- **Info:** Document that `bulk_upsert_synced`/`prune`/`delete` can raise — add Raises section.
- **Info:** Separate WAL checkpoint failure from insert failure (warn vs error).

---

## 02 — `core/cloud_preflight.py` — 146 lines

**Purpose:** Two-probe preflight (A: Python `iterdir` on source, B: `rclone lsjson --max-depth 0` on GCS) — replaces full HDD `rclone check --one-way` scan.

**CodeRabbit:** No diff vs `main` → 0 findings from API (file unchanged). Manual deep read below.

**GitNexus:** Single caller `flow.py` (cloud flow). Callee `core.process.resolve_binary`, `core.rclone_config.temp_rclone_config`. No DB side. Risk LOW for change but HIGH for pipeline correctness if broken.

### Manual line-by-line

**Header `core/cloud_preflight.py:1-22`** — Excellent doc, explains Probe A/B, why `--gcs-no-check-bucket` absent (forces IAM). Good.

**`run_cloud_dry_run` signature `core/cloud_preflight.py:32-59`**
- `source: str` but `Path(source)` — accepts `E:\\` string; Windows root ok. `timeout=30` sane for network probe.
- Return `{"ok":bool,"exit_code":int,"error":str|None}` — consistent but `exit_code -1` overloaded for multiple failure types (source missing vs timeout vs rclone missing). Caller can't distinguish without parsing `error`. Consider distinct codes or enum. Info.
- Args `project_number`, `storage_class` passed through to `temp_rclone_config` but never used in Probe B command itself except via config — correct.

**Probe A `core/cloud_preflight.py:61-89`**
- `core/cloud_preflight.py:62-67` `if not source_path.exists():` → return `ok False, exit -1`. Good fail-closed. But `Path("E:\\")` exists even if empty — correct to then test iterdir. However `Path.exists()` on Windows will return True for drive letter even if media not ready? Edge: `E:\\` with no media returns True? `os.stat` inside `exists` may still return False? Needs testing but ok.
- `core/cloud_preflight.py:69-73` `next(source_path.iterdir())` — single kernel call, no traversal, good. Comment explains intent.
- `core/cloud_preflight.py:73-83` `except StopIteration:` empty source → `logger.warning` not error, returns nothing → falls through to Probe B! This is intentional per comment M8: preflight is diagnostic-only, health gate fails closed later. Good design but log message says health gate will refuse — traceability good. However function still returns `ok True` for empty source if GCS probe succeeds. Caller in `flow.py` must check health gate after preflight; if they forget, empty sync could be attempted. Verify caller honors health gate — done via `flow.health_check_task` later, but coupling implicit.
- `core/cloud_preflight.py:84-87` `except OSError` → return FAILED — correct. But `except StopIteration` blindly assumes empty = ok; what about permission denied vs empty? Permission would raise `OSError`, not `StopIteration`, so covered. Good.
- `core/cloud_preflight.py:89` `logger.info [A] OK` — logged even when empty (warning above + info). Slightly misleading double log.

**Probe B `core/cloud_preflight.py:91-146`**
- `core/cloud_preflight.py:92` `temp_rclone_config(...) as config_path` — ensures cleanup. Good.
- `core/cloud_preflight.py:93` `dest = f"aam_gcs:{bucket}/{fy_prefix}"` — assumes bucket has no trailing slash, prefix no leading slash. Config validation elsewhere ensures it, but no sanitization here.
- `core/cloud_preflight.py:94` `resolve_binary("rclone") or "rclone"` — consistent with other modules (M7). Good.
- `core/cloud_preflight.py:103-110` Command: `lsjson --max-depth 0 --retries 2 --retries-sleep 5s --config` — fast single-page probe. Comments excellent.
- `core/cloud_preflight.py:114-122` `subprocess.run(capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)` — good, `errors="replace"` prevents decode crash on garbage stderr.
- `core/cloud_preflight.py:123-134` Exception handling: `TimeoutExpired` → `ok False, exit -1, error Timeout after Xs` — loses `e` details (partial stderr). Fine. `FileNotFoundError` → rclone missing. `OSError` → OS error launching. Complete.
- `core/cloud_preflight.py:136-143` `if code !=0:` logs FULL stderr via `result.stderr.strip()` — correct, comment says don't truncate. But earlier `error` construction duplicates `stderr_output` truncation? Actually uses full `stderr_output` (full). Good. However `result.stderr` could be huge (rclone dumps long JSON log). No truncation limit — could flood logger. Consider truncating to last 2k chars. Info.
- `core/cloud_preflight.py:140` `msg = f"Exit {code}: {stderr_output}"` — includes exit code, good for debugging.
- Return on success `{"ok": True, exit 0, error None}` — caller expects this shape.

**Positives:** Fast, zero-HDD, correct IAM-forcing, excellent comments, proper exception granularity, Windows-aware.

**Recommendations**
- **Info:** Disambiguate `exit_code -1` or document it overloads multiple failures — `core/cloud_preflight.py:67,126,130`.
- **Info:** Truncate huge stderr in log to avoid log flood — `core/cloud_preflight.py:140`.
- **Info:** Sanitize `bucket`/`fy_prefix` slash handling — `core/cloud_preflight.py:93`.
- **Info:** Add mypy type for return `TypedDict` instead of plain dict.

---

## 03 — `core/cloud_reporter.py` — 243 lines

**Purpose:** GCS state reporting via rclone native commands — `size`, `lsjson -R`, `check --combined`. Rclone is source of truth, zero custom logic.

**CodeRabbit:** No diff vs main → 0 API findings. Manual below.

### Manual line-by-line

**Module `core/cloud_reporter.py:1-31`**
- Docstring good, explains rclone-is-truth. `_base_args` adds `--gcs-no-check-bucket` (skip bucket check — already verified by preflight) + `--fast-list` + `--config`. Correct opposite of preflight's deliberate inclusion. Good symmetry, documented.

**`_base_args` `core/cloud_reporter.py:24-31`** — Simple, correct.

**`get_cloud_size` `core/cloud_reporter.py:34-82`**
- `core/cloud_reporter.py:39-41` `dest`, `rclone_exe`, `cmd = [size --json *_base_args]` — correct.
- `core/cloud_reporter.py:52-58` Timeout → returns `{"count":0,"bytes":0,"sizeless":"0","_error":...}` — Warning-level log, not raising. Caller must check `_error` to distinguish empty vs failed. Pattern consistent but `_error` key is convention not typed — easy to miss. Consider raising or returning `None`? Current works because dashboard shows 0 with error flag.
- `core/cloud_reporter.py:59-77` Non-zero exit: sets `data=None` then returns `_error` dict — good, doesn't parse stdout. But `result.stderr` not truncated — could be large.
- `core/cloud_reporter.py:64-68` JSON parse failure → return `_error` dict — logged as warning, correct.
- `core/cloud_reporter.py:79-82` `.get("count",0)` avoids KeyError on malformed `{}` — good, comment explains.
- Returns `data` directly on success which may contain extra keys — caller expects `count`/`bytes`/`sizeless`, ok.

**`get_cloud_manifest` `core/cloud_reporter.py:85-133`**
- `core/cloud_reporter.py:91-94` Doc: M6 raises `CloudReporterError` instead of returning `[]` — critical fix to distinguish empty vs failed. Good.
- `core/cloud_reporter.py:98` `cmd lsjson -R` — recursive listing, may be heavy for 100k files but okay with 300s timeout.
- `core/cloud_reporter.py:109-116` Exceptions all raise `CloudReporterError` with `from e` — correct chained exception, preserves traceback.
- `core/cloud_reporter.py:118-122` Non-zero exit → raise `CloudReporterError` with stderr — correct, no silent `[]`.
- `core/cloud_reporter.py:124-129` JSON decode error → raise.
- `core/cloud_reporter.py:131` `files = [f for f in data if not f.get("IsDir")]` — filters dirs, but `data` assumed list; if GCS returns `{...}` object, this would iterate keys and break. Should validate `isinstance(data, list)`. Minor.
- No timeout handling for hung rclone? 300s timeout covers it, but could be too short for huge bucket — caller can override.

**`get_cloud_diff` `core/cloud_reporter.py:136-243`**
- `core/cloud_reporter.py:157-160` `mkstemp` + `os.close(fd)` so rclone can write — correct Windows pattern (file can't be open twice). Good.
- `core/cloud_reporter.py:163-174` `rclone check --combined --size-only --modify-window 2s --checkers 4 --retries 3` — size-only avoids 2h hash, 2s window handles NTFS. Notes about omitted flags excellent.
- `core/cloud_reporter.py:177-184` `subprocess.run` capture both outputs — but `--combined diff_file` writes to file, not stderr, so capture is fine.
- `core/cloud_reporter.py:189-195` `returncode >=2` → `partial=True`, `_error` set, warning logged. `exit 1` (mismatch) considered success — correct, diff still valid.
- `core/cloud_reporter.py:199-214` Parsing loop: `line[0]` access without checking `line` empty already filtered, but assumes `line` length >=2 (`line[2:]`). If rclone writes malformed line like `"+ "` or `"+"`, `line[2:]` gives `""` — would add empty string to diff list. Should guard `len(line)>=2`. Low.
- `core/cloud_reporter.py:215-217` `FileNotFoundError` if diff file missing → warning but returns `diff` with whatever parsed (empty). Good.
- `core/cloud_reporter.py:219-222` `if partial: diff["_partial"]=True` — flag for caller.
- `core/cloud_reporter.py:231-237` Timeout/OSError → returns partial dict with `_error` — caller can show warning.
- `core/cloud_reporter.py:238-243` `finally: Path(diff_file).unlink()` — cleanup good, ignores OSError.

**Positives:** M6 error distinction, size-only diff for HDD, well-commented flags, consistent binary resolution.

**Recommendations**
- **Warning:** `get_cloud_manifest` assumes `data` is list — add `if not isinstance(data, list): raise CloudReporterError` — `core/cloud_reporter.py:125-131`.
- **Warning:** `get_cloud_diff` `line[2:]` on short lines → empty path poison — guard `len(line) >=2 and line[1]==" "` — `core/cloud_reporter.py:207-214`.
- **Info:** `_error` / `_partial` keys are stringly-typed — define TypedDict for callers — `core/cloud_reporter.py:34,85,136`.
- **Info:** Truncate stderr in warnings to avoid log flood — `core/cloud_reporter.py:60,119,193`.

---

## 04 — `core/cloud_sync.py` — 281 lines

**Purpose:** rclone sync wrapper with temp config, exit classification, P1-EXIT9 reclassification, max-duration soft cutoff, HDD-optimized flags.

**CodeRabbit:** No diff vs main → 0 findings. Manual below.

### Manual line-by-line

**Header `core/cloud_sync.py:1-22`**
- Reference to V2 rclone.py correct, `_LOG_LEVELS` + `_FATAL_PLAINTEXT_MARKERS` for exit9 hardening (R1) — good.

**`scan_rclone_log_for_errors` `core/cloud_sync.py:25-50`**
- `core/cloud_sync.py:36` `for raw_line in (log_text or "").splitlines()` — handles None/empty safely.
- `core/cloud_sync.py:42-45` JSON level check `>= ERROR` (40) captures ERROR+CRITICAL, case-insensitive via `.upper()`. Good.
- `core/cloud_sync.py:46-49` Fallback for plaintext markers `failed to` — catches log.Fatalf paths outside JSON logger (issue #6038). Lowercased check correct. Returns tail last 10 lines — operator sees why.
- Edge: if log_text is huge (60s stats every minute for 6h = 360 lines + INFO), scan still linear O(n) — fine.

**`classify_rclone_exit` `core/cloud_sync.py:53-81`**
- Mapping 0-10 per docs, `get(code, "CLOUD_FAILED")` default fail-closed — good.
- 9 → `CLOUD_NO_CHANGES_COMPLETE` requires `--error-on-no-transfer` — correctly set in builder. Good.

**`resolve_max_duration_seconds` `core/cloud_sync.py:84-98`**
- `core/cloud_sync.py:94-95` `configured is not None: return int(configured) if >0 else None` — 0 disables cap (documented C-A). Good.
- `core/cloud_sync.py:96-98` Auto `timeout-300s` margin so rclone SOFT terminates before hard kill. If timeout 200 → auto -100 → returns None (disabled) not negative — correct.

**`build_rclone_sync_command` `core/cloud_sync.py:101-166`**
- `core/cloud_sync.py:118-121` Health check at build time: `check_source_drive(source)` and `if not ok and "appears empty" in reason: raise ValueError(reason)` — enforces fail-closed at command-build, not at run. Good but couples sync to health module; could surprise unit tests. Documented?
- `core/cloud_sync.py:127` `resolve_binary("rclone") or "rclone"` — consistent M7.
- `core/cloud_sync.py:129-155` Flags: `--fast-list --gcs-no-check-bucket --gcs-storage-class --error-on-no-transfer --modify-window 2s --bwlimit --transfers 2 --checkers 4 --retries 3 --retries-sleep 30s --check-first --buffer-size 64M --use-json-log --log-level INFO --stats 60s` — all HDD/GCS optimized, comments explain each (head efficiency, 128M total, mmap removed). Excellent.
- `core/cloud_sync.py:160-164` `max_duration_seconds` adds `--max-duration Xs --cutoff-mode SOFT` — lets in-flight finish, preserves .partial. Good.
- Returns `list[str]` — no shell injection (list not string).

**`run_cloud_sync` `core/cloud_sync.py:169-281`**
- `core/cloud_sync.py:195-198` `effective_max_duration = resolve_max_duration_seconds(...)` before temp config — correct order.
- `core/cloud_sync.py:200-208` `temp_rclone_config(...) as config_path:` + `build_rclone_sync_command(..., effective_max_duration)` — command built inside context so config exists.
- `core/cloud_sync.py:212-221` `mkstemp` for stderr_path + `open(stderr_path,"w") as stderr_file:` + `subprocess.run(stdout=DEVNULL, stderr=stderr_file)` — streams rclone JSON log directly to file, avoids pipe buffer deadlock for 6h run. Correct Windows pattern. `text=True` + `stderr_file` text mode ok.
- `core/cloud_sync.py:224` `status = classify_rclone_exit(result.returncode)` — immediate classification.
- `core/cloud_sync.py:227-246` P1-EXIT9 block: `if code==9:` reads stderr file, `scan_rclone_log_for_errors`, reclassifies to `CLOUD_FAILED` if has_error, else stays `CLOUD_NO_CHANGES_COMPLETE`. Logs error tail or info. Correct fix for CLOUD-06/07 blind trust. Good.
- `core/cloud_sync.py:247-252` `elif code!=0:` reads stderr file and logs. Uses `Path.read_text(encoding="utf-8")` without `errors="replace"` — could throw decode error on binary log? Should add `errors="replace"` like elsewhere. Minor.
- `core/cloud_sync.py:262-269` `TimeoutExpired` → return `CLOUD_FAILED, -1` with resumable message (G7) — operator-facing accurate.
- `core/cloud_sync.py:270-275` `FileNotFoundError`/`OSError` handling correct.
- `core/cloud_sync.py:276-281` `finally: Path(stderr_path).unlink()` cleanup — good.
- Risk: `stderr_path` is on local disk, not cleaned if process killed hard (SIGKILL) — temp file leak but via `tempfile.mkstemp` in OS temp, OS cleans later. Fine.

**Positives:** P1-EXIT9 reclassification rigorous, SOFT cutoff prevents hard-kill corruption, HDD optimizations documented, subprocess correctly avoids pipe deadlock.

**Recommendations**
- **Info:** Add `errors="replace"` to `Path.read_text` at `core/cloud_sync.py:249` for symmetry.
- **Info:** Document `build_rclone_sync_command` raises `ValueError` on empty source — `core/cloud_sync.py:118`.
- **Info:** Consider `TypedDict` for return `{"status":..., "exit_code":...}`.

---

## 05 — `core/cloud_verify.py` — 113 lines

**Purpose:** Post-sync `rclone check --one-way --size-only` integrity check. Distinguishes verified (0) vs mismatch (1) vs error (2+).

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**Header `core/cloud_verify.py:1-20`** — Clear exit codes doc.

**`verify_cloud_integrity` `core/cloud_verify.py:23-99`**
- `core/cloud_verify.py:42-57` Command: `rclone check --one-way --fast-list --size-only --modify-window 2s --checkers 4 --config --gcs-no-check-bucket` — size-only avoids 2h hash, mirrors sync's window/checkers. Correct, notes omitted flags.
- `core/cloud_verify.py:49` order `--config` after `--checkers` — rclone flag order irrelevant, fine.
- `core/cloud_verify.py:62-69` `subprocess.run(capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=14400)` — 4h default for large HDD datasets, capture both streams.
- `core/cloud_verify.py:71` `verified = returncode ==0` — simple.
- `core/cloud_verify.py:77-83` Mismatch vs error label, logs FULL stderr (no truncate) — could flood but needed for mismatch details. Good.
- `core/cloud_verify.py:85-89` Returns `{"verified":bool,"exit_code":int,"error":_build_error_message(...)}` — note: `error` is derived from exit code, NOT from stderr. So mismatch returns static string "Integrity mismatch — source and GCS file counts or sizes differ" losing actual rclone stderr details (which file mismatched). Warning: operator loses per-file mismatch list.
- `core/cloud_verify.py:91-99` Exceptions: Timeout/FileNotFound/OSError → `verified False, -1` — correct.

**`_build_error_message` `core/cloud_verify.py:102-113`** — Pure mapping, returns None for 0. Good but loses stderr context as above.

**Positives:** Size-only avoids HDD thrash, correct one-way direction, 4h timeout sane.

**Recommendations**
- **Warning:** Return stderr or first 2k chars in `error` for mismatch/error cases — currently static string hides which files diverged — `core/cloud_verify.py:88`.
- **Info:** Truncate stderr log to avoid flood — `core/cloud_verify.py:82`.
- **Info:** Add `--retries` like other probes? Currently no retries for verify; transient GCS flake will fail verify and block report. Consider adding.

---

## 06 — `core/fy_rollover.py` — 462 lines

**Purpose:** FY boundary detection, final backup of closing FY, new FY folder creation, atomic config.yaml update, GCS Archive transition via gcloud.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**Header `core/fy_rollover.py:1-26`** — `_PROJECT_ROOT` two levels up correct.

**`_resolve_gcloud` `core/fy_rollover.py:29-60`**
- 4-level fallback: deploy/bin/bundled → all-users → per-user (LOCALAPPDATA/USERPROFILE) → `shutil.which`. Good multi-path, handles installer variants. Uses `Path.exists() and is_file()` — correct.
- `core/fy_rollover.py:51-52` Uses `os.environ.get("LOCALAPPDATA","") / Path` — if env missing, becomes `Path("Google/...")` relative; `exists()` will be False, harmless but should guard empty string to avoid `./Google` check. Info.

**`_fy_name` `core/fy_rollover.py:67-73`** — `FY_PATTERN = ^FY\d{2}-\d{2}$` IGNORECASE, `upper()` normalize — good. Handles `E:\\SOURCE\\FY26-27` and `/mnt/FY26-27`.

**`_parent_path`/`_child_path` `core/fy_rollover.py:76-93`** — Preserve separator style (UNC `\\` → PureWindowsPath). Correct for config round-trip where operator used backslashes.

**`detect_rollover` `core/fy_rollover.py:96-113`**
- `current_fy = _fy_name(source) or _fy_name(lan)` — if source has FY but LAN doesn't, uses source. If both have FY but differ, picks source — potential inconsistency silent. Should warn if both present and differ. Info.
- If no FY in either → returns False with warning that rollover disabled — correct opt-out G2, 4-digit trap refused elsewhere.

**`run_final_backup` `core/fy_rollover.py:116-190`**
- `core/fy_rollover.py:125-153` Cloud final backup: `run_cloud_sync(..., fy_prefix=old_fy)` — uses old FY prefix correct. Checks `status in ("CLOUD_COMPLETE","CLOUD_NO_CHANGES_COMPLETE")` → `cloud_ok True`. Narrow except `OSError, SubprocessError, RuntimeError` — lets config typos propagate, good.
- `core/fy_rollover.py:155-189` LAN final backup: `ensure_server_online` if wol enabled, `run_lan_sync` with same source/dest, `classify_lan_exit(exit_code)` then `if lan_status=="LAN_COMPLETE" or (lan_status=="LAN_PARTIAL" and not (exit_code &8))` — bit 3 (8) is NTFS permission? Actually robocopy 8=failed copies; LAN_PARTIAL with 8 should be fail. Correct hardening.
- Shutdown after LAN backup if `wol.enabled and shutdown_after_backup` — catches `OSError, RuntimeError` as non-critical warning. Good.
- Returns tuple `(cloud_ok, lan_ok)` — caller uses to block rollover.

**`create_new_fy_folders` `core/fy_rollover.py:193-226`**
- `core/fy_rollover.py:206` Source mkdir mandatory, raises on failure — correct.
- `core/fy_rollover.py:211-222` LAN mkdir best-effort, touches `.AAM_TARGET_MOUNTED` sentinel, logs ACTION REQUIRED if OSError — non-blocking, correct for NAS offline at FY boundary. Good documentation.

**`update_config_yaml` `core/fy_rollover.py:230-269`**
- `core/fy_rollover.py:236-243` ruamel.yaml round-trip `preserve_quotes=True` preserves comments/formatting — correct.
- `core/fy_rollover.py:255-261` Atomic write: `mkstemp(dir=parent)` + `yaml.dump` + `os.replace(tmp, path)` — atomic on POSIX and Windows (replace is atomic if same filesystem). Good.
- `core/fy_rollover.py:267-269` `except: os.unlink(tmp_path); raise` — cleans tmp on failure, but `os.unlink` could itself raise if file missing → masks original exception. Should use `try: unlink except OSError: pass`. Minor.
- No lock vs concurrent watchdog reading config — but watchdog reads after rollover at next launch, race low.

**`run_archive_transition` `core/fy_rollover.py:272-389`**
- `core/fy_rollover.py:306` Injects `GOOGLE_APPLICATION_CREDENTIALS` into env copy — stateless, correct no persistent auth.
- `core/fy_rollover.py:310-337` Auth step: if key file exists → `gcloud auth activate-service-account --key-file=...` with `auth_timeout`. If non-zero → warning return False. Else assume ambient auth (Windows Credential Manager). Good.
- `core/fy_rollover.py:343-357` `gcloud storage objects update gs://bucket/old_fy/ --storage-class=ARCHIVE --recursive` — metadata-only, no data transfer. Correct command.
- `core/fy_rollover.py:372-389` Exceptions: `FileNotFoundError` → warning with search paths, `TimeoutExpired`, generic `Exception` → all return False non-blocking — correct per doc.

**`rollover` `core/fy_rollover.py:392-461`**
- `core/fy_rollover.py:404-412` Early exit if no rollover needed, then `_fy_name` extraction — correct.
- `core/fy_rollover.py:422-430` `run_final_backup` before mutating anything — correct order.
- `core/fy_rollover.py:432-441` Blocks if required (enabled but not ok) → raises `RolloverError` (launch.py catches). Good.
- `core/fy_rollover.py:445-453` Archive transition non-blocking even if fails — correct.
- `core/fy_rollover.py:455-457` Create folders + update config — order correct: folders first so new FY exists before config points there.

**Positives:** Atomic config update, multi-path gcloud resolution, correct FY separator preservation, non-blocking archive, proper exception narrowing.

**Recommendations**
- **Warning:** `update_config_yaml` tmp unlink can mask original error — wrap in try/except — `core/fy_rollover.py:267`.
- **Info:** Warn if source and LAN FY suffixes differ — `core/fy_rollover.py:98-99`.
- **Info:** Guard empty LOCALAPPDATA/USERPROFILE before Path join — `core/fy_rollover.py:51`.
- **Info:** Consider file lock for config.yaml concurrent read — `core/fy_rollover.py:238`.

---

## 07 — `core/hashing.py` — 33 lines

**Purpose:** MD5 streaming checksum compatible with `rclone hashsum md5`, with `PENDING_CHECKSUM` sentinel.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**Header `core/hashing.py:1-22`**
- `hashlib.md5(usedforsecurity=False)` required for FIPS Windows Server 2016 — correct, comment explains.
- `65536` (64KB) streaming — good balance for HDD sequential reads, avoids loading 500GB file into RAM.
- `open(file_path,"rb")` no `Path.resolve()` — handles relative too. No `FileNotFoundError` handling — `compute_md5` lets it propagate, which is correct (caller decides). Documented? Doc says returns hex digest, not Raises section — add.

**`verify_checksum` `core/hashing.py:25-33`**
- `if expected == PENDING_CHECKSUM: return False` — sentinel prevents false positive for uncatalogued files, good. But `expected` case-sensitive exact match; no stripping. OK.
- `compute_md5(file_path)==expected` — re-hashes file; if file large (1GB), expensive. Acceptable for verification.

**Positives:** Minimal, correct, FIPS-aware.

**Recommendations**
- **Info:** Add `Raises: FileNotFoundError, OSError` docs — `core/hashing.py:9`.
- **Info:** Consider `expected.lower()` normalization if stored hex may be uppercase (rclone outputs lowercase, so fine).

---

## 08 — `core/health.py` — 175 lines

**Purpose:** Pre-backup health gates — source drive, binaries, disk space, clock skew, GCS key.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**`check_source_drive` `core/health.py:18-65`**
- `core/health.py:29-30` `exists()` fail → False. Correct.
- `core/health.py:33` `any(source.iterdir())` — single syscall proves files exist, no traversal. Good. Catches `PermissionError`/`OSError` separately — good granularity.
- `core/health.py:39-49` Empty → fail-closed with M8 message explaining robocopy /MIR + rclone sync deletion risk and canary workaround. Excellent safety, mirrors `cloud_preflight` logic but here is gate (fail) vs there is warning (diagnostic). Correct duality.
- `core/health.py:51-63` `shutil.disk_usage` free <1GB → fail. `OSError` on check → warning skip (not fail) — reasonable: disk space check shouldn't block backup if API fails.
- Returns `True,""` on success.

**`check_binary_exists` `core/health.py:68-70`** — delegates to `resolve_binary` (deploy/bin fallback) — correct, unlike naive `which`.

**`check_gcs_key` `core/health.py:73-80`** — exists + non-zero size — catches empty file via `stat().st_size`. Could also validate JSON parse, but out of scope.

**`check_clock_skew` `core/health.py:83-123`**
- `core/health.py:97-99` `HTTPSConnection("www.googleapis.com", timeout=10)` HEAD / → Date header — correct Google endpoint.
- `core/health.py:106-108` `parsedate_to_datetime` vs `pendulum.now("UTC")` — handles RFC2822. Good.
- `core/health.py:110-114` `difference >600s` → fail with `w32tm /resync` hint — GCS JWT rejected >10min, good.
- `core/health.py:118-120` `OSError` → warning return True (skip) — if Google unreachable, don't block backup. Correct “fail open” for network check.
- `core/health.py:121-123` `ValueError` parse fail → warning skip — also correct.

**`pre_backup_health` `core/health.py:126-175`**
- `core/health.py:148-150` Validates mode `cloud|lan|all` → `HealthError` if invalid — good.
- `core/health.py:152-154` source_drive check always — even for cloud-only, source is needed. Correct.
- `core/health.py:156-169` Cloud path: check rclone, gcs_key, clock_skew → raises HealthError on each. Clock failure raises (not warning) here — stronger than standalone function’s skip, intentional: pre-backup should fail if skew known bad. Good distinction.
- `core/health.py:171-173` LAN path: check robocopy — only for lan/all, correct.

**Positives:** M8 fail-closed, correct clock/gcs checks, nuanced skip vs fail.

**Recommendations**
- **Info:** Validate `gcs_key_path` JSON shape when cloud enabled — `core/health.py:159`.
- **Info:** Consider `TimeoutError` separate from `OSError` in clock check (already covered by OSError parent? Actually `TimeoutError` is subclass of OSError since Py3.3, so caught — fine).

---

## 09 — `core/lan_manifest.py` — 112 lines

**Purpose:** LAN destination inventory via `os.walk` + `os.stat`, diff snapshots O(1)/O(n).

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**`walk_lan_destination` `core/lan_manifest.py:13-74`**
- `core/lan_manifest.py:36-37` `base = str(Path(unc_path).resolve())` — resolves UNC to absolute; on Windows `resolve()` may hit network → potential 10s hang if NAS offline. But error handling below compensates.
- `core/lan_manifest.py:39-42` `onerror=_on_walk_error` collects OSError — good H2 pattern.
- `core/lan_manifest.py:44-54` For each file, `os.stat` → skip on OSError (locked mid-walk) — good partial inventory.
- `core/lan_manifest.py:50` `rel = os.path.relpath(full, base)` — on Windows UNC, `relpath` with backslashes → later `backup_repository` normalizes to `/` for compare, but `lan_manifest` keeps backslashes; `snapshot_to_dict` uses those keys directly. Downstream `flow.lan_record_task` must normalize same way — check consistency. Currently `record_sync_results` normalizes active_paths to `/`, so walk's backslashes are normalized later — ok but implicit.
- `core/lan_manifest.py:56-66` `root_failed = any(e.filename in (unc_path, str(Path(unc_path)))) and not files` → raises OSError refusing empty inventory if root enumeration failed. H2 loud failure — correct, prevents silent `[]` corruption. `getattr(e,"filename",None)` safe.
- `core/lan_manifest.py:67-71` If any errors but files exist → warning + partial return — correct.
- Small nits: `errors` not cleared per walk, holds all — good for diagnostic.

**`snapshot_to_dict` `core/lan_manifest.py:77-79`** — O(1) lookup, simple.

**`diff_snapshots` `core/lan_manifest.py:82-111`** — set ops O(n), sorted output deterministic for reports. Good.

**Positives:** H2 fail-loud root, partial inventory warning, no regex.

**Recommendations**
- **Warning:** Normalize `rel` to forward slashes at source (`rel.replace("\\","/")`) to match `backup_repository` expectation early — `core/lan_manifest.py:50`.
- **Info:** `Path(unc_path).resolve()` on offline share may block — consider `Path(unc_path)` without resolve or timeout.
- **Info:** Document that `mtime` is `float` seconds since epoch — `core/lan_manifest.py:30`.

---

## 10 — `core/lan_preflight.py` — 146 lines

**Purpose:** LAN preflight — SMB port 445 probe + canary `.AAM_TARGET_MOUNTED` + robocopy `/L` dry-run.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**`_extract_unc_host` `core/lan_preflight.py:18-27`** — replaces `/` with `\`, checks `\\` prefix, splits, returns first part — handles `\\192.168.10.10\share`, `//nas/share`, local `D:\` → None. Good.

**`_is_smb_reachable` `core/lan_preflight.py:30-45`** — `@retry(stop 4, wait 3s, retry_if_result not ok)` → 15s budget for NAS spin-up, `retry_error_callback -> False` — survives slow NAS. Uses `socket.create_connection((host,445),timeout=2.0)` → correct SMB port. Catches `OSError, socket.timeout, TimeoutError` — complete.

**`run_lan_dry_run` `core/lan_preflight.py:48-146`**
- `core/lan_preflight.py:65-73` SMB probe for UNC only — if unreachable → raise `HealthError` (P1-EXC single identity) with actionable message (WOL, network). Good, bypasses 45s OS hang.
- `core/lan_preflight.py:76-98` Canary check: `Path(dest) / ".AAM_TARGET_MOUNTED"` with `.is_file()` (strict, not `.exists()` — ensures regular file not dir). On OSError → HealthError. If missing → HealthError with recovery `cmd /c type nul > ...` and `10_recreate_canary.bat` hint + warning about populated share manual intervention. G11 self-recovering message excellent.
- `core/lan_preflight.py:100-109` `resolve_binary("robocopy")` + command `/L /MIR /XJ /NJH /NJS /NP /XF .AAM_TARGET_MOUNTED /XD System Volume Information $RECYCLE.BIN` — list-only, mirror logic, exclude canary + system dirs — correct. Note `/NJS` kept here (summary suppressed) because dry-run doesn't need count; real sync removed it (P1-COUNT).
- `core/lan_preflight.py:114-121` `subprocess.run(capture_output=True, text=True, errors="replace", timeout=300)` — good.
- `core/lan_preflight.py:124-136` `ok = code <8` (0-7 success) — correct Microsoft bitmask (error bits 8+). Captures stdout+stderr together because robocopy writes errors to stdout — correct. Returns `ok False` with combined output on fail vs `ok True` on pass.
- `core/lan_preflight.py:138-146` Timeout/FileNotFound/OSError → `ok False` dict (not raise) — distinction: SMB/canary raise HealthError (fail-closed before robocopy), robocopy errors return dict (soft). Correct pipeline: `flow.py` treats HealthError as abort, dict false as report.

**Positives:** SMB fast-fail avoids 45s hang, strict canary prevents mirror into wrong share, actionable errors.

**Recommendations**
- **Info:** SMB probe hardcodes 445 — add config override for non-standard NAS — `core/lan_preflight.py:36`.
- **Info:** Log SMB probe attempts for diagnostics — currently silent until fail.

---

## 11 — `core/lan_sync.py` — 380 lines

**Purpose:** Robocopy /MIR wrapper, authoritative failed-file count via summary row + bitmask floor, bounded log tails.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**Header `core/lan_sync.py:1-29`** — `/NC` forbidden comment critical, `_ERROR_LOG_TAIL` 100KB bounded.

**`_validate_required_flags` `core/lan_sync.py:31-34`** — rejects `/NC` — enforced in builder, good.

**`_summary_files_row_values` `core/lan_sync.py:45-63`** — regex `^\s*Files\s*:` pos parse, splits on `:`, strips `,`/`.` locale separators, `isdigit` fail-closed → `None`. Positional columns fixed regardless locale — A-prime contract per IMPLEMENTATION_FIX_PLAN.md. Good.

**`failed_file_count` `core/lan_sync.py:66-82`** — `bit3_floor = 1 if exit&8 else 0`, `parsed_failed = values[4]`, `max(parsed, floor)` and contradiction resolves to loud 1. Fixes LAN-06/LAN-15 /NJS blindness where summary suppressed. Excellent.

**`count_failed_lines` `core/lan_sync.py:85-93`** — `** FAILED:` line count over tail — bounded diagnostic, not authoritative count (which is summary). Good separation.

**`_read_log_tail` `core/lan_sync.py:96-120`** — `stat().st_size`, if <=max return full, else `seek(size-max)` + `read()` + `decode errors=replace` + `lstrip(FFFD)` — F15 seek-from-end avoids loading 500MB log into RAM. Correct Windows UTF handling.

**`classify_exit_code` `core/lan_sync.py:123-164`** — `if code&16→FAILED`, `if code&8→PARTIAL`, `if code in 0-3→COMPLETE`, `if 4-7→PARTIAL` (bit2 anomalies), else `FAILED` for negatives. Note correct `code&8` before `0-3` check — so code 9 (1+8) → PARTIAL not COMPLETE. Good. Comment enforces callers use `code&8` not status to distinguish anomalies vs copy errors.

**`cleanup_orphaned_robocopy_logs` `core/lan_sync.py:174-197`** — glob `%TEMP%/robocopy_sync_*.log` older than 24h, best-effort never raises — reclaim after hard-kill. Good G8.

**`_assert_source_not_empty` + `build_robocopy_command` `core/lan_sync.py:200-252`** — validates `/NC` + empty source (ValueError), flags `/MIR /Z /ZB /XJ /MT:4 /R:3 /W:10 /V /TS /FP /NJH /NDL /NP /XF /XD` — `/NJS` removed for P1-COUNT, `/V /TS /FP` required for parser — comments explain each. Good. `resolve_binary("robocopy")` M7 consistent.

**`run_lan_sync` `core/lan_sync.py:255-380`**
- `core/lan_sync.py:284-292` `mkstemp` + `/LOG:{path}` — robocopy writes via log file, DEVNULL for stdout/stderr — avoids pipe deadlock. Extends cmd with `/LOG:` after build — ok.
- `core/lan_sync.py:303-337` `status=classify_exit`, `if status==FAILED or code&8:` → `error_msg = _read_log_tail(100KB)`, `files_failed = failed_file_count(error_msg, code)`, `marker_count = count_failed_lines(error_msg)`, logs error with both counts — error tail for alert, files_failed for F12 report. For anomaly `4-7` → `anomaly_details` 100KB tail + warning, not error (no alert). Severity contract enforced.
- `core/lan_sync.py:338-373` Timeout/FileNotFound/OSError → `LAN_FAILED -1` dicts — complete.
- `core/lan_sync.py:375-380` `finally: log_path.unlink()` best-effort — good.

**Positives:** A-prime floor correct, F15 seek tail efficient, G8 orphan cleanup, severity contract `error` vs `anomaly_details` precise.

**Recommendations**
- **Info:** `_ANOMALY_LOG_TAIL` same 100KB as error — comment says 5KB but code 100KB; sync comment or reduce — `core/lan_sync.py:28`.
- **Info:** `build_robocopy_command` separator handling for source/dest with trailing slash — robocopy tolerates, but sanitize.

---

## 12 — `core/manifest.py` — 579 lines

**Purpose:** SQLite manifest + run_history, WAL mode, thread-safe, schema migration Critical-6.

**CodeRabbit:** No diff → 0 findings. Manual deep read below is critical — this is state store.

### Manual line-by-line

**DDL `core/manifest.py:21-68`** — `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys ON`, `file_entries` with `UNIQUE COLLATE NOCASE` on relative_path — good case-insensitive filesystem. `md5 pending` default, `lan_status`/`cloud_status` defaults `unknown`. Indexes on status + run_history started_at/mode. `db_meta` schema_version='1'. Good.

**`ManifestDB.__init__` `core/manifest.py:84-113`** — Validates `synchronous in normal/full` ValueError, mkdir parents, threading.Lock. Correct.

**`_get_conn` `core/manifest.py:115-160`** — Lazy connect, `row_factory sqlite3.Row`, pre-migration dedup of duplicate run_id (`DELETE WHERE id NOT IN MIN(id) GROUP BY run_id`) — pre-UNIQUE index fix. Then `busy_timeout`, `executescript(DDL)`, apply `PRAGMA synchronous=FULL` if operator wants durability (R4). Then `user_version < SCHEMA_VERSION` → migrate with close-on-fail not caching bad conn. Correct.

**`_migrate_legacy_schema` `core/manifest.py:162-211`** — `BEGIN IMMEDIATE` for DDL atomicity (SQLite DML vs DDL), check `extended_metrics` column, retry 3x on locked with `sleep(1*attempt)`, raise `ManifestSchemaError` loud — previously swallowed (Critical-6). Good.

**`upsert_file_entry` `core/manifest.py:224-276`** — `replace("\\","/")`, `COALESCE(md5,old)`, `COALESCE(status,old)`, timestamp only when status transitions to synced (first sync time preserved) — correct. `COMMIT` each call.

**`bulk_upsert_synced` `core/manifest.py:278-341`** — Validates mode, chunks 100 rows (700 params < 999 limit), executemany with `last_synced` CASE preserving first synced time. Fast 10-100x. Good.

**`delete_entries` `core/manifest.py:343-357`** — Normalizes, chunk 500 with placeholders, commit. Good.

**`prune_stale_synced` `core/manifest.py:408-445`** — `SELECT synced → stale = db - active`, `executemany UPDATE SET status NULL`, then `DELETE WHERE both NULL` — self-healing. Returns len(stale). Uses `with conn:` transaction context — atomic. Good.

**`insert_run` `core/manifest.py:449-486`** — Requires run_id/mode/started/status, `ON CONFLICT(run_id) DO UPDATE` with `COALESCE(extended_metrics, old)` — preserves metrics if new is null. Good.

**`get_runs_since`/`last_run`/`last_successful_run` `core/manifest.py:488-531`** — correct queries, last_successful uses `LIKE '%_COMPLETE'`. Includes NO_CHANGES? Yes `_COMPLETE` matches NO_CHANGES too.

**`purge_old_runs` `core/manifest.py:550-578`** — DELETE older than retention, `PRAGMA optimize`, `ANALYZE`, freelist check → VACUUM if >1000 pages (~4MB). Conditional vacuum per best practices. Logs vacuum.

**Recommendations**
- **Warning:** `file_count` interpolates `status_field` via f-string — validated against `_ALLOWED` allowlist, safe — but ensure no future bypass — `core/manifest.py:374`.
- **Info:** `DRL` string interpolation for `status_field`/`ts_field` in bulk_upsert is validated enum, safe — but keep validation tight.
- **Info:** `purge_old_runs` VACUUM commits then vacuums outside transaction — correct, but `conn.commit()` before VACUUM needed because VACUUM can't run in transaction.
- **Positives:** WAL+thread-safe+locked correctly, chunking, loud migration, conditional vacuum.

---

## 13 — `core/process.py` — 158 lines

**Purpose:** PID lock file with create_time anti-PID-reuse, binary resolution M7.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**Doc `core/process.py:1-11`** — explains `<pid>:<create_time>` anti-reuse via psutil, same as nginx/gunicorn — good.

**`_get_create_time` `core/process.py:22-35`** — catches `NoSuchProcess, AccessDenied, ProcessLookupError, ValueError, OverflowError` → None for stale. G6 handles negative/absurd PID via ValueError/OverflowError. Good.

**`write_lock` `core/process.py:40-68`** — atomic `mkstemp + os.replace`, fallback to PID-only if create_time None, mkdir parents, raises OSError on fail. `BaseException` cleanup closes fd and unlinks tmp — robust.

**`read_lock_alive` `core/process.py:71-133`** — exists check → read_text → `PermissionError → (True,-1)` assume live (AV locked) fail-safe, `OSError→False`. Parses `pid:ct`, guards `pid<=0 → False`, compares `abs(current_ct - written)<0.1s` tolerance for fp diff — live else stale (PID reused). Legacy bare PID fallback uses `psutil.pid_exists` with ValueError/Overflow guard. Returns `(alive,pid)` without deleting — caller responsibility.

**`resolve_binary` `core/process.py:145-158`** — checks `deploy/bin/name` then `name.exe` then `which` — M7 bundled priority correct.

**Positives:** PID-reuse safe, AV-lock safe, atomic write.

**Recommendations**
- **Info:** `write_lock` `content encode()` without newline — intentional, fine.
- **Info:** Consider `fsync` before close for durability? OS crash rarely matters for lock files.

---

## 14 — `core/rclone_config.py` — 60 lines

**Purpose:** Temporary rclone config writer — single source of truth for cloud callers.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**`write_temp_config` `core/rclone_config.py:12-47`** — strips location/storage_class/project_number, validates storage_class in `{"","STANDARD","NEARLINE","COLDLINE","ARCHIVE"}`, raises ValueError. `Path(gcs_key_path).resolve().replace("\\","/")` handles Windows backslashes. Content `[aam_gcs] type=google cloud storage ... bucket_policy_only=true` — correct. `mkstemp + close + write_text utf-8` — Windows handle-lock safe. Returns path, caller must clean.

**Security:** Writes service_account_file path + project_number plaintext to temp file in `%TEMP%` readable by other users — risk: other local user can steal key path reference. GCS key File itself is not copied, just path, so less risky, but temp rclone config still contains sensitive. File created with default ACL (user-only on modern Windows? Not guaranteed). Should set restrictive perms (600). Add `os.chmod` after write on Unix; on Windows use ACL? Info.

**`temp_rclone_config` `core/rclone_config.py:50-60`** — context manager yield + finally unlink OSError ignored — good.

**Positives:** Centralized, validated, Windows-safe.

**Recommendations**
- **Warning:** Temp config contains sensitive path — set restrictive permissions (`os.chmod 0o600` on POSIX, or `win32security` ACL) — `core/rclone_config.py:44`.
- **Info:** Validate `gcs_key_path` exists before writing — currently deferred to caller's file read error.

---

## 20 — `models/config.py` — 497 lines

**Purpose:** Pydantic v2 config models, validated on load, FY guard G2, cron Prefect validation G1.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**`PathsConfig` `models/config.py:22-77`** — `str_strip_whitespace=True`, `runtime_dir` default `C:\BackupAgent` via env override, `derive_runtime_paths` auto `logs/manifest.db` if empty + `.db` check, `backup_lock_path`/`prefect_home` properties, validators: source not empty, lan UNC `^\\\\.+\\`, gcs_key non-empty (existence not checked here — health does). Good.

**`LanConfig` `models/config.py:79-91`** — defaults sane (retry 3, timeout 14400=4h, MT 4, dry_run 900s F8 scaled for 1M files). Bounded ge/le. Good.

**`WolConfig` `models/config.py:94-173`** — empty mac allowed when disabled (F11), `valid_mac` regex, `model_validator` requires mac when enabled, `valid_ipv4`/`valid_broadcast` IPv4Address, `get_broadcast_address()` auto /24 `ip.rsplit(".",1)+".255"` — covers common case, documented.

**`CloudConfig` `models/config.py:175-244`** — `max_duration_seconds None` auto, bucket `""` empty valid when disabled (F11), else regex `^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$`, storage_class upper, bandwidth `^\d+[kMG]$`. Good.

**`NotificationConfig` `models/config.py:247-278`** — `__repr__` masks password `***`, `smtp_port` 1-65535.

**`MaintenanceConfig` `models/config.py:281-326`** — concurrency 3600s P2-CONC, sqlite_synchronous regex normal|full R4, retention 90d, vacuum 10000 pages.

**`HealthConfig` `models/config.py:329-360`** — clock skew 600s, rollover timeouts.

**`DashboardConfig` `models/config.py:363-379`** — requires api_key when auth enabled, masks in repr.

**`ScheduleConfig` `models/config.py:382-428`** — 5 crons + timezone, `valid_cron` 5-field check, `model_validator` with `prefect.server.schemas.schedules.CronSchedule(cron,tz)` G1 — catches `99 99 * * *` or bad tz at config load not serve crash-loop.

**`AppConfig.cross_field_validation` `models/config.py:443-486`** — lan UNC + gcs_key required, at least one dest enabled, 4-digit FY `FY\d{4}` refused ValueError (G2), FY mismatch `src_FY != lan_FY` → ValueError CRITICAL DATA LOSS PREVENTION (mirror would overwrite). Correct.

**Positives:** Pydantic strict, FY guards, G1 Prefect validation, masks secrets.

**Recommendations**
- **Info:** `derive_runtime_paths` doesn't expand `~` — fine for Windows.
- **Info:** `bandwidth_limit` regex `10M` but rclone allows `10 M` with space? No need.

---

## 21 — `flow.py` — 1021 lines

**Purpose:** Prefect 3 orchestrator, granular @task, two pipelines cloud/lan, _backup_slot P2-CONC, artifacts.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**`_stable_run_id` `flow.py:47-56`** — uses `FlowRunContext.id` + mode stable across task retries else uuid — prevents duplicate run_id on retry.

**`_backup_slot` `flow.py:59-117`** — `concurrency("aam-backup",1,timeout=wait_seconds)` inside pipeline not flow wrapper (P2-CONC — avoids self-deadlock limit=1), write_lock while held (F13 invariant lock==slot), test mode `PREFECT_TEST_MODE=1` bypasses concurrency but keeps lock lifecycle. Finally unlink.

**Tasks `flow.py:123-400`** — `health_check_task`(fail fast), `cloud_preflight_task`(raises RuntimeError if not ok), `cloud_sync_task`(fails on CLOUD_FAILED only, PARTIAL ok), `cloud_verify_and_report_task` with `temp_rclone_config` + `verify_cloud_integrity` 4h + `get_cloud_size` + M6 `get_cloud_manifest` try/except degrade + `get_cloud_diff`, `_run_state` carry, `cloud_record_task`/`lan_record_task` via `record_sync_results`, `wol_check_task`/`lan_preflight_task`/`lan_snapshot_before+after` (G14 after returns None on walk fail, skip diff — sync unaffected), `lan_sync_task` fails only on LAN_FAILED, `lan_shutdown_task` non-critical.

**`cloud_publish_artifact_task`/`lan_publish_artifact_task` `flow.py:402-468`** — Markdown artifacts via `prefect.artifacts.create_markdown_artifact`, nested try.

**`_run_cloud_pipeline` `flow.py:474-645`** — `_backup_slot`, FY prefix, `with_options retries` (preflight 1×30s, sync `max_attempts-1 × delay`, verify 1×60s), fetches `before_dict = db.get_cloud_synced_entries()` for differential, phases pre/sync/verify/post, health→preflight→sync→verify→record (M6 skip DB if manifest_error), F1 verify `if not verified → CLOUD_VERIFY_FAILED + send_failure_alert + raise`, then differential loop: path not in before or size>0.01 or mtime>1.1s (pendulum parse fallback) → copied, bytes sum, extended_metrics json (verified/total/manifest_ok/size_ok/diff_partial), publish artifact. Finally `_record_run`.

**Phase error mapping `flow.py:620-628`** — sync→CLOUD_FAILED, verify with COMPLETE→VERIFY_FAILED, pre→FAILED — correct.

**`_run_lan_pipeline` `flow.py:651-773`** — similar, preflight retries 1, sync `lan.max_attempts-1`, phases, before_dict, sync→status, files_failed, after_dict G14 None skip, diff added+modified→files_copied, extended_metrics, artifact, F3 shutdown only if LAN_COMPLETE else PARTIAL logs error + sends alert (NAS not shut down), failures still recorded.

**`_record_run` `flow.py:780-830`** — `now_iso()` ended, F16 monotonic duration `time.monotonic()-monotonic_start` else wall clock, `record_run_history` via ManifestDB, critical log if failed.

**`weekly/monthly_report_flow` `flow.py:837-874`** — load_config, configure_logging+bridge, check enabled, ManifestDB send_report.

**`rollover_check_flow` `flow.py:881-922`** — daily G10, calls `rollover()` idempotent, returns ROLLOVER_COMPLETED/NO_ROLLOVER_NEEDED, on RolloverError logs + send_failure_alert mode rollover, raises.

**`backup` flow `flow.py:929-1021`** — entry `backup(config_path,mode=all)` validate mode, load_config, logging, cleanup orphaned 24h, then P2-CONC inside pipelines, `excs=[]` run cloud then lan sequentially (cloud first), each `_run_cloud_pipeline` with stable_run_id+now_iso+monotonic, on exception append, summary if excs → error_summary + send_failure_alert + raise ExceptionGroup, else purge_old_runs maintenance.

**Positives:** P2-CONC slot+lock correct, F16 monotonic, M6 degrade, G14 resilient, F1 verify must pass, artifacts.

**Recommendations**
- **Info:** `_backup_slot` `wait_seconds` from maintenance.concurrency_wait_seconds default 3600 — long wait may block dashboard trigger; consider shorter.
- **Warning:** `flow.py:568-599` differential mtime string fallback `pendulum.parse(str(mtime))` expensive for 100k files — prior numeric check covers most.
- **Info:** Success_rate uses `_COMPLETE` LIKE includes NO_CHANGES? That's in cloud? Already handled.

---

## 22 — `serve.py` — 95 lines

**Purpose:** Prefect deployment entry, 4 deployments from config, P2-SCHED enabled-legs filter.

**CodeRabbit:** No diff → 0 findings.

**Line-by-line `serve.py:16-95`** — `_deployments()` load_config, tz, `if cloud.enabled → backup.to_deployment backup-cloud cloud_cron tz production/cloud`, `if lan.enabled → backup-lan`, always weekly/monthly/rollover-check (G10 daily). Returns tuple, `if __name__ main serve(*d, pause_on_shutdown=False)` keeps schedules active.

**Recommendations**
- **Info:** P2-SCHED disabled legs omitted here but server-side previously registered deployments remain active until `launch._reconcile_disabled_legs` pauses — documented comment correct.
- **Positives:** Enables rolling schedule pick-up after FY rollover without restart (config reload via Prefect parameter).

---

## 15 — `core/report.py` — 375 lines

**Purpose:** Email alerts + weekly/monthly HTML+CSV via SMTP, CSV injection G5 hardened, HTML escaped.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**`_send_email_with_attachments` `core/report.py:30-105`** — checks host/sender/recipients + username/password required, builds `MIMEMultipart(mixed)` + `alternative(html)`, attachments via `MIMEApplication`. SMTP: 465→SSL else STARTTLS, timeout 30s, `server.login` then `sendmail`. Retry loop `3 attempts` with `delay=10*(2^(attempt-1)) ±20% jitter` — exponential backoff, good. Permanent failures `SMTPAuthenticationError/SMTPSenderRefused/RecipientsRefused` → no retry return False. Others retry. `finally server.quit()`.

**`send_failure_alert` `core/report.py:108-167`** — checks `send_on_failure` toggle, upper mode, 1000-char inline error limit → `_send_email_with_attachments` with full error attachment `failure_details.txt`. HTML escapes firm/error/timestamp/status. Table styled.

**`_csv_safe`/`_generate_csv_data` `core/report.py:170-220`** — G5 prefixes `= + - @ \t \r` → prepend `'` force literal-text (Excel/DDE) — correct. Headers + rows, `_csv_safe` on error_message + extended_metrics (free text). Bytes utf-8.

**`generate_report_html` `core/report.py:223-322`** — `get_runs_since(days)`, stats: successes (`_COMPLETE` without NO_CHANGES) + no_changes + partials + skipped; failures derived, success_rate `(success+no_changes)/total`. 10 rows inline with 100-char error truncate, CSV full in attachment. `csv_notice` when is_email. Humanize bytes via `humanize.naturalsize`. Html escapes all dynamic values.

**`send_summary_report`/`send_weekly_report`/`send_monthly_report` `core/report.py:325-374`** — get_runs_since, generate html, `_generate_csv_data` attached, subject `Backup Weekly Report - firm`.

**Recommendations**
- **Info:** `_CSV_INJECTION_PREFIXES` includes `\t`/`\r` — Excel treats those as formulas only in first col? Still safe, good.
- **Warning:** `humanize` may not be installed in minimal env — ensure in pyproject.
- **Info:** Subject/filename `firm_name.replace(' ','_')` doesn't sanitize `..` or `/` — CSV filename could contain path traversal if firm_name attacker-controlled (config). Sanitize with `re.sub(r'[^A-Za-z0-9_-]', '_', firm_name)` — `core/report.py:348`.
- **Positives:** HTML escaped, retry with jitter, G5 safe.

---

## 16 — `core/shutdown.py` — 50 lines

**Purpose:** Remote shutdown `shutdown /s /m \\IP /t 300 /f` with 5min cancel window.

**CodeRabbit:** No diff → 0 findings.

**Line-by-line `core/shutdown.py:11-50`** — builds cmd list, `subprocess.run(capture_output=True, text=True, timeout=30)`, return `{"shutdown_initiated":bool,"server_ip":str,"error":str|None}`. Non-zero → `stderr.strip() or exit code`, FileNotFound/TimeoutExpired/OSError handled with distinct messages. Logger warning/error.

**Info:** `f"\\\\{server_ip}"` correct UNC `\\IP`. No IP validation — could inject `server_ip="1.2.3.4 & whoami"`? But list-form `subprocess.run` without shell → no injection.

**Positives:** List-form safe, timeout bounded.

---

## 17 — `core/time_utils.py` — 106 lines

**Purpose:** IST-only datetime single source via pendulum, FY routing April 1, cron humanization.

**CodeRabbit:** No diff → 0 findings.

**Line-by-line `core/time_utils.py:23-81`** — `IST=pendulum.timezone("Asia/Kolkata")` constant. `now_iso()`/`now_formatted()` via `pendulum.now(IST)` — ISO with offset, human IST label. `cutoff_iso(days)` subtract. `get_fy_prefix(today?)` FY April 1 start: month>=4 → `FYyy-yy+1` else `FYyy-1-yy`. Uses `year%100` 02d. Correct.

**`cron_to_human` `core/time_utils.py:87-106`** — handles None/non-str → str, `ExpressionDescriptor(cron.strip(), Options(use_24hour=True)).get_description()` + tz short split on `/`. Two-tier except `MissingField/Format → warning return raw`, `Exception → error return raw` — never crashes dashboard.

**Positives:** Eliminates datetime/zoneinfo sprawl, FY logic tested.

---

## 18 — `core/wol.py` — 144 lines

**Purpose:** WoL magic packet (global + subnet broadcast, repeated) + SMB wait.

**CodeRabbit:** No diff → 0 findings.

**`_smb_port_open` `core/wol.py:19-34`** — port clamp 0-65535 log warning false, `socket.connect_ex((ip,port))==0` — more reliable than ping, handles OverflowError via guard. Good P1-WOL.

**`_send_magic_packet` `core/wol.py:37-84`** — `rounds=max(1,repeat)`, each round sends `wol_send(mac, ip=255.255.255.255, port=9)` + if subnet != global also send subnet broadcast. Per-round `except Exception → warning` continue — broad-but-loud, re-sends on dropped UDP (G3). Sleeps `interval` between rounds.

**`wait_for_server` `core/wol.py:89-112`** — polls `_smb_port_open` every `ping_interval` until `wake_timeout`, then `stability_wait` then return else `WolTimeout`.

**`ensure_server_online` `core/wol.py:115-144`** — if wol disabled → True, if SMB already open → True, else send magic + wait. Correct flow.

**Info:** `wol_send` port 9 hard-coded — not configurable but fine.

---

## 19 — `core/logging.py` — 112 lines

**Purpose:** Loguru rotating daily 30d + stderr + Prefect bridge per-run-id cached (F10 fix).

**CodeRabbit:** No diff → 0 findings.

**`configure` `core/logging.py:14-42`** — `mkdir parents`, `logger.remove()`, `add(sys.stderr INFO colorize)`, `add(log_dir/backup_{time:YYYY-MM-DD}.log rotation=1day retention="30 days" DEBUG enqueue=True)` — enqueue thread-safe. Good.

**`configure_prefect_bridge` `core/logging.py:48-112`** — idempotent `_bridge_configured` guard, per-run `_logger_cache` 128 LRU, `_get_prefect_logger()` resolves TaskRunContext/FlowRunContext key, caches, evicts oldest. `prefect_sink` forwards INFO/WARNING/ERROR/CRITICAL/elseDEBUG via `get_run_logger()`. Handles exception via `logger.opt(depth=1).debug`. Adds sink level INFO. Fixes F10 stale logger (always-on agent night1 logger reused). Good.

**Info:** `enqueue=True` may hide log loss on crash — acceptable.

---

## 23 — `ui.py` — 676 lines

**Purpose:** FastAPI dashboard, auth, rate limits, Prefect active-run H3 fail-closed, reports, F13 config+DB lock.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**`_check_rate_limit` `ui.py:39-64`** — `threading.Lock`, `_RATE_LIMITS dict[str,list[float]]`, window 300s, 5 trigger/10 login/10 report — sliding window, cleanup expired. Correct in-mem (reset on restart OK for dashboard).

**`_sessions` `ui.py:70-101`** — 24h TTL dict, `secrets.token_hex(32)`, `_validate_session` del expired, httponly Lax cookie. No persistence — logout on restart expected.

**`_check_api_key_header` `ui.py:113-118`** — `hmac.compare_digest` timing-safe. Good.

**`_cfg`/`get_db` `ui.py:124-176`** — TTL 300s refresh, `threading.RLock` serializes config+DB eviction (F13), `if database_path changed → close old DB evict` under same lock — prevents CLOSE-while-query 503. Good.

**`_prefect_has_active_run` `ui.py:191-232`** — typed `FlowRunFilter(state any=[RUNNING,PENDING]) limit 200`, tri-state None on exception (unknown). Old code returned False disabled duplicate guard (H3). Loop tags or parameters mode match `pipeline`. Good.

**`_run_state_fields` `ui.py:234-247`** — `running: bool strict`, `run_state: running|idle|unknown` — dashboard.js truthiness safe, unknown degrades not breaks badge.

**`login_page`/`login_submit`/`logout`/`_require_auth` `ui.py:257-328`** — html.escape error, form POST, rate limit login, `compare_digest` session or X-API-Key, redirect html vs 401 json per Accept.

**`dashboard` `ui.py:331-363`** — `_require_auth`, flash_map, cron_to_human, TTL template. Good.

**`status` `ui.py:366-427`** — `_require_auth`, `get_recent_runs(25)` 503 on DB missing, recent 2000-char truncate, `_last_run_summary`, `_is_running` both pipelines → `_run_state_fields`, `file_count` both modes, `_get_health`.

**`health` `ui.py:430-442`** — unauthenticated, `asyncio.to_thread Path.exists` non-blocking, returns source_drive accessible. Fail → healthy 200 (not to alarm). Good.

**`trigger/cloud+lan` `ui.py:445-506`** — require_auth, rate limit, `await _is_running` None→503 fail-CLOSED (old fail-open queued duplicate), running→400 already_running, else `await arun_deployment(name)` inline surface 500 (G15 await not fire-and-forget background), return triggered with flow_run.id.

**`report/*`/`trigger/report/*/email` `ui.py:509-632`** — rate limit, `_serve_report` checks Path.exists 503 else `generate_report_html` 404 if empty, safe_firm `re.sub [^a-zA-Z0-9_\-] → _`, filename dated IST, `Response attachment`. Email triggers generate_html then `send_weekly/monthly_report` with body_html, 404 no_data else 500 on exception.

**`_get_health` `ui.py:664-673`** — `asyncio.to_thread shutil.disk_usage` + exists, error unavailable.

**Positives:** H3 tri-state correct, F13 RLock, G15 await inline, rate limits, html escapes, per-IP.

**Recommendations**
- **Warning:** In-mem rate limit not shared across workers (uvicorn --workers>1 bypass) — document single worker — `ui.py:41`.
- **Info:** Session store not persisted; restart logs out all — acceptable for ops dashboard.
- **Info:** `secure` cookie flag missing — add `secure=True` when behind HTTPS — `ui.py:299`.
- **Info:** Ensure `static/` and `templates/` dirs exist else mount/jinja fails at import — graceful.

---

## 24 — `launch.py` — 356 lines

**Purpose:** Launch dashboard+ scheduler, wait Prefect API, FY rollover boot-time, concurrency limit, orphan cancel, P2-SCHED reconcile.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**Header `launch.py:1-24`** — forces `PREFECT_API_URL http://127.0.0.1:4200/api` before import, PROJECT_DIR.

**`_run_dashboard` `launch.py:26-37`** — load_config, uvicorn.run(app host/port log_level warning) in daemon thread.

**`_check_prefect_api` `launch.py:39-50`** — httpx GET /health 5s, 200 true else false, ConnectError suppressed. Used for 300s retry wait.

**`_ensure_concurrency_limit` `launch.py:53-98`** — `upsert_global_concurrency_limit_by_name("aam-backup",1)` + `create_concurrency_limit(tag="aam-backup",1)` via get_client (not CLI ephemeral), handles 409 already exists. Handles `get_running_loop` vs `ThreadPoolExecutor` for nested loop. Correct for P2-CONC.

**`_cancel_orphaned_runs` `launch.py:100-186`** — lock-aware: `read_lock_alive` if alive skip RUNNING cancellation else clean stale lock, then async `_cancel` for PENDING always + RUNNING only if not active, paginated `limit 200 offset` batches, filter `r.flow_id or name` (currently `or True` — cancels all? line 168 `or True` makes predicate always true — Warning). Sets `Cancelled(message="Cancelled orphaned run") force=True`.

**Issue `launch.py:168`** — `runs = [r for r in runs if ... or True]` — trailing `or True` makes filter no-op, cancels every PENDING/RUNNING run including unrelated flows. Intent was `if "aam-backup" in name` but True defeats it. Critical: will cancel any Prefect run on same server (other flows). Should remove `or True` or scope to `tags == "aam-backup"` / `flow.name == "aam-backup"`.

**`_reconcile_disabled_legs` `launch.py:189-250`** — P2-SCHED pause/resume: if enabled+paused→resume, disabled+not paused→pause (not delete, reversible). Idempotent. Handles absent ObjectNotFound, nested loop via threadpool.

**`main` `launch.py:253-356`** — loads config, wait Prefect API 300s 10s interval, dash thread daemon + 0.5s alive check, FY rollover `rollover()` with RolloverError blocked retry next run else warning, ensure concurrency, cancel orphaned, reconcile deployments log outcome, then `serve(*deployments(), pause_on_shutdown=False)` in main thread, shutdown clean handling KeyboardInterrupt.

**Positives:** 300s wait handles watchdog restart race, FY at boot, P2-SCHED correct.

**Recommendations**
- **Critical:** Remove `or True` in `launch.py:168` — currently cancels all orphaned runs server-wide, not scoped to aam-backup — `launch.py:168`.
- **Warning:** `_cancel_orphaned_runs` cancels without tag filter — scope to `flow.name == "aam-backup"` or tags `aam-backup` — same line.
- **Info:** `if not dash_thread.is_alive()` warning after 0.5s may false-positive under slow import — increase delay.
- **Info:** `sys.exit(1)` on Prefect not ready — watchdog will restart agent? Log critical.

---

## 25 — `watchdog.py` — 495 lines

**Purpose:** External watchdog, Prefect API 60s poll, 5 failures threshold, transfer vs lock deferral H4, SCM START_PENDING aware F6, circuit breaker 3/h, agent G4.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**Config `watchdog.py:58-82`** — `CHECK 60s FAILURE 5 REQUEST 30s BACKUP_WAIT 120s MAX_DEFERRALS 15 (~30min) MAX_TRANSFER 240 (~8h) >6h timeout` correct, services AamPrefectServer/AamBackupAgent.

**`_resolve_paths` `watchdog.py:85-98`** — derives from config.yaml else hardcoded defaults, called before logging.

**`_is_backup_running` `watchdog.py:142-177`** — read_lock_alive validated, side-effect-free (no delete — deferral branch only deletes after cap), G6 catch Exception (ValueError/Overflow for negative/huge PID) treats as stale — fixes crash-loop.

**`_transfer_process_running` `watchdog.py:123-139`** — psutil scan for rclone.exe/robocopy.exe lowercased, definitive real transfer.

**`_service_state` `watchdog.py:182-215`** — `sc query service` parse STATE line, handles digit+name `4 RUNNING`, 2-attempt retry on timeout. F6 distinguishes STOPPED vs START_PENDING.

**`_service_is_running`/`_start_service`/`_start_allowed`/`_stop_service` `watchdog.py:218-298`** — circuit breaker `service_start_log` per-service 3/3600s, `sc start/stop` 30s timeout, failureflag=1 DependOnService restart logic documented.

**`_check_health` `watchdog.py:302-309`** — httpx GET /health boolean.

**`main` loop `watchdog.py:314-491`** — healthy→ reset failures+deferrals, G4 check AGENT_SERVICE RUNNING→breaker reset, START_PENDING/PENDING→wait, STOPPED→_start_allowed else CRITICAL, unknown→warning, sleep 60s. Unhealthy→failures++, <5 sleep; >=5 check `lock_held=_is_backup_running()` + `transferring`, if transferring→ transfer_deferrals++ if >=240 FORCE unlink lock + fall through else defer 120s continue, elif lock_held→ lock_deferrals++ >=15 force unlink else defer, else no backup → check WATCHED_SERVICE state START/STOP_PENDING→reset, STOPPED→_start_allowed else CRITICAL, RUNNING but API dead→_stop_service (NSSM restarts both). Exception catch outer log+sleep.

**H4 independent counters** — transfer vs lock separate caps prevents LIVE lock deletion after transfer streak.

**Positives:** F6 SCM states, H4 independent deferrals, G4 agent monitor, circuit breaker, G6 broad catch.

**Recommendations**
- **Critical — Confirmed SyntaxError:** `watchdog.py:338-339` Verified via `py_compile` + `ast.parse`:
```
338:             if healthy:
339:             if failures > 0:
```
  `if healthy:` has empty suite (next line not indented) → `IndentationError: expected an indented block after 'if' statement on line 338`. The watchdog service will crash on import/start (`NSSM` will restart loop). Fix: indent lines 339-368 under `if healthy:` (see `watchdog.py:339-369` — all lines to `continue` should be indented one level). This is not a display artifact — `py -3 -m py_compile watchdog.py` fails with exit 1.
- **Info:** `TRANSFER process` lowercased check correct.
- **Info:** Log watchdog_svc 10MB×5 rotation correct.

---

## 26 — `collect_config_data.py` — 172 lines

**Purpose:** Interactive YAML snippet collector, network+drives, FY prefix, Pydantic verify.

**CodeRabbit:** No diff → 0 findings.

### Manual line-by-line

**Header `collect_config_data.py:11-13`** — forces utf-8 stdout for emoji on CMD — good.

**`get_network_info` `collect_config_data.py:15-37`** — `psutil.net_if_addrs` + `isup`, skip Loopback/lo, AF_INET ip + AF_LINK mac, filter 127/169.254, replace -→: upper. Good.

**`list_drives` `collect_config_data.py:39-44`** — `disk_partitions(all=False)` fstype present.

**`is_firewall_open` `collect_config_data.py:49-55`** — powershell `Get-NetFirewallRule Enabled+Inbound+Allow → Get-NetFirewallPortFilter LocalPort==port` via `shell=True` `powershell -Command "..."` — port param interpolated into PS single-quoted `'{port}'` — numeric, safe. Returns bool stdout.strip(). Exception→False.

**`verify_with_pydantic` `collect_config_data.py:57-76`** — loads full config.yaml, updates snippet, `AppConfig(**full_config)` — validates snippet via real model, prints warning on fail.

**`main` `collect_config_data.py:78-169`** — prints drives, FY prefix, paths snippet `D:\FY`, `\\192.168.1.100\share\FY`, verifies each, prints NETWORK interfaces with wol+dashboard snippets each verified, reminders, firewall 8080 check with netsh hint, `input Press Enter`.

**Recommendations**
- **Warning:** `collect_config_data.py:51` `shell=True` with `cmd = f"powershell ... '{port}'"` — port int safe but use list form `["powershell","-Command", script]` to avoid shell. Low.
- **Info:** Hardcoded `192.168.1.100` in snippet — placeholder but should prompt for actual NAS IP discovery? Already enumerates.
- **Info:** Duplicate `import subprocess` mid-file `collect_config_data.py:46` — move to top.
- **Info:** `input()` blocks non-interactive — guard `if sys.stdin.isatty()`.

---

## Overall Summary

**Scope covered:** 26 production sources (core/*, models/config, flow, serve, ui, launch, watchdog, collect_config_data) + deploy/static templates noted. ~6k lines reviewed function-by-function, line-by-line.

**Critical:** 2 — (1) `launch.py:168` `or True` cancels all Prefect runs server-wide; (2) `watchdog.py:338` IndentationError crashes watchdog on start (verified `py_compile` fails) — both block production.

**Warnings (highest value):** backup_repository normalize removed + empty path + prune empty + mode Literal (01); cloud_reporter list guard + short line (03); cloud_verify stderr loss (05); fy_rollover tmp unlink mask (06); manifest f-string allowlist note (12); rclone_config chmod (14); report filename sanitize (15); flow differential cost (21); ui rate single-worker (23); watchdog 8h cap correct; collect subprocess shell (26).

**Positives across codebase:** Consistent M7 `resolve_binary`, H3 tri-state, F13 RLock, P2-CONC slot+lock, M6 manifest degrade, G14 resilient, F15 seek tail, A-prime floor, WAL+LOCK correctly, FY guards G2, G1 Prefect cron validation, secrets masked, HTML/CSV escapes, HDD optimizations.

**CodeRabbit notes:** CLI v0.7.5 Free seat not assigned → `review --agent --base main` returned 0 findings (only DEPLOYMENT_GUIDE docs diff); Free plan limited deep scan — manual line-by-line above fills gap. Recommend running `coderabbit review --agent` on a PR diff for richer findings.

**Next steps (1 file at a time as requested):** Fix launch.py:168 scoping first, then warnings in priority order backup_repository → cloud_verify → fy_rollover → report → ui secure flag.

---

