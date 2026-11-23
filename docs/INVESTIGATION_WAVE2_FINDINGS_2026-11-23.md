# Investigation: Wave 2 findings (2026-11-23)

Read-only investigation of the live TEST stack (C:\AAMTest). No edits, no tests, no service changes.
Status tags: CONFIRMED = read in the DB/logs/code; GUESSED = inferred, not verified.
Times are local IST (+05:30) as written in the logs.

---

## Q1: Cloud runs 44 and 46 "overlapped" by about 80 s

**Verdict: NOT A DOUBLE RUN. Recorded-row artefact caused by the order in which the row is written (CONFIRMED).**
It is not a server-restart effect and not a slot/lock bug. The rclone processes never overlapped.
One minor design defect in row timing is confirmed (see below).

### Facts (CONFIRMED)

| Item | Run 44 (`8dfdd478`, flow run `exotic-orangutan`) | Run 46 (`77c0174f`, flow run `ruby-ermine`) |
|---|---|---|
| `run_history.started_at` | 05:40:30.25 | 05:40:40.25 |
| `ended_at` | 05:41:52.96 | 05:43:08.37 |
| `duration_seconds` | 82.6 | 75.0 |
| `ended_at - started_at` | 82.7 s | 148.1 s |
| `extended_metrics.budget.used_hours` | 0.02 | 0.04 (about 144 s) |
| Status | CLOUD_NO_CHANGES_COMPLETE | CLOUD_NO_CHANGES_COMPLETE |

Agent log `C:\AAMTest\runtime\logs\agent_svc.log`:
- L36-46: run 44 starts, `Backup lock acquired (PID=4984)` at 05:40:30.305.
- L68-70: run 46 logs `FY overlap ...` and `Starting cloud backup for FY26-27` at 05:40:40.251. No task, rclone or lock line follows for it.
- L178: run 44 `Backup lock released` at 05:41:52.985. L182: run 44 `Finished in state Completed()` at 05:41:53.049.
- L183-186: run 46 `Backup lock acquired (PID=14676)` at 05:41:53.289, pipeline starts 05:41:53.321.
- L188-189 (approx.): run 46 first rclone (`rclone-preflight-probe`) at 05:41:57.121, artefact folder `...0540_cloud_FY26-27_77c0174f`.
- L307: run 46 `Backup lock released` at 05:43:08.377.

