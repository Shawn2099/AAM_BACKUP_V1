# HANDOVER — AAM Backup Automation audit lab

**Date:** 2026-10-10 · **Repo:** `C:\Users\malliath\Desktop\aam_backup` · **Audience:** a new session/agent picking this up cold.

> Read this first. It is the map. The detail lives in `docs/audit/LAB_VERIFICATION_REPORT_2026-10-10.md` (the findings) and `audit_lab/LEDGER.md` (the evidence). A ready-to-paste prompt is in `HANDOVER_PROMPT.md`.

---

## 1. What this repo is

**AAM Backup Automation V1** — a nightly backup system for an accounting firm. Two destination legs, orchestrated by Prefect 3:

| Leg | Engine | Destination |
|---|---|---|
| **cloud** | `rclone sync` | GCS bucket `aam-backup-demo-innovizta`, prefix derived from the fiscal year |
| **lan** | `robocopy /MIR` | `\\127.0.0.1\C$\BackupAgent\mock_lan\FY26-27` (loopback SMB rig) |

Production entry points: `flow.py` (Prefect flow + `@task`s), `serve.py` (6 deployments), `launch.py` (agent + dashboard + concurrency limit + orphan cleanup), `ui.py` (FastAPI dashboard), `watchdog.py` (Prefect server health monitor). Business logic in `core/*.py`. Config: `config.yaml` at repo root.

## 2. What `audit_lab/` is (my addition)

A **hypothesis-based audit lab**: 28 test files, ~258 tests, that execute the **shipped production code** against a fully isolated rig — mock source, loopback SMB `\\127.0.0.1\C$` as the NAS, an rclone **local** backend as the fake GCS bucket (config section `[aam_gcs]`, the name the production code hardcodes). **Zero network, zero cost, nothing real is touched.**

Safety rules enforced in code by `audit_lab/labconf.py::assert_lab_path`:
- every destructive/filesystem operation is confined to `%TEMP%\opencode\aam_lab`
- `build_config()` never reads the repo `config.yaml`
- WoL and shutdown are force-disabled *and* monkeypatched to raise if ever called
- `/ZB` is stripped from robocopy (this account has no `SeBackupPrivilege` — proven, see C-2)

Every experiment appends a verdict to `audit_lab/raw/evidence.jsonl`; `make_ledger.py` renders `LEDGER.md` from it.

## 3. Wave map

