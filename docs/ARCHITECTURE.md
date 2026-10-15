# AAM Backup Automation — How the program works

**State described:** branch `mvp-hardening` @ M1 complete + M2 building blocks (2026-10-14).
Items marked **[built, not wired yet]** exist as tested modules but are not yet called by the
flow — they are wired in during M2/M3 (see `MVP_HARDENING_PLAN.md` §13).

Reading guide: §1 big picture → §2 schedules → §3 the nightly backup step by step →
§4 every decision point (where you tune logic) → §5 the other flows → §6 file-by-file map →
§7 data written → §8 tunables → §9 what changes next → **§10 gates: every check that must
pass for a backup to run, to count as successful, and to be verified.**

---

## 1. Big picture — processes on the Windows server

```
 ┌──────────────────────────── Windows Server 2016 ─────────────────────────────┐
 │                                                                              │
 │  [Service] AamPrefectServer  (NSSM → "uv run prefect server start")          │
 │     Prefect API + UI on 127.0.0.1:4200, its own SQLite DB in PREFECT_HOME     │
 │     ▲  keeps schedules, flow-run states, task states, flow logs              │
 │     │ HTTP                                                                   │
 │  [Service] AamBackupAgent  (NSSM → "uv run python launch.py")                │
 │     launch.py ─┬─ thread: Dashboard  (ui.py, FastAPI/uvicorn, :8080)         │
 │                ├─ boot: FY rollover check, concurrency limit, orphan cleanup │
 │                └─ main: prefect serve(deployments from serve.py)             │
 │                        │ at each cron time Prefect starts a SUBPROCESS:       │
 │                        ▼                                                      │
 │                 flow.py  backup / weekly / monthly / rollover / audit flows  │
 │                        │ calls                                                │
 │                        ▼                                                      │
 │                 core/*.py  → robocopy.exe, rclone.exe, shutdown.exe, SMTP     │
 │                                                                              │
 │  [Service] AamWatchdog  (NSSM → "uv run python watchdog.py")                 │
 │     polls Prefect /api/health every 60 s; restarts services; honours lock    │
 │                                                                              │
 │  Runtime data (C:\BackupAgent = paths.runtime_dir):                          │
 │     manifest.db  backup.lock  logs\  run_logs\<day>\<run_id>\  .prefect\     │
 └──────────────────────────────────────────────────────────────────────────────┘
        │ robocopy /MIR (SMB 445)                 │ rclone sync (HTTPS)
        ▼                                         ▼
   NAS  \\<nas>\share\FY26-27               GCS  gs://<bucket>/FY26-27/
   (woken by WoL, shut down after)          (versioned bucket + lifecycle)
```

## 2. Schedules (config `schedule.*`, timezone `schedule.timezone`)

| Deployment (serve.py) | Flow function (flow.py) | Default cron | What it does |
|---|---|---|---|
| `aam-backup/backup-cloud` | `backup(mode="cloud")` | `0 18 * * *` | rclone sync to GCS + size check |
| `aam-backup/backup-lan` | `backup(mode="lan")` | `0 1 * * *` | WoL → robocopy /MIR to NAS → shutdown |
| `weekly-report/weekly-report` | `weekly_report_flow` | `0 8 * * MON` | email 7-day summary + CSV |
| `monthly-report/monthly-report` | `monthly_report_flow` | `0 8 1 * *` | email 30-day summary + CSV |
| `rollover-check/rollover-check` | `rollover_check_flow` | `0 6 * * *` | **log retention** + FY rollover on April 1 |
| `integrity-audit/integrity-audit` | `integrity_audit_flow(mode="all")` | `0 3 * * SUN` | hash-level source↔backup audit |

All backup-type work (cloud, LAN, audit) takes ONE global slot: Prefect concurrency limit
`aam-backup` (limit 1, created by `launch._ensure_concurrency_limit`) + the PID file
`backup.lock`. Waiting longer than `maintenance.concurrency_wait_seconds` fails the run.

---

## 3. The nightly backup, step by step

### 3.1 Entry: `flow.backup(config_path, mode)`

```
backup(mode)
 ├─ load_config()                      models/config.py   (strict: unknown key = error)
 ├─ configure_logging()                core/logging.py    → logs\backup_YYYY-MM-DD.log
 ├─ configure_prefect_bridge()         core/logging.py    → loguru lines also in Prefect UI
 ├─ disabled-leg guard                 mode=cloud/lan but leg disabled → ValueError (FAILED)
 ├─ cleanup_orphaned_robocopy_logs()   core/lan_sync.py   (legacy %TEMP% logs, 24h)
 ├─ if cloud enabled: _run_cloud_pipeline()   ── §3.2   (exceptions collected)
 ├─ if lan   enabled: _run_lan_pipeline()     ── §3.3   (runs even if cloud failed)
 ├─ any errors? → send_failure_alert() (skipped for PartialRun: already alerted)
 │               → raise ExceptionGroup  ⇒ Prefect state FAILED
 └─ no errors  → ManifestDB.purge_old_runs(maintenance.db_retention_days)  ⇒ COMPLETED
```

### 3.2 Cloud pipeline: `flow._run_cloud_pipeline()`

