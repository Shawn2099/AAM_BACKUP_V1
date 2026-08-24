# Code Audit Report — AAM_BACKUP_V1

**Date:** 2026-08-23
**Scope:** All 28 application Python files (~5,700 lines), audited function-by-function (syntax, logic, bad practices). Audit only — no code changes were made. No prior audit documents were referenced.
**Perspective:** Production readiness for an internal Windows tool (backup automation: Robocopy → LAN NAS, rclone → GCS, Prefect 3 orchestration, Python 3.12).

---

## Overall Verdict

This is a well-above-average internal codebase. Error contracts are consistent, exit-code classification is documented against vendor docs, secrets are masked in `__repr__`, HTML/CSV injection is handled, and the lock/concurrency design is thoughtful. The findings below are real but most are edge-case or hygiene issues.

---

## CRITICAL / HIGH

### [FIXED 2026-08-23] H1. `launch.py:237-248` `main()` — rollover deployment is created but never served
`rollover_deployment` is unpacked from `deployments()` but **not passed to `serve()`** (only cloud, lan, weekly, monthly are). The entire G10 rationale — "24x7 servers rarely reboot, so a scheduled daily rollover check is required" — is dead in this entrypoint. If production runs via `launch.py` (watchdog restarts `AamBackupAgent` → this script), the April 1 rollover only fires if someone reboots. `serve.py` passes all five correctly; the two entrypoints have diverged. Docstring at `launch.py:10` ("4 deployments") confirms the drift.

### [FIXED 2026-08-23] H2. `core/lan_manifest.py:27` `walk_lan_destination()` — silent empty result on inaccessible share
`os.walk(unc_path)` is called with no `onerror=` callback. os.walk swallows directory-open errors by default. If the NAS share is unreachable/mid-reboot, the function returns `[]` — indistinguishable from "destination legitimately empty." Consequences:
- `flow.py:583` (`lan_snapshot_before_task`) gets an empty "before" snapshot → after sync, diff reports *everything* as added → inflated metrics.
- The G14 guard in `lan_snapshot_after_task` only triggers on exceptions, which never occur here.

The docstring claims "the filesystem IS the truth" — but walk errors are silently discarded, so the truth can be quietly empty.

### H3. `ui.py:192-197` `_prefect_has_active_run()` — raw dict passed as Prefect filter
`FlowRunFilter(state={"type": {"any_": [...]}})` uses a hand-built nested dict while `launch.py:137-142` correctly uses `FlowRunFilterState`/`FlowRunFilterStateType`. It works today only because Pydantic coerces dicts into models; if Prefect tightens validation (or the dict key drifts), the `except Exception` at line 204 returns `False` — i.e., **"nothing is running"**, which disables the already-running guard on both trigger endpoints. Failure mode is silent fail-open. Also: `limit=20` means >20 queued runs can hide an active one.

### H4. `watchdog.py:321, 372-426` `main()` — one shared `deferrals` counter for two different signals
Transfer-deferrals and stale-lock-deferrals increment the same counter with different caps (240 vs 15). Scenario: a long backup generates ~90 transfer deferrals (API unhealthy during heavy IO), then the transfer ends but the flow still holds the lock → the lock branch sees `deferrals >= MAX_DEFERRALS` immediately and force-deletes the lock + restarts without its intended 30-minute grace.

Additionally, the forced-restart path at lines 386-389 falls through into `if lock_held:` using the **stale pre-unlink value** of `lock_held`, so it sleeps one extra 120 s cycle before actually restarting (next iteration re-reads the deleted lock). Both branches should use independent counters and re-evaluate state after forcing.

---

## MEDIUM

### M1. `models/config.py:71-76` `gcs_key_exists()` — misleading name, weak check
Named "exists" but only checks non-empty string (file existence is checked later in `health.check_gcs_key`). Also its error message says "when cloud is enabled" but the validator runs unconditionally.

### M2. `models/config.py:202-218` bucket regex duplicated
`valid_bucket` and `_bucket_required_when_enabled` repeat the same regex inline — drift risk when one is edited. Also the pattern forbids dots; real GCS bucket names allow them (conservative, but worth knowing).

### M3. `models/config.py:458-461` `from_yaml()` — no error handling
Missing file → raw traceback; empty YAML file → `cls(**None)` raises confusing `TypeError`. For an ops tool whose first failure symptom is usually "config problem," friendlier errors would pay off. Related global nit: `CONFIG_PATH = "config.yaml"` is CWD-relative everywhere (serve, launch, watchdog, flows) — safe only as long as every service sets working dir to project root.

