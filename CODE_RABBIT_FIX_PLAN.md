# Fix Plan for Deep Review Findings — What We Will Do, Exactly

> Date: 2026-09-02
> Base: `CODE_RABBIT_DEEP_REVIEW.md` (26 files, line-by-line)
> Auth: CodeRabbit CLI v0.7.5 Free + manual verification (`py_compile`, `ast.parse`)
> Rule: AGENTS.md — `impact()` before edit, `detect_changes()` before commit, warn on HIGH/CRITICAL
> Working branch: `development` (will use fix branch `fix/coderabbit-deep-review` off development)

## Goal
Resolve findings with **minimal, reversible diffs**, no behavior widening, verified at each step. Zero risk to running Prefect 4200 agent.

---

## Phase 0 — Preparation (no code change)
1. Create branch `fix/coderabbit-deep-review` from `development`.
2. For each target symbol, run `impact({target, direction:"upstream", repo:"AAM_BACKUP_V1"})` and record blast radius in commit body. This satisfies AGENTS.md MUST.
3. Run `git status` baseline.

**Verification:** branch created, tools reachable, `py -3 -m py_compile <file>` passes after each edit.

---

## Phase 1 — P0 Critical (block deploy, fix FIRST, one commit per file)

### 1A. `watchdog.py:338-369` — IndentationError (Crash on Start)

**What it is:** `if healthy:` empty suite, next line `if failures > 0:` same indent → `IndentationError: expected an indented block on line 338`. Verified: `py -3 -m py_compile watchdog.py` fails exit 1. Watchdog service (AamWatchdog via NSSM) will crash-loop, never reach health polling. No tests catch this because file not imported in CI? But it blocks prod.

**Best fix (chosen):** Indent `watchdog.py:339-369` exactly one level under `if healthy:` so healthy-path (reset counters, G4 agent check, `sleep 60s`, `continue`) is inside the `if`. Keep unhealthy path (`failures++` at `watchdog.py:371`) outside. This restores intended control flow from git history pre-regression (compare `git log --oneline watchdog.py` — regression was a formatting commit). **Alternative rejected:** wrapping in `if healthy: pass` — would change semantics; moving sleep outside would double-sleep.

**Exact change:**
```diff
-            if healthy:
-            if failures > 0:
-                logger.info(f"Prefect API healthy (recovered after {failures} failure(s))")
-            failures = 0
+            if healthy:
+                if failures > 0:
+                    logger.info(f"Prefect API healthy (recovered after {failures} failure(s))")
+                failures = 0
+                transfer_deferrals = 0
+                lock_deferrals = 0
+                # ... all lines through `continue` indented 4 spaces ...
+                time.sleep(CHECK_INTERVAL_SECONDS)
+                continue

-            transfer_deferrals = 0
-            lock_deferrals = 0
-            agent_state = _service_state(AGENT_SERVICE)
-            # ...
-            time.sleep(CHECK_INTERVAL_SECONDS)
-            continue
```

**File:** single file `watchdog.py` only.

**Risk:** LOW after fix (restores original logic). Risk of NOT fixing: CRITICAL — watchdog never runs.

**Tests / Verify:**
- `py -3 -m py_compile watchdog.py` must exit 0
- `py -3 -c "import ast; ast.parse(open('watchdog.py').read()); print('ok')"`
- `pytest tests/test_watchdog.py tests/test_watchdog_comprehensive.py tests/test_h4_watchdog_counters.py -v`
- `pytest tests/test_watchdog_fixes.py -v` if present
- Manual: `python watchdog.py` should not SyntaxError (will loop, Ctrl+C)

**Commit:** `fix(watchdog): indent healthy block (P0 IndentationError)`

---

### 1B. `launch.py:168` — Over-broad Orphan Cancellation (`or True`)

**What it is:**
```python
runs = [r for r in runs if getattr(r, "flow_id", None) is not None or "aam-backup" in str(getattr(r, "name", "")) or True]
```
Trailing `or True` makes predicate always True → every PENDING/RUNNING flow on the Prefect server is cancelled at boot, not just `aam-backup`. On a shared Prefect server this kills unrelated flows. Proven via read: `launch.py:168` always true.