```
with _backup_slot(config):                     concurrency slot + backup.lock (acquire_lock)
│
├─ before_dict = ManifestDB.get_cloud_synced_entries()      (for "files copied" metric)
├─ artefacts   = _open_run_artefacts()  → run_logs\<day>\<run_id>\   (full logs folder)
│
├─ phase "pre"
│   ├─ health_check_task   → core/health.pre_backup_health()
│   │      source exists & has a top-level entry · rclone binary · GCS key file ·
│   │      clock vs Google, else w32tm → OK | SKEWED (fail) | UNVERIFIED (policy warn|block)
│   └─ cloud_preflight_task (1 retry, 30 s) → core/cloud_preflight.run_cloud_dry_run()
│          Probe A: source readable · Probe B: rclone lsjson --max-depth 0 (auth/bucket/net)
│
├─ phase "sync"
│   └─ cloud_sync_task (retries = cloud.max_attempts-1, delay cloud.retry_delay_seconds)
│        → core/cloud_sync.run_cloud_sync()
│            build_rclone_sync_command()  (refuses an "appears empty" source)
│            core/tool_runner.run_tool("rclone-sync", … --log-file <artefact>)
│            exit code → classify_rclone_exit() → STATUS  (+ log scan, see D2)
│        task raises on CLOUD_FAILED / CLOUD_SUSPECT  ⇒ Prefect retries the task
│
├─ phase "verify"
│   └─ cloud_verify_and_report_task (1 retry, 60 s)
│        core/cloud_verify.verify_cloud_integrity()   rclone check --one-way --size-only
│        core/cloud_reporter.get_cloud_size()         rclone size
│        core/cloud_reporter.get_cloud_manifest()     rclone lsjson -R
│        core/cloud_reporter.get_cloud_diff()         rclone check --combined
│     EVIDENCE GATE (D3) fails → CLOUD_VERIFY_FAILED + alert + raise
│
├─ phase "post"
│   ├─ cloud_record_task → backup_repository.record_sync_results() → file_entries
│   ├─ files/bytes copied = manifest vs before_dict     (KNOWN WRONG, fixed in WP6)
│   ├─ cloud_publish_artifact_task (Prefect UI markdown)
│   ├─ CLOUD_PARTIAL → partial alert
│   └─ terminal policy: status ∉ success set → raise PartialRun
│
└─ finally: _finalize_run_artefacts() (gzip big logs) · _record_run() → run_history row
```

### 3.3 LAN pipeline: `flow._run_lan_pipeline()`

```
with _backup_slot(config):
│
├─ artefacts = _open_run_artefacts()
├─ phase "pre"
│   ├─ health_check_task  (source + robocopy binary)
│   ├─ wol_check_task     → core/wol.ensure_server_online()
│   │      already on 445? → done · else magic packet ×wake_retry_count,
│   │      poll 445 every ping_interval up to wake_timeout, then stability_wait
│   ├─ lan_preflight_task (1 retry) → core/lan_preflight.run_lan_dry_run()
│   │      SMB 445 probe (HARD-CODED 2 s × 4 ≈ 15 s — replaced in M2 2.5)
│   │      canary \\nas\…\.AAM_TARGET_MOUNTED must be a file
│   │      robocopy /L /MIR … (exit < 8 = ok)
│   └─ lan_snapshot_before_task → core/lan_manifest.walk_lan_destination()
│
├─ phase "sync"
│   └─ lan_sync_task (retries = lan.max_attempts-1, delay lan.retry_delay_seconds)
│        → core/lan_sync.run_lan_sync()
│            build_robocopy_command()  /MIR /Z /ZB /XJ /MT /R /W /V /TS /FP …
│                                      /XF .AAM_TARGET_MOUNTED  /XD SysVolInfo $RECYCLE.BIN
│            core/tool_runner.run_tool("robocopy", … /UNILOG:<artefact>)
│            tail = _read_log_tail(UTF-16, lan.log_tail_kb)
│            decide_lan_result(exit, tail) → STATUS  (D4)
│        task raises on LAN_FAILED / LAN_SUSPECT ⇒ Prefect retries
│
├─ phase "post"
│   ├─ lan_snapshot_after_task (walk failure → metrics skipped, run not failed)
│   ├─ lan_record_task → file_entries
│   ├─ files/bytes copied = snapshot diff
│   ├─ LAN_COMPLETE → lan_shutdown_task → core/shutdown.shutdown_from_config()
│   │                  (only if lan.shutdown_after_backup AND wol.enabled; WP10: exit
│   │                  0/1190 = accepted, then port 445 must stay closed within
│   │                  wol.shutdown_verify_timeout_seconds, slot still held →
│   │                  VERIFIED | NOT_VERIFIED | FAILED in extended_metrics.nas_shutdown;
│   │                  not VERIFIED = WARNING email + amber banner, status unchanged)
│   ├─ LAN_PARTIAL  → partial alert; shutdown only if shutdown_on_all_retries_exhausted
│   └─ terminal policy: not LAN_COMPLETE → raise PartialRun
│
└─ finally: finalize artefacts · _record_run() → run_history
   (exception path: SUSPECT alert; optional shutdown on failure)
```

---

## 4. Decision points — where outcomes are decided (tune here)

