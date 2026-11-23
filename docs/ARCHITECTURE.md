# AAM Backup Automation — How the program works

**State described:** branch `mvp-hardening`, all development groups of
`docs/PLAN_OPEN_TOPICS_2026-10-18.md` built and wired (2026-10-19). Everything in `core/` is
called by the flows or the dashboard; the old "built, not wired yet" markers are gone.
Sections 5.2 (rollover journal), 5.3 (report model), 5.6 (interruption), 5.7 (emails) and
5.8 (passwords and secrets) describe the open-topics work.

Reading guide: §1 big picture → §2 schedules → §3 the nightly backup step by step →
§4 every decision point (where you tune logic) → §5 the other flows → §6 file-by-file map →
§7 data written → §8 tunables → §9 what changes next → **§10 gates: every check that must
pass for a backup to run, to count as successful, and to be verified.**

---

## 1. Big picture — processes on the Windows server

```
 ┌──────────────────────────── Windows Server 2016 ─────────────────────────────┐
 │                                                                              │
 │  [Service] AamPrefectServer  (NSSM → .venv python -m prefect server start)   │
 │     Prefect API + UI on 127.0.0.1:4200, its own SQLite DB in PREFECT_HOME     │
 │     ▲  keeps schedules, flow-run states, task states, flow logs              │
 │     │ HTTP                                                                   │
 │  [Service] AamBackupAgent  (NSSM → .venv python launch.py; svc account)      │
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
 │  [Service] AamWatchdog  (NSSM → .venv python watchdog.py)                    │
 │     polls Prefect /api/health every 60 s; restarts services; honours lock    │
 │                                                                              │
 │  Runtime data (C:\BackupAgent = paths.runtime_dir):                          │
 │     manifest.db  backup.lock  logs\  run_logs\<day>\<date>_<HHmm>_<leg>_<id>\  .prefect\     │
 └──────────────────────────────────────────────────────────────────────────────┘
        │ robocopy /MIR (SMB 445)                 │ rclone sync (HTTPS)
        ▼                                         ▼
   NAS  \\<nas>\share\FY26-27               GCS  gs://<bucket>/FY26-27/
   (woken by WoL, shut down after)          (versioned bucket + lifecycle)
```

## 2. Schedules (config `schedule.*`, timezone `schedule.timezone`)

| Deployment (serve.py) | Flow function (flow.py) | Default cron | What it does |
|---|---|---|---|
| `aam-backup/backup-cloud` | `backup(mode="cloud")` | `0 18 * * *` | rclone sync to GCS + size check (no cron when `cloud.scheduled` is false, section 5.9) |
| `aam-backup/backup-lan` | `backup(mode="lan")` | `0 1 * * *` | WoL → robocopy /MIR to NAS → shutdown (no cron when `lan.scheduled` is false, section 5.9) |
| `weekly-report/weekly-report` | `weekly_report_flow` | `0 8 * * MON` | email report of the previous calendar week (Mon-Sun) + CSV |
| `monthly-report/monthly-report` | `monthly_report_flow` | `30 8 1 * *` | email report of the previous calendar month + CSV |
| `rollover-check/rollover-check` | `rollover_check_flow` | `0 6 * * *` | **log retention** + the FY rollover resumer (§5.2) |
| `integrity-audit/integrity-audit` | `integrity_audit_flow(mode="all")` | `0 10 * * SUN` | hash-level source↔backup audit |

The crons above are the model defaults. The trial box runs cloud at 02:00 and LAN at 21:00.
At load, `core.budget.schedule_problems` refuses a schedule whose run budgets could overlap
(§8 `budget`).

All backup-type work (cloud, LAN, audit) takes ONE global slot: Prefect concurrency limit
`aam-backup` (limit 1, created by `launch._ensure_concurrency_limit`) + the PID file
`backup.lock`. Waiting longer than `maintenance.concurrency_wait_seconds` fails the run.

---

## 3. The nightly backup, step by step

### 3.1 Entry: `flow.backup(config_path, mode, fy=None)`

```
backup(mode)
 ├─ load_config()                      models/config.py   (strict: unknown key = error)
 ├─ configure_logging()                core/logging.py    → logs\backup_YYYY-MM-DD.log
 ├─ configure_prefect_bridge()         core/logging.py    → loguru lines also in Prefect UI
 ├─ disabled-leg guard                 mode=cloud/lan but leg disabled → ValueError (FAILED)
 ├─ cleanup_orphaned_robocopy_logs()   core/lan_sync.py   (legacy %TEMP% logs, 24h)
 ├─ _activate_overlap()                core/fy_overlap.py (starts backing up next year's folder when it is time, §3.4)
 ├─ folders = _active_fys()            [next FY, configured FY] in the overlap window, else [configured FY]
 ├─ if cloud enabled: _run_leg_folders("cloud")  ── slot held ONCE, per folder _run_cloud_pipeline  §3.2, §3.4
 ├─ if lan   enabled: _run_leg_folders("lan")    ── slot held ONCE, per folder _run_lan_pipeline    §3.3, §3.4
 │                                                  (runs even if cloud failed; ONE NAS shutdown decision)
 ├─ any errors? → send_failure_alert() (skipped for PartialRun: already alerted)
 │               → raise ExceptionGroup  ⇒ Prefect state FAILED
 └─ no errors  → ManifestDB.purge_old_runs(db_retention_days, db_retention_max_mb)  ⇒ COMPLETED
                 (size rule: oldest run rows go only while run_history > db_retention_max_mb (default 5 MB) AND the
                  row is older than db_retention_days (floor); never running rows, the newest and last-success row per
                  (mode, fy); still over budget => one WARNING, no mail)
```

### 3.4 FY overlap: two year folders for three months (plan `docs/PLAN_FY_OVERLAP_2026-11-20.md`)

US clients work in the NEXT financial-year folder from 1 January, Indian clients from 1 April, so from
`rollover.early_start_month` (default January) until the April switch BOTH folders are backed up:
each FY folder is backed up for 15 months (its own 12 plus January-March before it).