Last rclone process of run 44 is `rclone-diff`, exit at 05:41:52.828 (L161). First rclone process of run 46 starts at 05:41:57.121. There is a 4.3 s gap and no overlap.
Run-artefact mtimes in `C:\AAMTest\runtime\run_logs\2026-11-23\2026-11-23_0540_cloud_FY26-27_77c0174f\` are all 05:42 or later. Run 44's folder spans 05:40-05:43. Run 46 was therefore queued for about 73 s (05:40:40.25 to 05:41:53.29) behind run 44's slot.

Correction to LIVE_RESULTS.md Wave 2 addendum: it says both rclone log sets show activity at the same time 05:40-05:42. That is not correct for run 46; its first rclone activity is at 05:41:57.

Prefect server context: stopped with -Force at about 05:36:10, restarted at 05:39:09 (LIVE_RESULTS Task 2). No RUNNING run existed at either point. Both triggers came after the restart. The log file `prefect_svc.log` contains only the server banner and "Server stopped!". It does not record slot events. The flow-run lines are in `agent_svc.log`.

### Code (CONFIRMED)

- `flow.py:3436` `started_at = now_iso()` is taken BEFORE the slot wait. `flow.py:3437` takes `monotonic_start` before the wait too.
- `flow.py:3443` `_record_running(...)` writes the `CLOUD_RUNNING` row with that `started_at` and the owner token (`core/backup_repository.py:127-132`), BEFORE the slot is requested.
- `flow.py:3446` `_backup_slot(config)` then blocks in `flow.py:232` `concurrency("aam-backup", occupy=1, timeout_seconds=wait_seconds)`.
- `flow.py:3467` `budget_monotonic_start=monotonic_start` (before the wait). So the run budget includes the queue wait. This matches the `models/config.py` budget doc ("including any wait for the backup slot").
- `flow.py:3472` passes `time.monotonic()` taken AFTER the slot as the pipeline's `monotonic_start`. `flow.py:2310-2312` (`_record_run`) therefore computes `duration_seconds` without the wait.
- Result: `started_at`/`ended_at` span the queue wait, `duration_seconds` does not, and the budget does. That is the 148 s vs 75 s mismatch above. A queued run also shows as `CLOUD_RUNNING` in the DB and dashboard while it is only waiting.
- Slot release: `concurrency()` exit (Prefect 3.9.4). Lease default `lease_duration=300` (`.venv/Lib/site-packages/prefect/concurrency/sync.py:27`), renewal at 0.75 x duration (`_leases.py:21`). The lock is released in `flow.py:270-273` (`_hold_backup_lock` finally), inside the slot, so the lock always goes before the slot.
- Lock guard, the second line of defence: `flow.py:238-273`. If `acquire_lock` fails and the holder is a different live PID, it raises `RuntimeError` (`flow.py:251-258`). `_pipeline_slot` (`flow.py:553-575`) records `*_SLOT_TIMEOUT`. Even with the Prefect slot free, a second pipeline cannot run rclone while the first holds `backup.lock`.
- Lock format `PID:create_time` (`core/process.py` docstring, `acquire_lock`, `read_lock_alive`): PID reuse is detected.
- ABANDONED sweep (`core/manifest.py` `abandon_dead_runs`, called from `_record_running` at `flow.py:460-474`): rows whose owner token is alive are never touched. Run 44 was not swept, consistent with its normal finish.
- The slot is not persisted in SQLite. `deploy/06_install_services.ps1:319` sets `PREFECT_SERVER_CONCURRENCY_LEASE_STORAGE=prefect.server.concurrency.lease_storage.filesystem`. Whether this survives a -Force restart was not checked (GUESSED). It does not matter here: no slot was held at either restart.

### Scenarios checked (for "second run gets the slot while the first still works")

| Scenario | Happened in Wave 2? | Result |
|---|---|---|
| Server restart loses slot state | No. No RUNNING run at the -Force stop or restart | Not triggered |
| Lease expiry while holder alive (server down mid-run, renewal fails, lease ages out after 300 s) | No | `_lease_renewal_loop` returns when renewal gives up (`_leases.py`, GUESSED on exact server-side behaviour). A second pipeline would then hit the lock guard (`flow.py:251-258`) and record `*_SLOT_TIMEOUT`, with no rclone. Protected by the lock file |
| Stale lock / PID reuse | No | `create_time` token detects reuse (`core/process.py`) |
| ABANDONED sweep marks a live run | No | `abandon_dead_runs` skips alive owners |
| Lock file removed while holder alive | Not seen | Only `_release_backup_lock` (own PID) and the watchdog's dead-owner branch delete it (`watchdog.py` ~line 800-810, fail-closed) |

Conclusion: a real double rclone would need the Prefect slot AND the lock file to fail together while the first run is alive. Nothing in the logs shows this.

### Minimal reproduction idea (not written)

1. Row timing, to pin the defect: patch the slot so `concurrency()` blocks about 0.5 s, call `_run_leg_folders` with a stub pipeline, and assert that a queued run's `started_at` is not earlier than its first real work, or that a separate queued timestamp exists. Also assert `duration_seconds` approximately equals `ended_at - started_at` when there is no wait. Do not use `PREFECT_TEST_MODE`, which bypasses the slot.
2. Lock guard: create a lock with the `PID:create_time` token of a live child process, call `_backup_slot` with a stubbed `concurrency` that returns immediately, and assert `RuntimeError` with no rclone call. Existing `tests/test_a7_backup_lock.py` may already cover the lock part.

---

## Q2: Watchdog behaviour when the Prefect server (and so the agent) is down

### What the watchdog does (CONFIRMED, `watchdog.py`)

Defaults from `models/config.py` `WatchdogConfig` (lines 969-1020):
- `check_interval_seconds` 60 (973)
- `failure_threshold` 5 (975)
- `backup_wait_interval_seconds` 120 (979)
- `max_deferrals` 15 (981), `max_transfer_deferrals` 240 (984)
- `restart_cooldown_seconds` 120 (988)
- `max_starts_per_window` 3 and `start_window_seconds` 3600 (990-993), circuit breaker
- `stop_pending_timeout_seconds` 600 (994), `api_ready_timeout_seconds` 300 (1001)
- `stale_check_interval_seconds` 1800 (1016), `healthy_mark_seconds` 300 (1012)

Related keys: `dashboard.stale_grace_hours` 2 (`models/config.py` ~1042); `alerts.stale_repeat_hours` 24 (582); `alerts.repeat_cooldown_hours` 12 (548); `budget.cloud_max_hours` 6 (952); `budget.lan_max_hours` 4 (956).

Timeline with Prefect DOWN and NOT hung (Wave 2 LS7a timestamps, `watchdog_app.log` L114-117):
- Failures are counted per check. Failure k occurs at t0 + (k-1) x 60 s (`watchdog.py:752` sleeps between failures 1-4).
- At failure 5 (about t0 + 240 s; with the LS7a data about 05:41:10 if untouched) the watchdog checks for a live lock or transfer process (`watchdog.py` ~762-870).
- No lock: `_service_state(AamPrefectServer)` is STOPPED, so `_start_allowed` (breaker) and `_start_service(AamPrefectServer)` run (`watchdog.py:901-902`). `failures` resets to 0. It sleeps 120 s (`watchdog.py:911`).
- It does NOT start the agent in this branch.
- Next poll, when the API is healthy: `watchdog.py:728-730` finds `AamBackupAgent` STOPPED and runs `_start_allowed(AGENT)` then `_start_service(AGENT)`. So a stopped agent IS restarted by the watchdog, silently, but only after the API answers. (LIVE_RESULTS says the watchdog did not restart the agent. That is true for the observed window, which ended before the threshold. The code does restart it once Prefect is healthy.)

Timeline with Prefect RUNNING but API dead (hung): `watchdog.py:919` `_restart_stack()` (609-650): stop agent, stop server, start server (wait RUNNING up to 300 s), wait API healthy (up to 300 s), start agent. Then `RESTART_COOLDOWN` 120 s. No mail on success.

Start failures: `_start_service` returns a bool that the caller drops (`watchdog.py:274-285`, `:902`). A failed `sc start` is only logged. The breaker counts every attempt, so the 4th attempt within an hour sends `start_suppressed` (`watchdog.py:903-909`). Until then, the failures are silent.

Windows dependency: `deploy/06_install_services.ps1:346` sets `DependOnService AamPrefectServer` on the agent. Stopping the server while the agent runs is refused (1051) unless forced; a forced stop takes both down (LS7a, CONFIRMED). The SCM failure actions (`deploy/06_install_services.ps1:322, 392`) do not restart a service stopped by a dependency stop or a manual stop (CONFIRMED from LS7a observation: agent stayed STOPPED 3 min). Whether `sc start AamBackupAgent` would also start the dependency was not tested (GUESSED); the watchdog never issues that call while the API is down.

### Mail (what is sent, when) (CONFIRMED from code)

Subject format: `core/notify.py:547`: `[{firm} Backup] {fy_label}{kind}: {title}`. The `fy_label` part for watchdog mails was not checked (GUESSED).

| Situation | key | kind | status | fault | title |
|---|---|---|---|---|---|
| Prefect start suppressed (4th start in an hour) | `watchdog:start_suppressed:AamPrefectServer` | ACTION_REQUIRED | START_SUPPRESSED | AAM-SVC-03 | "AamPrefectServer is down" |
| Agent start suppressed | `watchdog:start_suppressed:AamBackupAgent` | ACTION_REQUIRED | START_SUPPRESSED | AAM-SVC-03 | "AamBackupAgent is stopped" |
| restart_failed (stop/start failed, API not healthy) | `watchdog:restart_failed` | ACTION_REQUIRED | RESTART_FAILED | AAM-SVC-01; AAM-SVC-04 for "API not healthy after restart" | (per case) |
| stuck stop | `watchdog:stuck_stop:<svc>` | ACTION_REQUIRED | STOP_FAILED / STOP_PENDING_UNDER_BACKUP | AAM-SVC-02 | (per case) |
| lock held at deferral cap | `watchdog:live_lock_cap` | ACTION_REQUIRED | LIVE_LOCK_CAP_EXCEEDED | AAM-SVC-02 | "Backup lock held while the scheduler is unhealthy" |
| all clear after a CRITICAL | `watchdog:recovered` | RECOVERED | RECOVERED | none | "Scheduler healthy again" |
| overdue leg | `stale:<fy>:<leg>` | ACTION_REQUIRED | STALE | AAM-SYS-03 | "<LEG> backup overdue" |

Notes:
- `watchdog.py:442-447`: RECOVERED is sent only if `_attention_raised` is set. That flag is set only by `_critical` (`watchdog.py:414`). So a self-healed outage never gets a RECOVERED mail, and never a CRITICAL mail either.
- The comment at `watchdog.py` ~line 850-856 says the lock-cap CRITICAL is "not emailed". The code is `_alert_wedged_lock` (`watchdog.py:530-535`), which calls `_critical` and so DOES send one mail per PID (`watchdog.py:858`). The comment is stale. CONFIRMED.
- Repeat cadence: `core/notify` `repeat_cooldown_hours` 12 for CRITICAL/ACTION keys; `stale_repeat_hours` 24 for stale.

### Gaps: silently down with no mail (CONFIRMED unless marked)

1. Watchdog self-heal of a Prefect outage shorter than the stale window: no mail at any stage. The restart and the agent start are both silent (`watchdog.py:901-902`, `:728-730`). LS7a confirmed this: mails stayed at 12 during the 3-minute outage. (The watchdog never reached the threshold in that run, so the self-heal path itself was not observed.)
2. Agent stopped while Prefect is healthy: silently restarted, up to the breaker (3 per hour). Silent until the 4th attempt.
3. The dashboard runs inside AamBackupAgent (`launch.py` `_run_dashboard`). With the agent STOPPED there is no dashboard, so the email is the only signal.
4. A scheduled run missed during the outage writes no row and sends nothing itself. The only backstop is the stale check (`watchdog.py:698-699`, every 1800 s, independent of the API). An overdue leg is flagged after its due time + run budget + `stale_grace_hours`. For cloud that is due + 8 h by default (6 + 2). For LAN it is due + 6 h (4 + 2). Arithmetic from defaults, CONFIRMED against `core/attention.py:186-232`. Dashboard `stale_grace_hours` is the only knob here.
5. Failed start attempts 1-3 are silent (see start failures above). With the 5-minute threshold and about 7-minute cycles, the first mail can arrive roughly 25 minutes after the first failure (GUESSED from timing, not run).
6. Not checked: whether the watchdog itself stopping is ever alerted. Nothing in watchdog.py alerts on that.

### Answers to the explicit questions

- After how many failures/seconds does it restart which service? Prefect is started (not restarted) after 5 failed checks, about 240 s after the first failure (`failure_threshold` 5, `check_interval_seconds` 60). A hung-but-RUNNING Prefect is restarted with the full stack at the same threshold. The agent is started only after the API answers (`watchdog.py:728-730`).
- What mail? See the table. Nothing on a successful self-heal.
- Is a stopped agent restarted when Prefect is also stopped? Not while Prefect is down. The watchdog starts the agent only after the API is healthy again. If Prefect never comes back, the agent is never restarted by the watchdog, and the only mail names Prefect (`start_suppressed:AamPrefectServer`, after the breaker trips).

---

## Summary for the owner

- Q1: not a double run. Row timing defect: `started_at` and the RUNNING row are written before the slot wait, while the budget includes the wait and `duration_seconds` does not (`flow.py:3436, 3443, 3446, 3467, 3472`). Minor; no data impact (0 files copied in both runs).
- Q2: a Prefect outage that the watchdog heals is silent (no RECOVERED, no CRITICAL). A missed scheduled night is flagged only at due + budget + 2 h. Failed starts are silent until the breaker trips.
- Doc fix: the lock-cap comment (`watchdog.py` ~850) says "not emailed"; the code emails once per PID.
