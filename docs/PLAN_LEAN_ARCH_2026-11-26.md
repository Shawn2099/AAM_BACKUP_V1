# Lean architecture plan - one rclone engine, no Prefect (2026-11-26)

Opus (lead architect), read-only design; no product code changed. Based on the 2026-11-26 review + appendix + research,
the 2026-11-25 design and the code on `rclone-engine` (HEAD 5971804 + uncommitted AF-1..17; file:line refs are to that
tree). UNVERIFIED = from research or memory; a spike (section 9) must confirm it before anyone builds on it.

## 1. Goals, non-goals, principles

- **Goal:** the same protection as `rclone-rc1`, with 2 services instead of 3, one scheduler, one truth (the SQLite DB),
  no once-a-year state machine, and mail only when a human is genuinely needed. About 30 % less production code.
- **Non-goals:** new data-path features; changing rclone flags; dead-man's switch / external heartbeat (owner: later);
  ransomware vs the mirror; GCS version depth; VSS.
- **P1 Freeze the data path.** `cloud_sync`, `lan_sync`, `cloud_verify`, `integrity`, `tool_runner`, `lan_versions` stay
  exactly as `rclone-rc1` ships them. This migration changes orchestration, alerting and the FY model only.
- **P2 Reuse before writing.** APScheduler fires jobs, tenacity retries, filelock excludes, cronsim parses cron, psutil kills.
  Custom code stays only where a library has no notion of our truth (catch-up from `run_history`, delete budgets).
- **P3 Fail closed only where data is at risk.** Wrong destination or empty source: block and ask. Everything that
  copying cannot harm (a mass delete, a measurement we could not take) goes copy-only and keeps protecting new data.
- **P4 A folder only ever goes to its own destination.** `<source_root>\FY27-28` -> `<nas_root>\FY27-28` and
  `gs://bucket/FY27-28`. The date decides WHICH years run, never WHERE they go. So a wrong clock can at worst add or
  skip a pass; it can never write one year into another.
- **Trade-off:** APScheduler is a new dependency with a known trap (silent skip past the grace) and a probable one (3.x
  numeric day-of-week); section 2.2 neutralises both. Our own cronsim loop would be dependency-free and similar in size;
  I still recommend APScheduler: the owner asked for it, and its executor/misfire code is code we would otherwise own.

## 2. Target architecture

### 2.1 Diagram and process model

```
Windows Server 2016
 [Service] AamBackupAgent  (NSSM, service account)  .venv\Scripts\python.exe launch.py
   |- thread: dashboard (FastAPI/uvicorn :8080)  -- reads DB + in-process job registry only
   |                                               "Run now"/"Approve and run" -> jobs.enqueue()
   |- APScheduler 3.11.x BackgroundScheduler (tz Asia/Kolkata, MemoryJobStore, CronSimTrigger)
   |     executor "backup" (1 worker): cloud, lan, audit          (serial by construction)
   |     executor "light"  (2 workers): weekly, monthly, daily (06:00), heartbeat (every 60 s, in-process)
   |     listeners: EVENT_JOB_MISSED / MAX_INSTANCES / ERROR -> run_history row + log (no silent skip)
   |     start-up: sweep ABANDONED -> catch-up from run_history -> add jobs -> start
   '- every job except heartbeat = CHILD PROCESS:  python -m aam run <job> --run-id <id> --scheduled-for <iso>
          child: filelock(backup.lock) + PID token -> PREFLIGHT -> per FY pass: ENGINE (cloud|lan: backup+verify)
                 -> run_history row (finally) -> INCIDENTS (classify, mail if breaking)
          parent kills the child's process tree at budget + margin (psutil), records the kill
 [Service] AamWatchdog  (NSSM, LocalSystem)  python watchdog.py
     60 s: agent SCM state, heartbeat age, backup lock -> start / restart (never under a live lock)
     15 min: overdue legs (core.overdue) -> mail;  daily: DB quick_check, runtime disk free
 [Task] AamRestarter  (Task Scheduler, runs as SYSTEM, at boot + every 5 min): `sc start` each service if STOPPED
 Runtime C:\BackupAgent: manifest.db (WAL) | backup.lock (+ .owner token) | heartbeat.json | config.lastgood.yaml
                         | run_logs\<day>\<run>\ | logs\
        | rclone sync/copy (local -> UNC SMB 445)          | rclone sync (HTTPS)
        v                                                  v
   NAS \\nas\share\FY..  (+ AAM_versions\<run>)       GCS gs://<bucket>/FY../
```

**Services: 2 (agent, watchdog) + 1 stateless SYSTEM task.** Today: 3 services plus the Prefect DB.
### 2.2 Scheduler decision: APScheduler 3.11.x in-process (not a Task Scheduler tick)

| Option | Verdict | Why |
|---|---|---|
| APScheduler 3.11.x in the agent | **chosen** | One process already runs the dashboard; the dashboard can enqueue and read queue state exactly (fixes the 200-run bug by construction). Fake-clock testable. Schedule source = config.yaml only. |
| Task Scheduler every 5 min + `aam tick` | rejected | Needs its own due/missed state anyway, the dashboard still needs a service, history is split between the OS and our DB, "run whether logged on" with a stored password fails silently on a password change (research R1). Kept only as the stateless restarter, run as SYSTEM (no stored password). |
| APScheduler 4.x | rejected | Pre-release, "not for production" (R1). |
| SQLAlchemy job store | rejected | Adds SQLAlchemy, pickles jobs as a second truth that must be reconciled with config edits, and knows when a job fired, not whether the backup succeeded. |

How each known failure mode is handled:

| APScheduler failure mode | Design answer |
|---|---|
| `misfire_grace_time` default is small (reported 1 s, UNVERIFIED) -> late run silently skipped | Set per job explicitly: backup legs = their run budget (`budget.cloud_max_hours` ...), reports 12 h, daily 6 h. A listener on `EVENT_JOB_MISSED` writes the existing `<LEG>_MISSED` row, so a skip is never silent. |
| Queued job in a 1-worker pool runs late | `run_job` re-checks the grace when the worker picks it up (UNVERIFIED, spike S1); either way the child re-checks with the existing `RunBudget` (`flow._missed_if_no_budget`, flow.py:573) and records MISSED. |
| `coalesce` | `True` on every job: several missed runs collapse into one. |
| `max_instances` | 1 on every job; `EVENT_JOB_MAX_INSTANCES` -> log + one row, never silent. |
| MemoryJobStore forgets misses across a restart | Our start-up catch-up (about 40 lines): for each scheduled job, `due` = latest cron time <= now (cronsim); if `run_history` has no row with `scheduled_for == due` and `now - due <= grace`, enqueue once with `scheduled_for=due`; if past the grace, write one MISSED row. Truth = `run_history`, as today. |
| 3.x `CronTrigger.from_crontab` numeric day-of-week (0 = Monday, not Sunday; known 3.x trap, UNVERIFIED in 3.11) and DST bugs fixed only in 3.11.1+ | We never use APScheduler's cron parser. `CronSimTrigger(BaseTrigger)` (about 25 lines) returns cronsim's next time in IST, so the scheduler, `core.overdue`, `core.budget` and the mail "next run" text share ONE cron semantics. IST has no DST; a test with `Europe/London` proves the trigger anyway. |
| Wall-clock jump (NTP correction) leaves the wait sleeping too long | The heartbeat job calls `scheduler.wakeup()` every 60 s, so due times are re-evaluated against the wall clock at least once a minute. |
| Single-process crash stops all jobs | Jobs run as child processes: a crashing backup cannot take the scheduler or dashboard down. If the agent itself dies, NSSM restarts it (AppExit Restart); start-up catch-up runs; the watchdog restarts a wedged agent via the stale heartbeat; the SYSTEM task restarts a stopped watchdog. |
| Scheduler thread dead but process alive | Heartbeat is written BY a scheduler job (proves firing, not just liveness) and lists every job's next run time; stale > `watchdog.heartbeat_stale_seconds` (300) and no live lock -> watchdog restarts the agent. |