| Piece | Where | Rule |
|---|---|---|
| Active set | `core/fy_overlap.active_folders` | `[next FY, configured FY]` (new first = run order) when overlap is on AND (in window OR switch pending) AND the next FY has an activation row; else `[configured FY]`. "Switch pending" = calendar FY is the next FY and the rollover journal's `config_switch` step is not done (follow-up steps do not count) |
| Run date | `fy_overlap.run_date` | the IST date of the SCHEDULED time (a late run keeps its own day), taken once per flow run |
| Activation | `fy_overlap.ensure_overlap_activated` (called by `flow._activate_overlap` at the start of EVERY backup flow run and of `rollover_check_flow`) | direct clock proof (`check_clock` = OK), parent folder exists, not a link / junction; an existing canonical folder is ADOPTED (placeholder only into a folder with no real file, same rule as guard G1), else created without `parents=True`; durable row in `overlap_activations`; INFO mail `overlap:start:<fy>`; failures and an unproven clock are retried every run and escalate once to ACTION REQUIRED after `rollover.activation_alert_after_hours` |
| Missing folder later | existing health check (`AAM-SRC-01`) | a missing / unreadable early folder fails that FY's legs; it is NEVER recreated. (A link replacing it later is not caught yet - see task list) |
| NAS folder | `fy_overlap.ensure_overlap_nas_folder`, called in the LAN pipeline of the early FY after the NAS is awake | missing -> create (no parents) + mount marker; marker -> ok; empty -> marker; non-empty without marker -> untouched (guard G5 asks, AAM-NAS-10) |
| Folder view | `fy_overlap.folder_view` | `config` copy whose `source_drive` / `lan_destination` point at that FY; the pipelines read every path from it, so the cloud prefix `gs://bucket/<fy>`, guard baselines and log folders follow |
| Leg loop | `flow._run_leg_folders` | one slot + `backup.lock` hold per leg around all folders (pipelines get `slot_held=True`); both folders share the leg budget (second folder with none left = `<LEG>_MISSED` for its FY); one folder's failure never stops the next; run ids `<flow run>-<leg>-<fy>`; run-log folders `<date>_<HHmm>_<leg>_<fy>_<short id>`; Prefect artifact keys per FY |
| NAS power | `_run_lan_pipeline(shutdown_votes=...)` + `flow._lan_shutdown_decision` | pipelines never shut the NAS down in a loop: each votes what today's rule says; ONE shutdown at the end only when every folder ran and voted yes (MISSED, cut off, cancel = stays on) |
| Siblings | `fy_overlap.sibling_findings`, `flow._alarm_siblings` (daily 06:00) | a misnamed FY-looking folder pointing at a newer year, or a canonical newer FY with real files that is not active -> ACTION REQUIRED; past years and `rollover.sibling_ignore` are ignored; unreadable folders never produce a false RESOLVED |
| Small folders | `guards.max_delete_allowance`, `g2_source_shrink` | below `guards.small_folder_files` (100) the percentage limits are replaced by `guards.small_folder_max_deletes` (10); one function for G3 and the rclone `--max-delete` backstop; explicit 0 % and an empty destination stay blocked |
| Database | `core/manifest.py` schema v6 | `fy` on `file_entries` (`UNIQUE(fy, relative_path)`), `run_history`, `integrity_audits`, `notifications`, `approvals`; table `overlap_activations`; every per-FY reader takes `fy` with no default (`ALL_FYS` = explicit "every FY"); an older database file is refused ("rename it, start fresh"), never migrated |
| Approvals | `core/approvals.evaluate_approval`, `ManifestDB` approvals, `flow.backup(fy=)` | an approval belongs to ONE (leg, FY): superseding is per (leg, FY), `evaluate_approval` needs `approval.fy == run fy` (the 500-vs-400-deletes case of another FY is refused), the dashboard re-run passes the approval's `fy` to the backup deployments only (`rollover_check_flow` has no `fy`). A re-run with an approval id but no `fy` touches only the configured FY |
| First run of a leg in an FY | `guards.g3_mass_delete(first_run=...)` via `flow` (`first_run = not baseline`) | no successful baseline for (leg, FY) and planned deletes > 0 -> a DECISION (`*_BLOCKED` + approval): a filled `gs://bucket/FY27-28` or NAS folder is never trimmed unasked |
| Closed FY at the switch | `flow._close_old_fy` -> `ManifestDB.close_fy` (called whenever the rollover's `config_switch` step is finished) | the closed FY's open approval requests become CLOSED ("FY closed"), its open incidents end silently (no RESOLVED mail); history stays; its approvals can no longer run (`backup(fy=)` needs an active FY). The final-backup G3 -> G6 two-approval path still works |
| Alerts and incidents | `AlertEvent.fy`, `core/email_compose.compose`, `core/notify` | keys `backup:<fy>:<leg>:<status>`, `blocked:`, `missed:`, `recovered:`, `stale:`, `audit:<fy>:<mode>:...`, abandoned-run key carry the FY; per-leg subjects get `[FY27-28]`; incidents, cooldown and RECOVERED run per (mode, fy); `nas_shutdown:` and `watchdog:*` stay global |
| Overdue / attention / watchdog | `core/overdue`, `core/attention.leg_timing`, `watchdog._stale_code_parts` | overdue per (leg, FY); the early FY's base is its `activated_at` (no red on 1 Jan before the first run); codes `stale:<fy>:<leg>`, `approval:<fy>:<leg>`, `last_run:<fy>:<leg>`; the watchdog suppresses per FY and an FY it cannot work out suppresses nothing; closed FYs never appear |
| Integrity audit | `flow.integrity_audit_flow(mode, fy=None)` | loops the active set (new first) under ONE slot, ONE `budget.audit_max_hours`, ONE NAS wake / shutdown vote; rows, alerts and `_pending_context` per FY |
| Reports | `core/report.py`, `core/report.fy_window` | weekly / monthly mail has a section per FY; scheduled counts run from `max(period start, activation)` to `min(period end, switch)` (the switch comes from the rollover journal when there is no activation row) |
| Dashboard | `ui._folders`, `ui._active_fy_list`, `static/js/dashboard.js` | `/status` has `folders[]` (new FY first: `fy`, `role` current/early, `active_since`, per leg last run / success / timing / integrity / nights / `running` / open `approval`); the old top-level fields stay; one card per FY; the rollover card lists the active set. `POST /trigger/cloud|lan|verify/{mode}?fy=` takes only an active FY (409 otherwise) and is refused (409) while any backup, audit or rollover is running or about to start |
| Validator | `models/config.py` (OV-10.3) | while LAN and overlap are on, a NAS FY folder that is the share root itself (`\\nas\FY26-27`) fails to load: the next FY's folder must be created BESIDE it |
| Tools and lab | `tools/fy_overlap_rehearsal.py`, `audit_lab/test_w25_fy_overlap.py` | rehearsal: dry run by default, `--execute` runs one cloud + one LAN flow on a sandbox config with `fy_overlap.run_date` patched to 15 January of test years FY90-91 / FY91-92 (clock and production config untouched, test folders / prefixes removed, `--cleanup`). Lab WAVE 25: 15-month fake-clock scenario (Dec, 1 Jan activation, Jan-Mar two folders + Sunday audit, 1 Apr switch, Apr-Dec one folder, next 1 Jan) through the production flows with real robocopy / rclone |

### 3.2 Cloud pipeline: `flow._run_cloud_pipeline()`

```
with _backup_slot(config):                     concurrency slot + backup.lock (acquire_lock)
│
├─ before_dict = ManifestDB.get_cloud_synced_entries()      (for "files copied" metric)
├─ artefacts   = _open_run_artefacts()  → run_logs\<day>\<date>_<HHmm>_<leg>_<id>\   (full logs folder)
│
├─ phase "pre"
│   ├─ health_check_task   → core/health.pre_backup_health()
│   │      source exists & has a top-level entry · rclone binary · GCS key file ·
│   │      clock vs Google, else w32tm → OK | SKEWED (fail) | UNVERIFIED (policy warn|block)
│   └─ cloud_preflight_task (1 retry, 30 s) → core/cloud_preflight.run_cloud_dry_run()
│          Probe A: source readable · Probe B: rclone lsjson --max-depth 0 (auth/bucket/net)
│
├─ phase "sync"
│   └─ _sync_with_retries → cloud_sync_task (no Prefect retries; XR, see CS4)
│        → core/cloud_sync.run_cloud_sync()
│            build_rclone_sync_command()  (refuses an "appears empty" source)
│            core/tool_runner.run_tool("rclone-sync", … --log-file <artefact>)
│            exit code → classify_rclone_exit() → STATUS  (+ log scan, see D2)
│        task raises SyncFailed on CLOUD_FAILED / CLOUD_SUSPECT ⇒ attempt loop decides:
│        retry only if retryable, deletions known, budget fits, guards re-measured
│        (cumulative G3, --max-delete = what is left); --max-delete stop ⇒ BLOCKED
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
│   └─ _sync_with_retries → lan_sync_task (no Prefect retries; XR, see LS5)
│        → core/lan_sync.run_lan_sync()
│            build_robocopy_command()  /MIR /Z /ZB /XJ /MT /R /W /V /TS /FP …
│                                      /XF .AAM_TARGET_MOUNTED  /XD SysVolInfo $RECYCLE.BIN
│            core/tool_runner.run_tool("robocopy", … /UNILOG:<artefact>)
│            tail = _read_log_tail(UTF-16, lan.log_tail_kb)
│            decide_lan_result(exit, tail) → STATUS  (D4)
│        task raises SyncFailed on LAN_FAILED / LAN_SUSPECT ⇒ attempt loop decides:
│        repeat /MIR only after robocopy /L + G1-G5 + access again (cumulative G3)
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
| D2 | rclone result | `core/cloud_sync.classify_rclone_exit` + `scan_rclone_log_for_errors` + final stats | exit code + ERROR lines of the **final** attempt + final `stats` | 0 COMPLETE · 9 NO_CHANGES only if stats prove 0 transfers/deletes/deletedDirs/renames, else COMPLETE (FAILED if errors) · 4/5 PARTIAL · 10 PARTIAL time-limit stop (verify skipped) · else FAILED · 0+final-attempt errors or stats.errors>0 → SUSPECT (XR-3/4/5) |
| D3 | Cloud evidence gate | `flow._run_cloud_pipeline` (`evidence_ok`) | verified + normal termination + diff has no missing/modified + manifest/size ok | fail → `CLOUD_VERIFY_FAILED` |
| D3a | Size check verdict | `core/cloud_verify.decide_cloud_verify_result` | exit + "N differences found" summary | verified / abnormal |
| D4 | robocopy result | `core/lan_sync.decide_lan_result` | exit bitmask + completed job summary in log | bit16 FAILED · no summary SUSPECT · bit8 PARTIAL · 0-7 COMPLETE (failed>0 → SUSPECT) |
| D5 | Terminal status → Prefect state | `flow` terminal policy + `core/statuses.py` | row status | success set → COMPLETED, else raise |
| D6 | Status meaning everywhere | `core/statuses.py` (`category/label/describe`) | status string | used by DB "last success", reports, dashboard |
| D7 | Integrity audit verdict | `core/integrity._run_rclone_check` + `core/audit_pending.classify` | rclone check (hash) diff + summary; then each difference is PENDING (source proves a change after the leg's last good backup) or REAL | VERIFIED / PENDING_BACKUP (amber, no email) / VERIFICATION_FAILED |
| D8 | FY rollover needed? | `core/fy_rollover.detect_rollover` | FY leaf of paths vs date | rollover / no-op |
| D9 | Shutdown NAS? | `flow._run_lan_pipeline` | status + `lan.shutdown_*` + `wol.enabled` | shutdown / skip |
| D10 | Watchdog action | `watchdog.main` | API health, lock alive, rclone/robocopy running, SCM state, time in STOP_PENDING | wait / defer / start / ordered restart / kill stuck service (no lock only) |
| D11 | Alert sent? | `core/report.send_failure_alert` (today) → `core/notify.notify` **[built, not wired yet]** | `notifications.send_on_failure`, SMTP config, cooldown | SENT/FAILED/SUPPRESSED |
| D12 | Which FY folders are backed up now? | `core/fy_overlap.active_folders` via `flow._active_fys` (taken ONCE per flow run) | `rollover.overlap_enabled`, `early_start_month`, scheduled run date (IST), journal `config_switch` step, `overlap_activations` row | `[next FY, configured FY]` or `[configured FY]`; an unprovable set mails a WARNING and falls back to the configured FY |
| D13 | One NAS shutdown for all folders | `flow._lan_shutdown_decision` | each LAN pipeline's vote, folders run, MISSED / cut off / cancel | shut down only when every folder ran and voted yes, else the NAS stays on |
| D14 | Leg overdue for an FY? | `core/overdue` + `core/attention.leg_timing(fy, role)` | schedule, leg budget, grace, last success of (leg, FY), `activated_at` for the early FY | on_time / late / overdue / waiting |

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
         → db.record_audit(); not VERIFIED and not PENDING_BACKUP → collect error
         (pending=_pending_context: reference = start of the leg's last successful run;
          only a completed check WITH differences reaches core/audit_pending; delete tripwire =
          guards.max_delete_files/percent; unknown reference/time/stat error = REAL)
  lan:   wol_check_task → integrity.audit_lan() (rclone check local↔UNC, hash)
         → lan_shutdown_task (if configured) → db.record_audit()
  errors → send_failure_alert + raise ExceptionGroup
FY overlap (OV-8.1): the block above runs once per active FY (new first) inside ONE slot hold with ONE
audit budget; the LAN part wakes the NAS once and one shutdown vote covers all folders (D13); a row
(`integrity_audits.fy`) and an alert (`audit:<fy>:<mode>:...`) per (leg, FY). `fy=` narrows a manual audit
to one active FY. Known: a folder skipped or failed to wake can leave a woken NAS on without an attention item.
```

### 5.2 `rollover_check_flow` — daily 06:00
The rollover is a **persisted, resumable step journal** (saga): table `rollover_journal` +
`rollover_journal_steps` (one journal per `from_fy`→`to_fy`; per step a state `pending / done /
failed / skipped`, attempts, last error, `first_failed_at`). Step vocabulary and rules are pure
data in `core/rollover_journal.py`; the step functions are in `core/fy_rollover.py`; the resumer
is `flow._rollover_resume`.
```
log_retention_task(config)          purge run_logs (run_logs.retention_days)
                                    + NSSM service logs (run_logs.service_log_retention_days)
rollover_incomplete() FIRST         an open journal is resumed before anything is classified
                                    (after the switch config already says the new FY)
else classify: only exactly the next FY on the clock proof; clock behind / unproven →
               CLK-03 / ROL-03; a jump of more than one FY → approval card
steps, in order (each idempotent):
  1 final_backups   final backup of the closing FY on every enabled leg through the normal
                    pipelines (own slot hold; the LAN one does NOT shut the NAS down), then G6.
                    A retry that still has to switch repeats this step first (D-17).
  2 source_folder   new source FY folder + placeholder file (exist_ok)
  3 nas_folder      NAS woken / SMB ready as in the LAN leg, new NAS FY folder + mount marker
  4 config_switch   compare-and-set inside config_transaction: at from_fy → switch; already at
                    to_fy → done; anything else → refuse (ROL-04). Validate, restore on failure.
                    Once finished, `flow._close_old_fy(from_fy)` closes the old FY's approvals and incidents (§3.4);
                    it also runs when a crash left the step finished
  5 archive         old FY objects to ARCHIVE (gcloud, key as a file; §5.8). Failure = ROL-05,
                    retried every morning, ACTION_REQUIRED after rollover.archive_alert_after_days
  6 nas_shutdown    only if configured and the NAS step was reached
  7 announce        INFO email; says "nothing to do" only when every step is finished
BLOCKING steps (1, 2, 4) stop the run on failure; the others do not (a NAS that is off must not
stop the switch - the cloud protects the new FY from day 1). A pending nas_folder is completed by
the next LAN pipeline (`_complete_pending_rollover_nas_folder`, before the guards, only when the
config already points at the new folder and the share root exists).
With overlap on, the switch is PENDING from 1 April until `config_switch` is done and both folders are
still backed up meanwhile (§3.4); manual triggers are refused during a rollover. The next FY's source and
NAS folders usually exist already (activation), so steps 2 and 3 treat "exists (+ marker)" as done.
Rows: ROLLOVER_RUNNING (swept as ABANDONED after a crash), ROLLOVER_COMPLETE, ROLLOVER_FAILED
(never counted as a backup's "last success"). Dashboard: Settings > FY rollover step card.
```

### 5.3 Reports — `weekly_report_flow` / `monthly_report_flow`
One model, many renderers.
```
ReportPeriod (core/report_period.py)   IST start/end, label, kind
   scheduled emails:  previous_week() / previous_month(), computed from the flow run's
                      SCHEDULED time (a report sent late still covers the right period)
   dashboard download + "send email" buttons:  last_days(7) / last_days(30), up to now
ManifestDB.get_runs_between(start, end)
report.build_report_model(...)         per-leg backup totals (backup modes only, rollover rows
                                       excluded), last good copy, problems with "resolved"
                                       checked against the FULL history, latest audit per
                                       ENABLED leg, open decisions, data copied per leg
render_report_html / CSV / subject     render the model only
```
With overlap, a report has one section per FY in the period (see §3.4, Reports).
An empty period still reports ("action needed: no backup ran"). Scheduled report emails are
recorded in `notifications` (mode `report`, excluded from incident threading and the banner), so
a failed delivery is visible on the dashboard. Status meaning always comes from `core/statuses`.

### 5.4 Dashboard — `ui.py` (thread inside the agent)
```
GET  /            page (templates/dashboard.html + static/js/dashboard.js, polls /status every 2 s)
GET  /status      last runs (status_label/category/description from core/statuses), `folders[]` per active FY (§3.4),
                  last success, integrity (latest_audit), manifest counts, Prefect run state
GET  /health      unauthenticated (always "healthy" today — WP9)
POST /trigger/cloud | /trigger/lan | /trigger/verify/{mode}[?fy=FY27-28]   → prefect arun_deployment (fy must be active)
GET  /report/weekly|monthly          HTML download
POST /trigger/report/*/email         send now
auth: always on - dashboard password (`dashboard.login_password_hash`) → session cookie; Advanced password (`dashboard.admin_password_hash`) → second cookie; sessions carry the fingerprint of the hash they were opened with (a change or reset ends older sessions); no hash = 'not set up' page, nothing opens; rate limits in memory
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
Stale-leg mails parse the attention code `stale:<fy>:<leg>` and are suppressed per (leg, FY) (§3.4).
Every CRITICAL → core.notify (ACTION_REQUIRED, mode watchdog) → red banner
until a RECOVERED notification follows.
```

### 5.6 Interruption — a reboot or power cut mid-run resumes on the next run
- The `aam-backup` slot is a Prefect concurrency lease (300 s, renewed while held): a dead process
  frees it by itself. `backup.lock` holds `PID:create_time`; a dead owner is not honoured.
- robocopy runs with `/Z` (restartable), GCS uploads are atomic, so the next run continues.
- `backup_repository.sweep_abandoned_runs` (agent start and every pipeline start) marks a RUNNING
  row whose owner process is dead as ABANDONED and sends ONE email (wording per mode: backup,
  audit, "FY rollover interrupted").
- The run budget counts from the flow run's SCHEDULED start (`core/budget.RunBudget`; manual runs
  from the actual start). A run Prefect starts late (e.g. 09:30 after a reboot) gets only what is
  left; nothing left → `*_MISSED` row + WARNING email, the next night continues. Several missed
  nights give one `missed:<leg>` incident, not one email per night. Rollover final backups count
  from their own start; report flows have no budget.