### M4. `core/manifest.py` — semantic issues in the data layer
- `last_synced_at` fields actually store the **first** sync timestamp: both `upsert_file_entry` (L169-178) and `bulk_upsert_synced` (L239-243) preserve the old timestamp once status is `'synced'`. Name vs behavior mismatch.
- `upsert_file_entry` INSERT branch stamps `*_last_synced_at = now` even for non-synced statuses (e.g. `'failed'`) on first insert.
- `last_successful_run` (L492): `LIKE '%_COMPLETE'` — underscore is a LIKE wildcard; should be escaped or use `GLOB`.
- Constructor default `vacuum_freelist_threshold=1000` vs `MaintenanceConfig` default `10000` vs `flow.py` record-task defaults `10000` — three defaults that only agree by caller discipline. Report flows (`flow.py:750,770`) and UI (`ui.py:170`) construct `ManifestDB` with none, silently using 1000.
- Scale: `get_cloud_synced_entries` / snapshots load whole inventories (1M–2.5M files) into RAM — acknowledged tradeoff, but worth tracking.

### M5. Mixed mtime types in DB
Cloud entries store rclone lsjson ISO-8601 strings in `mtime REAL`; LAN entries store epoch floats. `flow.py:496-506` heroically handles both at comparison time, but any future consumer comparing across modes will misbehave. Schema-level inconsistency.

### [FIXED 2026-08-23] M6. `core/cloud_reporter.py` — error states collapse into "zero"
- `get_cloud_size`: on rclone failure it logs a warning, then still parses empty stdout → returns `{count: 0, bytes: 0, _error: ...}`. Callers (`flow.py:166,513`) ignore `_error` entirely → failed queries are recorded in `extended_metrics` as real totals of 0 files / 0 GB.
- `get_cloud_diff`: timeout/OSError returns a clean all-empty diff **without** `_partial` or `_error` — a timed-out scan is indistinguishable from "no changes."
- Neither catches `FileNotFoundError` (missing binary), so they raise where sibling functions return error dicts — inconsistent contract.

### [FIXED 2026-08-23] M7. `core/cloud_sync.py:66` `build_rclone_sync_command()` — hardcodes `"rclone"`
Preflight, verify, size, manifest, and diff all use `resolve_binary("rclone") or "rclone"` (bundled-binary-first); the sync command does not. If rclone ships only in `deploy/bin`, preflight passes and sync fails with "rclone not found."

### M8. `core/health.py:39-40` vs `core/cloud_preflight.py:73-75` — contradictory empty-drive semantics
Health check **fails** on an empty source drive; cloud preflight explicitly treats it as valid ("first run OK"). Whichever runs first wins; the pipeline's stated support for first-run-empty is blocked by `pre_backup_health`.

### M9. `core/fy_rollover.py` — ordering/state hazards
- `run_archive_transition` runs **before** folder creation and config update (L450-460). If source-folder creation then raises (L209), the closing FY is archived while config still points at it; subsequent retries re-run final backups into an ARCHIVE-tier prefix and re-attempt archiving.
- L379 FileNotFoundError message tells operators to "place nssm.exe in deploy\bin\" — copy-paste artifact; nssm has nothing to do with gcloud. Misleading recovery instructions in exactly the situation an operator is troubleshooting.
- `run_final_backup` treats `LAN_PARTIAL` as success for rollover gating (L174) — partial data gets archived/config-switched. Deliberate? Worth a comment or a stricter gate.
- Untyped parameters (`lan_config, cloud_config, paths_config, config`) plus redundant `config` alongside sub-configs.

### [FIXED 2026-08-23] M10. `deploy/test_config.py:37-45` — always exits 0
Validation failure prints `[ERROR]` but never `sys.exit(1)`, so batch scripts checking `ERRORLEVEL` treat invalid configs as valid. (Missing-file path does exit 1.)

### M11. `core/logging.py:90-109` `prefect_sink()` — dropped lines are nearly invisible
If forwarding fails, the message is re-logged at DEBUG into loguru only, and the sink is registered at INFO — so the drop note never reaches the Prefect console and only lands in the file log. Silent log gaps during Prefect-context hiccups. (No recursion risk only because the fallback is DEBUG < sink level — fragile-by-luck; deserves a comment.)

---

## LOW