| ID | Decision | Code | Inputs | Output |
|---|---|---|---|---|
| D1 | Source looks empty? | `core/health.check_source_drive` | top-level entries minus system names | HealthError → `*_FAILED` |
| D2 | rclone result | `core/cloud_sync.classify_rclone_exit` + `scan_rclone_log_for_errors` | exit code + ERROR lines | 0 COMPLETE · 9 NO_CHANGES (FAILED if errors logged) · 4/5/10 PARTIAL · else FAILED · 0+errors → SUSPECT |
| D3 | Cloud evidence gate | `flow._run_cloud_pipeline` (`evidence_ok`) | verified + normal termination + diff has no missing/modified + manifest/size ok | fail → `CLOUD_VERIFY_FAILED` |
| D3a | Size check verdict | `core/cloud_verify.decide_cloud_verify_result` | exit + "N differences found" summary | verified / abnormal |
| D4 | robocopy result | `core/lan_sync.decide_lan_result` | exit bitmask + completed job summary in log | bit16 FAILED · no summary SUSPECT · bit8 PARTIAL · 0-7 COMPLETE (failed>0 → SUSPECT) |
| D5 | Terminal status → Prefect state | `flow` terminal policy + `core/statuses.py` | row status | success set → COMPLETED, else raise |
| D6 | Status meaning everywhere | `core/statuses.py` (`category/label/describe`) | status string | used by DB "last success", reports, dashboard |
| D7 | Integrity audit verdict | `core/integrity._run_rclone_check` | rclone check (hash) diff + summary | VERIFIED / VERIFICATION_FAILED |
| D8 | FY rollover needed? | `core/fy_rollover.detect_rollover` | FY leaf of paths vs date | rollover / no-op |
| D9 | Shutdown NAS? | `flow._run_lan_pipeline` | status + `lan.shutdown_*` + `wol.enabled` | shutdown / skip |
| D10 | Watchdog action | `watchdog.main` | API health, lock alive, rclone/robocopy running, SCM state, time in STOP_PENDING | wait / defer / start / ordered restart / kill stuck service (no lock only) |
| D11 | Alert sent? | `core/report.send_failure_alert` (today) → `core/notify.notify` **[built, not wired yet]** | `notifications.send_on_failure`, SMTP config, cooldown | SENT/FAILED/SUPPRESSED |

**M2 decision points.** G1–G4 are **wired** (2.2): tasks `lan-guards` / `cloud-guards` run
after preflight, before any transfer, with no Prefect retries. A measurement that cannot be
taken → `*_FAILED`; a guard verdict → `GuardBlocked` → `*_BLOCKED` row with
`extended_metrics.guards = {fy, source_file_count, destination, destination_files,
[max_delete], results}`. That record is also the next run's baseline:
`ManifestDB.guard_baseline(leg, fy)` = last **successful** run of the same leg **and FY**.
The others are built but not wired yet.

| ID | Guard | Module | Measured by | Blocks when |
|---|---|---|---|---|
| G1 | source has a real file | `core/guards.g1_source_has_files` + `first_regular_file` (+ `require_source_files` backstop in both command builders) | walk, junctions/system entries ignored | empty, folders-only or system-entries-only source |
| G2 | source shrink | `g2_source_shrink` | LAN: `robocopy /L` Files **Total**; cloud: `rclone size <source>` | file count dropped > `guards.max_source_shrink_percent` vs same-FY baseline |
| G3 | mass delete | `g3_mass_delete` | LAN: number of `*EXTRA File` lines of `robocopy /L` (not the Extras column, which also counts the kept mount marker; lines > column = measurement failure) vs pre-sync walk count; cloud: `"skipped":"delete"` lines of `rclone sync --dry-run` vs `rclone size` of the destination | planned deletes > `max_delete_files` or > `max_delete_percent`; cloud sync also gets `--max-delete` = `max_delete_allowance()` |
| G4 | destination identity | `g4_destination_identity` | config destination vs baseline; LAN host resolved vs local IPs | destination changed since same-FY baseline / LAN target is this machine (`forbid_local_destination`) |
| G5 | mount canary | `g5_canary` | `lan_preflight` reports `canary_present` (root marker is a regular file); only the read-only `/L` listing runs without it (verified to write nothing) | marker missing on a populated share / empty share to initialise / unknown; an approved G5 creates the marker before the mirror |
| A | approval valid? | `core/approvals.evaluate_request` (per guard: `evaluate_approval`) via `flow._apply_approval` | dashboard re-run passes `approval_id` | a blocked guard not covered, other leg, not approved, expired, used, dismissed, or numbers worse than approved (`approvals.tolerance_percent`); used up atomically (`mark_approval_used`) before any transfer |
| R | NAS ready | `core/smb_ready.wait_for_smb_ready` | 445 + real SMB stat + stability within `wol.*` timings |
| E | transport vs content | `core/rclone_errors.find_transport_failure` | TLS/DNS/AUTH/NETWORK/LISTING → "audit could not run" |

---

## 5. Other flows

### 5.1 `integrity_audit_flow(mode)` — weekly, read-only
```
load_config → ManifestDB.from_config
with _backup_slot:
  cloud: temp_rclone_config → integrity.audit_cloud()  rclone check (hash, no --size-only)
         → db.record_audit(); not VERIFIED → collect error
  lan:   wol_check_task → integrity.audit_lan() (rclone check local↔UNC, hash)
         → lan_shutdown_task (if configured) → db.record_audit()
  errors → send_failure_alert + raise ExceptionGroup
```