- Proven with real tools in the audit lab (W22: hard-kill mid-sync, mid-audit, mid-rollover).

### 5.7 Emails — `core/notify.py`, `core/email_compose.py`, `core/support_bundle.py`
```
producer (flow, watchdog, rollover, reports, password change)
  → faults.coded(...)  fault code (never parsed from text)
  → email_compose      severity + what happened + "Is my data safe?" + numbered steps + next run
                       (IST) + dashboard link + "For support" block
  → notify.send        cooldown / incident threading (key + fault code), record in `notifications`
  → _smtp_session      connect, EHLO, read the RFC 1870 SIZE limit
  → attachment ladder  full (support report + logs zip) → report only → none, sized to
                       min(server SIZE, alerts.attachments_max_total_kb); a 5xx or a hang-up
                       during DATA steps ONE rung down and resends at once; the rung delivered
                       is stored in `notifications.attachments_rung`
```
- ACTION_REQUIRED / RECOVERED / INFO are never silenced by `alerts.send_on_failure`.
- The dashboard link is resolved per email (`core/netinfo.py`: default-route address, else the
  route to the NAS, else the host name; never 127.x / 169.254.x / 0.0.0.0; cached
  `dashboard.link_cache_seconds`). A manual `dashboard.public_url` that differs from the detected
  address makes readiness and the watchdog amber.