**Best fix (chosen):** Scope strictly to this project. Historical intent (via comment and prior version) was to cancel only `aam-backup` flows. Use tag + deployment name filter. Change to:
```python
runs = [r for r in runs if "aam-backup" in (getattr(r, "tags", None) or []) or getattr(r, "name", "").startswith("aam-backup") or (getattr(r, "flow_id", None) is not None and hasattr(r, "deployment_id") and r.deployment_id is not None)]
```
Simpler, minimal, matches `ui.py:227-230` and previous `serve.py` deployment name `aam-backup/backup-*`. **Alternative considered:** keep `flow_id is not None` alone — too broad (all flows have flow_id). Rejected. **Alternative:** filter by `flow.name == "aam-backup"` — Prefect stores flow name separately, but tag is reliable per `serve.py: tags=["production","cloud"]`? Actually `serve.py` tags flows? It tags deployments, flow runs inherit tags. So check `tags`.

**Minimal safe version (chosen for smallest diff):**
```python
runs = [r for r in runs if "aam-backup" in str(getattr(r, "tags", "")) or "aam-backup" in str(getattr(r, "name", ""))]
```
This matches `ui.py:229` logic: `if pipeline in tags or parameters.get("mode")==pipeline`. We will reuse `tags` based.

**File:** `launch.py` only (line 168).

**Risk:** MEDIUM if not fixed (data loss of other flows), LOW after fix (narrows cancellation, strictly safer). Need to ensure we do NOT miss cancelling genuine orphans: still cancels `aam-backup` with RUNNING/PENDING correctly.

**Tests / Verify:**
- `py -3 -m py_compile launch.py`
- `pytest tests/test_launch.py tests/test_launch_comprehensive.py -v`
- Dry-run: boot with TEST_MODE=1 — log shows only `aam-backup` runs cancelled (manual inspection), no other flows affected

**Commit:** `fix(launch): scope orphan cancellation to aam-backup (P0 over-broad)`

**Order note:** 1A must land before 1B because watchdog guards Prefect health; but both P0, do 1A → verify → 1B → verify. Do NOT batch.

---

## Phase 2 — P1 Warning (high value, one commit per file/group, after P0 merged)

We do these ONLY after P0 is green. Each gets its own commit and its own `impact()` + test run.

### 2A. `core/backup_repository.py` (4 warnings)
- Validate `mode: Literal["cloud","lan"]` — add `if mode not in ("cloud","lan"): raise ValueError` at `record_sync_results:14` (already validated in `bulk_upsert_synced`, lift same check up).
- Normalize `removed` paths: `normalized_removed = [p.replace("\\","/") for p in removed]` before `delete_entries`.
- Filter empty `path` entries: `normalized = [e for e in normalized if e["path"]]` before upsert; also handle `e={"Path":""}` via `or` fallback not `is not None`.
- Document prune-empty: keep current `if entries:` skip (intent: empty GCS listing on first run should not prune), but add comment clarifying, OR add `else: prune_stale` with empty set — we choose comment-only (no behavior change) to avoid deleting on transient empty.

**Tests:** `tests/test_backup_repository*.py` + `test_workflows.py::TestBackupRepositoryWorkflow`

### 2B. `core/cloud_reporter.py`
- Add `if not isinstance(data, list): raise CloudReporterError` after `json.loads` in `get_cloud_manifest:125`.
- Guard `line[2:]` in `get_cloud_diff:207`: `if len(line) < 2 or line[1] != " ": continue` plus `line[0] in "+-*="`.

**Tests:** `tests/test_cloud_reporter*.py` + `test_cloud_sync_comprehensive` maybe

### 2C. `core/cloud_verify.py`
- Include stderr snippet in error: `error = _build_error_message(code) + (f": {stderr[:2000]}" if stderr else "")` instead of static string. Preserves per-file mismatch for operator while keeping verified boolean.

**Tests:** `tests/test_cloud_verify*.py`

