# Live-box test plan - FY overlap, 728 simulated days, normal + adverse (2026-11-24)

Status: PLAN (test architect, read-only research; nothing was run on the box). Owner constraints of 2026-11-24 are binding.
Evidence root: `agent_runs/livebox_<stamp>/` (dev PC) and `C:\AAMLivebox\<stamp>\evidence\` (box, pulled back).
Harness code: `tools/livebox/` (written by the work packages in section 5; nothing exists yet).

---

## 0. Read first: what is really on the box (deploy-consistency finding F-0)

The brief says "overlap build dc50265 is deployed" and points at the `fy-overlap` documents. These are two DIFFERENT
lineages. Checked in git (read only) and in the coordinator's box inspection of 2026-11-24 12:04 IST:

| Fact | `baseline-v1-overlap` @ dc50265 (ON THE BOX) | `fy-overlap` @ 736887c (this checkout) |
|---|---|---|
| Plan | `docs/PLAN_BASELINE_OVERLAP_2026-11-23.md` (rev 2) | `docs/PLAN_FY_OVERLAP_2026-11-20.md` (rev 4) |
| DB schema | `SCHEMA_VERSION = 5`, no DDL change (box: user_version 5 = consistent) | v6 fresh DB, `overlap_activations` table |
| Activation | `fy_overlap.activate_if_due` in the 06:00 `rollover_check_flow` only, from `rollover.overlap_activate_on` (default `"12-31"`), record `db_meta['overlap:<fy>']`, INFO key `rollover:activated:<fy>`, wait mail `rollover:activation:<fy>` AAM-ROL-07 | every backup flow + 06:00, `rollover.early_start_month` (Jan), INFO key `overlap:start:<fy>` |
| Pass order | configured FY first, then next FY | next FY first |
| Run ids | configured pass unsuffixed `<run>-cloud`; next pass `<run>-cloud-FY27-28` | `<run>-<leg>-<fy>` for every pass |
| `/status` | `cloud`, `lan`, `cloud_next`, `lan_next`, `overlap{enabled,active,next_fy,activates_on,activated_at,note}` | `folders[]` |
| Retention defaults | `db_retention_days` 90, `log_retention_days` 90, `run_logs.retention_days` 90 (box: 90 = consistent) | 730 (W40) |
| Test tools | `audit_lab/labclock.py`, `audit_lab/test_fy_overlap_lab.py`, `test_support/{fake_tools,smtp_server}.py`; NO `tools/fy_overlap_rehearsal.py`, NO `test_support/sim_calendar.py`, NO `fy_overlap.run_date` | all of them |
| time_machine | dev group only (`pyproject` dev: `time-machine>=3.5.1`) | dev group only |

Consequences: (a) **owner decision D-0**: which build is battle-tested. This plan targets **dc50265** (what runs) and
lists the fy-overlap deltas in section 8 so the same harness can be re-pointed. (b) The rehearsal tool cannot run
against the box code (it patches `fy_overlap.run_date`, absent in dc50265). (c) The box's 90-day retention, missing
hash-before-close and the 2026-11-23 owner decisions (rollover touches server + cloud only) are NOT in dc50265: they
are recorded as expected-red FINDINGs, not harness failures. (d) Phase 0 below must prove the box HEAD before
anything else.

Coordinator box facts folded in (2026-11-24): source `E:\FY26-27` 703 files / ~96 MB (test fixtures); E: 199 GB free,
C: 1.3 TB free; no `E:\FY25-26`; cloud prefixes FY24-25, FY25-26, FY26-27 (703 objects), AAM_PYTEST_FY,
E2E_ARCHIVE_FY, docs; box powered off 2026-11-04..11-23 left `CLOUD_MISSED` / `LAN_MISSED` rows (budget used up,
exit -1) = real precedent for the "server off" scenarios; `/health` attention item is a stale boot-time audit
(NAS share not ready + DNS failure 1 min after boot; 21:01 LAN of 23 Nov had 0 mismatches) - not planned around;
Prefect slot `aam-backup` free; schedule LAN 21:01, cloud 02:01, rollover 06:00, audit Sun 08:00, weekly Mon 08:00,
monthly 1st 08:30; `AamBackupAgent` runs as `.\aambackup`, the other two services as LocalSystem; the NAS share is
access-denied for the ssh user; mail from the box currently fails DNS (irrelevant: the sandbox uses a loopback SMTP).

---

## 1. Sandbox harness architecture

### 1.1 Layout on the box (nothing under `C:\AAMBackup` or `C:\BackupAgent` is written)

```
C:\AAMLivebox\
  overlay\            time_machine package + _time_machine*.pyd + dist-info (copied from the dev PC venv, cp312)
  harness\            tools/livebox/*.py (copied by ssh pipe, read-only after copy)
  <stamp>\<lane>\     one folder per lane / scenario (A, A730, X01, ...)
     src\FY90-91\ src\FY91-92\ ...      sandbox source parent (sibling year folders, owner layout)
     nas\backup\FY90-91\ ...            LAN target, used as \\127.0.0.1\C$\AAMLivebox\<stamp>\<lane>\nas\backup\FY..
     bucket\aam-livebox-local-<stamp>\FY90-91\ ...   rclone LOCAL remote "bucket"
     runtime\           manifest.db, backup.lock, logs\, run_logs\, rclone_local.conf, dummy key
     prefect_home\      own ephemeral Prefect sqlite (never the production server)
     tmp\               TEMP/TMP of the lane (robocopy orphan-log cleanup only sees this)
     config.livebox.yaml
     evidence\          events.jsonl, checks.jsonl, mail\*.eml + mail_index.csv, status\*.json, trees\*.json
  checkpoints\<lane>\<simdate>\   optional zip of a lane root at a calendar checkpoint (resume / forensics only)
```

Run command (each lane is one process, started detached so an ssh disconnect does not kill it):
```
Invoke-CimMethod Win32_Process Create -Arguments @{CommandLine='C:\AAMBackup\.venv\Scripts\python.exe -B
  C:\AAMLivebox\harness\run_lane.py --lane A --root C:\AAMLivebox\<stamp>\A --plan lanes\A.yaml'}
env set by run_lane.py itself BEFORE any project import: sys.path = [overlay, harness, C:\AAMBackup];
AAM_CONFIG=<lane>\config.livebox.yaml; PREFECT_HOME=<lane>\prefect_home; PREFECT_API_URL removed;
TEMP/TMP=<lane>\tmp; PYTHONDONTWRITEBYTECODE=1; process priority BELOW_NORMAL (inherited by rclone/robocopy).
```
UNVERIFIED: that a `Win32_Process Create` child survives the ssh session close on this OpenSSH build (spike L1).

### 1.2 Config derivation (`tools/livebox/derive_config.py`)

Input: production `C:\AAMBackup\config.yaml` read ONCE, read-only (yaml load, `_drop_deprecated_keys`), validated with
the box's own `AppConfig`. Output: `config.livebox.yaml`, re-validated with `AppConfig`, then checked by section 1.8.
Rule: keep every production key (schedule, budget, guards, alerts, approvals, audit, health thresholds, rollover keys,
leg enabled/scheduled flags) unless listed here.

| Key | Sandbox value | Why |
|---|---|---|
| `paths.source_drive` | `<lane>\src\FY90-91` | test FY, owner layout (siblings under one parent) |
| `paths.lan_destination` | `\\127.0.0.1\C$\AAMLivebox\<stamp>\<lane>\nas\backup\FY90-91` | UNC validator, parent share exists, ssh user can write |
| `paths.runtime_dir` | `<lane>\runtime`; **`paths.database_path`, `paths.log_directory`, `run_logs.dir` removed** | derived under runtime; a copied `log_directory` would let a 2090 clock purge PRODUCTION service logs (`log_retention_task` -> `purge_older_than(config.paths.log_directory, ...)`) |
| `paths.gcs_key_path` | `<lane>\runtime\dummy-key.json` (`{"type":"service_account","livebox":true}`) | any missed cloud seam fails auth (AAM-CLD-03) instead of writing GCS |
| `cloud.bucket` | `aam-livebox-local-<stamp>` | sentinel name that does not exist on GCS (fail closed) |
| `notifications.smtp_host/port` | `127.0.0.1` / port of the lane's fake server | no real mail |
| `notifications.smtp_security/smtp_auth/smtp_username/smtp_password` | `none` / `false` / `""` / `""` | never a credential |
| `notifications.sender/recipients/report_recipients` | `livebox@livebox.invalid` / `["ops@livebox.invalid"]` / `["partners@livebox.invalid"]` | two lists kept distinct so audience routing is asserted |
| `notifications.retry_attempts/retry_base_seconds` | 1 / 0 (min bounds) | no sleeps |
| `wol.enabled`, `wol.mac_address`, `wol.server_ip` | `true`, `02:00:00:00:00:00`, `127.0.0.1` | wake/shutdown DECISION code runs; the wake and shutdown calls are spies (1.3) |
| `lan.shutdown_after_backup` | as production (true) | decision tested; the action is a spy |
| `lan.use_backup_mode` | production value if `whoami /priv` shows SeBackupPrivilege for the ssh user, else `false` (documented deviation, only `/ZB` dropped) | lab experiment C1 |
| `guards.forbid_local_destination` | `false` (target is this machine) | G4-local tested separately (X20b) |
| `health.ntp_server` / `w32tm_timeout_seconds` | `127.0.0.1` / 5 | the real w32tm would prove the REAL clock and make the 2090 clock look "proven" |
| retry waits (`lan.retry_wait_seconds`, `cloud.retry_delay_seconds`, ...) | minimum model bounds | speed; counts unchanged |
| `maintenance.*retention*`, `run_logs.retention_days` | production values (90) in lane A; lane A730 uses 730; lane R35 uses 35 / 7 | retention boundaries |
| `rollover.overlap_enabled` / `overlap_activate_on` | `true` / production value (`12-31`) | the box behaviour |

### 1.3 What is real, what is faked (fake only where unavoidable)

| Piece | Real / seam | Seam detail |
|---|---|---|
| Flows `flow.backup`, `rollover_check_flow`, `integrity_audit_flow`, `weekly_report_flow`, `monthly_report_flow` | REAL Prefect flow calls inside `prefect.testing.utilities.prefect_test_harness()` (lane-private ephemeral server, `launch._ensure_concurrency_limit()` creates the lane's own `aam-backup` limit) | the w25 / `test_fy_overlap_lab` approach; retry wrapper `_flow_call` for the harness-DB artefact |
| Scheduled time / lateness / auto-scheduled | seam | `prefect.context.FlowRunContext.get` faked ONLY for callers `_scheduled_time`, `_scheduled_lateness_seconds`, `_auto_scheduled_run` (frame name check, `sim_calendar` trick, ported): `auto_scheduled=True`, `expected_start_time=<cron instant>`; manual presses pass `None` |
| rclone | REAL binary `C:\AAMBackup\deploy\bin\rclone.exe`, real flags; remote `[aam_gcs] type = local` | `temp_rclone_config` patched in `flow`, `core.cloud_sync`, `core.cloud_preflight` (`labconf.patch_cloud_modules_to_local` + flow attr); cwd = `<lane>\bucket` |
| Empty-prefix semantics | seam | GCS lists an empty prefix as `[]`, a local remote says "directory not found": create the empty prefix folder right before the first rclone call on a new FY (lab K8 / w25 `_dry_run_with_listable_prefix`) |
| robocopy | REAL `robocopy.exe`, real command (minus `/ZB` only if 1.2 says so) to the loopback admin share | none |
| SMB readiness, canary, walks, guards G1-G5, verify, audit, approvals, journal, notify, email compose, DB | REAL | none |
| NAS wake | spy | `flow.ensure_server_online` -> counter; `core.wol` send function replaced by a raiser (no packet ever) |
| NAS shutdown | spy | `flow.lan_shutdown_task` -> counter returning `{"simulated": True}`; `core.shutdown.shutdown_server` replaced by a raiser (labconf belt and braces) |
| Archive (`gcloud storage objects update --storage-class=ARCHIVE`) | recorder | `core.fy_rollover.run_archive_transition` -> records `(bucket, old_fy)`; no local equivalent exists |
| Clock proof (`core.health.check_clock`) | seam at the HTTP edge | `labclock.install_google_date(skew_seconds=...)` replaces `health.http.client.HTTPSConnection` (production check logic stays real); variants: agree, skew +/-N s, raise `OSError` (no internet) |
| Mail transport | REAL SMTP client to `test_support.smtp_server.smtp_server()` (thread in the lane process) | modes: normal, `fail_with="451 ..."`, `"550 ..."`, stopped (port closed) |
| Watchdog overdue mails | REAL function `watchdog._check_stale_legs(cfg)` called by the driver at 10:00 and 22:30 sim time (never `watchdog.main`) | UNVERIFIED: that it runs standalone without the service probes |
| Dashboard | REAL `ui.app` through FastAPI `TestClient` (signed-in session; reuse the login helper of the dc50265 tests, UNVERIFIED name), `_start_approved_run` / trigger deployment calls replaced by a recorder; the driver then calls `flow.backup(..., approval_id=, fy=)` exactly as the deployment would | no Prefect deployments in the sandbox |

### 1.4 Simulated clock (decision)

Decision: **time_machine, process-wide, in the box venv via a path overlay** (no install into `C:\AAMBackup\.venv`).
The labclock module seam is NOT enough for 728 days: dc50265 reads the clock through `time.time()` in
`flow._scheduled_lateness_seconds`, `audit_pending` reference, `integrity` pending, `run_artefacts.purge_older_than`,
`lan_sync` orphan cleanup, through `pendulum.now` in `core.attention`, `core.notify` (cooldowns, quiet days),
`core.manifest` (`cutoff_iso` / retention), `core.report_period`, `ui` (17 reads), `watchdog`. time_machine moves
`time.time`, `datetime.now`, `pendulum.now` for ALL of them at once.

- Install: copy `time_machine\`, `_time_machine.cp312-win_amd64.pyd`, `time_machine-*.dist-info` from
  `C:\Users\malliath\Desktop\aam_backup\.venv\Lib\site-packages` to `C:\AAMLivebox\overlay\`. Pre-check: box venv is
  CPython 3.12 x64 (UNVERIFIED). Alternative if the ABI differs: `uv venv C:\AAMLivebox\venv` + `uv pip install
  -r <exported dc50265 lock> time-machine` (needs internet; owner OK).
- Use: one `time_machine.travel(start, tick=True)` per lane process, `traveller.move_to(event_instant, tick=True)`
  before every event (clock runs at real speed inside an event, so Prefect state timestamps never collide); then
  `pendulum.travel_to(event_instant, freeze=False)` as in `sim_calendar` (UNVERIFIED that both are needed with tick).
- Not moved (and how it is handled):
  - **file mtimes** written by the OS: the churn engine sets `os.utime(path, sim_now)` on every file it writes;
    after each event the **mtime normaliser** sets mtime (and creation time via `SetFileTime`, ctypes) of every NEW
    file / folder under `runtime\run_logs` and `runtime\logs` to the event's sim instant. Without it the first 06:00
    check (2090 clock) would purge every run log at once (real mtime 2026 = "64 years old"). This is the only
    filesystem clock seam; it never touches source / NAS / bucket data written by the tools (robocopy and rclone copy
    the source mtimes, which are already sim mtimes).
  - **subprocesses** (rclone / robocopy log timestamps): real 2026 time; nothing in the outcome logic compares them
    (UNVERIFIED for `cloud_sync` json-log parsing: spike checks one outcome per status class).
  - **`time.monotonic`** (run budgets): real elapsed time - correct.
  - **SQLite**: dc50265 uses no `CURRENT_TIMESTAMP` / `datetime('now')` (grepped `core/manifest.py`).
  - **Prefect ephemeral server**: same process, sees 2090 too. UNVERIFIED that the global-concurrency lease and the
    late-run service tolerate day jumps between flows; spike L1 runs 10 events with 1-day jumps and a slot hold.
- Clock-skew / backward scenarios only move this traveller (or the Google seam). The system clock is never changed.

Sim years: configured `FY90-91`, next `FY91-92`, then `FY92-93`, `FY93-94`. `fy_overlap._start_year` = 2000 + nn, so
FY90-91 = Apr 2090 - Mar 2091. Lane A day 0 = **Fri 2090-12-01**, day 727 = **Thu 2092-11-27** (728 days; 104
Sundays, 104 Mondays, 24 first-of-month, 183 overlap days, leap day 2092-02-29 inside the second overlap).

### 1.5 Per-day driver loop (`tools/livebox/run_lane.py`)

```
for day in lane.days:                                   # sim dates, IST
  for ev in calendar.events(day, lane.overrides):        # sorted by instant; box schedule:
      # 02:01 cloud | 06:00 rollover | 08:00 audit (Sun) | 08:00 weekly (Mon) | 08:30 monthly (1st)
      # 09:00 status+watchdog | 12:00 churn | 21:01 LAN | 22:30 watchdog | 23:30 status
    safety.pause_if_production_busy()                    # 1.8 S10-S12 (real clock), then re-check
    if lane.outage(ev.instant): log SKIPPED_BY_OUTAGE; continue   # server off: nothing runs, nothing recorded
    scenario.before(ev)                                  # injections (section 4)
    clock.move_to(ev.instant)
    result = run(ev)                                     # real flow call / status read / churn / watchdog
    mtime_normaliser.apply(ev)
    oracles.after(ev, result) -> checks.jsonl            # rows, files, mails, status, storm rules
    scenario.after(ev, result)
  oracles.end_of_day(day)                                # calendar assertions of section 2
  state.save(day)                                        # resume point (lane can be restarted after a pause/crash)
```
Server-off catch-up (scenarios X04-X06): at the boot instant the driver replays the late runs the way Prefect does
after a power-on - the number and order of late runs is CALIBRATED from the 2026-11-04..23 precedent (Phase 0 L0.4:
which late runs Prefect started at boot on 23 Nov and which became `*_MISSED`), each with `expected_start_time` = its
cron instant and the real-clock lateness. Assertions never depend on a guessed replay count: they read it from the
calibration file `lanes\catchup_model.yaml`.

### 1.6 Fake SMTP and mail accounting

One `smtp_server()` per lane process. After every event the driver drains `srv.messages` to
`evidence\mail\<seq>_<simtime>.eml` and appends `mail_index.csv` (sim time, real time, rcpts, subject, X- headers if
any) and joins it with the `notifications` rows (key, kind, status, fault_code, audience). Storm rules (every lane,
every day; violations are FAIL):
- M1: the same notification key is mailed at most once per `alerts.repeat_cooldown_hours` (12); `stale:` keys at most
  once per `alerts.stale_repeat_hours` (24); `rollover:activation:<fy>` at most once per 24 h.
- M2: healthy days (no injection active and none in the previous 24 h) send 0 FAILURE / ACTION_REQUIRED / WARNING /
  RECOVERED mails.
- M3: per sim day at most 4 problem mails per lane (2 legs x 2 FYs) unless the scenario's expected-mail list says more.
- M4: after an outage of N days, catch-up sends at most 1 mail per (leg, FY) per class (missed / overdue / failure),
  never N.
- M5: report mails go to `report_recipients` only; problem mails to `recipients` (audience rule of dc50265).

### 1.7 Cloud "no internet" without touching GCS

Normal: local remote. Injections (all keep the dummy key, so nothing can authenticate even if a packet left):
- NI-1 blackhole endpoint: rclone config `[aam_gcs] type = google cloud storage`, `endpoint = http://127.0.0.1:9`,
  `service_account_file = <dummy key>` -> connection refused (expect AAM-CLD-01 / transport `NETWORK`).
- NI-2 DNS: `endpoint = https://aam-livebox.invalid` -> getaddrinfo failure (box precedent; expect AAM-CLD-01 `DNS`).
- NI-3 remote path gone: rename `<lane>\bucket` -> "bucket not found" class (expect AAM-CLD-10 / CLD-06, record).
- Clock seam raises `OSError` at the same time (no internet also means no Google Date header); w32tm points at
  127.0.0.1, so the clock is UNVERIFIED (policy `health.clock_unverified_policy` decides the cloud leg).

### 1.8 Safety refusals (`tools/livebox/safety.py`; every one aborts the lane before the first event)

| ID | Refuse when |
|---|---|
| S1 | box HEAD is not the commit the owner named in D-0, or `git -C C:\AAMBackup status --porcelain --untracked-files=no` is not empty |
| S2 | any sandbox path (source, NAS local path, bucket dir, runtime, database, log dir, run_logs dir, lock, prefect home, TEMP) is not under `C:\AAMLivebox\<stamp>\<lane>\` |
| S3 | a path equals or is under `C:\AAMBackup`, `C:\BackupAgent`, `E:\`, the production NAS share, or the production `log_directory` |
| S4 | a sandbox FY leaf is not in `{FY90-91 .. FY93-94}` (or the century lane's `FY99-00`/`FY00-01`), or production is configured on one of them |
| S5 | `models.config.CONFIG_PATH` after import is not the lane config, or `flow.__file__` is not under `C:\AAMBackup` |
| S6 | `PREFECT_API_URL` resolves to anything but the lane's ephemeral server inside the harness (never `:4200`) |
| S7 | `notifications.smtp_host` is not `127.0.0.1`, `smtp_auth` true, or a recipient domain is not `.invalid` |
| S8 | the rclone config the flows use is not the lane's file, or its remote type is `google cloud storage` without an endpoint on 127.0.0.1 / `.invalid` (outside the gated smoke) |
| S9 | `core.shutdown.shutdown_server` / wol send are not the raisers, or `flow.lan_shutdown_task` is not the spy |
| S10 | production `backup.lock` exists (path from the production config, read only) -> pause, not abort |
| S11 | a production job (cloud / LAN / audit / rollover / reports, crons from the production config) starts within 30 min (real clock) -> pause until it ended (lock gone 10 min, `cronsim` from `tools`-style `check_schedule`) |
| S12 | free space on C: < 50 GB or the lane root > 20 GB -> abort |
| S13 | more than 3 lane processes run at once |
| S14 | the production Prefect slot `aam-backup` is read as taken (read-only GET) at lane start |
| S15 | a robocopy / rclone argv built by the flows contains a path outside the lane root (checked by a `run_tool` wrapper that only inspects argv, then calls the real `run_tool`) |

### 1.9 Cleanup

`run_lane.py --cleanup <lane>`: only deletes `C:\AAMLivebox\<stamp>\<lane>` after S2-S4 re-checks; never a
production path, never a cloud prefix (none exists outside the gated smoke). The lane's Prefect home and TEMP are
inside the lane. Leftover check (Haiku): `Get-ChildItem C:\AAMLivebox` is the only place with test data; production
`E:\`, `C:\BackupAgent`, `git status` unchanged; production `/health` items unchanged versus the pre-test snapshot.

### 1.10 Runtime estimate and acceleration (UNVERIFIED until spike L1 measures seconds per event)

| Event | Est. per FY pass (small seed) |
|---|---|
| cloud flow (preflight lsjson, dry-run, sync, check, size, lsjson, combined) | 10-15 s |
| LAN flow (SMB probe, 2 x `/L`, mirror, 2 walks) | 8-12 s |
| rollover check + log retention, status reads, watchdog check, churn | 3-5 s per day |
| Sunday audit | 10-20 s |

Lane A: ~545 single-FY days x ~30 s + 183 overlap days x ~55 s + 104 audits ≈ 8-9 h of compute, plus pauses around
production jobs (S11) ≈ 1.5-2 elapsed days. Acceleration that does not fake results:
- small seed (default 300 files / ~15 MB in a shape derived from `E:\FY26-27`, section 3); one fidelity lane F
  seeds the full 703-file / ~96 MB shape for 30 days around activation;
- full sha256 tree convergence only on Sundays and special days; other days compare (count, total bytes, newest
  mtime) per tree;
- **no day is skipped in lane A**. Lane A' (iteration only, never the verdict) runs special windows + every 7th day
  and marks skipped days `NOT_SIMULATED`; overdue / report / strip assertions are switched off for those ranges;
- parallelism: each adverse scenario is its own short lane (fresh seed, warm-up of 10-14 sim days before its trigger
  date, so it has baselines and history) - 10-20 min each; up to 3 lane processes at once (S13), all paused together
  by S10/S11. Forks from a lane-A checkpoint are NOT used (absolute paths are baked into G4 baselines and the overlap
  record; rewriting them would hide bugs).

---

## 2. Calendar assertions (lane A, dc50265 semantics, box schedule)

Notation: `C` = configured FY, `N` = next FY; `p` = flow-run prefix. "Rows" = new `run_history` rows of that event.
Every day, in addition to the class rules: Prefect state COMPLETED for every flow with only success rows; FAILED
otherwise (D5); `backup.lock` absent after each event; no RUNNING row left; storm rules M1-M5; NAS wake spy = 1 per
LAN flow; shutdown spy = 1 per LAN flow whose passes are all `LAN_COMPLETE`, else 0; archive recorder untouched except
on switch days.

| Day class / date (sim) | Cloud 02:01 | 06:00 check | LAN 21:01 | Data truth (end of day) | Mails | `/status` 09:00 / 23:30 |
|---|---|---|---|---|---|---|
| D0 Fri 2090-12-01 (first run) | 1 row `p-cloud` `CLOUD_COMPLETE` | `NO_ROLLOVER_NEEDED`; activation `NOT_DUE` | 1 row `p-lan` `LAN_COMPLETE` | src FY90-91 == NAS FY90-91 == bucket/FY90-91 (sha256); bucket holds only FY90-91 | monthly 08:30 (Nov period, may say "no backup ran" = expected on day 0, record) | `cloud`/`lan` green, `cloud_next`/`lan_next` absent, `overlap.active` false, `activates_on` 2090-12-31 |
| Ordinary Dec 2..30 | 1 row, COMPLETE or `CLOUD_NO_CHANGES_COMPLETE` (stats prove 0 transfers) | NOT_DUE; no `overlap:` meta | 1 row `LAN_COMPLETE` | converged; no FY91-92 anywhere | 0 problem mails; Mon weekly 1 to partners | 2 cards |
| Sibling alarm (Dec, lane A seeds NO FY91-92 folder) | - | `_alarm_unprotected_new_fy` silent | - | - | 0 | - |
| **Sun 2090-12-31 activation** | 1 row (C only; activation is at 06:00) | `ACTIVATED`: `src\FY91-92` + `README_FY.txt` created; `db_meta['overlap:FY91-92']` {activated_at 2090-12-31T06:00+05:30, from_fy FY90-91, source_folder, clock OK}; config.yaml unchanged | 2 rows: `p-lan` (C) then `p-lan-FY91-92` (N), both `LAN_COMPLETE`; `nas\backup\FY91-92` created + `.AAM_TARGET_MOUNTED` | NAS FY91-92 == src FY91-92 (placeholder) | exactly 1 INFO `rollover:activated:FY91-92` to partners; 0 problem mails | 4 cards after 06:00: next-FY cards "first backup pending" until their first success; `overlap.active` true |
| Sun 2090-12-31 08:00 audit (edge E1) | - | - | - | audit runs before N's first cloud pass and first LAN pass | **expect no ACTION mail**; N rows `AUDIT_INCOMPLETE` / `PENDING_BACKUP` or N skipped; a `VERIFICATION_FAILED` + AAM-AUD-01 mail = FINDING | amber at most |
| **Mon 2091-01-01** | 2 rows: `p-cloud` (C), `p-cloud-FY91-92` (N); bucket/FY91-92 appears | NO_ROLLOVER; `ALREADY_ACTIVE` | 2 rows | all 4 trees converged | weekly (Mon 08:00): sections per (leg, FY), N counted from activation (no false "no backup ran"); monthly 08:30 (Dec): N cloud section must not claim a missed run for 31 Dec 02:01 (before activation) - else FINDING | 4 green cards |
| Overlap ordinary days Jan 2 - Mar 30 | 2 rows (C first) | ALREADY_ACTIVE | 2 rows | converged; same relative path in both FYs independent (churn writes `shared.txt` in both) | 0 problem mails | 4 cards; `recent_runs` carry `fy` |
| Overlap Sundays | as above | as above | as above | 08:00 audit: 4 rows (cloud/lan x C/N), `VERIFIED` or `PENDING_BACKUP` (amber, no mail) | 0 | integrity per card |
| Sat 2091-03-31 last in-window | 2 rows | NO_ROLLOVER | 2 rows | converged | 0 | 4 cards |
| **Sun 2091-04-01 switch** | 02:01: 2 rows, prefixes still FY90-91 / FY91-92 (leaf rule `effective_fy_prefix`) | journal: final_backups (cloud C + LAN C, LAN with `allow_shutdown=False`), G6 pass, source_folder/nas_folder "exists" done, config_switch -> lane config now FY91-92 (both paths), `overlap:FY91-92.ended_at` stamped, approvals of FY90-91 superseded, archive recorder == [(bucket, FY90-91)] only, nas_shutdown step, announce; row `ROLLOVER_COMPLETE`; return `ROLLOVER_COMPLETED` | 1 row `p-lan` = FY91-92 (now configured, unsuffixed) | FY90-91 NAS + bucket frozen (hash snapshot F1); FY91-92 converged | 1 announce INFO; 0 problem mails; NAS wake/shutdown spy counts during the 06:00 check recorded (FINDING F-OWN-1 if > 0, owner 2026-11-23: rollover never touches the NAS) | 2 cards, FY91-92; known gap B7-Q10 (card may show FY90-91 rows for ~1 day) = KNOWN, not FINDING |
| Sun 2091-04-01 08:00 audit / 08:30 monthly | - | - | - | audit: FY91-92 only; FY90-91 audit rows unchanged | monthly (Mar): sections FY90-91 + FY91-92 | - |
| After switch, Apr 2 2091 - Dec 30 2091 | 1 row | NO_ROLLOVER; activation NOT_DUE for FY92-93 | 1 row | FY90-91 trees == F1 forever (a late edit in src FY90-91 on Apr 2 is NOT copied = accepted risk, asserted) | 0 problem mails | 2 cards |
| Retention boundary (lane A, 90 days) day 91 = Sat 2091-03-02 and every day after | - | `log_retention_task`: run_logs day folders older than 90 days removed, 89-day folder kept | `purge_old_runs` after successful flows: oldest `run_history.started_at` >= sim_now - 90 d - 1 d, nothing younger removed | `db_meta` overlap records never purged; guard baselines still present (daily success) | - | 7-night strips correct |
| Closed-FY history (lane A) ~2091-06-30 | - | - | - | all FY90-91 rows purged at 90 days: reports / status must not crash or show FY90-91 as red | 0 | - |
| **Mon 2091-12-31 activation 2** | 1 row (FY91-92) | ACTIVATED FY92-93 (new meta key; `overlap:FY91-92` untouched) | 2 rows | as day 0 activation | 1 INFO `rollover:activated:FY92-93` | 4 cards |
| Tue 2092-01-01 .. Mon 2092-03-31 | 2 rows | ALREADY_ACTIVE | 2 rows | converged; **Fri 2092-02-29** ordinary (leap day irrelevant: same rows, no date error) | 0 | 4 cards |
| **Tue 2092-04-01 switch 2** | 2 rows | switch FY91-92 -> FY92-93 as above | 1 row | FY91-92 frozen (F2) | 1 announce | 2 cards |
| Apr 2 - Thu 2092-11-27 (end) | 1 row | NO_ROLLOVER | 1 row | converged | 0 problem mails | 2 cards |

Whole-lane totals (lane A, healthy): problem mails 0; INFO activation 2; announce 2; weekly 104; monthly 24;
`overlap:` meta keys exactly {FY91-92, FY92-93}, both with `ended_at`; archive recorder exactly
[(bucket, FY90-91), (bucket, FY91-92)]; open approvals 0; open incidents 0; per-day row counts as in the table (the
calendar generator emits the exact expected list; the oracle compares lists, never totals only).

Lane A730 (`db_retention_days`/`run_logs.retention_days`/`log_retention_days` = 730): same calendar, assertion: **no
row, run-log folder or app log is ever deleted** in 728 days (730 is not reachable; the young side of the boundary is
what is proven). Lane R35 (60 sim days, db 35 / run_logs 7 / log 7): exact boundary days 8 and 36 (kept at N-1,
removed at N+1), plus baseline-bug-2 probe (retention 0 refused by the model bounds).

---

## 3. Daily data churn model (`tools/livebox/churn.py`, deterministic, seeded RNG per lane)

Seed shape: read-only walk of `E:\FY26-27` (count of folders, depth, files per folder, size histogram, extensions;
**no names copied**), regenerated as `Client_###\<Type>\file_####.<ext>` with random bytes. Default scale 300 files;
lane F full 703 files / ~96 MB. Every write sets `os.utime(sim_now)`.

| Event | When | Configured FY (C) | Next FY (N) |
|---|---|---|---|
| add | every working day (Mon-Sat) 12:00 | 3-8 new files | Jan-Mar: 2-6 new files (US work); Apr-Dec: N is the configured FY |
| modify | working days | 2-5 files rewritten (same size and different size, same-second mtime once a week) | 1-3 |
| delete | working days | 0-2 files (keeps G3 at 0-2 %) | 0-1 |
| rename / move folder | 1st Wednesday of month | 1 folder renamed (deletes + adds, below G3) | - |
| same relative path in both FYs | Jan 3 / Feb 15 | `Clients\shared.txt` in both; deleted in C only on Feb 15 (N copy must survive) | |
| idle | Sundays, and 7 consecutive days in Aug | no change -> `CLOUD_NO_CHANGES_COMPLETE` path | |
| late edit of a closed FY | Apr 2 after each switch | file added to the closed FY source: must NOT appear in NAS / bucket | |
| large file | 15th of each month | one 50 MB file added, deleted next 15th | |

Periodic guard events (adverse lanes, section 4 wave 3): mass delete 15 % (G3 AAM-DEL-01), source shrink 25 % (G2
AAM-SRC-04), source wipe to empty (G1 AAM-SRC-03), destination path changed (G4 AAM-NAS-08), canary deleted (G5
AAM-NAS-10), office-made next-year folder with files in December (sibling alarm ROL-06, adoption at activation).
Small-folder rule (D-11) does not exist in dc50265: a 10-file N folder with 2 deletes is 20 % > 10 % -> G3 block
expected on dc50265 (record; fy-overlap would pass).

---

## 4. Adverse scenario matrix (each = its own lane unless "lane A")

Columns: setup / injection (real tool behaviour unless marked FAKE) / expected (status, fault, mail, dashboard) /
pass criteria. "Record" = the outcome is written as observed and compared with the docs; a mismatch is a FINDING,
not a harness FAIL. Every scenario also runs the storm rules M1-M5 and the cleanup check.

### Wave 1 - special days x outages (highest priority)

| ID | Setup / injection | Expected | Pass |
|---|---|---|---|
| X01 | No internet across activation: NI-1 + clock seam `OSError` from Dec 30 21:00 to Jan 1 12:00 | Dec 31 02:01 `CLOUD_FAILED` AAM-CLD-01 (C); 06:00 `CLOCK_UNPROVEN`, nothing created, WARNING `rollover:activation:FY91-92` AAM-ROL-07; Jan 1 02:01 cloud fails again (no 2nd mail inside 12 h); Jan 1 06:00 ACTION_REQUIRED ROL-07; Jan 2 06:00 activated + INFO; LAN green all days | exactly 1 WARNING + 1 ACTION + 1 INFO for activation; cloud FAILURE mails <= 2 over 36 h; N files saved Jan 1 present on NAS after Jan 2 21:01 and in bucket after Jan 3 02:01 |
| X02 | No internet on switch day: NI-1 + clock OSError Mar 31 21:00 - Apr 2 03:00 | Apr 1 02:01 cloud FAILED (both passes); 06:00 clock unproven -> AAM-ROL-03 "switch waits" (refused, not approvable), config stays FY90-91; LAN both FYs Apr 1 21:01; Apr 2 06:00 switch completes | no switch while unproven; both FYs backed up every night until the switch; exactly 1 announce |
| X03 | No internet on switch day but clock proven (NI-1 only) | 06:00 final cloud backup fails -> G6 blocks: `ROLLOVER_BLOCKED`, approval request, ACTION `blocked:rollover` AAM-ROL-02; nightly runs keep both FYs; Apr 2 02:01 cloud OK (after restore) -> Apr 2 06:00 counts that verified copy (started >= closing moment) and switches without a click | switch on the first check after a verified copy exists; never before |
| X04 | Server off 3 days over activation: outage Dec 30 00:00 - Jan 2 09:00 (boot); catch-up per calibration | at boot: late runs -> `CLOUD_MISSED`/`LAN_MISSED` (AAM-SYS-07 WARNING) as in the Nov 2026 precedent; activation at the first 06:00 check run (late run at boot or Jan 3 06:00 - per calibration) with today >= activate_on (late activation allowed); overdue (watchdog) mails | per (leg, FY): <= 1 missed + <= 1 overdue mail, never 3 (M4); activation exactly once; RECOVERED/resolved per dc50265 rules (record) |
| X05 | Server off 3 days over the switch: Mar 30 00:00 - Apr 2 09:00 | first check after boot classifies FORWARD, switch pending meanwhile; final backups run (or `*_MISSED` -> G6 block, record); switch on the first possible morning | config never switched backwards; both FYs protected until the switch; mails per M4 |
| X06 | Server off 3 days mid-overlap (Feb 10-13) | overdue per (leg, FY): stale keys `stale:<leg>:FY91-92` separate from C | max 4 overdue mails per 24 h, one per (leg, FY) |
| X07 | Rollover day MISSED with a late scheduled 06:00 run (lateness 8 h) | rollover still runs (it has no budget MISSED rule? record); IST date = scheduled date | record |
| X08 | NAS unreachable on activation night: lane `lan_destination` host -> `192.0.2.1` (TEST-NET, real SMB timeout) for Dec 31 21:01 | both LAN passes fail: C `LAN_FAILED` AAM-NAS-01 (or SYS-06 = baseline bug 1, record), N fails before folder creation (AAM-LAN-01); shutdown spy 0; next night recovers, N NAS folder created then | 1 failure mail per (lan, FY); no NAS folder created on the wrong host; recovery converges |
| X09 | NAS unreachable on switch day 06:00 (scheduled LAN leg) | dc50265: LAN final fails -> G6 blocks (ROLLOVER_BLOCKED) | record as **FINDING F-OWN-1** (owner 2026-11-23: switch with cloud only, NAS never touched by rollover) |
| X10 | Junction: `mklink /J src\FY91-92 <lane>\elsewhere` on Dec 30 | `REFUSED_LINK`, ROL-07 WARNING Dec 31, ACTION Jan 1; nothing created; N not active | no write through the junction (target dir unchanged) |
| X11 | Next-year folder deleted after activation (Jan 15, 12:00) | N passes fail AAM-SRC-01 every night; folder never recreated; C passes green | 1 mail per (leg, N) per 12 h; `src\FY91-92` still absent after 5 days |
| X12 | Source parent not writable (icacls deny `CreateDirectories` on `<lane>\src` for the ssh user, sandbox only) at activation | `FOLDER_FAILED`, ROL-07 with the reason; retried each morning; ACL removed Jan 3 -> activated Jan 4 | as X10 |
| X13 | Clock skew +1 h (Google seam) on Dec 31 | activation `CLOCK_UNPROVEN` (SKEWED), cloud `CLOUD_FAILED` AAM-CLK-01 | no activation on a skewed clock |
| X14 | Forced switch: X09 state, approve G6 on the dashboard with phrase `APPROVE`; wrong phrase `approve ` first | wrong phrase -> 400, nothing changes; right phrase -> next check switches, announce + row name the forced leg (`final_status`) | switch only after the exact phrase; approval used once |
| X15 | Failed switch step: icacls deny write on the lane `config.livebox.yaml` (+ its lock file) at Apr 1 06:00 | `ROLLOVER_FAILED` AAM-ROL-04; Apr 2 06:00 repeats the final backup then switches (ACL removed) | journal resumes, no double archive |
| X16 | Archive step fails (recorder returns False) Apr 1-5 | ROL-05 WARNING daily, ACTION_REQUIRED after `rollover.archive_alert_after_days` (3) | switch done; archive retried; mail counts per cooldown |
| X17 | Crash mid-rollover: lane process killed (`taskkill /T /F` on a child flow process) right after `final_backups` | next morning: `ROLLOVER_ABANDONED` swept, journal resumes at the first unfinished step | no step repeated except final backups (D-17) |
| X18 | Hash-before-close (NOT BUILT): Mar 31 21:30 change bytes of one NAS FY90-91 file keeping size + mtime | dc50265 switches on Apr 1 without noticing | **expected red = FINDING F-OWN-2** (owner W9/W14) |
| X19 | Doc conflicts: on Apr 1 count NAS wake / shutdown spies and LAN runs inside the 06:00 check; try to approve a G1 block | record vs ARCHITECTURE 5.2 / owner 2026-11-23; G1 approvable? record | FINDINGs only |

### Wave 2 - guards, approvals, folder states

| ID | Setup / injection | Expected | Pass |
|---|---|---|---|
| X20 | G4: edit `lan_destination` to `...\nas\backup2\FY90-91` (UI settings, typed phrase) Feb 5 | `LAN_BLOCKED` AAM-NAS-08 + approval; approval re-run copies | per-FY block only on C |
| X20b | G4 local: `guards.forbid_local_destination: true` | `LAN_BLOCKED` AAM-NAS-07 | - |
| X21 | G5: delete `.AAM_TARGET_MOUNTED` in NAS FY91-92 (Feb 10) | N pass `LAN_BLOCKED` AAM-NAS-10; C green; approval creates marker | approval of N cannot run C (`evaluate_approval` fy check) |
| X22 | G3: delete 15 % of N files (Feb 20) / small N folder 2 of 10 files | `*_BLOCKED` AAM-DEL-01 per FY; approval flow; numbers worse than approved -> refused | per-(leg, FY) supersede; approval expiry 12 h; reuse refused |
| X23 | G2: shrink C by 25 % | AAM-SRC-04 BLOCKED | - |
| X24 | G1: wipe C source to empty (files gone, folders stay) | AAM-SRC-03 BLOCKED both legs (C only); is it approvable? record | no transfer ran (bucket / NAS unchanged) |
| X25 | Office-made `src\FY91-92` with 20 files on Dec 15 | Dec 16 06:00 sibling alarm ROL-06 (ACTION, once); Dec 31 adopted (no placeholder overwrite), alarm resolved | files present on NAS / bucket after first N passes |
| X26 | Misnamed `src\FY 91-92` with files | record what dc50265 does (fy-overlap: ROL-07 "wrong name"); expected at least one ACTION mail | FINDING if silent |
| X27 | Empty office-made `src\FY91-92` | adopted, placeholder written | - |
| X28 | Next-year NAS folder exists with files, no marker | N LAN pass G5 asks (AAM-NAS-10) | C unaffected |
| X29 | Approval of a closed FY after the switch (open FY90-91 approval on Mar 31, switch Apr 1) | request superseded / refused (409) | - |
| X30 | Manifest wipe: rename lane `manifest.db` (+ -wal/-shm) on Feb 12 | overlap record lost -> N not active until the next 06:00 re-activation (adopts the folder; a second INFO mail? record); first runs: G2/G3 baselines empty (no first-run approval in dc50265 = record vs fy-overlap 5.5) | no data deleted on NAS / bucket without a guard verdict |
| X31 | Overlap switched off Feb 1 (UI, typed phrase), on again Feb 3 | passes stop at once; attention RED `overlap_off:FY91-92`; on again -> N passes resume, files from Feb 1-3 copied | - |
| X32 | Overlap never on (`overlap_enabled: false` from Dec 1) | no activation; ROL-06 alarm when N has files | - |
| X33 | Config edit mid-window: `overlap_activate_on` 12-31 -> 01-15 on Jan 5 | record stays, N stays active | - |

### Wave 3 - legs and modes

| ID | Setup | Expected | Pass |
|---|---|---|---|
| X34 | Cloud only (`lan.enabled: false`) over activation + switch | config loads (check whether a UNC `lan_destination` is still required: record); activation creates source only; G6 cloud only; 2 cards | - |
| X35 | LAN only (`cloud.enabled: false`) over activation + switch | no archive call; G6 LAN only | - |
| X36 | LAN manual only (`lan.scheduled: false`) over the switch | 06:00 does not run LAN; G6 blocks until a manual LAN press on the FY90-91 card after the closing moment; next check switches (plan baseline section 4 example) | FINDING F-OWN-1 vs owner 2026-11-23 |
| X37 | Two triggers: dashboard `/trigger/lan` while a LAN flow runs | 409; no second robocopy (argv log) | - |
| X38 | Two processes start `backup(mode="lan")` at the same instant (both in one lane, shared lane Prefect server on 127.0.0.1:4299 - spike first) | second waits for / is refused by the slot, then `backup.lock`; never two robocopy | - |
| X39 | Run killed mid-sync: kill the child process tree during the N robocopy | next start: `LAN_ABANDONED` (FY in run id), AAM-SYS-02 mail once; no orphan robocopy left (check by argv); next night converges | - |

### Wave 4 - environment and robustness

| ID | Setup | Expected | Pass |
|---|---|---|---|
| X40 | Locked file: exclusive handle (CreateFile share 0) on one C file across 21:01 and 02:01 | LAN partial/suspect AAM-LAN-02; cloud PARTIAL/FAILED (record); one mail each | honest status (never COMPLETE) |
| X41 | Long paths (> 260) and Unicode names (Malayalam, emoji, trailing space) in both FYs | converged; audit VERIFIED | - |
| X42 | Disk full: lane runtime or NAS target on a 2 GB VHD (diskpart, **gated: owner OK, system change**) | AAM-SYS-05 / AAM-NAS-04, `LAN_PARTIAL` or FAILED | - |
| X43 | DB corruption (overwrite bytes 4096-8191 of the lane DB) / DB read-only ACL | loud failure (AAM-SYS-01, record), no silent success row | - |
| X44 | SMTP down 2 days (server stopped) during X08-like failures; then 451; then 550 | `notifications` FAILED rows; transient vs permanent by reply code; on recovery no storm (digest if built: record) | M4 |
| X45 | Clock backward 2 days on Jan 3 (traveller) / back across Apr 1 after the switch | no reverse switch (CLK-03), no second activation, overdue sane | - |
| X46 | Clock jump +13 months (FY jump) | AAM-ROL-01 approval card, no automatic switch | - |
| X47 | Report flows during an outage on Monday / the 1st, run late at boot | `_scheduled_time` keeps the right period | - |
| X48 | Century wrap lane (optional, low): FY99-00 -> FY00-01 around 2100-01-01 / 2100-04-01 | record (`_start_year` = 2000 + nn may classify FY00-01 as CLOCK_BEHIND) | FINDING only |

Not built per research, expected red (recorded as FINDINGs, never blocking the run): F-OWN-1 rollover touches the NAS
(X09, X19, X36), F-OWN-2 hash verify before old-year close (X18), F-OWN-3 "close this year anyway" button (UI has only
the G6 approval; check `ui.py` routes), F-OWN-4 next-year folder on 31 Dec (dc50265 DOES activate on 12-31: expected
PASS here, red on fy-overlap), F-OWN-5 730-day retention (box 90).

---

## 5. Work packages (small, independent; tests and code by different agents; Opus reviews)

Repo location: the build under test's branch (`baseline-v1-overlap`, worktree `C:\Users\malliath\Desktop\aam_baseline_overlap`)
under `tools/livebox/`; nothing is committed without the owner's go (that branch's handoff rule). Evidence:
`agent_runs/livebox_<stamp>/<wp>/`. "Tests" are pytest files under `tools/livebox/tests/` run on the dev PC.

| WP | Model | Inputs | Outputs | Acceptance | Depends |
|---|---|---|---|---|---|
| L0.1 Box identity probe (read only) | Haiku | this plan 0, 1.8 S1 | `agent_runs/livebox_<stamp>/L0/box_identity.txt`: `git -C C:\AAMBackup rev-parse HEAD`, branch, `status --porcelain`, venv `python -V`, `pip list`-equivalent of `C:\AAMBackup\.venv\Lib\site-packages` (names only), `whoami /priv`, `Test-Path \\127.0.0.1\C$\` | HEAD reported; mismatch with D-0 = stop and tell the owner | - |
| L0.2 Production schedule + lock snapshot | Haiku | prod config (read) | `L0/prod_schedule.json` (crons, lock path, log dir) | used by S10/S11 | L0.1 |
| L0.3 Source shape (read-only walk of `E:\FY26-27`, counts/sizes/extensions only) | Haiku | spec 3 | `L0/source_shape.json` (no names) | 703 files ±0 | L0.1 |
| L0.4 Catch-up calibration (read the 2026-11-23 boot evidence the coordinator already has; if a DB read is needed: `sqlite3` URI `mode=ro`, owner OK first) | Haiku | coordinator facts 4 | `tools/livebox/lanes/catchup_model.yaml` | owner-reviewed | L0.1 |
| L1 Clock + process spike | Sonnet (code) / Haiku (runs on box) | 1.4 | `tools/livebox/spike_clock.py`, `L1/spike.json`: time/datetime/pendulum moved, subprocess real, one real cloud + LAN flow at 2090-12-31 in a lane, 10 jumps with slot, mtime normaliser, detached process survives ssh close, seconds per event | all 7 probes green, or the plan's fallback chosen by Opus | L0.1 |
| L2a derive_config | Sonnet | 1.2 table | `tools/livebox/derive_config.py` | L2b tests green | L1 |
| L2b derive_config tests | Haiku | 1.2 table (exact) | `tools/livebox/tests/test_derive_config.py` (every row, plus "prod log_directory set" refusal) | red before L2a | - |
| L3a safety | Sonnet | 1.8 | `tools/livebox/safety.py` | L3b green | L2a |
| L3b safety tests | Sonnet (other agent) | 1.8 | `tests/test_safety.py`, one test per S-row | red first | - |
| L4a rig (seams 1.3, clock 1.4, SMTP 1.6, cloud variants 1.7, spies) | Sonnet | 1.3-1.7, `audit_lab/labconf.py`, `labclock.py`, `test_fy_overlap_lab.py` rig | `tools/livebox/rig.py` | L4b green on dev PC | L2a, L3a |
| L4b rig tests | Sonnet (other agent) | same | `tests/test_rig.py` (each seam installed / removed; raisers really raise; S15 argv check) | red first | - |
| L5a calendar generator (pure) | Sonnet | section 2 + box crons | `tools/livebox/calendar.py`: events + expected rows / mails / status per day, lane overrides, outage windows | L5b green | - |
| L5b calendar tests | Haiku | section 2 table (exact dates) | `tests/test_calendar.py` (activation 2090-12-31, switch 2091-04-01, 104 Sundays, 24 firsts, 183 overlap days, leap day) | red first | - |
| L6a churn | Haiku | section 3 (exact) | `tools/livebox/churn.py` | L6b green | L0.3 |
| L6b churn tests | Sonnet | section 3 | `tests/test_churn.py` (determinism, utime = sim time, G3 stays < 10 % on normal days) | red first | - |
| L7a oracles | Sonnet | 1.6, section 2 | `tools/livebox/oracles.py` (SQL on lane DB, tree hash, mail index + M1-M5, `/status` reader, spy counts) | L7b green | L4a |
| L7b oracle tests | Haiku | 1.6 rules (exact) | `tests/test_oracles.py` (fabricated DB / mail fixtures) | red first | - |
| L8 driver | Sonnet | 1.5 | `tools/livebox/run_lane.py` (loop, pauses, resume state, evidence, `--cleanup`) | runs lane A' 60 days on the dev PC (loopback NAS, never power off: spies asserted) | L4a-L7a |
| L9 scenario packs | Sonnet per wave (one agent per wave) | section 4 | `tools/livebox/scenarios/w1.py` .. `w4.py` (inject / expect per ID) | each scenario green or a written FINDING on the dev PC | L8 |
| L10 Opus review | Opus | all of tools/livebox + this plan | `agent_runs/livebox_<stamp>/L10/review.md` (safety first, then fidelity, loosened asserts) | no open S-level issue | L9 |
| L11 box deploy of harness + overlay | Haiku | 1.1 recipe | `L11/deploy.txt` (hashes of copied files) | S1-S9 dry-run passes on the box | L10, owner go |
| L12 box lane A' smoke (60 days) | Haiku runs / Sonnet triage | - | `<stamp>/A1/evidence` pulled to `agent_runs/livebox_<stamp>/A1/` | green or triaged | L11 |
| L13 box lane A (728 days) + A730 + R35 + F | Haiku monitors | - | evidence per lane | section 2 assertions | L12 |
| L14 box waves 1-4 (<= 3 lanes at once) | Haiku runs / Sonnet triage | section 4 | evidence per scenario | per-scenario verdict | L12 |
| L15 report | Haiku (assemble) + Opus (final review) | section 6 | `agent_runs/livebox_<stamp>/REPORT.md` | every row PASS / FAIL / FINDING with evidence path | L13, L14 |
| L16 gated real smoke | Sonnet + owner | section 7.2 | `L16/smoke.txt` | owner approval recorded first | L15, owner |
| L17 cleanup verification | Haiku | 1.9 | `L17/leftovers.txt` | nothing outside `C:\AAMLivebox`; production snapshot unchanged | all |

Builder rules (owner 2026-11-24): builders never edit tests; a chunk failing twice stops and is reported; no commits
by builders; Opus checks `git diff --stat` against scope.

---

## 6. Reporting

`checks.jsonl` (one line per assertion): `{lane, scenario, sim_time, real_time, event, check_id, expected, observed,
verdict: PASS|FAIL|FINDING|KNOWN|NOT_SIMULATED, evidence: [paths]}`. Per-scenario table (generated):

| ID | Wave | Verdict | Expected (short) | Observed (short) | Fault / status | Mails (count by kind) | Evidence |
|---|---|---|---|---|---|---|---|
| X01 | 1 | PASS | ROL-07 W+A, INFO once | ... | CLOUD_FAILED AAM-CLD-01 | W1 A1 I1 F2 | `X01/evidence/checks.jsonl#L..`, `X01/evidence/mail/0007_*.eml` |

`REPORT.md` template:
```
# Live-box test report <stamp>
Build under test: <branch>@<sha> (box HEAD, L0.1)   Owner decision D-0: <...>
Lanes: A (728 d) <result> | A730 | R35 | F | waves 1-4 (<n> scenarios)
Totals: PASS n / FAIL n / FINDING n / KNOWN n / NOT_SIMULATED n
1. Verdict in three lines
2. FAILs (harness says the product is wrong against its own plan) - each: ID, expected, observed, evidence, first sim day
3. FINDINGs (product vs owner decision / docs) - F-0, F-OWN-1..5, new ones
4. Calendar lane A summary (rows per day class, mail totals vs expected, retention boundary results)
5. Mail storm table (max mails per sim day, per key; M1-M5 violations)
6. Fidelity notes (seams used, deviations: /ZB, G4 local, archive recorder, catch-up model)
7. Production untouched proof (git status, /health snapshot before/after, no file outside C:\AAMLivebox, slot free)
8. Gated phase result (or "not run")
9. Evidence index
```
Findings are also appended to the newest session Part doc (owner rule "document findings always").

---

## 7. Risks, stop conditions, gated phase

### 7.1 Stop conditions (abort all lanes at once, then tell the owner)

- Any S1-S9 or S15 refusal at runtime (a path, host, SMTP or rclone remote outside the sandbox appears).
- A production `run_history` row of tonight's run is FAILED / MISSED / SLOT_TIMEOUT while a lane ran in the 30 min
  before it (possible interference) - stop, compare timings, report.
- Production `/health` gets a NEW red item, the production watchdog logs a deferral that names a harness process
  (the watchdog treats any alive `rclone.exe` / `robocopy.exe` as Signal A, `watchdog.py:190-198`), or the agent
  API is down while a lane runs.
- C: free < 50 GB, lane root > 20 GB, a lane process > 4 GB RAM.
- A spy or raiser was bypassed (a real WoL packet, `shutdown_server` reached, `gcloud` spawned).
- The same harness defect makes a scenario fail twice (escalate, do not loosen asserts).

Other risks: R1 the detached process trick fails (fallback: run lanes only in an attended ssh session, daytime);
R2 time_machine ABI mismatch (fallback 1.4); R3 Prefect ephemeral server misbehaves with 2090 day jumps (fallback:
`flow.backup.fn` with `_backup_slot` kept REAL but `PREFECT_TEST_MODE`-free lane lock only - documented as reduced
fidelity of the slot); R4 renamed / local remote semantics differ from GCS beyond K8 (cross-checked once in the gated
smoke); R5 interference with production IO (lanes on C:, production on E: + NAS; BELOW_NORMAL priority; S11 pauses).

### 7.2 Optional gated real smoke (only after REPORT.md, only with the owner's written OK, day window, S10/S11 clear)

| Step | What | Guard |
|---|---|---|
| G-1 | One cloud flow on a lane config whose remote is the REAL `aam_gcs` (production key file, read only), bucket = production bucket, prefixes `FY90-91` / `FY91-92` only, 6 small files each, clock NOT simulated (rehearsal-style `run_date` seam does not exist in dc50265: port a 15-January patch of `fy_overlap.activate_if_due(now=)` + `active_passes`, Sonnet, reviewed) | refuse if either prefix holds objects; `rclone purge` only of exactly those prefixes afterwards (`is_rehearsal_prefix` logic ported) |
| G-2 | Real NAS: the ssh user is access-denied. Option (a) owner runs G-2 in an RDP session as `.\aambackup` against `\\<nas>\<share>\FY90-91` / `FY91-92`; option (b, recommended) skip: tonight's production LAN run already proves the real NAS path for FY26-27 | never shutdown (spy), never a WoL packet unless the owner allows the normal wake |
| G-3 | Clean-up proof: prefixes gone (`rclone lsjson` empty), NAS test folders gone, production prefix FY26-27 object count still 703 | - |

---

## 8. Adapter: if the owner chooses fy-overlap (736887c) as the build under test

Change only `tools/livebox/build_adapter.py` (one class per lineage) and the expected tables:
activation trigger (every backup flow + 06:00, `early_start_month` 1 -> 1 Jan, not 31 Dec); record
(`overlap_activations` table, v6 fresh DB - the lane starts with an empty DB anyway); pass order (next first); run ids
(both suffixed); INFO key `overlap:start:<fy>`; `/status` `folders[]` (fy, role, active_since, per-leg last run /
success / timing / integrity / approval / running); retention 730 (lane A becomes the 730 lane, R35 unchanged);
first-run-with-deletes approval (X30 expected DECISION); small-folder rule (X22 small case passes); sibling misnamed
alarm (X26 expected ROL-07); rehearsal tool usable for G-1. F-OWN-4 (31 Dec folder) becomes expected red there.

## 9. UNVERIFIED items (each closed by a named WP before the box run)

- U1 box venv is CPython 3.12 x64 and imports the copied time_machine (L1). U2 `Win32_Process Create` child survives
  ssh close (L1). U3 Prefect ephemeral server + global concurrency tolerate day jumps (L1). U4 ssh user can write
  `\\127.0.0.1\C$\AAMLivebox\...` and has / lacks SeBackupPrivilege (L0.1). U5 `watchdog._check_stale_legs` callable
  standalone with the lane config (L4a). U6 dc50265 dashboard login helper for TestClient (L4a). U7 whether
  `rollover_check_flow` writes a `run_history` row on `NO_ROLLOVER_NEEDED` (L5a reads the code). U8 loguru retention
  reads mtime or ctime (L1). U9 number of late runs Prefect starts after a multi-day outage (L0.4). U10 rclone json-log
  timestamps never enter an outcome decision (L1). U11 cloud-only config still needs a UNC `lan_destination` (X34).