### 5.2 `rollover_check_flow` — daily 06:00
```
log_retention_task(config)          purge run_logs (run_logs.retention_days)
                                    + NSSM service logs (run_logs.service_log_retention_days)
fy_rollover.rollover()
  detect_rollover (FY leaf != date FY)  → no: return
  run_final_backup  (run_cloud_sync + run_lan_sync to OLD FY)  — NOT in the slot today (WP11)
  run_archive_transition (gcloud: old FY → ARCHIVE class)
  create_new_fy_folders (source + NAS + canary)
  update_config_yaml (atomic, ruamel keeps comments)
RolloverError → alert + FAILED
```
`launch.main()` also calls `rollover()` once at agent start (removed in WP11).

### 5.3 Reports — `weekly_report_flow` / `monthly_report_flow`
```
notifications.*_enabled? → core/report.send_weekly_report / send_monthly_report
   → generate_report_html (counts via core/statuses) + CSV of run_history → SMTP
```

### 5.4 Dashboard — `ui.py` (thread inside the agent)
```
GET  /            page (templates/dashboard.html + static/js/dashboard.js, polls /status every 2 s)
GET  /status      last runs (status_label/category/description from core/statuses),
                  last success, integrity (latest_audit), manifest counts, Prefect run state
GET  /health      unauthenticated (always "healthy" today — WP9)
POST /trigger/cloud | /trigger/lan | /trigger/verify/{mode}   → prefect arun_deployment
GET  /report/weekly|monthly          HTML download
POST /trigger/report/*/email         send now
auth: api_key → session cookie (dashboard.auth_enabled), rate limits in memory
```

### 5.5 Watchdog — `watchdog.py` (own service)
```
every watchdog.check_interval_seconds: GET watchdog.prefect_health_url
 healthy  → check AamBackupAgent state; STOPPED → sc start (≤ max_starts_per_window)
            STOP_PENDING > stop_pending_timeout_seconds → kill its process tree,
            ONLY if no backup holds the lock (else CRITICAL + wait)
            after a CRITICAL: RUNNING again → RECOVERED email
 N fails  → backup running? (backup.lock PID alive / rclone|robocopy process)
              yes → defer (caps: 8 h transfer, 30 min lock-only → CRITICAL + alert)
              no  → AamPrefectServer STOPPED → sc start
                    RUNNING but dead → ordered restart (WP13):
                      stop agent → STOPPED → stop server → STOPPED → start server
                      → API healthy → start agent   (any step fails → CRITICAL email)
sc.exe via core/services.py (run_capture; sc prints errors on STDOUT).
All tunables: config `watchdog:`. Own log: logs/watchdog_app.log.
Every CRITICAL → core.notify (ACTION_REQUIRED, mode watchdog) → red banner
until a RECOVERED notification follows.
```

---

## 6. File-by-file map

### Entry points
| File | Role | Key functions |
|---|---|---|
| `launch.py` | agent service main | `main`, `_run_dashboard`, `_ensure_concurrency_limit`, `_cancel_orphaned_runs`, `_reconcile_disabled_legs` |
| `serve.py` | builds Prefect deployments from config | `deployments()` |
| `flow.py` | all Prefect flows + tasks + pipelines | see §3–§5 |
| `ui.py` | dashboard API | `status`, `trigger_*`, `_status_fields`, `get_db`; approvals: `GET /approvals`, `POST /approvals/{id}/approve` (typed phrase, sign-in required), `/run`, `/dismiss` |
| `watchdog.py` | Prefect health monitor | `main`, `_is_backup_running`, `_service_state`, `_start_service`, `_restart_stack`, `_resolve_stuck_stop`, `_critical` |
| `collect_config_data.py`, `deploy/*` | install / discovery / restore scripts | — |