Why a child process per job: it keeps today's semantics (Prefect already runs every flow run as a subprocess, and
`backup.lock` holds that process's `PID:create_time`), it gives a real tree kill at the budget, and memory is
returned after each run on the DDR3 box. Cost: about 1-2 s Python start-up per job (spike S8).

### 2.3 Single-run lock: keep `core/process.py`, move exclusion to filelock

Decision: **exclusion = `filelock.FileLock(backup.lock, timeout)`** (filelock 4.2.2 is already a locked, direct
dependency, used by `core/config_file.py:26`), held by the child for the whole run. **Observability = the existing
`PID:create_time` token** written beside it (`backup.lock.owner`) for the watchdog, the dashboard and the abandoned sweep.

- The OS releases the lock when the process dies (handle closed), so there is no stale lock, no double-read takeover
  and no unlink/create race (process.py:197-221, Appendix A2 MED). A reboot cannot leave a lock behind.
- Antivirus opening the token file can no longer make a dead lock look alive (process.py:117-120 returns
  `alive=True` on PermissionError, Appendix A2 MED / review #11). Readers treat PermissionError as "unknown", retry,
  and after 10 min of "unknown" the watchdog mails instead of silently deferring forever.
- Waiting: the backup executor is serial, so the child only waits when an outside process (CLI tool, harness) holds
  the lock: `timeout = maintenance.concurrency_wait_seconds`, then the existing `<LEG>_SLOT_TIMEOUT` row.
- Rejected: portalocker (new dependency, no gain over filelock); keeping the hand-made O_EXCL lock as the exclusion
  (it needs the race and PermissionError fixes and still cannot survive a reboot cleanly).
- `resolve_binary` (process.py:233-246): with `tools.rclone_version_policy=block`, never fall back to PATH.

## 3. Layers and module map

```
scheduler (core/scheduler.py, core/jobs.py, aam/__main__.py)
  -> preflight (core/preflight.py + core/guards.py, core/smb_ready.py, core/wol.py)
  -> engine: core/tool_runner.py + core/cloud_sync.py / core/lan_sync.py (flags frozen)
  -> legs: core/cloud_leg.py (sync + size check), core/lan_leg.py (WoL, mirror, arrival + hash, versions, shutdown)
  -> recovery (core/recovery.py, tenacity)          -> data (core/manifest.py, core/run_artefacts.py)
  -> incidents (core/incidents.py) -> mailer (core/mailer.py) + emails (core/emails.py)
  dashboard (ui.py) and watchdog (watchdog.py) read the data layer only
```

### 3.1 Scheduler layer
- `core/scheduler.py` (NEW, about 220 lines): builds the job table from `schedule.*` (cloud, lan, audit, weekly,
  monthly, daily); `CronSimTrigger`; executors; listeners; start-up catch-up; heartbeat; `reload()` after a Settings
  change (replaces the NSSM restart of "Apply changes" for schedule-only edits; other edits keep `core/app_restart.py`).
- `core/jobs.py` (NEW, about 170): `enqueue(job, scheduled_for, approval_id=None)` under one `threading.Lock` that also
  checks the in-flight registry and the live lock (fixes the check-then-create race, Appendix A6 MED); spawns the
  child; kills its tree at budget + `budget.kill_margin_minutes`; maps the child exit code to a log line (the child
  writes its own row).
- `aam/__main__.py` (NEW, about 80): `python -m aam run <job>`: load config, open DB, take the lock, dispatch.
- Manual-only legs (`scheduled: false`): simply not added as cron jobs; still enqueued by the dashboard. Disabled legs:
  not added, not enqueuable. This deletes `launch._reconcile_disabled_legs` (launch.py:252) and `serve.py`.

### 3.2 Preflight layer (`core/preflight.py`, MERGE of health, cloud_preflight, lan_preflight, permissions)

Two kinds of check, run once per leg (environment) and once per FY pass (data).

| Check | Kind | On failure | Human? |
|---|---|---|---|
| Source root exists + listable; FY folder exists | env | FAILED, retry next run | only if repeated (2 runs) |
| rclone binary + major.minor version (health.py:77) | env | FAILED (policy block) | yes, at once (does not self-heal) |
| GCS key file present; probe `lsjson --max-depth 0` (cloud_preflight probe B) | env | FAILED; transport vs AUTH via `rclone_errors` | AUTH: at once; network: if repeated |
| Clock skew (health.check_clock) | env | SKEWED -> FAILED | if repeated |
| NAS wake + `smb_ready` (once per LAN job) | env | FAILED | if repeated |
| Destination write probe + free space (incl. `--backup-dir` growth, RF-4) | env | FAILED | NAS full: at once |
| **G1** source has a real file (guards.py:73) | data | **BLOCKED**, nothing runs | yes (empty source = data at risk) |
| **G4** destination identity vs same-FY baseline; never this machine (guards.py:168) | data | **BLOCKED** | yes. "Cannot resolve host" = FAILED, not BLOCKED |
| **G5** mount canary (guards.py:202) | data | missing/empty NEW folder -> auto-initialise (`ensure_nas_folder`, fy_overlap.py:193); marker missing on a populated share -> **BLOCKED** | yes |
| **G3** mass delete from the one dry-run (guards.py:118, `plan_lan_mirror` lan_sync.py:244, `measure_planned_deletes` cloud_sync.py:330) | data | **copy-only run** (new data protected), deletions held, DECISION card | decision (not urgent) |
| G3 not measurable (dry-run failed or timed out) | data | **copy-only run**, status PARTIAL "deletions not measured" | only if repeated |
| G2 source shrink | - | **removed as a gate** (G3 covers it; already gone on cloud, commit 0595922); the count stays as a report trend | - |
| G6 rollover | - | **deleted** with the journal (section 3.10) | - |

How false BLOCKED at 2 am is avoided: (1) BLOCKED comes only from G1, G4, G5 and the `--max-delete` backstop; (2) a
measurement that could not be taken is never BLOCKED; it is FAILED, or copy-only when copying is still safe;
(3) a transient environment fault is FAILED and retried; it is mailed only when it repeats; (4) files changed during the
run are "pending, next night" (AF-12 recheck, `audit_pending`), never a failure; (5) G5 approval matched on guard
code + destination (Appendix A2 MED), so one approval cannot clear a different problem.

### 3.3 Engine (reuse `core/tool_runner.run_tool`; flags frozen at rclone-rc1)

| Step | Command (as built today) | Code |
|---|---|---|
| Dry-run (all data guards) | `rclone sync --dry-run --use-json-log ...` (LAN adds `--track-renames` hash, `--backup-dir`) | cloud_sync.py:330, lan_sync.py:244 |
| Cloud backup | `rclone sync <src> aam_gcs:<bucket>/<FY> --check-first --modify-window --max-delete <left> --error-on-no-transfer --exclude NTFS_SYSTEM_EXCLUDES --use-json-log --log-file` (copy-only: `rclone copy`, no `--track-renames`, AF-2) | cloud_sync.py:495 |
| LAN backup | `rclone sync <src> \\nas\share\<FY> --check-first --track-renames --create-empty-src-dirs --skip-links --modify-window 2s --backup-dir \\nas\share\AAM_versions\<run> --max-delete <left> --retries <lan.retry_count> ...` | lan_sync.py:57 |
| Cloud verify (nightly) | `rclone check --one-way --size-only --combined` + one recheck (AF-12) | cloud_verify.py:264 |
| LAN verify (nightly) | arrival check (size, combined) + MD5 of tonight's transfers (`--files-from-raw`) | lan_sync.py:372, :434 |
| Weekly audit | `rclone check` with hashes, both legs | integrity.py:170 |
| Never | `--ignore-errors`, `--ignore-checksum`, `--inplace`, sync `--size-only`, nightly `--checksum`, `--local-no-check-updated`, `--immutable`, `--local-nounc` | research R2, OPUS_DESIGN 1.5 |

Not adopted here (P1): research ideas `--fast-list`, GCS `--modify-window 1s`, more transfers (spike S5, separate change).

**Exit-code policy (one table for both legs, extends `classify_rclone_exit` cloud_sync.py:443 and
`decide_lan_rclone_result` lan_sync.py:116):**

| Exit | Meaning | Status | Retried in-run? |
|---|---|---|---|
| 0 | success | COMPLETE if final stats `errors == 0` and the log scan is clean; else SUSPECT; no final stats -> SUSPECT on both legs (Appendix A1 LOW) | no |
| 1 | errors | only source-read/locked-file errors -> PARTIAL (deletes held, files named); transport errors (`rclone_errors`) and no fatal flag -> FAILED-retryable; else FAILED | transport only |
| 2 | usage | FAILED; breaking (our bug or rclone changed) | never |
| 3 | dir not found | FAILED | never |
| 4 | file not found | PARTIAL (vanished during the run) | never |
| 5 | temporary | FAILED-retryable | yes |
| 6 | no-retry error | FAILED | never |
| 7 | fatal; with `max_delete_reached` = backstop | backstop -> BLOCKED (copy-only next run), else FAILED | never |
| 8 | `--max-transfer` (not used) | PARTIAL | never |
| 9 | nothing transferred | NO_CHANGES only if stats prove 0 changes, else COMPLETE (XR-3) | no |
| 10 | `--max-duration` (budget) | PARTIAL, verify skipped | never |
| kill | timeout / S6 stall watchdog | FAILED; retryable once only if deletions so far are known | once |

### 3.4 Cloud module (`core/cloud_leg.py`, from flow.py:1713-2112)
Per FY pass: dry-run -> G1/G4/G3 -> sync (or copy-only) via the recovery layer -> size check + recheck -> gate
(D3, flow.py `evidence_ok`) -> row. Plain functions; `run_id`, `scheduled_for`, `budget` are arguments. Delete:
Prefect tasks/`with_options`, markdown artifacts (flow.py:1561), `cloud_record_task`/`file_entries`, `_cloud_diff_count`.

### 3.5 LAN module (`core/lan_leg.py`, from flow.py:2113-2562)
Per LAN job: WoL + `smb_ready` once -> per FY pass: `ensure_nas_folder` -> dry-run -> G1/G4/G5/G3 -> mirror (or
copy-only) via recovery -> arrival check + transfer hash -> versions purge (`lan_versions`) -> row. **NAS shutdown
once per job in the job's `finally`**, one function: `shutdown_after_backup and woke_it (shutdown_only_if_woken)`
and every pass ended COMPLETE (or `shutdown_on_all_retries_exhausted`). This fixes the failure-path shutdown that ignores
`shutdown_only_if_woken` (flow.py:2492-2514) and "last pass never ran, NAS stays on" (flow.py:3707), Appendix A5 MED.
A G3 copy-only branch keeps the `GuardBlocked` record when the access probe raises (Appendix A5 MED, flow.py:1795/2232).
Start of a LAN job: delete `*.partial` orphans older than 1 day in the destination (`rclone delete --include`).

### 3.6 Recovery layer (`core/recovery.py`, tenacity 9.x, replaces `flow._sync_with_retries` flow.py:844-916)

| What | Policy | Bounds (config) |
|---|---|---|
| Sync attempt (both legs) | `Retrying(stop=stop_after_attempt(n) OR next wait does not fit the budget, wait=wait_fixed(d), retry=retry_if_result(retryable))`. Before each re-try: **re-run the dry-run and G3 cumulatively**: `deleted_so_far = sum(counted_deletes(log))` over all attempts (cloud_sync.py:246); unknown -> stop, FAILED "deletions unknown"; next `--max-delete = remaining_delete_allowance(dest_files, deleted_so_far)` (guards.py:355). This fixes "a LAN stall retry gets a fresh `--max-delete`" (Appendix A1 MED). | `recovery.cloud_max_attempts` 3, `recovery.lan_max_attempts` 2, `recovery.retry_delay_seconds` 300/600 (today's `cloud/lan.max_attempts`, `retry_delay_seconds`, renamed) |
| Retryable | exit 5; exit 1 transport-only; one stall kill with known deletes. **Never 2, 3, 4, 6, 7, 8, 9, 10, SUSPECT, budget kills.** | - |
| Quick network probes (GCS lsjson) | `stop_after_attempt(2)`, `wait_fixed(30)` | existing `cloud.probe_retries`, `probe_retries_sleep_seconds` |
| NAS wake | the bounded loop in `wol.ensure_server_online` (keep, it is already a bounded retry) | `wol.*` |
| SMTP | tenacity in `notify.send_email` (keep, notify.py:349) | `notifications.retry_*` |
| SQLite busy | `manifest._retry_on_lock` (keep, manifest.py:403) + `_record_run` retries (AF-13) | `maintenance.*` |
| Across nights | no code: the next scheduled run is the retry; the incident ladder decides when a human hears | `alerts.escalate_after_runs` |
| Inside rclone | `--retries` / `--low-level-retries` stay the inner retry; tenacity is only the outer one | `lan/cloud.retry_count` |

### 3.7 Data layer (`core/manifest.py`, keep + fix)
- **Transactions:** one `_tx()` context manager (`BEGIN IMMEDIATE`; commit; `rollback()` on any exception) wraps every
  write (insert_run manifest.py:729, record_notification :1066, record_audit :893, `_set_meta` :1250, ...). This fixes
  "after `database is locked` the transaction stays open and reads go stale" (Appendix A3 MED). Each child process has
  its own short-lived connection; the dashboard and watchdog use `with self._tx()` too.
- **Tables kept:** run_history, integrity_audits, notifications, approvals, config_changes, db_meta. **Dropped from use:**
  `file_entries` (the per-file state; its only readers are the dashboard counters and the cloud cross-check, both
  replaced by rclone counts from the last run) and `rollover_journal*` (left in the file, never read). No destructive
  schema change, so a rollback to rc1 can read the same DB.
- **Retention (daily job):** run_history older than `maintenance.db_retention_days`, but always keep the last success
  and the last 30 rows per (leg, FY) (fixes "age retention drops the last success of a dead leg", Appendix A3 LOW);
  notifications, config_changes, closed approvals, integrity_audits by age (`maintenance.history_retention_days`, 365);
  **size cap** `maintenance.db_max_mb` (200): above it, purge the oldest unprotected rows, then VACUUM when the free list
  exceeds `sqlite_vacuum_freelist_threshold`. `latest_run_since` filters the real `*_RUNNING` statuses (Appendix A3 LOW).
- Run row adds `extended_metrics.scheduled_for` and `extended_metrics.fy` on every row (FY already on most).

### 3.8 Reporting and alerting layer (one coherent layer)

Today the decision "does a human hear about this?" is spread over attention (580 lines), overdue, alert_class,
faults, messages and notify (734). Target:

| Module | Holds | From |
|---|---|---|
| `core/faults.py` (data) | each fault code -> family, **class**, operator text | faults.py trimmed (Prefect, slot, journal, archive codes deleted) |
| `core/incidents.py` | `on_run_end(row)`, `on_overdue(leg, fy)`, `on_watchdog(...)`; one incident per (leg, FY, family); open / escalate / remind / resolve; dashboard status view (banner, leg cards, night strip) | attention + overdue + alert_class + night_strip |
| `core/mailer.py` | SMTP session, attachment ladder, recipients, delivery record, cooldown | notify.py |
| `core/emails.py` | rendering only: alert body, weekly/monthly report, support bundle | email_compose + report + support_bundle |
| `core/messages.py`, `core/statuses.py` | vocabulary | keep |

**Classes (decided from the fault code, never from text):**

| Class | Examples | Mail |
|---|---|---|
| BREAKING_NOW | G1/G4/G5 block, backstop hit, AUTH 401/403, rclone missing/wrong version, NAS or runtime disk full, config invalid, DB unreadable, weekly audit REAL differences, child killed at the budget twice | one ACTION_REQUIRED mail when the incident opens |
| BREAKING_IF_REPEATED | network down, NAS not waking, clock skew, source unreadable, FAILED/SUSPECT/PARTIAL of any kind, MISSED, ABANDONED | none for the first run; mail when the same family repeats on `alerts.escalate_after_runs` (2) consecutive runs of that leg+FY, or when the leg becomes overdue |
| DECISION | G3 deletions held (copy-only keeps protecting), previous-FY close-out not done after `rollover.close_out_alert_days` | one mail + the existing reminder ladder (`approvals.reminder_days`) |
| INFO | FY activated, FY closed (final copies), password changed, reports | reports list, once |
| RESOLVED | first verified success after a mailed incident | one mail, closes the thread |

Cooldown and threading stay as in notify (`incident_group`, notify.py:590). Fixes folded in: RecipientsPartlyRefused
counts as SENT_PARTIAL and feeds the cooldown, and a per-recipient 4xx is retried, not treated as permanent
(Appendix A3 MED); the `catching_up` exemption only holds while a newer run row exists, capped at 2 days
(Appendix A3 MED, attention.py:132-157); the approval reminders call the real `open_approvals()` and use per-guard text
(Appendix A7 MED, flow.py:2792); `waiting_summary` reads `guards` (Appendix A7 LOW-MED). Ruled out: the dead
`report.send_failure_alert` (report.py:44).

### 3.9 Dashboard (`ui.py`, keep + modify)
- Reads the DB and, in-process, `jobs.registry` (queued/running) and `scheduler.get_jobs()` (next run). **No Prefect
  query left**, so the 200-run cap that can hide the one RUNNING run (Appendix A6 HIGH, ui.py:229-341) and the Apply busy
  check (A7 MED, ui.py:1371) disappear by construction: "busy" = live lock token or registry entry.
- Triggers (ui.py:875, 917, 1648, 1094) -> `jobs.enqueue`; the duplicate check is inside the enqueue lock.
- Rollover card (ui.py:1501-1574) -> "Financial years" card: active passes, activation date, close-out state per leg.
- Carried: Origin check on POST (A6 MED); auth on `/docs`, `/openapi.json` (A6 LOW).

### 3.10 Rollover and activation: date-derived passes (DECIDED)

Given "a closed FY needs no further sync after Apr 1", the journal, config rewrite, G6, archive step, re-entered and
hand-switch detection, race guards and overlap records have no remaining job. **Replace them with one pure function**
(`core/fiscal_years.passes(now, config, db) -> list[FyPass]`), evaluated at the start of every backup and audit job:

| Pass | Included when | Order |
|---|---|---|
| previous FY (close-out) | today >= 1 Apr of the current FY **and** this leg has no row for `fy=previous` with `started_at >= 1 Apr 00:00 IST` and a verified status (`FINAL_COPY_STATUSES`, the existing R11 rule, `final_copy_state` fy_overlap.py:464 incl. AF-4) | first |
| current FY | always (FY of today, IST, 1 Apr boundary) | second |
| next FY | today >= `rollover.activate_on` (Dec-Mar, AF-16) **and** `<source_root>\<next>` exists | third |

Config holds `paths.source_root` and `paths.nas_root` (the parents). The old `source_drive`/`lan_destination` with
an FY leaf are accepted as deprecated aliases (parent = their parent), so the box config loads unchanged.

The **daily job (06:00)** does: log/DB retention; activation (on a proven clock, for exactly the next FY: create
`<source_root>\<next>` + placeholder, refuse links/junctions, INFO mail once; existing `activate_if_due`
fy_overlap.py:234 minus the overlap record); close-out watch (INFO "FY26-27 closed" when every enabled leg has its
closing copy; DECISION mail after `rollover.close_out_alert_days` (7) without one, with a dashboard card "stop
backing up FY26-27 without a closing copy"); approval reminders.

Year walk-through:
- **Dec 31 (activation):** daily job creates `D:\Data\FY27-28` + placeholder. From that night every leg runs 2 passes;
  the LAN pass creates `\\nas\share\FY27-28` + marker after WoL (`ensure_pass_nas_folder`). Server off on Dec 31: the
  first daily job after boot activates.
- **Mar 31 -> Apr 1:** nothing switches. The first cloud run after 1 Apr 00:00 (02:01) runs FY26-27 (close-out) then
  FY27-28 (now "current"). Its verified FY26-27 row is the closing copy; from the next run FY26-27 is gone. The LAN
  does the same at 21:01. Each leg closes independently.
- **Overlap end:** implicit: the year is not in the list once its closing copy exists. No `ended_at`, no `end_overlap`.
- **Crash or reboot mid-"rollover":** there is no rollover transaction. A killed pass is ABANDONED; the next run
  re-derives the same list (close-out still pending) and repeats it. Idempotent by construction.
- **NAS off on Apr 1:** the LAN close-out fails; the year stays in the LAN list; the cloud has already closed its
  copy; DECISION mail after 7 days if it never succeeds.
- **Missed Apr 1 (server off for days):** first run after boot includes the previous FY until its closing copy exists.
- **Clock wrong:** P4 guarantees no cross-year writes. Behind: current FY = an old folder (still its own destination;
  a harmless re-sync) or a missing folder (FAILED); the clock check (SKEWED) fails the run anyway. Ahead past Apr 1: a
  "closing copy" is taken early, but after the clock is corrected the year is "current" again and keeps syncing.
  Only the activation (folder creation) is one-way, and it already requires a proven clock.
- **Archive step:** deleted (review section 7); the 365-day lifecycle rule moves old objects to ARCHIVE. Removes
  gcloud and the daily retry (review MED #7). Owner question Q3.

Cost: about 250 lines replace about 1,500 (fy_rollover 559, fy_overlap 628, rollover_journal 108, `_rollover_resume`
and helpers in flow.py ~700, UI/attention parts).

### 3.11 Watchdog (`watchdog.py`, rewrite, about 300 lines from 836)

| Check (interval) | Action |
|---|---|
| Agent SCM state (60 s) | STOPPED -> `sc start`, max `watchdog.max_starts_per_window` per hour, then CRITICAL mail; STOP_PENDING > timeout -> kill tree (no live lock only; `_resolve_stuck_stop` watchdog.py:445 kept) |
| Heartbeat age (60 s) | > 300 s and no live lock -> stop, kill tree, start, log; live lock -> wait until the lock is gone or budget + 30 min, then CRITICAL mail (never kill a live backup) |
| Overdue legs (15 min, `_check_stale_legs` watchdog.py:382 kept on `core.incidents`) | mail per the incident rules; if overdue AND heartbeat fresh AND no lock -> one agent restart + note (self-heal of a wedged scheduler, review MED #6) |
| DB `PRAGMA quick_check` (daily), runtime disk free | BREAKING_NOW mail |
| Config | read every tick (lock path too, Appendix A7 LOW); if invalid, use `config.lastgood.yaml` (written by the agent after each good load) for SMTP and recipients, one mail per day |

Deleted: Prefect API health, ordered server/agent stack restart (watchdog.py:504-560), deferral counters for the API.

### 3.12 Config (cut 235 fields to about 45 operator + about 75 advanced)

Every tunable stays a pydantic field with default and bounds (project rule). Two tiers: **operator** keys appear in
Settings; **advanced** keys are accepted in config.yaml and documented in `config_help`, but not in the form.
Removed keys go to `DEPRECATED_KEYS` (dropped with a warning). The exact lists are the Phase 10 deliverable.

| Section | Operator keys (Settings) |
|---|---|
| top | `firm_name` |
| paths | `source_root` (alias `source_drive`), `nas_root` (alias `lan_destination`), `gcs_key_path`, `runtime_dir` |
| cloud | `enabled`, `scheduled`, `bucket`, `bandwidth_limit` |
| lan | `enabled`, `scheduled`, `shutdown_after_backup`, `versions_retention_days` |
| wol | `enabled`, `mac_address`, `server_ip`, `broadcast_address` |
| schedule | `cloud_cron`, `lan_cron`, `audit_cron`, `weekly_cron`, `monthly_cron` |
| budget | `cloud_max_hours`, `lan_max_hours`, `audit_max_hours` |
| guards | `max_delete_files`, `max_delete_percent`, `min_delete_floor_files` |
| notifications | `smtp_host`, `smtp_port`, `smtp_security`, `smtp_auth`, `smtp_username`, `smtp_password`, `sender`, `recipients`, `report_recipients`, `weekly_enabled`, `monthly_enabled` |
| dashboard | `bind_address`, `port`, `public_url` (+ the two password hashes, set on their own pages) |
| rollover | `activate_on`, `close_out_alert_days` (new) |

**Advanced** (kept, hidden): rclone transfers/checkers/modify window/retries/stall/version; wol and smb timings;
shutdown timings; recovery attempts/delays; run_logs and DB retention, `db_max_mb`, sqlite tunables; alert ladder and the
alert-class switches bound by `PLAN_ALERT_CONTROLS` rev 3; approvals expiry/tolerance/reminders; audit tunables;
dashboard sessions and rate limits; watchdog timings (`heartbeat_stale_seconds` new); `schedule.daily_cron`, `timezone`.

**Deleted (~115):** Prefect/API watchdog keys (models/config.py:1022-1075); per-step rclone timeouts the budget
already caps (`cloud.*_timeout_seconds`, `lan.verify/rclone_measure_timeout_seconds` -> `RunBudget.cap` + one advanced
`probe_timeout_seconds`); `preflight/verify_retries`; archive/journal keys; robocopy leftovers; Prefect-only
`dashboard.apply_*`; `cloud.project_number/location/storage_class` unless a deploy script reads them.

## 4. Module table (56 core modules + entry points)

Legend: K = keep as is, KM = keep + modify, M = merge into, D = delete, N = new. Lines: today -> result (estimate).

| Module (lines today) | Action | Result | What changes |
|---|---|---|---|
| `__init__` 7, `netinfo` 51, `wol_target` 51, `report_period` 52, `rclone_errors` 81, `messages` 102, `lan_versions` 111, `services` 112, `time_utils` 123, `shutdown` 174, `run_artefacts` 281, `cloud_verify` 363, `tool_runner` 395, `cloud_sync` 748, `lan_sync` 522 | **K** | ~3.2k | nothing, except: `cloud_sync` `track_renames` default passed by the caller (A1 LOW), `lan_sync` dead `_LAN_PARTIAL_EXITS` removed |
| `hashing` 33 | D | 0 | tests/legacy only (move to test_support if needed) |
| `email_samples` 104 | move to `tools/` | 0 | not production |
| `rollover_journal` 108, `fy_rollover` 559 | D | 0 | salvage `create_source_folder` (~25 lines) into `fiscal_years` |
| `fy_overlap` 628 | D -> **N `fiscal_years`** | 250 | keep FY math, activation, `ensure_nas_folder`, final-copy lookup (section 3.10) |
| `health` 315, `cloud_preflight` 117, `lan_preflight` 100, `permissions` 217 | M -> **N `preflight`** | 520 | probe B only; drop SeBackupPrivilege + cloud write probe |
| `attention` 580, `overdue` 70, `alert_class` 88, `night_strip` 53 | M -> **N `incidents`** | 520 | class table, ladder, status view; no overlap/switch states |
| `notify` 734 | -> **N `mailer`** | 520 | transport, ladder, cooldown; classification moves to incidents; partly-refused fix |
| `report` 858, `email_compose` 311, `support_bundle` 253 | M -> **N `emails`** | 1150 | delete `send_failure_alert`; stream the bundle zip (A3 LOW) |
| `audit_pending` 165 + `integrity` 519 | KM (merge) | 680 | **excludes in `audit_cloud`** (A1 HIGH) |
| `secrets` 89 + `secret_setup` 44 + `gcs_key` 48 | KM (merge) | 170 | atomic key write (A4 LOW) |
| `admin_auth` 80 + `password_setup` 59 | KM (merge) | 130 | compare_digest / iterations (A4 LOW) |
| `cloud_reporter` 274 | M `cloud_verify` / D | 60 | most calls replaced by the merged check (fd63e5e) |
| `manifest` 1592 | KM | 1150 | `_tx`, retention, size cap; drop file_entries/journal methods |
| `guards` 476 | KM | 400 | delete G2, G6 |
| `config_edit` 574 | KM | 520 | complete `_RISKY_KEYS` (A4 MED); no FY refusal; tiers |
| `config_help` 411 | KM | 300 | operator/advanced tiers |
| `faults` 433 | KM | 300 | class per code; delete Prefect/slot/journal/archive codes |
| `statuses` 314 | KM | 290 | ROLLOVER_* as labels for old rows only; label fix (A3 LOW) |
| `process` 246 | KM | 230 | filelock exclusion + token; PermissionError = unknown; no PATH fallback under `block` |
| `budget` 228 | KM | 220 | proof over all backup-executor jobs; no "2027 only" literal (A2 LOW) |
| `config_file` 189 | KM | 200 | flush + fsync before replace, atomic restore (A4 MED) |
| `approvals` 187 | KM | 190 | match on guard code (A2 MED), `waiting_summary` key, missed reminder day |
| `wol` 179, `smb_ready` 143 | KM | 330 | `already_up` needs 2 probes; stat with timeout (A2) |
| `backup_repository` 159 | KM | 120 | drop `record_sync_results`; keep start row, sweep, record |
| `source_picker` 148 | KM | 120 | parent only, no FY leaf rule |
| `logging` 116 | KM | 55 | delete the Prefect bridge (logging.py:52-115) |
| `rclone_config` 79 | KM | 80 | env passed to the child, no `os.environ` mutation (A1 LOW) |
| `app_restart` 64 | KM | 64 | refuse while registry/lock busy |
| **N** `scheduler`, `jobs`, `aam/__main__`, `recovery` | N | 600 | sections 3.1, 3.6 |
| `flow.py` 3752 | split | 1700 | -> `cloud_leg` 550, `lan_leg` 650, `flows` (backup dispatcher, audit, reports, daily) 500 |
| `ui.py` 1936 | KM | 1600 | no Prefect; registry; FY card; Origin check |
| `watchdog.py` 836 | rewrite | 300 | section 3.11 |
| `launch.py` 435 | rewrite | 120 | logging, sweep, catch-up, dashboard thread, scheduler |
| `serve.py` 134 | D | 0 | replaced by `core/scheduler.py` |
| `models/config.py` 1467 | KM | 1000 | tiers, aliases, DEPRECATED_KEYS, validators (floor < max, A6 LOW) |

**Totals (estimate):** today ~23.6k production lines -> ~**17k** (-28 %): ~3.3k unchanged (data path), ~9k with small
edits, ~2.5k moved from flow.py into the leg modules, ~2k new (scheduler, jobs, CLI, recovery, fiscal_years, watchdog,
launch). The rest is real features (dashboard, settings, reports, DB). The 52 rollover/overlap test files shrink to ~10.

## 5. Findings: how each is resolved

"Fix Fn now" = carried over and applied on `rclone-engine` regardless of the migration (list below the table).

| Finding (sev) | Resolution |
|---|---|
| Review #1 invalid config / DB = no alert (HIGH) | Owner: dead-man's switch deferred. Partly covered by the `config.lastgood.yaml` watchdog path (Q6). |
| Review #2 old FY after Apr 1 | Withdrawn by owner; the close-out rule keeps the closing copy. |
| Review #3 / AF-1 G3 copy-only never fired (HIGH) | Fixed in the uncommitted tree; commit it. G3 tests build records with `enforce()`. |
| #4 Prefect DB growth, #5 stuck slot, A6 200-run cap (HIGH), A7 Apply busy check (MED) | Gone with Prefect (DB + registry, filelock, serial executor). Interim on rc1: fix F5 (UI query filtered to RUNNING/PENDING state types), fix F6 (vacuum env var). |
| #6 wedged agent (MED) | Heartbeat + overdue-restart (3.11). |
| #7 archive retries forever, #8 switch ignores new year, #9 blocked rollover noisy (MED) | No archive, no switch, no G6 (3.10); NAS shutdown once per job in `finally`. |
| #10 / AF-12 verify race (MED), #18 activate_on, A2 version cache, A2 shutdown target | Done (AF-12, AF-16, AF-15, AF-9); kept. |
| #11 AV on lock, #13 launch deletes lock (LOW); A2 lock PermissionError + replace race (MED) | filelock (2.3); minimal fix F12 now. |
| #12 / A5 copy-only access probe replaces the block (MED) | Fix F9 now; same rule in both leg modules. |
| #14-17 config-rewrite races, two rollover checks, end_overlap, first-pass race; A4 settings save vs switch (MED) | Gone: no config rewrite, no journal. |
| #19 dead code, #20 doc drift | Deleted; ARCHITECTURE.md rewritten in Phase 11. |
| A1 `audit_cloud` without excludes (HIGH) | Fix F1 now (the `cloud_verify` part is done, AF-11). |
| A1 LAN stall retry gets a fresh `--max-delete` (MED) | Fix F10 now; recovery layer by design (3.6). |
| A1 LOW (exit 0 without stats, literal default, env thread-safety, dead constant) | Exit table (3.3); module table (4). |
| A2 G5 approval matched on destination only (MED) | Fix F14. |
| A2 LOW (single WoL probe, AccessDenied, disk-usage failure, reminder day, stat timeout, literals) | Module table KM rows. |
| A3 no rollback after "database is locked" (MED) | Fix F3 (`_tx`); carries over unchanged. |
| A3 `catching_up` hides overdue up to 14 days (MED) | Fix F15; incidents rule (3.8). |
| A3 partly refused recipients (MED) | Fix F4; mailer. |
| A3 LOW (RUNNING filter, retention, labels, report totals, bundle memory, resend) | Data layer (3.7), emails merge. |
| A4 `_RISKY_KEYS` incomplete, `write_atomic` without fsync (MED) | Fixes F13, F11. |
| A4 LOW (non-atomic restore, mkdir parents, FY_PATTERN, auth, DPAPI, key write) | Module table KM rows; FY_PATTERN moves to `fiscal_years`. |
| A5 failure-path shutdown ignores `shutdown_only_if_woken`; last pass never shuts down (MED) | Fix F8; one shutdown decision in the job `finally` (3.5). |
| A5 LOW (planned_deletes key, note status, overwritten error text, metrics of first plan) | Rewritten in the leg modules; matrix rows. |
| A6 check-then-create race, no CSRF (MED); guards switchable by bounds (LOW) | Enqueue lock (3.1), Origin check (3.9), validators. |
| A7 approval reminders never sent; wrong text; `waiting_summary` key (MED) | Fixes F2 + F7b. |
| A7 `schedule_problems` omits `rollover_cron` (MED) | Fix F7; lean daily job does not take the backup lock. |
| A7 LOW (nas_shutdown step, daily ROLLOVER_COMPLETE, superseded branch, race guard, watchdog lock path) | Gone with the journal / watchdog rewrite. |

**Fixes to apply regardless of the migration, on `rclone-engine` before the `rclone-rc1` tag (it runs the 2027
rollover), each with a failing test first and the owner's go:** F1 `audit_cloud` excludes; F2 + F7b approval
reminders and `waiting_summary`; F3 manifest `_tx` rollback; F4 partly refused recipients; F5 UI Prefect query filter;
F6 Prefect vacuum env in `06_install_services.ps1`; F7 `schedule_problems` incl. `rollover_cron`; F8 failure-path and
last-pass NAS shutdown; F9 copy-only keeps `GuardBlocked`; F10 cumulative LAN delete budget on a stall retry;
F11 `config_file` fsync; F12 lock PermissionError bounded + replace race; F13 `_RISKY_KEYS`; F14 G5 approval match;
F15 `catching_up` cap. Priority: F1, F2, F3, F5, F8, F10 first (they change outcomes on the box).

## 6. Dependencies

**Requested (owner approval needed):**

| Dependency | Pin | Why |
|---|---|---|
| `apscheduler` | `==3.11.x`, exact patch fixed by spike S1, at least 3.11.1 (DST/CronTrigger/shutdown fixes, R1) | job firing, executors, misfire/coalesce/max_instances, events |
| `tzlocal` | whatever APScheduler 3.11 requires (lock has 5.3.1 today, via Prefect; UNVERIFIED that 3.11 still needs it) | transitive of APScheduler |

**Already locked, must become direct pins (they arrive today only through Prefect or are unpinned):**
`cronsim==2.7` (used by budget.py, email_compose.py, overdue; **only transitive via Prefect today**, so removing
Prefect without pinning it breaks the build), `tenacity==9.1.4` (pyproject says `>=8.5.0`), `httpx` (dev only, for
FastAPI's TestClient). `filelock==4.2.2`, `psutil==7.2.2`, `pendulum==3.2.0` are already direct.

**Not requested:** portalocker, SQLAlchemy, Huey, rq, WinSW, pywin32.

**Removed:** `prefect==3.7.2` and its transitive tree (most of the 125 locked packages: SQLAlchemy, alembic, aiosqlite,
griffe, docker, ... exact list from `uv lock` after removal); the gcloud CLI requirement (archive step).

**Service wrapper:** keep **NSSM** (2.24-101 as deployed, proven on the box, `06_install_services.ps1` exists). WinSW
is maintained but would mean re-testing install, accounts and log rotation for no functional gain; it is the fallback
if an NSSM defect shows. **Independent restarter:** a Task Scheduler task as SYSTEM (no stored password, so no silent
failure on a password change), triggers at boot + every 5 min, runs a 10-line PowerShell that `sc start`s
AamWatchdog and AamBackupAgent if STOPPED and logs to `logs\restarter.log`. NSSM restarts on exit; the watchdog restarts
a wedged agent; the task restarts a stopped watchdog. No component restarts itself.

## 7. Failure modes of the new design

**"Breaking" (a human is told) means exactly:** (a) data at risk: empty source (G1), wrong or unmounted destination
(G4/G5), the `--max-delete` backstop hit; (b) a leg+FY has no verified backup past its overdue deadline
(`core.overdue`); (c) a fault that will not clear by itself: AUTH, missing/wrong rclone, disk full, config invalid,
DB unreadable, the agent cannot be restarted; (d) the weekly audit finds REAL differences; (e) a decision only a person
can make: deletions held by G3, a previous FY without a closing copy after 7 days. Everything else is handled
automatically, shown on the dashboard, and mailed only when it repeats (`alerts.escalate_after_runs`, default 2).

| Failure | What happens | How the human is told |
|---|---|---|
| Scheduler thread dies / wedges | heartbeat stale -> watchdog restarts agent (no live lock); catch-up runs missed jobs within grace, else MISSED rows | only if a leg becomes overdue, or the restart fails (CRITICAL) |
| Agent process crash | NSSM restarts; a running child is killed with it or survives (spike S3); RUNNING row swept ABANDONED; catch-up within budget | only if repeated / overdue |
| Watchdog dies | NSSM restarts; SYSTEM task within 5 min | not told (no effect on backups) |
| DB locked | busy timeout + tenacity + `_record_run` retries; `_tx` rolls back | if the row still cannot be written: BREAKING (c) |
| DB corrupt | jobs FAILED (nothing transferred: approvals/baselines unknown, fail closed); daily quick_check | BREAKING (c) via last-good config; runbook: rename DB, fresh DB (G3/G5 do not need history, G4 passes without baseline but still refuses a local target) |
| config.yaml invalid (hand edit; Settings validates before writing) | agent exits, NSSM restart loop (throttled) | watchdog mails from `config.lastgood.yaml`, once a day |
| rclone hang | S6 stall watchdog / budget timeout -> tree kill; recovery retries once if deletes known; parent kills child at budget + margin | if repeated |
| NAS does not wake | bounded WoL loop -> LAN FAILED; NAS not shut down (it is off) | if repeated / overdue |
| SMTP down | tenacity 3 tries; recorded FAILED; red dashboard banner; open incidents retried next tick (max per day) | dashboard only (no second channel until the dead-man's switch) |
| Disk full (NAS / source / runtime) | free-space preflight FAILED; runtime: run_logs min-free check, DB writes fail -> as DB | BREAKING (c) at once |
| Clock wrong | SKEWED -> run FAILED; P4 prevents cross-year writes; activation needs a proven clock | if repeated |
| Missed Apr 1 | nothing special: previous FY stays in the list until its closing copy | INFO when closed; DECISION after 7 days |
| Server off for days | on boot: sweep, one catch-up per leg if inside grace, else one MISSED row per leg; watchdog computes overdue at once | overdue mail right after boot; nothing while it is off (accepted risk) |
| Power loss mid-sync | `.partial` on the NAS, nothing half-named; ABANDONED; next run re-copies; orphans purged at the next LAN job | only if repeated |
| Locked files every night | PARTIAL, deletes held for that night, files named | if the leg has no COMPLETE for `escalate_after_runs` nights (Q8) |
| Mass delete | copy-only, deletions held | DECISION mail once + reminders |
| GCS auth revoked | FAILED (AUTH class) | BREAKING (c) at once |
| Internet down | probe/transport FAILED, retried in-run (exit 5/transport) | if repeated |

## 8. Migration plan

**Ordering rule:** the box keeps running `rclone-rc1` (Prefect) until cutover; nothing lean touches the box before the
shadow phase. This is not stacked with the rclone LAN switch: Phase 0 finishes and deploys that switch; the lean work is
built on a branch in parallel and only goes live after rc1 has run 4 weeks clean AND after the 2027 close-out. The
engine modules are frozen (P1), so rc1 fixes merge into the lean branch without conflict.

**Cutover window (recommended, Q1): mid-April 2027, after the old system has done the Dec 31 activation and the Apr 1
switch.** Never change the scheduler across the once-a-year event; instead the lean shadow watches that event.

Build process per CLAUDE.md: Opus writes `docs/TESTMATRIX_LEAN_<phase>_<date>.md` first; a Haiku test agent writes
failing tests from it; a different Haiku agent writes code; 2 failed attempts -> Sonnet; interface change -> Opus; scope
flag on `git diff --stat`; full unit suite + Opus diff review with mutation checks at the end of each phase.

| Phase | Work (one chunk = one function / behaviour) | Exit | Sessions |
|---|---|---|---|
| 0 | rc1: commit AF-1..17, fixes F1-F15 (owner go), WS-G, shadow trial A/B, deploy rc1, 4-week watch | rc1 on box, clean | (existing plan) + 4 |
| 1 | Opus: test matrices for phases 3-10; spikes S1-S9 (section 9) | matrices + spike notes in docs | 1 Opus + 3 |
| 2 | Shim `core/runtime.py`: `flow`/`task` decorators returning the plain function with `.fn` and `.with_options(**kw) -> fn`; `prefect_test_harness` -> no-op context; switch imports. Prefect still runs it in production | full suite green unchanged | 1 |
| 3 | Pull Prefect context out of the pipelines: `run_id`, `scheduled_for`, `late_seconds` as arguments (flow.py:132, 544-566); `_backup_slot` -> filelock + token (flow.py:196) | suite green; lab w22/w23 green | 3 |
| 4 | Data layer: `_tx`, retention keep-last-success, size cap, drop file_entries readers | DB matrix rows | 2 |
| 5 | `scheduler`, `jobs`, `aam/__main__` with fake-clock tests (catch-up, coalesce, MISSED listener, max_instances, serial executor, kill at budget, reboot at 20:55 and 21:30, clock jump, CronSimTrigger vs cronsim on a year of crons) | matrix green; not wired to services yet | 3 |
| 6 | Recovery layer + exit table; delete `_sync_with_retries` | exit-table tests, cumulative delete tests | 2 |
| 7 | `fiscal_years`: passes, activation, close-out, aliases; delete journal, G6, archive, `_rollover_resume`; new calendar test (reuse test_fy_overlap_calendar's fake clock) over Dec 31 / Apr 1 / missed Apr 1 / NAS off / clock skew | calendar matrix green | 4 |
| 8 | Alerting merge: incidents, mailer, emails; class table; ladder | alert matrix; `test_alert_class` style scan of every event site | 4 |
| 9 | ui on DB + registry; watchdog rewrite; launch rewrite; delete serve.py; deploy scripts (2 services + SYSTEM task) | UI tests, watchdog tests, lab w14/w19/w24 | 4 |
| 10 | Config tiers, aliases, DEPRECATED_KEYS, Settings catalogue | old box config loads unchanged; Settings tests | 2 |
| 11 | Remove Prefect dependency; delete shim users; audit lab + livebox harness adapted (harness drives the scheduler with a simulated clock: closes the "cron catch-up / reboot never exercised" gap); ARCHITECTURE.md rewrite | `git grep -i prefect -- '*.py'` = DEPRECATED notes only; full lab once; 2-year livebox lane | 3 + 1 Opus |
| 12 | Shadow on the box (below), then cutover + 14-night watch | go/no-go criteria met | 2 + calendar time |

Total: ~34 builder + ~8 Opus sessions. Calendar: build Dec 2026 - Feb 2027, shadow Feb - Apr, cutover mid-April 2027.

**Tests reused:** the shim keeps the 83 test files with 166 `.fn` uses working through Phases 2-10 (about 30 lines
of shim; worth it, because each module is then migrated off `.fn` only when its own phase rewrites it). The data-path
tests (cloud_sync, lan_sync, cloud_verify, integrity, guards, tool_runner, lab w1-w25) carry over unchanged. Deleted
with their code: journal/switch/archive/reentered/hand-switch tests (most of the 52 rollover files), Prefect-launch and
API-watchdog tests. Rewritten from the new matrices: scheduler, watchdog, fiscal_years, incidents.

**Shadow trial (owner go, Phase 12):** service `AamLeanShadow` with `AAM_MODE=shadow`, own runtime dir, the real DB
opened read-only (`mode=ro` URI). It fires at the same crons, and per job records: fired time, catch-up/MISSED decision,
the derived FY pass list, preflight results that need no NAS (G1, source, binary/version, key, clock, GCS probe), and
the dashboard/attention JSON computed from the real DB. Optional real end-to-end cloud runs of a small test folder to
`gs://<bucket>/_lean_shadow/` (memory: test prefixes allowed). No LAN writes, no WoL, no shutdown. Pass criteria over
14+ nights including Apr 1: every job fired within 60 s of Prefect's start; derived pass list == the FY of the real
rows every night (incl. the close-out pair on 1 Apr); banner items equal or explained; zero exceptions. Then the
box spikes S4/S7 (reboot, restarter) with the owner present.

**Cutover and rollback:** lean code in `C:\AAMBackup_lean`, same runtime dir and DB (additive only). Stop and set
the 3 old services to Disabled (do not uninstall); install and start the 2 new ones + the task. During the 30-day
watch the lean code never rewrites config.yaml keys into the new names (aliases only) and never writes the journal
tables, so **rollback** = stop/disable the new services and task, re-enable the 3 old ones, `C:\AAMBackup` untouched,
the DB rows the lean code wrote are readable by rc1. After 30 clean days: uninstall the old services, rename the
config keys (one Settings save), remove Prefect from the box.

## 9. Risks of the new design and spikes

| Risk | Why it matters | First test |
|---|---|---|
| APScheduler silent skip, defaults differ, 3.x day-of-week / DST traps | missed run with no row; wrong audit day | S1; CronSimTrigger (never `from_crontab`) |
| Child process killed or orphaned with the agent under NSSM | a restart kills a backup mid-run, or an orphan runs on | S3 |
| Start-up catch-up double-runs (catch-up + the cron firing a minute later) | two backups | fake-clock tests; `scheduled_for` dedupe in `jobs.enqueue` |
| filelock behaviour on hard kill / AV / service account | lock not released or not exclusive | S2 |
| Close-out rule never satisfied (folder renamed, NAS gone) | previous year kept forever | DECISION mail at 7 days; calendar test |
| rclone flags marked UNVERIFIED (modify window on SMB, `$RECYCLE.BIN/**` at a drive root, `--backup-dir` sibling on the same share, dry-run JSON field names) | false copies/deletes | S5 (most already lab-proven on local disks; SMB is the gap) |
| Locked files without VSS | nightly PARTIAL noise or deletes never applied | S6 |
| Config alias migration | box config fails to load after cutover | Phase 10 test on a copy of the box config |
| Rewrite churn hides a lost safety check | data risk | each kept guard has a test that fails when it is removed; Opus mutation review per phase |

**Spikes (1-2 h each, before Phase 3; S4 and S7 on the box with owner go, the rest on the dev PC):**
- **S1 APScheduler 3.11.x:** read the installed `schedulers/base.py` and `executors/base.py`; confirm defaults
  (`misfire_grace_time`, `coalesce`), that a job queued in a 1-worker pool is re-checked against its grace, that
  `EVENT_JOB_MISSED` and `EVENT_JOB_MAX_INSTANCES` fire; `from_crontab('0 0 * * 0')` day; `wakeup()` after a
  time-machine jump; a custom `BaseTrigger` backed by cronsim; Ctrl-C/shutdown under NSSM-like stop. Fixes the pin.
- **S2 filelock on Windows:** owner killed with TerminateProcess -> second process acquires at once; an AV-style
  reader holding the token file open does not block exclusion; works as the service account in `C:\BackupAgent`.
- **S3 child process vs NSSM stop:** agent spawns child that spawns rclone; `nssm stop` / crash of the agent -> what
  survives (NSSM `AppKillProcessTree`); decide whether children must be in a Job object; ABANDONED sweep sees it.
- **S4 reboot on the box:** reboot at 20:55 (job runs after boot within grace) and at 21:30 mid-run (ABANDONED +
  catch-up if budget remains). Owner present.
- **S5 rclone on SMB:** modify window 2 s on the NAS, `$RECYCLE.BIN/**` / `System Volume Information/**` from a drive
  root, `--backup-dir` sibling folder is a rename, dry-run `"skipped":"delete"` lines on 1.74.3 local->UNC (read-only
  dry-run plus a test folder).
- **S6 locked-file frequency:** grep the box's cloud run_logs for "used by another process" over 30 nights.
- **S7 SYSTEM restarter task:** create with schtasks as SYSTEM; stop both services; restarted within 5 min; no password.
- **S8 cost on the box:** Python child start-up time and RSS; APScheduler idle CPU.
- **S9 Prefect-free import:** in a scratch venv without Prefect, with the shim, import every module and run the fast
  suite; count failures (sizes Phases 2-3).

## 10. Open questions for the owner (with my recommended default)

1. **Cutover window:** after the old system has done the 2027 activation and Apr 1 switch (mid-April 2027), with the
   lean shadow watching both events? *Default: yes, mid-April 2027.*
2. **New dependency APScheduler 3.11.x** (exact pin after S1), or our own cronsim loop (no new dependency, similar
   size)? *Default: APScheduler 3.11.x with the cronsim-backed trigger.*
3. **Delete the GCS archive step** and rely on the 365-day lifecycle rule (also removes gcloud)? *Default: yes.*
4. **Close-out:** previous FY keeps syncing until each leg has a verified run started on/after 1 Apr; DECISION mail
   after how many days without one? *Default: 7 days; the year then stays in the list until you dismiss it.*
5. **G3 deletions held:** stay a human decision (copy-only protects meanwhile), never auto-applied? *Default: yes,
   human decision, one mail + reminders.*
6. **Last-good-config alert path** in the watchdog now (about 30 lines), although the dead-man's switch stays future?
   *Default: yes.*
7. **Stop using `file_entries`** (per-file DB state); dashboard counts come from the last run's rclone numbers?
   *Default: yes.*
8. **Transient faults** (one failed or PARTIAL night, NAS not waking once, locked files): mail only when the same
   problem repeats on 2 consecutive runs or the leg becomes overdue; the dashboard shows every one at once?
   *Default: yes, `alerts.escalate_after_runs = 2`.*