- `core/email_samples.py` holds one sample per email kind and fault code; `tools/preview_emails.py
  --to <address>` sends them through the real composer and transport marked `[PREVIEW]`.
- Wording for every email: `docs/EMAIL_CONTENT_SPEC.md`.

### 5.8 Passwords and secrets
- **Two dashboard passwords** (sign-in always on): `dashboard.login_password_hash` (dashboard)
  and `dashboard.admin_password_hash` (Advanced settings), PBKDF2-SHA256 600k, stored only as
  hashes. Changed in Settings > Advanced > Passwords (current Advanced password required, INFO
  email); set at install by `deploy/set_dashboard_passwords.py`; forgotten →
  `deploy/11_reset_dashboard_passwords.ps1` on the server. Sessions carry a fingerprint of the
  hash they were opened with and are checked against the current config, so a change or a reset
  signs everyone else out. No hash = a "not set up" page (fail closed). No API key, no
  `X-API-Key` header. `/health` is unauthenticated.
- **config.yaml is written by one locked writer**: `core/config_file.config_transaction` (cross
  process `filelock` beside the file, backup, atomic replace with bounded retry, reload and
  validate, restore on failure). Dashboard settings, password changes, the rollover switch and
  the installer all use it.
- **SMTP password**: encrypted with Windows DPAPI, machine scope (`core/secrets.py`,
  `dpapi:<base64>` in config.yaml, decrypted in memory by the model, never written back plain).
  The installer runs `deploy/encrypt_secrets.py` (`core/secret_setup.py`) which encrypts a plain
  value; Settings encrypts on save. Readiness checks it decrypts as the service account
  (`probe_service_account.py`) and as LocalSystem (`probe_mail_secret.py`, the watchdog sends the
  alerts). A copy of config.yaml on another server cannot be decrypted: the mail password loads
  empty and mail reports it; enter it again in Settings.