### `core/` — business logic
| Module | Responsibility | Called by |
|---|---|---|
| `health.py` | pre-backup checks (source exists/listable/free space, binaries, key, clock) | `flow.health_check_task` (+ SeBackupPrivilege via `permissions` for LAN) |
| `cloud_preflight.py` | source probe + GCS probe | `cloud_preflight_task` |
| `cloud_sync.py` | rclone sync build/run/classify | `cloud_sync_task`, `fy_rollover` |
| `cloud_verify.py` | rclone check --size-only + verdict | `cloud_verify_and_report_task` |
| `cloud_reporter.py` | rclone size / lsjson / check --combined | `cloud_verify_and_report_task` |
| `rclone_config.py` | temp rclone config (service-account key) | cloud modules |
| `lan_preflight.py` | SMB probe, mount-marker state, robocopy `/L /BYTES` via `run_tool` + `/UNILOG` (full listing kept, streamed: `*EXTRA File` count, summary) | `lan_preflight_task` |
| `permissions.py` | preflight rights (WP4): `check_backup_privilege`, `probe_destination_write`, `check_destination_free_space`, `probe_source_read`, `probe_cloud_write` | `health_check_task`, `lan_access_task`, `cloud_access_task` |
| `lan_sync.py` | robocopy build/run/parse/decide | `lan_sync_task`, `fy_rollover` |
| `lan_manifest.py` | walk destination, snapshot diff | LAN snapshot tasks |
| `wol.py` | magic packet + wait for 445 | `wol_check_task`, rollover, audit |
| `shutdown.py` | `shutdown /s /m \\ip /t <wol.shutdown_delay_seconds> /f` via run_tool, then port-445 verification | `lan_shutdown_task` (LAN pipeline, LAN audit) |
| `integrity.py` | weekly hash audit | `integrity_audit_flow` |
| `fy_rollover.py` | FY boundary handling | `rollover_check_flow`, `launch` |
| `manifest.py` | SQLite: file_entries, run_history, integrity_audits, notifications | everything |
| `backup_repository.py` | write sync results / run history | flow record tasks |
| `report.py` | failure alert + weekly/monthly emails | flow, ui, watchdog |
| `statuses.py` | single status vocabulary | flow, manifest, report, ui |
| `tool_runner.py` | one subprocess wrapper: `run_tool` (files, tree-kill, manifest) and `run_capture` (small status queries, in memory, capped) | cloud_sync, lan_sync, preflights, permissions, shutdown, health (w32tm), services |
| `services.py` | sc.exe `queryex`/`stop`/`start`, `wait_for`, `kill_process_tree` (WP13) | watchdog |
| `run_artefacts.py` | per-run full-log folders + retention | flow, tool_runner |
| `process.py` | lock file, binary resolution | flow, watchdog, launch |
| `logging.py` | loguru setup + Prefect bridge | flows |
| `time_utils.py` | IST time, FY prefix, cron text | many |
| `hashing.py` | md5 helpers | tests/legacy |
| `notify.py` | **[built, not wired yet]** single alert path, delivery recorded, cooldown | (WP8) |
| `guards.py` | G1–G4 wired via `flow.lan_guards_task` / `cloud_guards_task` (2.2); `enforce`, `GuardBlocked`, `max_delete_allowance`, `require_source_files`; G5 not wired yet | (M2 2.3 for G5) |
| `approvals.py` | approval decisions: statuses PENDING/APPROVED/DISMISSED/USED/SUPERSEDED, `request_phrase`, `evaluate_request`, `evaluate_approval`; storage in `ManifestDB` (`approvals` table) | `flow._apply_approval`, `flow._open_block_request`, `ui` |
| `smb_ready.py` | **[built, not wired yet]** NAS readiness | (M2 2.5) |
| `rclone_errors.py` | **[built, not wired yet]** transport-failure classifier | (WP7) |

`models/config.py` — every setting, validated (pydantic, unknown keys rejected).
`test_support/` — test-only: sandbox config, fake tool runner, real SMTP test server.

---

## 7. Data written