| Wave | File | What it establishes |
|---|---|---|
| 0 | `test_w0_enabler.py` | the real cloud pipeline runs on the local backend |
| 1a–1d | purge / ACL / max-path / exit-16 / kill-with-exit-0 | **findings C-1, C-2, D2** |
| 1e | `test_w1e_bugs.py` | suspected dead code + misreported status — **BUG-1, BUG-2** |
| 1f | `test_w1f_props.py` | Hypothesis property tests + parser forgery (pure functions) |
| 2 | `test_w2_scale.py`, `test_w2b_costs.py` | memory/IO cost at 100k–200k objects — **R5** |
| 4 | `test_w4_pipeline.py` | full pipelines end-to-end; kill mid-sync → gate rejects |
| 5a–5c | paths / during-run scenarios / ops failure modes | junctions, locked files, mirror race, canary, source loss |
| 6 | `test_w6_flow.py` | the **real Prefect flow layer**: `flow.backup()`, retries, rollover, deployments |
| 7 | `test_w7_deployed.py` | the **deployed** box (needs services running — currently blocked) |
| 8 | `test_w8_prefect.py` | **deployment layer**: registration, dashboard trigger, fail-closed, orphan cleanup, concurrency serialisation, report flows |
| 9 | `test_w9_stress.py` | Hypothesis over real I/O: random trees, fault injection, metamorphic mutations, DB concurrency (**M-6**) |
| 10 | `test_w10_frameworks.py` | `cronsim` schedule verification, `hypothesis.stateful` sequences, `sys.monitoring` coverage map |
| 11 | `test_w11_wrapper.py` | **wrapper correctness**: real-log corpus replay, audit-engine branches, round-trip parser canary |
| 12 | `test_w12_clock.py` | **fake clock** (`audit_lab/labclock.py`): FY boundary, rollover flow, **M-4 reproduced**, clock-skew branches, retention purge, 7-day cycle |
| 13 | `test_w13_branches.py` | **branch-exhaustive** wrapper tests (parsers, command contracts, `audit_lan`, health, rollover helpers, manifest, reporter) |
| 14 | `test_w14_ui.py` | **dashboard + reports**: `ui.py` endpoints, auth/rate limits, `_prefect_has_active_run` tri-state, `core/report.py` CSV/HTML/SMTP branches |
| 16 | `test_w16_flow_first.py` | **flow-first property stress**: every scenario through the production entry point `flow.backup()`; covers `core/hashing.py`, `core/shutdown.py`, `core/logging.py`, `core/backup_repository.py`, `collect_config_data.py`, `watchdog.py`; cronsim schedule-collision sweep. (Wave 15 was never created — the numbering jumps 14 → 16.) |
| 17 | `test_w17_ops.py` | **coverage-driven ops layer (1/2)** — `core/wol.py` + `watchdog.py`: SCM state parsing, start/stop/breaker, wedged-lock alert, magic-packet contract, SMB probe, and 9 `main()`-loop scenarios (healthy / agent-stopped / breaker / transfer deferral / transfer caps / lock-only cap / hung restart) |
| 18 | `test_w18_ops.py` | **coverage-driven ops layer (2/2)** — `launch.py` (`main()` boot order, SystemExit path, dashboard thread, API wait, concurrency limits, orphan cancellation with pagination + live-lock guard, leg reconciliation), `collect_config_data.py`, and the `cloud_preflight`/`lan_preflight` dry-run branches |
| 19 | `test_w19_ui.py` | **dashboard branch sweep** — login/logout/sessions/rate limits, `_require_auth` (303 vs 401), config-refresh DB-path change, `/status` error truncation + 503, all three trigger endpoints (disabled/unreachable/already-running/success/failure), `_trigger_result` states, `_trigger_timeout`, the two Prefect-query helpers' full branch sets, `/health`, both email-report endpoints, `_integrity_summary` honesty |
| 20 | `test_w20_report.py` | **reporting** — SMTP policy (config guards, 465 SSL vs starttls, permanent-error no-retry, transient backoff + give-up), `send_failure_alert` body/attachment/escaping/exit-0, `generate_report_html` status variants + row limit + audit rows, CSV injection guard, weekly/monthly delegation |

## 4. Current findings (full detail in the report §2)

### CRITICAL
- **C-1** — An empty *or dirs-only* source wipes the destination and reports success. `check_source_drive()` only needs ≥1 **entry** in the source root, so an empty subdirectory passes. Cloud: all objects deleted, exit 9 → `CLOUD_NO_CHANGES_COMPLETE` (counted as success by `last_successful_run()`). LAN: 12→0 files, exit 2 → `LAN_COMPLETE`. **The destination canary does not protect against this** — it lives on the destination and is excluded from the mirror by `/XF`, so it survives the purge and the next preflight still passes.
- **C-2** — `/ZB` hard-fails (exit 16) without `SeBackupPrivilege`, copying nothing. `AamBackupAgent` logs on as `.\innovizta_2`, which does not hold it.

### HIGH
- **H-1** — The daily `--size-only` gate cannot see same-size content corruption; the dashboard labels it "Verification Passed". The weekly hash audit *does* catch it but **never repairs** it, and rclone sync won't re-copy it either (size+mtime match) — so the corruption is permanent until the source file changes. `integrity_audits = 0` rows on the deployed box: **the weekly audit has never run there.**
- **H-2** — `watchdog._alert_wedged_lock()` imports `core.notifications`, which does not exist (the real module is `core.report`). The `ModuleNotFoundError` is swallowed and logged at DEBUG, so the CRITICAL wedged-lock alert never reaches the operator.

### MEDIUM
- **M-1** — A verify-gate failure pages twice (in-pipeline gate alert + `backup()`'s generic alert; only `PartialRun` suppresses the duplicate).
- **M-2** — A **post-phase** exception leaves `run_history = LAN_COMPLETE` while the flow FAILS — the `except` handlers in both pipelines only rewrite `status` for `phase` `sync`/`pre`/`verify`, never `post`.
- **M-3** — At 1M objects the cloud leg holds ~1 GB and writes ~102 MB/night into the Prefect result store (~37 GB/year). RAM is a non-issue on this 80 GB box; disk is the constraint.
- **M-4** — Cloud prefix is calendar-derived, LAN prefix comes from config; only source↔LAN is cross-validated at config load. A failed April-1 rollover silently splits the FY trees.
- **M-5** — The repo's own committed suite is red: 34 failed / 1632 passed / 19 of them code-level (`ui._trigger_result` etc. don't exist).
- **M-6 (new, from Wave 9)** — Concurrent `ManifestDB` opens **silently lose run-history rows** (4 of 12 stress rounds lost rows; one stored 45 of 60). `PRAGMA busy_timeout` is applied at `core/manifest.py:162` **after** the legacy dedup DELETE+commit (150-158), so `executescript(DDL)` at 163 runs with SQLite's default 0 ms timeout. `record_run_history()` swallows the resulting `OperationalError` into `return False`.