### models/config.py
- L147: `if v and v != ""` — redundant second condition.
- L139-140, 150: `raise ValueError(...)` inside `except` without `from e` (loses chain).
- L43: `.endswith(".db")` case-sensitive.
- L258-264: `__repr__` masks password, but any `model_dump()` elsewhere would not — keep an eye on future logging.
- L387: importing `prefect.server...` at config-load time couples config validation to Prefect internals (~1.8 s import, API-breakage risk on upgrades). Documented, accepted — still brittle.

### core/time_utils.py
- `cron_to_human` (L100, 105): `int(hour)` crashes on step/range crons (`*/30`, `8-18`) that pass validation; `(4 <= d <= 20) or d in (11, 13)` — the `or` clause is dead (11, 13 ⊂ [4, 20]).

### core/hashing.py
- `hasattr(file_digest)` fallback is dead code on the pinned Python 3.12.
- `verify_checksum` raises on missing files rather than returning False.

### core/process.py
- `write_lock` is replace-not-test-and-set: TOCTOU window between `read_lock_alive` and write allows two simultaneous flows to both "acquire" (mitigated in practice by the Prefect concurrency slot).
- Legacy bare-PID locks are PID-reuse-vulnerable by design (documented as legacy).

### core/wol.py
- `ensure_server_online` docstring says "Returns WolTimeout" (it raises); return value is always True (pointless bool).
- Global-broadcast send logs at DEBUG while subnet send logs at INFO (asymmetric).

### core/shutdown.py
- On `TimeoutExpired` the shutdown may actually have been delivered remotely, but the result reports failure → caller may retry (mostly idempotent, minor).

### core/cloud_sync.py
- stderr tail (stats every 60 s + JSON logs over hours) is stored unbounded into `run_history.error_message`.
- `location="asia-south1"` default duplicated in model + preflight + sync (DRY).

### core/lan_sync.py
- Docstring says `anomaly_details` up to "5KB" but `_ANOMALY_LOG_TAIL` is 100KB.
- `/LOG:` writes ANSI — non-ASCII filenames become U+FFFD mojibake on read-back (`/UNILOG` would fix).
- `count_failed_lines` depends on literal English `"** FAILED:"` — breaks on localized Windows.

### core/lan_preflight.py
- Mixed error styles: raises `HealthError` for canary, returns dicts otherwise.
- The /L dry-run omits `/NFL /NDL` although nothing ever reads stdout — at 1M+ files it streams hundreds of MB into memory just to be discarded (real perf/memory concern at target scale).

### core/rclone_config.py
- Newline injection into the temp rclone config is possible from unvalidated free-text fields (`location`, `project_number`); low risk given config validation, zero sanitization though.

### core/report.py
- `failures` computed as residual (`total − successes − no_changes − partials − skipped`), so any new/unexpected status inflates failures.
- `send_summary_report` double-queries `get_runs_since` (once itself, once inside `generate_report_html`) — results can diverge between HTML body and CSV attachment.
- CSV lacks UTF-8 BOM → Excel mojibake for non-ASCII.
- `attachments: list[dict] = None` type hint should be Optional.

### flow.py
- `cloud_record_task` takes `sync_result` but never uses it (dead param).
- LAN diff computed twice (`lan_record_task` + `_run_lan_pipeline:599`).
- Verify-failure alert + summary alert + partial alert can produce duplicate emails per night.
- A bookkeeping exception during the verify phase mislabels status as `CLOUD_VERIFY_FAILED` even though integrity was fine.
- `wol_check_task` has no retry (a single WoL flake fails the run).
- Empty-manifest success case (bucket truly emptied) never updates/prunes DB.
- Final `except Exception: raise` at L943-944 is a no-op.

### launch.py
- Tag-based concurrency-limit creation targets a mechanism flow.py doesn't use (global limit only); its failure is swallowed.
- Dashboard thread crash (e.g. bad static dir) leaves launch running scheduler-only with just a print.
- `_check_prefect_api` swallows everything.

### serve.py
- Docstring "Registers four deployments" vs five registered.

### ui.py
- Relative `static`/`templates` dirs (CWD-dependent).
- Sessions/rate-limits unbounded per unique IP (pruned lazily).
- Logout via GET.
- `/status` masks all DB errors as "ManifestDB not found".
- Trigger check-then-create race can queue duplicate runs (concurrency slot makes it safe, just wasteful).

### collect_config_data.py
- `import subprocess` mid-file (L46).
- `"lo" in interface_name` loopback filter can false-positive on unusual adapter names.
- Firewall probe spawns an unbounded, notoriously slow PowerShell pipeline with no timeout.