| Where | What | Writer | Reader |
|---|---|---|---|
| `manifest.db` › `run_history` | one row per leg run: status, exit, files/bytes, error (bounded), extended_metrics JSON (stage timings, artefacts_dir) | `flow._record_run` | ui, reports, `last_successful_run` |
| `manifest.db` › `file_entries` | per-file state per leg | record tasks | metrics, dashboard counts |
| `manifest.db` › `integrity_audits` | weekly audit verdicts | audit flow | ui, reports |
| `manifest.db` › `notifications` | every alert outcome | `core/notify` (WP8) | dashboard (WP9) |
| `run_logs\<day>\<run_id>\` | FULL rclone/robocopy logs + `manifest.json`, gzip > `run_logs.compress_after_kb` | tool_runner / run_artefacts | operator, dashboard link |
| `logs\backup_YYYY-MM-DD.log` | app log (loguru) | all flows | operator |
| `logs\*_svc*.log` | NSSM stdout/stderr per service | NSSM | operator |
| `backup.lock` | `PID:create_time` of the running backup | `_backup_slot` | watchdog, launch |
| Prefect DB (`.prefect`) | flow/task states, Prefect UI logs | Prefect | Prefect UI, ui.py |

---

## 8. Tunables (config.yaml) — what each section controls

| Section | Controls | Main consumers |
|---|---|---|
| `paths` | source, NAS destination, runtime dir, GCS key | everything |
| `lan` | robocopy retries/wait/threads/timeout, flow retries, shutdown policy, `log_tail_kb`, dry-run timeout, `use_backup_mode` (/ZB), `min_free_dest_gb`, `privilege_check_timeout_seconds` | lan_sync, lan_preflight, permissions, flow |
| `wol` | enable, MAC, NAS IP, broadcast, wake timing, `shutdown_*` (delay, command timeout, verify window, closed probes) | wol, smb_ready, shutdown |
| `cloud` | bucket/project/class, bandwidth, transfers/checkers, all rclone timeouts, flow retries, `error_tail_lines`, `dry_run_timeout_seconds`, `write_probe_enabled` | cloud_*, permissions |
| `schedule` | 6 cron expressions + timezone | serve.py |
| `notifications` | SMTP host/port/security/auth/timeout/retries, recipients, enable flags | report, notify |
| `alerts` | cooldown, body/attachment size | notify |
| `dashboard` | auth, api key, bind, port, `public_url` (used in emails) | ui, notify |
| `maintenance` | DB retention, log retention, SQLite busy/vacuum/sync, slot wait | manifest, flow |
| `health` | clock skew limit/timeout, `ntp_server`, `w32tm_timeout_seconds`, `clock_unverified_policy`, min free source GB, rollover gcloud timeouts | health, fy_rollover |
| `run_logs` | full-log folder, retention 90 d, compression, min free GB, service-log retention | run_artefacts, flow |
| `guards` | `max_delete_files` 500, `max_delete_percent` 10, `max_source_shrink_percent` 20, `forbid_local_destination` true (+ `cloud.dry_run_timeout_seconds` 1800) | flow `lan-guards` / `cloud-guards` |
| *(M2)* `approvals`, more `lan` keys (WP4) | added with their consumers | approvals, lan_preflight |

---

## 9. What changes next (so you can review logic before it lands)

1. **M2 wiring** — guards G1–G5 run in preflight before any transfer; a BLOCK writes
   `*_BLOCKED`, emails "open the dashboard", and the dashboard shows Approve/Dismiss;
   approval re-runs the deployment once. Readiness check replaces the 2 s probe. Rollover
   runs inside the slot, forward-only. Preflight checks `/ZB` privilege + destination write.
2. **M3 truth** — one terminal-status function (no success row on a failed run),
   metrics from rclone/robocopy's own counters, "Size check" wording, audit
   `AUDIT_INCOMPLETE` for network failures, one alert per failure via `core/notify`,
   dashboard staleness banner + truthful `/health`.
3. **M4 operations** — shutdown verified, clock tri-state, watchdog restart order,
   schedule/time-budget validation, installer runs services as a dedicated account.

---

## 10. Gates — what must be true (today vs. after the plan)

Legend: **Today** = what the code on `mvp-hardening` enforces now. **Plan** = added by the
work package named (M2/M3). Every row is a hard stop unless marked *(warn only)*.
"Fails" means the run row gets a failure status, the Prefect run is FAILED and an alert is sent.

### 10.1 Before anything runs (both legs)

| # | Condition | Today | Plan |
|---|---|---|---|
| C1 | `config.yaml` loads and validates (types, ranges, cron, bucket name, UNC path, MAC when WoL on, at least one leg enabled, source/LAN FY folder names match, no 4-digit FY names) | yes, otherwise the agent/flow cannot start | + unknown keys rejected (done M1); schedule/time-budget check (WP14) |
| C2 | Requested leg is enabled (`mode=cloud` with `cloud.enabled=false` is refused) | yes | — |
| C3 | Backup slot free: Prefect limit `aam-backup` obtained within `maintenance.concurrency_wait_seconds` (default 3600 s) | yes | timeout recorded as `*_SLOT_TIMEOUT` row (WP5; today no row is written) |
| C4 | `backup.lock` not held by another **live** process (a dead/reused PID is taken over) | yes | — |

### 10.2 Cloud leg — must pass to START the sync

| # | Condition | Code | Today | Plan |
|---|---|---|---|---|
| CL1 | Source path exists | `health.check_source_drive` | yes | — |
| CL2 | Source has at least one real *file* at any depth (junctions, `System Volume Information`, `$RECYCLE.BIN`, `RECYCLER`, `RECYCLED`, `desktop.ini`, `Thumbs.db`, the canary ignored) | `cloud-guards` **G1** | yes (2.2) → `CLOUD_BLOCKED`; health no longer judges emptiness | — |
| CL3 | Source readable: root listable (health) and the first byte of the first real file reads (`permissions.probe_source_read`, `cloud-access`) | `health`, `cloud-access` | yes (2.4) | — |
| CL4 | Free space on the **source** drive >= `health.min_free_source_gb` | same | yes *(skipped if unmeasurable)* | — |
| CL5 | rclone binary found (`deploy\bin` first, then PATH) | `health.pre_backup_health` | yes | — |
| CL6 | GCS key file exists and is not empty | same | yes | — |
| CL7 | Clock within `health.max_clock_skew_seconds` of Google time, else of `w32tm /stripchart` against `health.ntp_server` | `health.check_clock` | SKEWED: yes. UNVERIFIED (no source answered): per `health.clock_unverified_policy` - `warn` (default) runs, `block` fails | **done (WP12):** verdict in `extended_metrics.clock`; UNVERIFIED = amber dashboard item. w32tm exit 0 is not trusted without a sample line |
| CL8 | Preflight probe A: source exists and can be listed | `cloud_preflight` | yes *(empty = warn only)*; task retried once after 30 s | — |
| CL9 | Preflight probe B: `rclone lsjson --max-depth 0 aam_gcs:<bucket>/<FY>` exits 0 within `cloud.preflight_timeout_seconds` (proves key valid, bucket exists, network + DNS + TLS OK) | `cloud_preflight` | yes | the network/TLS/DNS cause is named in the error (`rclone_errors`, WP7) |
| CL10 | Builder re-checks the source has a real file | `guards.require_source_files` in `build_rclone_sync_command` | yes (2.2, same G1 rule; an unlistable source is left to rclone, which then deletes nothing — exit 3) | — |
| CL11 | Source (`rclone size`) not shrunk more than `guards.max_source_shrink_percent` vs last success of the same FY | `cloud-guards` **G2** | yes (2.2) | — |
| CL12 | Planned deletions (`rclone sync --dry-run`, within `cloud.dry_run_timeout_seconds`) <= `guards.max_delete_files` and <= `guards.max_delete_percent` of the destination (`rclone size`) | `cloud-guards` **G3** | yes (2.2); real sync gets `--max-delete` (rclone stops with exit 7 → FAILED) | — |
| CL13 | Cloud write permission: `rclone touch` + `rclone deletefile` of `<bucket>/_aam_preflight/probe_<run_id>` (outside every FY prefix; cause named via `rclone_errors`) if `cloud.write_probe_enabled` | `cloud-access` | yes (2.4) → `CLOUD_FAILED` | — |
| CL12a | Bucket/prefix unchanged since last success of the same FY | `cloud-guards` **G4** | yes (2.2) | — |
| — | Any G-check blocks: `CLOUD_BLOCKED` row with the guard record, nothing transferred, ONE approval request + ONE "ACTION REQUIRED - open the dashboard" email (`core.notify`, key `blocked:cloud`, cooldown); approve once on the dashboard re-runs with `approval_id` | `flow`, `ui` | yes (2.2, 2.3) | — |

### 10.3 Cloud leg — must hold for the run to count as SUCCESS

| # | Condition | Code |
|---|---|---|
| CS1 | rclone exit 0 gives `CLOUD_COMPLETE`, exit 9 gives `CLOUD_NO_CHANGES_COMPLETE` | `classify_rclone_exit` |
| CS2 | No ERROR/CRITICAL lines (or plaintext "Failed to ...") in the rclone log; otherwise exit 0 becomes `CLOUD_SUSPECT` and exit 9 becomes `CLOUD_FAILED` | `scan_rclone_log_for_errors` |
| CS3 | Exit 4/5/10 gives `CLOUD_PARTIAL` (alert, run FAILED, resumes next night); 1/2/3/6/7/8/other, timeout or spawn error gives `CLOUD_FAILED` | same |
| CS4 | Sync FAILED/SUSPECT is retried by Prefect `cloud.max_attempts - 1` times, `cloud.retry_delay_seconds` apart | `flow` |
| CS5 | **Size check** passes (see 10.6 V1) | evidence gate D3 |
| CS6 | Post-sync diff (`rclone check --combined --size-only`) completed (exit < 2) and shows **0 missing** and **0 size-changed** files | `cloud_reporter.get_cloud_diff` |
| CS7 | Bucket listing (`rclone lsjson -R`) and size query (`rclone size`) both succeeded | `get_cloud_manifest`, `get_cloud_size` |
| CS8 | Final status is `CLOUD_COMPLETE` or `CLOUD_NO_CHANGES_COMPLETE` | terminal policy D5 |

If CS5 to CS7 fail, the status is `CLOUD_VERIFY_FAILED` + alert. *(Known gap, WP5: an
exception in the verify/post steps can still leave a success status in the row — fixed in M3.)*

### 10.4 LAN leg — must pass to START robocopy

| # | Condition | Code | Today | Plan |
|---|---|---|---|---|
| LA1 | CL1, CL3, CL4 for the source (same health checks); CL2 as **G1** in `lan-guards` | `health`, `lan-guards` | yes (G1 since 2.2) | — |
| LA2 | robocopy binary found | `health` | yes | — |
| LA3 | NAS awake (only if `wol.enabled`): TCP 445 answers within 5 s, otherwise magic packet x `wol.wake_retry_count`, then poll 445 every `wol.ping_interval_seconds` up to `wol.wake_timeout_seconds`, then `wol.stability_wait_seconds` | `wol.ensure_server_online` | yes (wake capped at 600 s) | `smb_ready`: 445 **and** a real SMB stat **and** a stability re-check; defaults 900 s wake / 90 s settle, cap 3600 s (M2 2.5) |
| LA4 | NAS host answers TCP 445 | `lan_preflight._is_smb_reachable` | yes — **hard-coded 2 s x 4 tries (about 15 s)** | `smb_ready` with `wol.smb_connect_timeout_seconds` (15) x `lan.preflight_smb_attempts` (8), wait `lan.preflight_smb_wait_seconds` (15) |
| LA5 | Mount canary `<dest>\.AAM_TARGET_MOUNTED` exists at the destination ROOT and is a regular file | `lan_preflight` → `lan-guards` **G5** | yes (2.3): missing → `LAN_BLOCKED` + approval that recreates it (was: hard failure with "create the canary" text) | — |
| LA6 | `robocopy /L /MIR /BYTES` dry-run exits < 8 within `lan.dry_run_timeout_seconds`; full listing kept (`/UNILOG`, run_logs) and streamed for the summary and `*EXTRA File` count | `lan_preflight` | yes; preflight task retried once after 30 s | — |
| LA7 | Destination writable: create / write / read back / delete `.aam_write_probe_<run_id>` at the NAS root (pattern `/XF`-excluded), AFTER the guards so an unconfirmed destination is never written to | `lan-access` | yes (2.4) → `LAN_FAILED` naming the fix (service account) | — |
| LA8 | Service account holds `SeBackupPrivilege` when `lan.use_backup_mode` (`/ZB`) | `health_check_task` → `permissions.check_backup_privilege` (System32 `whoami /priv /fo csv /nh`, by name) | yes (2.4), before the dry-run → `LAN_FAILED` with the secpol fix | — |
| LA9 | Free space on the NAS >= `/L` Bytes Copied + `lan.min_free_dest_gb` (`shutil.disk_usage` on the UNC path) | `lan-access` | yes (2.4) → `LAN_FAILED` with the numbers | — |
| LA10 | Source (`/L` Total) not shrunk; planned deletions (`/L` `*EXTRA File` lines) within limits of the walked destination count; destination unchanged and not this machine | `lan-guards` **G2, G3, G4** | yes (2.2) → `LAN_BLOCKED` | — |
| — | Pre-sync destination snapshot | `lan_snapshot_before_task` | yes since 2.2: a failed walk = unknown destination count → `LAN_FAILED` (was: tolerated) | — |

### 10.5 LAN leg — must hold for the run to count as SUCCESS

| # | Condition | Code |
|---|---|---|
| LS1 | robocopy ran to completion: the log ends with the job summary (`Files :` row **and** `Ended :` line); otherwise `LAN_SUSPECT` even with exit 0-3 (killed process) | `decide_lan_result` |
| LS2 | Exit code has no bit 16 (fatal, else `LAN_FAILED`) and no bit 8 (copy errors, else `LAN_PARTIAL`) | same |
| LS3 | Exit 0-7 **and** summary FAILED column = 0 **and** no `** FAILED:` lines gives `LAN_COMPLETE` (4-7 = mismatches/extras: complete, logged as an anomaly) | same |
| LS4 | Exit code is a real integer (bool / None / timeout gives `LAN_FAILED`) | same (M1 1.8) |
| LS5 | FAILED/SUSPECT retried by Prefect `lan.max_attempts - 1` times, `lan.retry_delay_seconds` apart; hard timeout `lan.subprocess_timeout_seconds` | `flow`, `run_lan_sync` |
| LS6 | Final status is `LAN_COMPLETE` | terminal policy D5 |
| — | Post-sync snapshot / DB record failure does **not** fail the run (metrics skipped, logged CRITICAL) | `lan_snapshot_after_task` |
| — | NAS shutdown only after `LAN_COMPLETE` **and** `lan.shutdown_after_backup` **and** `wol.enabled` (or after PARTIAL/FAILED if `lan.shutdown_on_all_retries_exhausted`); the result is verified (port 445 closed) and recorded, never assumed | D9 |

There is **no separate daily LAN verification step** today: LAN success rests on robocopy's
own completed summary; content verification is the weekly audit (V3).

### 10.6 Verification

| # | Check | When | How | Passes / VERIFIED only if | Limits today | Plan |
|---|---|---|---|---|---|---|
| V1 | **Cloud size check** (daily) | after every cloud sync | `rclone check --one-way --size-only --checkers 4` | exit 0 **and** rclone's own `0 differences found` line **and** no `Failed to check` **and** no ERROR lines | sizes only, so same-size corruption is invisible; labelled "Verification Passed" / "Cryptographic Checks" | renamed **"Size check"** everywhere (WP7) |
| V2 | **Weekly integrity audit — cloud** | Sun 03:00 (`schedule.audit_cron`) or dashboard "Verify" | `rclone check` with **MD5 hashes** (source re-read vs GCS stored MD5), both directions, `--checkers 1`, timeout `cloud.verify_timeout_seconds` | exit 0/1, diff file written, completion summary present, no read-error lines, summary count consistent with the diff, and **0** missing + changed + extra files (the canary is ignored) | a network/TLS/DNS outage is recorded as "content mismatch, missing-from-dest=N" (false alarm) | network/listing errors become `AUDIT_INCOMPLETE` "audit could not run: <cause>" (WP7, `rclone_errors`) |
| V3 | **Weekly integrity audit — LAN** | same run, after cloud | WoL, then `rclone check` source vs `\\nas\...` with MD5 hashes (both sides read), timeout `lan.subprocess_timeout_seconds`, then shutdown | same rules as V2 | same false-alarm issue | same fix; NAS readiness via `smb_ready` |
| V4 | Result storage | — | one `integrity_audits` row per leg; dashboard shows the latest; no row = NOT_VERIFIED | — | — | `AUDIT_INCOMPLETE` added |

### 10.7 FY rollover (daily 06:00 check)

| # | Condition | Today | Plan |
|---|---|---|---|
| R1 | Source/NAS paths end in a 2-digit FY folder (`FY26-27`); otherwise rollover is permanently off | yes | — |
| R2 | Rollover happens when that FY differs from the FY of today's date (IST, April 1 boundary) | yes — **also rolls backwards if the clock is behind** | only exactly the next FY; anything else BLOCKED + approval (WP11) |
| R3 | Final backup of the closing FY must succeed on every enabled leg (cloud COMPLETE/NO_CHANGES, LAN COMPLETE), else `ROLLOVER_BLOCKED` alert, retried daily | yes — but **outside the backup slot** | runs through the normal pipelines inside the slot (WP11) |
| R4 | Archive the old FY in GCS (`gcloud ... --storage-class=ARCHIVE`) | best effort, 600 s | timeout from config; failure = warning + dashboard flag |
| R5 | Create the new FY folder on source + NAS (+ canary on NAS); rewrite `config.yaml` atomically | yes (NAS failure tolerated) | + placeholder file in the new source folder; config backup first |