- **Google key**: stays a PLAIN JSON file (owner decision 2026-10-19). `deploy/lock_gcs_key.ps1`
  (run by the installer) leaves read access only to SYSTEM, Administrators and the service
  account. `core/gcs_key.py` can read a `.dpapi` copy but the installer does not create one.
  The FY archive step cannot use `rclone settier` (not supported on GCS); it runs gcloud with the
  key as a file (`CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE`), config in `<runtime>/gcloud`.

---

### 5.9 Manual-only legs: `lan.scheduled` / `cloud.scheduled` (owner 2026-11-23)

A leg (`cloud`, `lan`) is in one of three modes, decided in ONE place, `core/manual_leg.py`:
`off` (`<leg>.enabled` false), `nightly` (enabled and `scheduled` true, today's behaviour, the default) and
`manual` (enabled, `scheduled` false: started from the dashboard button only). Every schedule-based check asks
`manual_leg.is_nightly()`; every truth line comes from `manual_leg.mode_line()` / `mode_lines()`.

| Area | `manual` mode rule |
|---|---|
| `serve.py` / `launch._reconcile_disabled_legs` | the `backup-<leg>` deployment stays registered (the button uses it) but with NO cron (`_nightly_schedules`); at every start the reconcile deletes schedules left on it by an earlier boot (and resumes it if paused). Not treated like a disabled leg |
| `models.config` | the leg is left out of the schedule-budget check (`_schedule_fits_the_budgets`) |
| `core/attention` (banner, `/health`, watchdog C6) | no `stale:` / `first_run:` item and no "schedule cannot be read" item for the leg; `leg_timing` returns state `manual` (no due / overdue time). Instead: AMBER `manual_old:<leg>` (never succeeded, or last success older than `<leg>.on_demand_warn_after_days`), AMBER `manual_not_final:lan` (see rollover), AMBER `manual:none_automatic` when every enabled leg is manual. A FAILED manual run is still a real `last_run:` item |
| Dashboard | `/status` `<leg>.mode` = `{mode, text, soft_warn}`: "LAN: manual only, last backup <date>, N days ago" (amber after the warn days); "LAN: off" for a disabled leg; the "Next scheduled backup" line shows only for a nightly leg; the button works (WoL, backup, verify, shutdown for LAN) |
| Reports (weekly / monthly / dashboard download) | no `scheduled` count, no shortfall ("N of M nights have no backup record") and no "no backup ran" for the leg; a "mode notes" block under the table states every off / manual-only leg and "Nothing runs automatically ..." when all are manual; an old manual backup makes the headline `note: ...` (amber, `act` false, no "all backups OK") |
| Rollover final backup (1 April) | the final backups still run. LAN: a NAS that cannot be reached or woken (fault `AAM-NAS-01/02`, `faults.NAS_UNREACHABLE_CODES`) at that moment is EXPECTED: the LAN pipeline gets `nas_offline_expected=True` (run row `LAN_FAILED` + metric, no failure mail), `final_status["lan"]` becomes `LAN_NOT_REACHED`, G6 does not judge it, the switch proceeds, the rollover run row carries `lan_final_not_reached=<FY>`, `db_meta.lan_final_missing` remembers the FY (dashboard / report line "<FY> was not final-synced to the NAS" until a LAN backup of that FY succeeds) and ONE WARNING mail `rollover:<FY>:lan_not_final` is sent. Guard blocks and every other failure still block (G1-G6 as today). Cloud: a manual-only cloud leg behaves exactly as today, a real failure blocks |
| Weekly audit | still wakes the NAS. LAN unreachable (same fault codes) = row `AUDIT_INCOMPLETE` ("not run: the NAS is not reachable (LAN is manual only)") with no alert mail and no flow failure; any other audit failure alerts as today. Cloud audit unchanged |
| Manual LAN run | covers both active FY folders under one slot, one wake, one shutdown decision; after a SUCCESS the NAS is always shut down (no "was on" flag). A failed manual run that left the NAS answering on SMB records `nas_left_on` on its last run row: AMBER `nas_left_on` item (cleared by the next run). Failure mail text says "the next manual backup will re-sync" (`flow._next_run_words`) |
| Mails | `_next_text` does not promise an automatic next run for a manual leg; the FY activation mail names which backups copy both folders; `email_compose._facts`, `notify._nas_host` and the support bundle ignore the placeholder NAS path when `lan.enabled` is false |

Config notes: `lan.enabled: false` still needs a UNC `paths.lan_destination` (placeholder) and `wol.enabled: false`
(`wol.enabled` defaults to true and then needs MAC + IP); not relaxed (reported to the owner).

---

## 6. File-by-file map

### Entry points
| File | Role | Key functions |
|---|---|---|
| `launch.py` | agent service main | `main`, `_run_dashboard`, `_ensure_concurrency_limit`, `_cancel_orphaned_runs`, `_reconcile_disabled_legs` |
| `serve.py` | builds Prefect deployments from config | `deployments()` |
| `flow.py` | all Prefect flows + tasks + pipelines | see §3–§5 |
| `ui.py` | dashboard API | `status`, `trigger_*`, `_status_fields`, `get_db`; approvals: `GET /approvals`, `POST /approvals/{id}/approve` (typed phrase, sign-in required), `/run`, `/dismiss` |
| `ui.py` settings (DEC-9) | `/settings` page + `/api/settings` GET/POST + `/api/settings/history`; logic in `core/config_edit.py` (catalog from the pydantic models; locked / risky / secret / effect; candidate loaded exactly like service start; typed phrase for risky keys; refused while the backup lock is held); writer `core/config_file.py` (backup + ruamel round-trip + atomic replace, shared with the FY rollover); table `config_changes` | dashboard, operator |
| `watchdog.py` | Prefect health monitor | `main`, `_is_backup_running`, `_service_state`, `_start_service`, `_restart_stack`, `_resolve_stuck_stop`, `_critical` |
| `restore.py` (CLI) + `core/restore.py` | operator restore from GCS. `core.restore.restore_fy` defaults `forbidden_paths=()`; the CLI always passes `_forbidden_paths(config)` + `--protect` (live source folders, NAS twins, follows the overlap switch). **Rule:** any new caller of `restore_fy` / `restore_from_cloud` (e.g. a dashboard restore) must pass `fy_overlap.protected_folders`, and the parameter becomes keyword-required in that same commit (opus-decision C, 2026-11-23) | operator |
| `collect_config_data.py`, `deploy/*` | install / discovery / restore scripts | — |
| `deploy/04_check_readiness.ps1 [-ServiceAccount .\x]` | `uv sync --frozen`; tools/registry/ports; base Python outside user profiles; config.yaml valid for this version; with an account: runs `probe_service_account.py` AS it (runtime + config folder write, source read, NAS write, SeBackupPrivilege) | operator, before 06 |
| `deploy/06_install_services.ps1 [-ServiceAccount .\x]` | validates config, then NSSM services running `.venv\Scripts\python.exe` (no uv at boot), `AAM_CONFIG` on all three; AamBackupAgent as the account (Modify on runtime folder + config.yaml/folder), server + watchdog LocalSystem | operator |

### `core/` — business logic
| Module | Responsibility | Called by |
|---|---|---|
| `manual_leg.py` | leg mode (off / nightly / manual), last-success age, truth lines for manual-only / off legs (section 5.9) | attention, report, ui, serve, launch, flow, email_compose |
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
| `notify.py` | single alert path: cooldown, incident threading, delivery recorded, SMTP session + attachment ladder, dashboard link | flow, watchdog, report, ui |
| `guards.py` | G1–G5 (`enforce`, `GuardBlocked`, `max_delete_allowance`, `require_source_files`, mount marker) | `flow.lan_guards_task` / `cloud_guards_task` |
| `approvals.py` | approval decisions: statuses PENDING/APPROVED/DISMISSED/USED/SUPERSEDED, `request_phrase`, `evaluate_request`, `evaluate_approval`; storage in `ManifestDB` (`approvals` table) | `flow._apply_approval`, `flow._open_block_request`, `ui` |
| `smb_ready.py` | NAS readiness (port 445 + share) | LAN pipeline, rollover `nas_folder` |
| `rclone_errors.py` | transport-failure classifier (TLS/DNS/AUTH/NETWORK/LISTING) | integrity, cloud probe |
| `rollover_journal.py` | rollover step vocabulary and rules (pure data) | `fy_rollover`, `flow._rollover_resume` |
| `report_period.py` | `ReportPeriod` (calendar and rolling periods, IST) | `report`, `ui`, report flows |
| `faults.py` | fault codes (AAM-xxx-nn) at raise sites | everything that alerts |
| `email_compose.py`, `email_samples.py`, `support_bundle.py` | email layout, one sample per kind, support report + logs zip | `notify`, `tools/preview_emails.py` |
| `admin_auth.py`, `password_setup.py` | PBKDF2 hashes, the two passwords, sessions | `ui`, `deploy/set_dashboard_passwords.py` |
| `config_file.py`, `config_edit.py`, `config_help.py` | the locked config writer; settings catalog / validation / history; plain-language help | `ui`, `fy_rollover`, installer |
| `secrets.py`, `secret_setup.py`, `gcs_key.py` | DPAPI; mail password encryption + readiness check; Google key reader | model, installer, probes, `rclone_config` |
| `netinfo.py`, `attention.py`, `night_strip.py`, `app_restart.py` | dashboard address; banner and `/health`; 30-night strips; "Apply changes" restart | `notify`, `ui` |
| `fy_overlap.py` | FY overlap: `run_date`, `next_fy`, `in_window`, `switch_pending`, `active_folders`, `folder_view` / `folder_for` / `role_of`, `ensure_overlap_activated`, `ensure_overlap_nas_folder`, `sibling_findings` | `flow` (backup, audit, rollover check), `ui`, `report` |
| `overdue.py` | overdue deadline per leg (schedule + budget + grace) | `attention`, watchdog |
| `audit_pending.py`, `budget.py` | audit "newer files wait for the next backup"; run budgets and the schedule proof | `integrity`, `flow`, `models.config` |

`models/config.py` — every setting, validated (pydantic, unknown keys rejected).
`test_support/` — test-only: sandbox config, fake tool runner, real SMTP test server.
`tools/fy_overlap_rehearsal.py` — box rehearsal of the overlap (not production code, §3.4). `audit_lab/test_w25_fy_overlap.py` — the 15-month overlap scenario.

---

## 7. Data written

| Where | What | Writer | Reader |
|---|---|---|---|
| `manifest.db` › `run_history` | one row per leg run: status, exit, files/bytes, error (bounded), extended_metrics JSON (stage timings, artefacts_dir) | `flow._record_run` | ui, reports, `last_successful_run` |
| `manifest.db` › `file_entries` | per-file state per leg and FY (`UNIQUE(fy, relative_path)`, schema v6) | record tasks | metrics, dashboard counts |
| `manifest.db` › `integrity_audits` | weekly audit verdicts | audit flow | ui, reports |
| `manifest.db` › `notifications` | every alert and report email outcome: fault code, thread, attachment rung | `core/notify` | dashboard, banner |
| `manifest.db` › `approvals`, `config_changes` | approval requests and decisions; every settings change | `core/approvals`, `core/config_edit` | dashboard |
| `manifest.db` › `rollover_journal`, `rollover_journal_steps` | the FY rollover steps and their state | `core/fy_rollover`, `flow` | the daily resumer, dashboard step card |
| `manifest.db` › `overlap_activations` | when each next FY folder started to be backed up early (never purged) | `core/fy_overlap` | active set, reports |
| `run_logs\<day>\<date>_<HHmm>_<leg>[_<fy>]_<short id>\` | FULL rclone/robocopy logs + `manifest.json`, gzip > `run_logs.compress_after_kb` | tool_runner / run_artefacts | operator, dashboard link |
| `logs\backup_YYYY-MM-DD.log` | app log (loguru) | all flows | operator |
| `logs\*_svc*.log` | NSSM stdout/stderr per service | NSSM | operator |
| `backup.lock` | `PID:create_time` of the running backup | `_backup_slot` | watchdog, launch |
| Prefect DB (`.prefect`) | flow/task states, Prefect UI logs | Prefect | Prefect UI, ui.py |

---

## 8. Tunables (config.yaml) — what each section controls

| Section | Controls | Main consumers |
|---|---|---|
| `paths` | source, NAS destination, runtime dir, GCS key | everything |
| `lan` | `scheduled` (true; false = manual only, section 5.9), `on_demand_warn_after_days` (30, 1-365), robocopy retries/wait/threads/timeout, flow retries, shutdown policy, `log_tail_kb`, dry-run timeout, `use_backup_mode` (/ZB), `min_free_dest_gb`, `privilege_check_timeout_seconds` | lan_sync, lan_preflight, permissions, flow |
| `wol` | enable, MAC, NAS IP, broadcast, wake timing, `shutdown_*` (delay, command timeout, verify window, closed probes) | wol, smb_ready, shutdown |
| `cloud` | `scheduled` (true; false = manual only, section 5.9), `on_demand_warn_after_days` (30, 1-365), bucket/project/class, bandwidth, transfers/checkers, all rclone timeouts, flow retries, `error_tail_lines`, `dry_run_timeout_seconds`, `write_probe_enabled` | cloud_*, permissions |
| `schedule` | 6 cron expressions + timezone; at load, `core.budget.schedule_problems` (cronsim, 1 year) refuses any slot-job start whose budget could run past the next slot job's start + `maintenance.concurrency_wait_seconds` (WP14) | serve.py, models.config |
| `budget` | per-job run budgets `cloud/lan/audit_max_hours` (from job start, slot wait included) + `cloud_verify_reserve_minutes`, `lan_post_reserve_minutes`. The long steps get `RunBudget.cap(...)`: cloud upload = rclone `--max-duration`, robocopy / checks = timeout. Budget stop = `*_PARTIAL` + `budget_stopped` (cloud: not verified, not recorded as bucket state), audit = `AUDIT_INCOMPLETE`; `extended_metrics.budget` on every run. Manual override: `backup(budget_hours=...)` | flow, core/budget |
| `notifications` | SMTP host/port/security/auth/timeout/retries, `recipients` (the ALERTS list) and `report_recipients` (the REPORTS list), enable flags | report, notify |
| `alerts` | cooldown, body/attachment size | notify |
| `dashboard` | the two password hashes, bind, port, `public_url` (empty = auto), session/rate-limit/refresh, "Apply changes" timings, history nights | ui, notify |
| `rollover` | `archive_alert_after_days`, `final_backup_wait_retries` (2, 0-7: mornings the final backup may only wait for the slot before G6 asks), `overlap_enabled` (true), `early_start_month` (1, 1-3), `activation_alert_after_hours` (24), `sibling_ignore` | fy_rollover, fy_overlap |
| `maintenance` | DB retention, log retention, SQLite busy/vacuum/sync, slot wait | manifest, flow |
| `health` | clock skew limit/timeout, `ntp_server`, `w32tm_timeout_seconds`, `clock_unverified_policy`, min free source GB, rollover gcloud timeouts | health, fy_rollover |
| `run_logs` | full-log folder, retention 90 d, compression, min free GB, service-log retention | run_artefacts, flow |
| `guards` | `max_delete_files` 500, `max_delete_percent` 10, `max_source_shrink_percent` 20, `forbid_local_destination` true, `small_folder_files` 100, `small_folder_max_deletes` 10 (+ `cloud.dry_run_timeout_seconds` 1800) | flow `lan-guards` / `cloud-guards` |
| *(M2)* `approvals`, more `lan` keys (WP4) | added with their consumers | approvals, lan_preflight |

---

## 9. Not built (post-MVP) and accepted risks

- Not built: file-list CSV attachment, separate recipient lists, "test mail settings before
  saving", HTTPS for the dashboard (plain HTTP on the office network, firewall `LocalSubnet`),
  Postgres for Prefect (the SQLite `database is locked` messages are accepted), the LAN leg on
  rclone, an approval for the clock-check "block" policy (it stays a FAILED backup), the weekly
  "backup first" audit chain, encryption of the Google key file.
- Accepted by the owner: ransomware versus the LAN mirror, GCS lifecycle keeping about 2
  versions, locked files (no VSS), no alert when the whole server is down, the size-only daily
  cloud check, the Google key readable by SYSTEM / Administrators / the service account.
- The guards, approvals, truthful statuses and operations work (M2-M4) are described in
  §3, §4 and §10.

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
| C6 | Watchdog (independent service), every `watchdog.stale_check_interval_seconds`: each enabled leg that is OVERDUE -> `stale:<leg>` ACTION_REQUIRED email (same `core.attention` evaluation as the banner; catches runs that never start). Overdue = now past (next scheduled run after the last success STARTED) + the leg's run budget + `dashboard.stale_grace_hours` (`core.overdue`), using the schedule the service registered (`db_meta.schedule_in_effect`, falls back to config). The mail is skipped while a failure/action mail about the latest owed run was SENT/SUPPRESSED, and repeats at most per `alerts.stale_repeat_hours` | yes (M5) | — |
| C6b | Watchdog, every loop (L1, opus-decision 2026-11-23): `notify.send_undelivered_digest` - alert mails (ACTION_REQUIRED/FAILURE/WARNING/INFO) recorded FAILED because the mail server was down, with no later SENT row of the same key, newer than `alerts.undelivered_digest_max_age_hours` (72, 0 = off) and than the last SENT `undelivered:digest` row -> ONE digest mail naming them; a failed attempt is retried at most every `alerts.undelivered_digest_retry_minutes` (30). A RECOVERED mail after an undelivered failure is never suppressed and says the failure alert was lost | yes | — |
| C5b | Agent start (`launch._check_db_identity`, L2, opus-decision 2026-11-23): `core/db_identity` - the DB meta `created_at` is on a LATER day than the oldest run-log day folder under `config.run_logs_dir` (a lost/renamed `manifest.db` was silently re-created) -> meta `db_recreated_detected`, ONE `db:recreated` ACTION_REQUIRED mail, and a red attention item until an admin presses Acknowledge (`POST /attention/db-recreated/ack` sets meta `db_recreated_ack`). Backups are NOT blocked. No run-log folder (fresh install) or no `created_at` (old DB) = no alarm | no (loud only) | — |
| C5 | Earlier runs whose process died (`*_RUNNING` row, owner `pid:create_time` gone): recorded `*_ABANDONED` + one FAILURE email - at agent start and at every pipeline start (`backup_repository.sweep_abandoned_runs`); an unreadable owner falls back to `maintenance.abandon_running_after_hours` | yes (M5) | — |

| C7 | FY overlap (every backup flow run, before the legs): the next FY folder is activated when it is time (clock proven directly, parent exists, not a link); a failure is retried every run, never stops the configured FY's backup, and escalates to ACTION REQUIRED after `rollover.activation_alert_after_hours` | yes (OV-2.7) | — |
| C8 | `fy=` (flow argument or `?fy=` of a manual trigger) names an active FY, else refused | yes (OV-3.2, OV-7.3) | — |
| C9 | Every gate below is evaluated per FY folder (own source, prefix, NAS folder, baseline, approval); a second folder with no budget left is `<LEG>_MISSED` for its FY | yes (OV-3.x) | — |

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
| CL12 | Planned deletions (`rclone sync --dry-run`, within `cloud.dry_run_timeout_seconds`) <= `guards.max_delete_files` and <= `guards.max_delete_percent` of the destination (`rclone size`); on a retry the deletions of earlier attempts count too | `cloud-guards` **G3** | yes (2.2); real sync gets `--max-delete` = what is left of the run's allowance; rclone stopping there (exit 7) → re-measure → **CLOUD_BLOCKED** + approval, never retried (XR-1) | — |
| CL13 | Cloud write permission: `rclone touch` + `rclone deletefile` of `<bucket>/_aam_preflight/probe_<run_id>` (outside every FY prefix; cause named via `rclone_errors`) if `cloud.write_probe_enabled` | `cloud-access` | yes (2.4) → `CLOUD_FAILED` | — |
| CL12a | Bucket/prefix unchanged since last success of the same FY | `cloud-guards` **G4** | yes (2.2) | — |
| — | Any G-check blocks: `CLOUD_BLOCKED` row with the guard record, nothing transferred, ONE approval request + ONE "ACTION REQUIRED - open the dashboard" email (`core.notify`, key `blocked:cloud`, cooldown); approve once on the dashboard re-runs with `approval_id` | `flow`, `ui` | yes (2.2, 2.3) | — |

### 10.3 Cloud leg — must hold for the run to count as SUCCESS

| # | Condition | Code |
|---|---|---|
| CS1 | rclone exit 0 gives `CLOUD_COMPLETE`; exit 9 gives `CLOUD_NO_CHANGES_COMPLETE` only when the final stats show 0 transfers, deletes, deleted dirs and renames (a deletes-only sync also exits 9), else `CLOUD_COMPLETE` (XR-3) | `classify_rclone_exit`, `stats_prove_no_changes` |
| CS2 | No ERROR/CRITICAL lines in the **final** attempt (after rclone's last `Attempt k/N failed`; its `Attempt k/N succeeded` notice excluded), no plaintext "Failed to ...", and final `stats.errors` = 0; otherwise exit 0 becomes `CLOUD_SUSPECT` and exit 9 becomes `CLOUD_FAILED`. Failed internal attempts are logged (XR-4) | `scan_rclone_log_for_errors` |
| CS3 | Exit 4/5 gives `CLOUD_PARTIAL`; exit 10 (rclone `--max-duration`, run budget or config cap) gives `CLOUD_PARTIAL` with verify skipped (XR-5); 1/2/3/6/7/8/other, timeout or spawn error gives `CLOUD_FAILED` | same, `cloud_sync_task` |
| CS4 | Up to `cloud.max_attempts` attempts, `cloud.retry_delay_seconds` apart, **only** for exit 1/4/5 not marked fatal in rclone's final stats, with known deletions, a delay that fits the budget, and guards re-measured (cumulative G3) first. SUSPECT, timeouts and exits 2/3/6/7/8 run once. Run row keeps the real exit code + `sync_attempts` (XR-1/XR-6) | `flow._sync_with_retries`, `rclone_retryable` |
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
| LS5 | Up to `lan.max_attempts` attempts, `lan.retry_delay_seconds` apart, only for LAN_FAILED that is not a timeout, with a known deletion count (`*EXTRA File` lines) and a delay that fits the budget; before a repeat the robocopy /L listing, destination count, G1-G5 (cumulative G3) and access probes run again. SUSPECT runs once. Hard timeout `lan.subprocess_timeout_seconds` (XR-2/XR-6) | `flow._sync_with_retries`, `run_lan_sync` |
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
| R1 | The SOURCE path MUST end in a 2-digit FY folder (`FY26-27`) whichever backup legs are on (config load refuses otherwise; owner 2026-10-22). The NAS path must end in the SAME FY folder while the NAS leg is on; with the NAS off it is an unused placeholder that nothing reads or rewrites. The source leaf alone decides the FY (`fy_rollover.configured_fy`). Only the current FY is backed up; older years are bulk-uploaded at deployment by a separate tool | yes | done |
| R2 | Rollover happens when that FY differs from the FY of today's date (IST, April 1 boundary) | only exactly the next FY on a proven clock; clock behind (CLK-03), not proven (ROL-03) or a jump of more than one FY (G6 → approval card) stop it | done |
| R3 | Final backup of the closing FY must succeed on every enabled leg (cloud COMPLETE/NO_CHANGES, LAN COMPLETE) + G6, else the step fails (ROL-01/02), retried daily; the LAN one does not shut the NAS down | through the normal pipelines inside the slot; a retry repeats it (D-17) | done |
| R4 | Archive the old FY in GCS (gcloud, key as a file, `ARCHIVE` class) | failure = ROL-05 WARNING, retried every morning, ACTION_REQUIRED after `rollover.archive_alert_after_days` | done |
| R5 | New source FY folder + placeholder; NAS folder + mount marker; `config.yaml` switched by compare-and-set under the config lock (backup, validate, restore on failure) | each step journaled and resumable; a failure before the switch = ROL-04 ACTION_REQUIRED; a NAS that is off does not stop the switch (the next LAN pipeline completes the folder) | done |
| R6 | Every step state is persisted (`rollover_journal`); a crash is swept as ABANDONED; the INFO email says "nothing to do" only when every step is finished | — | done |
| R7 | The new local FY folder is created BEFORE the final backups (`rollover.create_source_folder_early`, default on), only for exactly the next FY on a proven clock. With overlap on it is normally already active and backed up from 1 January; the old "new year not backed up" alarm (AAM-ROL-06 `unprotected_new_fy`) is gone, replaced by the sibling check (a misnamed or inactive newer FY folder -> AAM-ROL-07, §3.4). `flow._resolve_unprotected_new_fy` only closes such an alarm that was already open | step SOURCE_FOLDER first in `_rollover_resume`; `fy_overlap.sibling_findings` | done |
| R8 | Settings never change the FY (409, `config_edit._refuse_fy_change`); both paths end in an FY folder or neither; a finished rollover is never re-run when the paths are put back (`rollover:reentered:<fy>`); the Advanced picker (`core/source_picker.py`) chooses only the PARENT folder, never creates folders | done | done |
| R9 | FY overlap: from `rollover.early_start_month` until the switch both FY folders are backed up on every enabled leg (each FY 15 months); the switch is journaled, `config_switch` is the only step that counts as "switched", and the closed FY is closed (§3.4) | `fy_overlap.active_folders`, `flow._close_old_fy` | done (not yet on the trial box, OV-11.4) |