### watchdog.py
- `_resolve_paths` silently falls back to hardcoded paths if config is unreadable at service start (watches wrong lock file with zero log output).
- `_pid_is_alive` is dead code.
- `sc query STATE` parsing is locale-dependent.
- `_start_allowed` burns breaker budget even when `sc start` fails.

### deploy/read_config.py
- Fallback parser never exits parent scope (matches keys under later unrelated sections).
- Inline-comment stripping corrupts values containing `#`.
- `read_section` returns `{}` both for "missing" and "PyYAML unavailable".

---

## Notable Strengths

- Exit-code classification (rclone bitmask, robocopy bitmask) matches vendor docs and is unit-documented.
- Atomic config update (`fy_rollover.update_config_yaml`), atomic lock write (`process.write_lock`), PID + create-time lock validation — correct patterns.
- CSV-injection neutralization, consistent `html.escape`, masked secrets in reprs.
- FY-mismatch data-loss guard at config load is genuinely good defensive design.

---

## Fix Log

All Tier-1 items were implemented on 2026-08-23 with RED-to-GREEN test evidence
(see IMPLEMENTATION_PLAN.md):

| Finding | Fix | Tests |
|---------|-----|-------|
| H1 | `launch.py` now serves all five deployments incl. rollover-check | `test_launch.py::TestServeCallCompleteness` |
| H2 | `walk_lan_destination`: root failure raises OSError; subtree failures warn | `test_lan_manifest.py::TestWalkFailureSemantics` |
| M6 | Reporters never fake zeros: `CloudReporterError`, `_error`/`_partial` flags; pipeline skips DB write on degraded manifest and stamps `manifest_ok`/`size_ok`/`diff_partial` into extended_metrics | `test_cloud_reporter*.py`, `test_flow_orchestration.py::TestCloud{VerifyAndReportDegradation,PipelineSkipsRecordOnManifestError}` |
| M7 | `build_rclone_sync_command` resolves binary via `resolve_binary()` like all sibling modules | `TestBinaryResolution` in both cloud_sync suites |
| M10 | `deploy/test_config.py` exits 1 on invalid config; stdin-safe pause | `tests/test_deploy_test_config.py` |

Full suite at fix time: **1442 passed / 0 failed / 53 skipped** (skips are pre-existing
hardware-gated e2e tests).

### Deploy-risk round — 2026-08-24

Four deployment-blocking findings fixed with RED→GREEN evidence
(see IMPLEMENTATION_PLAN_DEPLOY_RISKS.md v1.1):

| Finding | Fix | Tests |
|---------|-----|-------|
| H4 | Watchdog: independent `transfer_deferrals`/`lock_deferrals` counters; forced restart proceeds same-iteration (`elif` kills stale fall-through); zombie-process ownership noted in log | `test_h4_watchdog_counters.py` |
| REVIEW Critical-6 | Manifest: schema migration is version-gated (`PRAGMA user_version`), atomic (`BEGIN IMMEDIATE`), retries transient BUSY ×3, raises `ManifestSchemaError` instead of swallowing; broken connection never cached | `test_manifest_migration_c6.py` |
| M8 | Empty-source contract unified fail-closed (anti-mirror-wipe): actionable HealthError message; preflight's "empty is valid" comment replaced with honest warning. Config escape hatch deliberately NOT added (Option B). | `test_m8_empty_source_contract.py` |
| H3 | UI: typed Prefect filters (`FlowRunFilterState(Type)`), tri-state helper; trigger endpoints fail-CLOSED with HTTP 503 when Prefect unreachable; `/status` keeps strict booleans + adds `run_state`; `limit=200` | `test_h3_ui_failclosed.py` |

Post-fix suite: **1505 passed / 0 failures attributable to this diff / 202 skipped**
(skip growth 53→202 is the Aug-23 `AAM_RUN_REAL_HARDWARE`-gated scenario catalog,
not a regression).

## Priority Shortlist

If you act on only four things:

| # | Finding | Why |
|---|---------|-----|
| 1 | **H1** — rollover deployment never served | Silent fiscal-year rollover failure on 24x7 hosts |
| 2 | **H2** — silent empty LAN walk | Corrupts diff metrics; masks NAS outages |
| 3 | **M10** — config tester always exits 0 | Automation can't detect invalid config |
| 4 | **M6/M7** — cloud metrics lie / bundled-rclone gap | Failed queries recorded as real zeros; preflight/sync binary resolution mismatch |