### LOW
- **L-1** a locked source file is purged from the destination rather than retaining the old copy (exit 11 → only `LAN_PARTIAL`) · **L-2** a folder renamed mid-mirror ends with 0 destination copies for one cycle · **L-3** concurrent DB *initialisation* raises `database is locked` (same root cause as M-6) · **L-4** `_clean_path()` is not idempotent · **L-5** the WoL/SMB probe is a bare TCP connect, so a hung NAS that accepts connections reads as "online".
- **W16.schedule_collision (LOW, new)** — whenever the 1st of the month is a **Monday**, `weekly-report` and `monthly-report` start in the same minute (next: **2027-02-01 08:00 IST**; then 2027-03-01, 2027-11-01). Both report flows run concurrently: two emails land in the same minute and the flows contend for the shared concurrency slot / manifest DB reads. Red-by-design in `test_w16_schedule_collisions_across_year`.

Design note recorded but not a defect: `check_clock_skew()` **fails open** — if Google is unreachable the health check passes, so clock skew (which breaks GCS JWT auth) is invisible exactly when the network is flapping.

## 5. Blocker: the deployed box cannot start

`AamPrefectServer` is RUNNING (LocalSystem), but **`AamBackupAgent` and `AamWatchdog` fail with error 1069 — "The service did not start due to a logon failure"**: NSSM's stored password for `.\innovizta_2` is invalid. There is no Prefect worker and no dashboard, so no backups run at all and **Wave 7 cannot pass**.

Remediation (Administrator PowerShell — it will prompt for the account password):
```powershell
.\audit_lab\setup_service_account.ps1 -AccountName ".\innovizta_2" -Apply
```
That also grants `SeBackupPrivilege` (clearing C-2). Then:
```powershell
.\.venv\Scripts\python.exe -m pytest audit_lab/test_w7_deployed.py -q -s --timeout=1800
```

Live state as verified: source `C:\BackupAgent\mock_source\FY26-27` = **0 entries** (so the M8 guard currently blocks every run — the only thing preventing C-1); LAN destination holds **only the canary**; `run_history` = 3 rows, all `…_FAILED` with "Source drive appears empty"; `integrity_audits` = 0.

## 6. How to run things

```powershell
.\.venv\Scripts\python.exe -m pytest audit_lab --ignore=audit_lab/test_w7_deployed.py -q -s --timeout=900   # ~15 min, waves 0-6 + 8-13
.\.venv\Scripts\python.exe -m pytest audit_lab/test_w12_clock.py -q -s                                 # fake clock, ~75s
.\.venv\Scripts\python.exe -m pytest audit_lab/test_w13_branches.py -q -s                               # branch tests, ~20s
.\.venv\Scripts\python.exe audit_lab\make_ledger.py                                                      # regenerate LEDGER.md
```

Coverage workflow (coverage.py 7.16.2 is installed into the venv; plan + gap map in `docs/audit/TEST_PLAN_2026-10-13_wave-17.md`):

```powershell
.\.venv\Scripts\python.exe -m coverage run --branch --source=core,models,flow,ui,launch,watchdog,collect_config_data -m pytest audit_lab --ignore=audit_lab/test_w7_deployed.py -q -s --timeout=900 -p no:cacheprovider
.\.venv\Scripts\python.exe -m coverage report -m                       # totals + missing lines
.\.venv\Scripts\python.exe -m coverage json -o audit_lab\raw\cov_vN.json
.\.venv\Scripts\python.exe audit_lab\gap_map.py                       # uncovered lines -> enclosing functions
```