### 2D. `core/fy_rollover.py`
- Wrap `os.unlink(tmp_path)` in `try: ... except OSError: pass` in `update_config_yaml:267` so original exception not masked.

**Tests:** `tests/test_fy_rollover*.py` + `test_rt_05`

### 2E. `core/rclone_config.py`
- After `write_text`, `try: os.chmod(cfg_path, 0o600) except OSError: pass` (POSIX). Windows ACL is already user-only via `%TEMP%`, so no additional win32security needed for now. Add comment.

**Tests:** `tests/test_rclone_config*.py`

### 2F. `core/report.py`
- Sanitize CSV/email filename: `safe_firm = re.sub(r"[^A-Za-z0-9_\-]", "_", firm_name)` at `send_summary_report:348` and `ui._serve_report` already does this — make consistent.

**Tests:** `tests/test_report*.py`

### 2G. `ui.py`
- Add `secure=True` when setting cookie if `request.url.scheme == "https"` else keep Lax (avoid breaking http local). Add comment about single-worker rate limit limitation (document in code comment, no code change needed beyond doc).

**Tests:** `tests/test_ui*.py` + `test_h3_ui_failclosed`

### 2H. `collect_config_data.py`
- Move `import subprocess` top, replace `shell=True` powershell string with list form `["powershell","-Command", script]` to avoid shell injection; keep port int safe. Guard `input()` with `if sys.stdin.isatty()`.

**Tests:** manual run `py collect_config_data.py` dry

**Grouping:** 2A separate commit; 2B+2C one commit (both cloud reporter/verify); 2D+2E separate; 2F+2G separate (reporting/UI).

---

## Phase 3 — P2 Info (optional, not blocking, can be deferred)

- `core/cloud_preflight.py:140` truncate stderr log to 2k last chars
- `core/cloud_sync.py:249` add `errors="replace"` to `Path.read_text`
- `core/manifest.py:374` keep f-string allowlist validated (no change)
- `flow.py:568` differential mtime string parse hot path — keep numeric fast path, no change now

We will NOT do P2 in this PR to keep diff minimal. File as follow-up.

---

## Execution Order & Branching

1. **Now:** Create `fix/coderabbit-deep-review` off `development` (no code yet).
2. **Step 1A:** Edit `watchdog.py` only → `py_compile` → `pytest watchdog` → `coderabbit review --agent -t uncommitted` to confirm no new complaints → commit → push.
3. **Step 1B:** Edit `launch.py` only (line 168) → compile → `pytest launch` → commit → push.
4. **Step 2A-H:** One file/group at a time, each: `impact()` → edit → compile → targeted pytest → `detect_changes()` → commit.
5. **Final:** `coderabbit review --agent --base development` on fix branch to confirm clean, then PR to development.

**Why this order:** P0 crashes block everything; P0 fixes are isolated single-line-scope, low regression risk, unlock watchdog + scheduler. P1 is ordered by blast radius (manifest first).

## Verification Checklist (must pass before PR)

- [ ] `py -3 -m py_compile watchdog.py launch.py core/backup_repository.py core/cloud_reporter.py core/cloud_verify.py core/fy_rollover.py core/rclone_config.py core/report.py ui.py collect_config_data.py` all 0
- [ ] `pytest -q` (or at least per-file suites) green — no new failures vs `development` baseline
- [ ] `coderabbit review --agent -t uncommitted` on fix branch shows no Critical
- [ ] `detect_changes({scope:"all"})` shows only intended symbols affected
- [ ] Manual: `python -c "import watchdog; print('watchdog loads')"` succeeds after 1A

## Rollback
Each phase is one commit per file — `git revert <commit>` restores. No DB migration in this plan, so rollback is safe. Watchdog revert would reintroduce SyntaxError, so keep 1A pinned.

## What We Will NOT Do
- No mass reformat, no-wide rename, no dependency upgrades
- No P2 batch changes in same PR
- No changing `serve.py` deployment names (would break scheduled runs)
- No adding new dependencies (e.g., win32security)

---

*Next action after you approve: I will start Phase 0 (branch + impact) then 1A watchdog fix, wait for your go before each P0 commit.*