Use detached runs for anything long — a foreground shell gets killed at 10 minutes:
```powershell
$p = Start-Process ".venv\Scripts\python.exe" -ArgumentList "-m","pytest","audit_lab","-q","-s","--timeout=1800","-p","no:cacheprovider" `
     -RedirectStandardOutput audit_lab\raw\run.log -RedirectStandardError audit_lab\raw\run.err -PassThru -WindowStyle Hidden
```

Known-good results: waves 0-6 + 8-11 → **59 passed / 2 skipped / 0 failing** (the only intentional red is `test_w9_manifest_db_concurrency_stress`, which fails loudly to report M-6).

After the 2026-10-10 conversion work (waves 12-13 added): **101 passed / 2 skipped / 3 failing**, and every failure is an intended signal — `test_w9_manifest_db_concurrency_stress` (M-6), `test_w1e_bugs.py::test_w1e_two_processes_concurrent_db_writes` (the M-6 root cause, now enforced: the traceback pins `core/manifest.py:163 executescript(DDL)`), and `test_w13_branches.py::test_w13_bool_exit_code_is_rejected_by_both_deciders` (new finding). No "all-green" sanitising: a red means we broke somewhere.

## 7. Lab bugs that were already fixed (do not re-introduce)

The lab itself had defects that made tests silently pass. If a wave looks green, check these first:
- **Hypothesis + function-scoped pytest fixtures share one sandbox across all examples** — use `_example_root(sandbox)` for per-example isolation (`test_w9_stress.py`). Without it, state (e.g. a junction) leaks between examples.
- `tree()` must not generate a file path that is also a directory path.
- Every rig must create the **calendar-derived** cloud bucket prefix (`get_fy_prefix()`) and a non-empty `lab-key.json`, or preflight fails with "directory not found" / "GCS key file not found".
- `_CloudRig.dest` is `<bucket_root>/<bucket-name>/<prefix>` — dropping the bucket segment silently inverts the results.
- `serve.deployments()` resolves `config.yaml` relative to **CWD** — `chdir` to the repo root before calling it.
- Don't monkeypatch a `@task` object with a plain lambda: use `monkeypatch.setattr(task, "fn", ...)` or `with_options` breaks.
- `_clean_path`/`tree_map` must not follow reparse points (junctions) — robocopy's `/XJ` excludes them by design, so measuring them makes a correct system look broken.
- **`flow.backup()` calls `cleanup_orphaned_robocopy_logs(24)`**, which deletes real `%TEMP%\robocopy_sync_*.log` — i.e. the W11 evidence corpus. Any wave that drives `flow.backup()` must monkeypatch that one function (W12 does).
- **A faked clock does not move Prefect's own clock.** Only `core/time_utils.py` and `core/health.py` have their `pendulum` replaced, so Prefect's harness DB still writes real timestamps — which is why ~11 flow runs in one harness intermittently trip Prefect's own `UNIQUE(task_run_id, timestamp)` constraint. `labclock.fake_clock(tick=...)` + a spacing sleep + one artefact-specific retry keeps Wave 12 stable.
- **A mechanical edit can silently drop a flow call.** The lab's own weekly audit caught the 7-day cycle skipping its nightly `flow.backup()` and reported full divergence — which is precisely what it is for. Always read back the diff.
- **`launch.py` hard-sets `os.environ["PREFECT_API_URL"]="http://127.0.0.1:4200/api"` at import time** (that is what the production service needs). Any rig that imports `launch` therefore poisons the env for SUBPROCESS-based tests — W8.5's children got the dead 4200 and failed with "Failed to reach API". Resolve the harness URL from `PREFECT_API_URL.value()` (the harness pins the setting at higher precedence than env) and pass *that* to children — never `os.environ`. That test previously "passed" only because a real Prefect server happened to listen on 4200 during one run.
- **`core/logging.py`'s file sink uses `enqueue=True`** — its writer thread holds the log file open, so on Windows a `TemporaryDirectory` containing the log cannot be cleaned up (WinError 32). `logger.remove()` + `logger.complete()` before exiting the temp dir (see `_drop_loguru_sinks()` in W16).
- **The source-loss status is timing-dependent** (`test_w5c_source_disappears_mid_run`): identical production code reported `LAN_PARTIAL` (fail-closed) in one run and `LAN_COMPLETE` (mirror finished before the rename landed) in the next. Assert the invariant — destination never wiped to zero — and record the status as evidence instead of asserting a specific value.
- **`labconf.build_config()` runs many times per session** — anything it stores on a module (e.g. `core.shutdown._LAB_ORIG_SHUTDOWN`) must be guarded with `hasattr`, or the second call stores the guard lambda as the "original".

## 8. Suggested next steps (cheapest first)

1. Convert the ~10 assert-free load-bearing tests into real assertions (today "80 tests pass" overstates enforcement). **DONE** — 21 tests converted (2026-10-10; see `docs/audit/TEST_PLAN_2026-10-10_waves-12-13.md`), which also fixed a vacuous test in `test_w1bd.py` (it ran with `/ZB`, so the kill never happened).
2. Wave 12: fake clock — FY rollover boundary (1 April 00:00 IST), clock skew, retention purge, a simulated 7-day cycle. **DONE** (7/7).
3. Branch-exhaustive tests for the under-covered wrapper functions. **DONE** as Wave 13 (26 experiments).
4. **Wave 14: `ui.py` + `core/report.py`** — dashboard render, trigger endpoints, CSV/HTML report generation. **DONE** (2026-10-11/12; `test_w14_ui.py`).
5. **Wave 16: flow-first property stress** — every scenario through `flow.backup()`; covered `core/hashing.py`, `core/shutdown.py`, `core/logging.py`, `core/backup_repository.py`, `collect_config_data.py`, `watchdog.py`. **DONE** (2026-10-12; `test_w16_flow_first.py`; found **W16.schedule_collision**, §4).
6. Wave 7 after the 1069 fix — the only thing that actually de-risks production.
7. **Wave 19 (next coverage target): `ui.py` + `core/report.py` + the `flow.py` rollover/report blocks** — the gap map after waves 17–18 ranks `ui.py` highest (`_is_verify_running` 28 lines, `trigger_monthly_email` 19, `login_submit` 18, `trigger_verify` 15); plan is in `docs/audit/TEST_PLAN_2026-10-13_wave-17.md`.
8. Commit the lab for durability (§9), then the fixes, in order: 1069 → C-2 → C-1 → M-2 → H-2/M-1 → M-6 → H-1 → M-4 → W16.schedule_collision.

**New findings from the conversion work (§4 additions):**

- **`W13.exit_code_bool`** — `decide_lan_result(True, log)` returns **`LAN_COMPLETE`** (a bool is an `int` subclass, so `isinstance(code, int)` is not enough; `True & 16 == 0` and `0 <= True <= 7`). `core/cloud_verify.py` already rejects bools explicitly; `core/lan_sync.py` does not. Latent, untriggered today. **Test is red by design.**
- **`W5c.canary_alert_delegation`** — a pre-flight failure inside `_run_lan_pipeline()` raises `LAN_FAILED` but sends **no alert of its own** (`flow.py:947-957` only alerts for `LAN_SUSPECT`); the operator is paged only by `backup()`'s generic path, so a direct/manual pipeline call is silent.

**Current baseline (2026-10-13 — full suite against production as merged from `origin/production`, with waves 17–18):** `190 passed / 3 skipped / 3 failed`. All three reds are intentional: `test_w9_manifest_db_concurrency_stress` (**M-6**), `test_w1e_two_processes_concurrent_db_writes` (the M-6 root cause), `test_w16_schedule_collisions_across_year` (**W16.schedule_collision**, §4). (`test_w13_bool_exit_code_is_rejected_by_both_deciders` and the `test_w12` seven-day flake passed in this run; both are timing/environment sensitive — re-check before trusting a green.)

**Coverage (coverage.py 7.16.2, branch mode, whole lab):** **81%** (was 71% before waves 17–18). Targeted modules: `core/wol.py` 32→86%, `collect_config_data.py` 37→95%, `watchdog.py` 41→92%, `launch.py` 46→92%, `core/cloud_preflight.py` 57→100%, `core/lan_preflight.py` 62→100%. Next targets by gap size: `ui.py` (64%), `core/report.py` (77%), `flow.py` (77%), `core/manifest.py` (75%) — see the plan doc.

## 9. Persistence / handoff

`audit_lab/` and this report are **not committed** (`git status` → `?? audit_lab/`). A new chat session has no memory of prior conversations — these files are the entire handoff. To make it durable:
```powershell
git add audit_lab docs/audit HANDOVER.md HANDOVER_PROMPT.md
git commit -m "audit lab: 80 experiments, 14 findings, handover docs"
```
