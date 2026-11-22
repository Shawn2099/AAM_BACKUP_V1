# Test design - release hardening (2026-11-22)

Scope: what can still hurt a customer's backups and is NOT covered by the unit suite (3478), the audit lab
(w0-w25), `tests/test_break_*.py` (flow, notify, fy_overlap, ui, db_artefacts, restore_upload) and
`tests/test_property_{release_break,config_fuzz}.py`. Before writing a card, grep the named function in `tests/`
and `audit_lab/`; if an existing test already asserts the same ORACLE, drop the card and note it in the run report.
All new tests follow CLAUDE.md test conventions (sandbox config, `fake_run_tool`, `smtp_server`, patch `task.fn`).
Live-stack scripts live in `agent_runs/live/` (gitignored), never in tracked files.

## A. Risk map (not yet covered), ranked

| # | Risk | Customer harm | Why still open | Cards |
|---|---|---|---|---|
| R1 | Agent cannot boot (old-schema manifest.db, invalid config, missing httpx2) and nobody is told | total silence, nights missed | boot failures tested only as exit codes; watchdog/NSSM loop + mail never proven end to end | U10 U11 LS8 LS13 |
| R2 | Same-size / same-mtime corruption on NAS or GCS not detected | silent false success | robocopy skips it by design; only the weekly MD5 audit can see it; no lab test corrupts a byte | L1 |
| R3 | Approval replayed after the world changed (bucket/NAS path/FY closed) | wrong-folder write, mirror deletes | approval tests check once-only, not re-guarding | U23 U24 |
| R4 | Wrong destination derivation for the 2nd FY folder (parent, share root, duplicate prefix) | write into wrong folder / `/MIR` over a parent | per-case tests only, no property over all inputs | U16 U17 |
| R5 | Power loss / hard kill of all services at once on the real NSSM stack | stuck lock, stuck Prefect slot, partial file counted good | w22/w24 use lab children, not NSSM services | LS2 LS10 |
| R6 | Alert storm (flapping NAS for weeks) or silence (SMTP down for days) | office ignores mails / never sees them | cooldown tested per key, not over long random sequences; mail-failure visibility untested | U6 U7 U25 LS11 |
| R7 | Timezone/DST: server tz not IST, or `schedule.timezone` with DST | missed run, false overdue, wrong FY day | `run_date` property only "within a day"; overdue not tested across DST | U3 U4 |
| R8 | rclone/GCS auth expiry, 401/403, 429 throttling in logs | false success or endless retries | exit-code tables tested, log content of auth/throttle not | U14 LS4 |
| R9 | Unicode normalization, case-only renames, trailing space, >260 chars on the cloud leg | churn every night, missing files, verify lies | w5a covers LAN paths only | L2 L3 |
| R10 | File modified / exclusively locked during cloud upload | partial object counted good | w5c locked file is LAN only | L4 L5 |
| R11 | NAS volume full mid-copy (pre-flight passed) | PARTIAL reported as COMPLETE, NAS shut down | LA9 tested only before the copy | LS5 |
| R12 | Secret leakage (SMTP password, GCS private key, svc password) into logs, bundle, mails, DB | credential theft (repo is PUBLIC) | no byte-scan test exists | U12 LS12 |
| R13 | Reports say "all fine" while a leg/FY never ran | false assurance | report tests use complete fixtures | U19 U20 |
| R14 | Lock/abandon edge: PID reuse, garbage lock file, lock is a directory | backup never runs again | dead-PID takeover tested, malformed not | U1 U2 |
| R15 | Watchdog loop dies on its own exception (DB locked, notify raises) | no restarts, no stale mails | branches tested, loop resilience not | U8 LS9 |
| R16 | Source swapped (different, smaller drive at same path) with real robocopy | NAS mirror emptied | guards unit-tested; real `/MIR` never proven untouched | L6 |
| R17 | ACL-denied subfolder without backup privilege | PARTIAL hidden | not in lab | L7 |
| R18 | First real rclone 1.76 + real GCS + DPAPI key path | unknown | lab uses local remotes | LS1 |

## B. Scenario cards

Format: **ID [layer] [model] -> file** / Setup / Action / Expect / Oracle / Cleanup.
"Oracle" is computed by the test from first principles (hashes, hand tables, os listings), never by calling the
function under test a second time. Files (new): `tests/test_break_release_ops.py` (locks, watchdog, boot),
`tests/test_break_release_time.py`, `tests/test_property_release_alerts.py`, `tests/test_break_release_secrets.py`,
`tests/test_break_release_reports.py`, `tests/test_property_release_paths.py`, `tests/test_break_release_cloudlog.py`,
`tests/test_break_release_approvals.py`, `audit_lab/test_w26_release_data.py`.

**U1 [unit] [HAIKU] -> test_break_release_ops.py** - malformed `backup.lock`.
Setup: runtime dir in tmp; write `backup.lock` as each of: `b""`, `b"abc"`, `b"999999:0"`, `b"\xff\xfe"`,
`f"{os.getpid()}:1.0"` (live PID, wrong create_time), and a DIRECTORY named `backup.lock`.
Action: enter `flow._backup_slot()` (patch the Prefect concurrency call to a no-op context manager) for each case.
Expect: cases 1-5 take the lock over (file now holds our `pid:create_time`); directory case raises/records a
failure, never silently runs without a lock. Oracle: read the file bytes after entry; `psutil.Process().create_time()`.
Cleanup: tmp dir.

**U2 [unit] [HAIKU] -> test_break_release_ops.py** - abandoned sweep with PID reuse.
Setup: ManifestDB in tmp; insert a `LAN_RUNNING` row with owner `f"{os.getpid()}:{create_time-100}"` and a second
with the exact live owner. Action: `backup_repository.sweep_abandoned_runs(...)` twice. Expect: row 1 ->
`LAN_ABANDONED`, row 2 unchanged, exactly 1 notification row after both calls. Oracle: `SELECT status` per id,
`SELECT COUNT(*) FROM notifications`. Cleanup: tmp.

**U3 [unit] [NEEDS-SONNET] -> test_break_release_time.py** - overdue across DST.
Setup: `schedule.timezone="Europe/London"`, `lan_cron="30 1 * * *"`; last LAN success started 2027-03-27 01:30 local
(spring forward 2027-03-28 01:00->02:00, 01:30 does not exist) and, second case, 2027-10-30 01:30 (01:30 occurs twice
on 2027-10-31). Action: `core.overdue` evaluation at hourly instants for 72 h (fake clock). Expect: never overdue
before (next real run + budget + `dashboard.stale_grace_hours`); overdue exactly once after it; no day skipped. Oracle:
hand-written expected next-run instants in UTC in the test. Cleanup: none. Release-blocking only for IST (no DST);
other zones = finding to the owner.

**U4 [property] [NEEDS-SONNET] -> test_break_release_time.py** - FY day is IST whatever the server tz.
Setup: hypothesis instants 2026-01-01..2030-12-31 UTC, biased to 18:00-19:00Z on Mar 31 / Dec 31. Patch the
module-level "now"/local-tz source used by `fy_overlap.run_date` and `fy_rollover` so local tz = UTC and = US/Pacific.
Expect: FY name and overlap window equal those computed from `instant.astimezone(ZoneInfo("Asia/Kolkata")).date()`
with the April-1 rule written in the test. Oracle: 6-line pure function in the test.

**U6 [property] [NEEDS-SONNET] -> test_property_release_alerts.py** - storm/silence bound.
Setup: real ManifestDB + `smtp_server()`; fake clock; strategy = 60 nights x {cloud,lan} x {FY26-27,FY27-28}, each
night one of COMPLETE/FAILED/PARTIAL/BLOCKED/MISSED. Action: feed each night's outcome through the same notify entry
the pipelines use. Expect per (leg, FY): mails <= streaks + ceil(streak_hours / cooldown) + recoveries; every failure
streak >= 1 mail (no silence); each streak ends with <= 1 RECOVERED. Oracle: counters computed in the test from the
generated sequence; mails counted from the SMTP sink, not from `notifications`.

**U7 [unit] [NEEDS-SONNET] -> test_property_release_alerts.py** - mail path down for 3 days is visible.
Setup: SMTP refuses (no server on port); 3 FAILED LAN nights. Action: GET `/api/status` (TestClient, signed in).
Expect: an attention/banner item says mail delivery failed. If the product has no such item: report finding
"SILENCE-1" (release decision for owner), do not write a passing test. Oracle: notifications rows all FAILED + body text.

**U8 [unit] [HAIKU] -> test_break_release_ops.py** - watchdog loop survives its own errors.
Setup: patch `watchdog` health check to unhealthy, `core.notify` send to raise `sqlite3.OperationalError("database is
locked")` on the first call only, service state STOPPED. Action: run 3 loop iterations (patch the sleep to count).
Expect: no exception escapes; iteration 2 and 3 still call `services.start_service`; an ERROR log line per failure.
Oracle: MagicMock call counts. Mutation: remove the try/except around the iteration body -> must go red.

**U10 [unit] [HAIKU] -> test_break_release_ops.py** - old-schema manifest.db at boot.
Setup: `sqlite3.connect(tmp/"manifest.db")`, `CREATE TABLE run_history(id INTEGER)`, `PRAGMA user_version=5`; sha256.
Action: `ManifestDB.from_config(cfg)`; then `launch.main()` with Prefect calls mocked. Expect: clear error naming the
schema version; launch exits non-zero BEFORE `prefect serve`; file sha256 unchanged; no `-wal` created beside it.
Oracle: hashlib on the file.

**U11 [unit] [NEEDS-SONNET] -> test_break_release_ops.py** - invalid config at boot: who alerts?
Setup: config with unknown key `lan.foo: 1`. Action: `watchdog.main` one iteration and `launch.main`. Expect (design):
watchdog keeps running on its last good / minimal config and sends ONE ACTION_REQUIRED mail. If it exits too: finding
"SILENCE-2" (release-blocking candidate: config typo = silent stop of everything). Oracle: SMTP sink.

**U12 [unit] [HAIKU] -> test_break_release_secrets.py** - secret byte-scan.
Setup: markers `SMTPPW-7f3a91`, GCS key JSON with `"private_key": "-----BEGIN PRIVATE KEY-----\nKEYMARK-55c2\n..."`,
`"private_key_id": "PKID-0b8e"`; sandbox config pointing at them. Action: one faked cloud run with exit 1 (fake_run_tool),
one FAILURE mail via `smtp_server()`, `core.support_bundle` build, `ui` `/api/settings` GET. Expect: none of the 3
markers appear in any file under the runtime dir (walk + read bytes, also inside the bundle zip), in any captured mail,
in any DB text column, in the HTTP response. Oracle: `bytes.find`. Cleanup: tmp.

**U14 [unit] [HAIKU] -> test_break_release_cloudlog.py** - auth / throttle log content.
Setup: rclone JSON log files written by the test (use `fake_tools` format): (a) 3 lines
`{"level":"error","msg":"... googleapi: Error 429: rateLimitExceeded"}` before `Attempt 1/3 failed`, final attempt
clean, final stats errors=0, exit 0; (b) same but final stats errors=2; (c) `Error 401: Invalid Credentials`,
stats `fatalError:true`, exit 1; (d) `403 ... does not have storage.objects.create`, exit 7.
Action: `scan_rclone_log_for_errors` + `classify_rclone_exit` + `rclone_retryable`. Expect: (a) CLOUD_COMPLETE,
(b) CLOUD_SUSPECT, (c) CLOUD_FAILED not retried, (d) CLOUD_FAILED not retried, cause text names credentials /
permission. Oracle: hand table in the test.

**U16 [property] [NEEDS-SONNET] -> test_property_release_paths.py** - folder derivation never escapes.
Setup: hypothesis over source parent (`E:\`, `E:\Data`, `D:\a b\c`), NAS parent (`\\nas\share`, `\\nas\share\x`),
configured FY FY20-21..FY98-99, run_date in/out of the overlap window, overlap on/off. Action: `fy_overlap.active_folders`.
Expect: every folder's source leaf == its FY == NAS leaf == cloud prefix; source/NAS parent == configured parent; no
two folders share a source, NAS path or prefix; NAS path never equals the share root. Oracle: `ntpath.split`.

**U17 [unit] [HAIKU] -> test_property_release_paths.py** - tool argv invariants.
Setup: two active folders FY26-27 / FY27-28. Action: build the robocopy and rclone sync command lists (the builders in
`lan_sync` / `cloud_sync`). Expect: robocopy dest arg ends with `\FY27-28` for folder 2; contains `/MIR`; contains
`/XF` with `.AAM_TARGET_MOUNTED` and `.aam_write_probe_*`; never `/MOV` `/MOVE` `/PURGE`; rclone dest ends with
`:<bucket>/FY27-28`; `--max-delete` present. Oracle: list membership.

**U19 [unit] [NEEDS-SONNET] -> test_break_release_reports.py** - report during overlap with one FY never run.
Setup: run_date 2027-01-20; DB has 7 COMPLETE nights for FY26-27 on both legs and zero rows for FY27-28 (activated
2027-01-01). Action: weekly report compose. Expect: FY27-28 shown as NOT backed up / action needed on both legs;
overall subject not "OK". Oracle: substring checks on the composed text written in the test.

**U20 [unit] [HAIKU] -> test_break_release_reports.py** - ABANDONED / MISSED / SLOT_TIMEOUT counted as failures.
Setup: month with 20 COMPLETE, 3 LAN_ABANDONED, 2 LAN_MISSED, 1 LAN_SLOT_TIMEOUT rows. Action: monthly report.
Expect: success count 20, problem count 6, each status label listed. Oracle: the numbers above.

**U23 [unit] [NEEDS-SONNET] -> test_break_release_approvals.py** - approval does not bypass changed guards.
Setup: CLOUD_BLOCKED + pending approval for FY26-27 with bucket `aam-a`; then config bucket -> `aam-b` (or NAS path
changed for a LAN approval). Action: approve via `/api/approvals/<id>/approve`. Expect: the re-run re-measures G4 and
blocks again (new approval), zero transfer calls. Oracle: `fake_run_tool` call log has no `sync`/robocopy non-/L call.

**U24 [unit] [HAIKU] -> test_break_release_approvals.py** - approval for a closed FY.
Setup: pending LAN approval for FY25-26; config now FY26-27, overlap off. Action: approve. Expect: 409 (or 4xx) with
text, approval not marked used, no flow run created (deployment run mock not called). Oracle: mock call count 0.

**U25 [unit] [HAIKU] -> test_property_release_alerts.py** - stale mails while agent dead 72 h.
Setup: last LAN success 3 days ago; fake clock stepped by `watchdog.stale_check_interval_seconds` over 72 h; SMTP sink.
Action: call the watchdog stale-check function each step. Expect: mails for `stale:FY26-27:lan` <=
ceil(72 / `alerts.stale_repeat_hours`) and >= 1. Oracle: SMTP sink count.

**L1 [lab] [NEEDS-SONNET] -> audit_lab/test_w26_release_data.py** - same-size corruption.
Setup: w6 rig (valid FY leaf), 50 files; LAN + cloud (local remote) COMPLETE. Then on the NAS copy and in the local
remote dir of `Tax\a.xlsx`: flip byte 100, `os.utime` back to the original mtime. Action: next LAN backup, then
`integrity_audit_flow("all")`. Expect: LAN backup COMPLETE (by design), audit LAN and cloud rows = mismatch naming
`Tax/a.xlsx`; FAILURE mail. Oracle: our own MD5 of source vs destination file. Cleanup: rig teardown.

**L2 [lab] [NEEDS-SONNET] -> test_w26_release_data.py** - unicode / case / normalization churn.
Setup: source files `മലയാളം രേഖ.xlsx`, `caf\u00e9.txt` and `cafe\u0301.txt` (both), `Ledger.xlsx`, `😀.pdf`. Run both
legs. Then rename `Ledger.xlsx` -> `LEDGER.xlsx`. Action: run both legs twice. Expect: run 2 after rename brings both
destinations to the new case; run 3 transfers 0 (rclone stats transfers=0 -> NO_CHANGES; robocopy Copied=0); audit
clean. Oracle: `os.listdir` of source == destination (exact strings) and the stats from the log files.

**L3 [lab] [NEEDS-SONNET] -> test_w26_release_data.py** - long paths on the cloud leg.
Setup: source path of 300 and 400 chars (create with `\\?\`). Action: cloud backup + audit. Expect: COMPLETE, object
present in the local remote, audit clean; if rclone refuses, status not COMPLETE. Oracle: walk of the remote dir.

**L4 [lab] [NEEDS-SONNET] -> test_w26_release_data.py** - file changing during upload.
Setup: 200 MB file; a thread appends 1 MB every 200 ms while the cloud sync runs (start it on the first `run_tool`
call). Action: cloud backup, stop thread, second backup. Expect: run 1 CLOUD_SUSPECT/PARTIAL (never COMPLETE); run 2
COMPLETE; remote MD5 == source MD5. Oracle: hashlib.

**L5 [lab] [HAIKU] -> test_w26_release_data.py** - exclusively locked file, cloud leg.
Setup: open `Clients\lock.xlsx` with `win32file.CreateFile(path, GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)`
(share mode 0). Action: cloud backup; close handle; cloud backup. Expect: run 1 not COMPLETE (PARTIAL/SUSPECT) and the
file named in the log; run 2 COMPLETE, object present. Oracle: remote dir listing. Cleanup: close handle in `finally`.

**L6 [lab] [HAIKU] -> test_w26_release_data.py** - source swapped for a smaller tree, real robocopy.
Setup: w6 rig, 100 files, LAN COMPLETE; record sha256 of every NAS file. Rename source `FY26-27` -> `FY26-27.real`,
create a new `FY26-27` with 5 files. Action: LAN backup + cloud backup. Expect: both `*_BLOCKED` (G2/G3), one approval
each, NAS file set and hashes identical to before, remote dir unchanged. Oracle: the recorded hashes.

**L7 [lab] [NEEDS-SONNET] -> test_w26_release_data.py** - ACL-denied subfolder, no backup mode.
Setup: `lan.use_backup_mode=false`; `icacls <src>\Audit /deny %USERNAME%:(OI)(CI)R`. Action: both legs. Expect: LAN
PARTIAL/FAILED, cloud not COMPLETE, files outside `Audit` copied. Oracle: destination listing. Cleanup (finally):
`icacls <src>\Audit /remove:d %USERNAME%`.

**Live-stack cards LS1-LS13** are the runbook steps in section C (layer live-stack, all NEEDS-SONNET; Haiku may only
run a step that a Sonnet reviewer wrote out and then collect evidence).

## C. Live-stack runbook (C:\AAMTest)

Variables: `$CODE` = install code dir of the test stack, `$PY="$CODE\.venv\Scripts\python.exe"`,
`$RT="C:\AAMTest\runtime"`, `$DB="$RT\manifest.db"`, `$MAILS="C:\AAMTest\mails.jsonl"`,
`$env:PREFECT_API_URL="http://127.0.0.1:4200/api"`, `$env:AAM_CONFIG="C:\AAMTest\config\config.yaml"`.
[ELEV] = submit through the elevated shell queue. Every step writes `agent_runs/live/LSn_<stamp>.txt`.
Rules: never touch the system clock; never a real GCS prefix other than the test bucket's; no step against the trial
box. Before each step: no flow run RUNNING (`prefect flow-run ls --state RUNNING`), `backup.lock` absent.

Helpers (write once as `agent_runs/live/h.ps1`):
- `Q-Runs`: `& $PY -c "import sqlite3;c=sqlite3.connect(r'$DB');print(c.execute('select * from run_history order by rowid desc limit 6').fetchall())"`
- `Q-Mails`: `Get-Content $MAILS -Tail 10` (count lines before/after each step: `(Get-Content $MAILS).Count`).
- `Run-Leg <mode>`: `prefect deployment ls` once to learn the backup deployment name, then
  `prefect deployment run '<flow>/<deployment>' -p mode=<mode>`; wait with `prefect flow-run ls --limit 1`.
- `Agent-Py`: `$s=(Get-CimInstance Win32_Service -Filter "Name='AamBackupAgent'").ProcessId; Get-CimInstance Win32_Process -Filter "ParentProcessId=$s"`.
- `Tree-Hash <dir>`: `Get-ChildItem -LiteralPath <dir> -Recurse -File | Get-FileHash -Algorithm SHA256 | Select Hash,@{n='Rel';e={$_.Path.Substring(<dir>.Length)}}` (exclude `.AAM_TARGET_MOUNTED`).
- Snapshot/restore stack state [ELEV]: stop 3 services (Watchdog, Agent, Server), `Copy-Item $DB*` and the Prefect
  `prefect.db*` to `C:\AAMTest\snap\<n>\`, start Server -> wait `/api/health` -> Agent -> Watchdog.

| Step | Fault (exact) | Pass criteria | Restore |
|---|---|---|---|
| LS1 baseline | none. Put 200 files incl. one 1 GB file (`fsutil file createnew` is sparse zeros - use a Python random writer) in `C:\AAMTest\source\FY26-27`. `Run-Leg all`, then the audit deployment. | run_history: CLOUD_COMPLETE + LAN_COMPLETE for FY26-27; `Tree-Hash` source == NAS; `rclone md5sum` of the test bucket prefix == source MD5s; audit rows verified; mails.jsonl has no FAILURE | snapshot 0 |
| LS2 kill agent mid-LAN | `Run-Leg lan`; when `Get-Process robocopy` exists: [ELEV] `taskkill /PID <Agent-Py python pid> /T /F` | within `watchdog.check_interval_seconds * failure_threshold + 120 s` the service is RUNNING again (NSSM or watchdog); old row -> LAN_ABANDONED; exactly 1 new FAILURE mail; `backup.lock` gone or taken over; Prefect slot `aam-backup` active slots 0 within 300 s (`prefect gcl inspect aam-backup`); next `Run-Leg lan` -> LAN_COMPLETE and `Tree-Hash` equal | none needed after the pass run |
| LS2b agent dies, robocopy survives | as LS2 but kill only the python PID (no `/T`) | next run must not start a 2nd robocopy while the orphan runs (lock / transfer-process check); watchdog defers restart per 5.5 | `Stop-Process -Name robocopy -Force` if still running after 15 min |
| LS3 NAS share stops mid-run | `Run-Leg lan`; during copy [ELEV] `Remove-SmbShare -Name AAMTestNAS -Force` | LAN_FAILED / PARTIAL / SUSPECT (never COMPLETE); no "NAS shut down" claim in the row; 1 FAILURE mail naming the NAS | [ELEV] `New-SmbShare -Name AAMTestNAS -Path C:\AAMTest\nas -FullAccess <svc account>`; re-check canary; `Run-Leg lan` -> COMPLETE |
| LS4 GCS key revoked | owner-approved: `gcloud iam service-accounts keys disable <KEY_ID> --iam-account <sa>` (needs a gcloud admin login, not the backup key). Wait 2 min. `Run-Leg cloud` | CLOUD_FAILED at pre-flight (CL9), cause names credentials/auth; zero objects changed in the bucket (`rclone lsjson -R` before/after equal); 1 FAILURE mail; no retry loop (sync_attempts <= 1) | `gcloud iam service-accounts keys enable <KEY_ID> ...`; `Run-Leg cloud` -> COMPLETE. Fallback if no admin: remove the SA's bucket role, then restore it |
| LS5 NAS volume full mid-copy | [ELEV] `New-VHD`/`diskpart`: 400 MB VHDX formatted NTFS mounted at `C:\AAMTest\nasvol`; share it as `AAMTestNASsmall`, create `FY26-27` + canary; point a COPY of the config's `lan_destination` at it (edit the copy, restart agent); source 300 MB; `Run-Leg lan`; during copy write a 250 MB filler into the VHD root | LAN_PARTIAL or LAN_FAILED (robocopy error 112 in log), never COMPLETE; no shutdown; mail; source untouched (hash) | delete filler; restore original config file; restart agent; dismount + delete VHD [ELEV] |
| LS6 patched run date | snapshot 1. Stop AamBackupAgent. Run in-process: `agent_runs/live/run_with_date.py` = sets `AAM_CONFIG`, `unittest.mock.patch("core.fy_overlap.run_date", return_value=date(2027,1,2))`, calls `flow.backup(config_path, mode="all")`. TLS dates stay real. | `C:\AAMTest\source\FY27-28` and `\\127.0.0.1\AAMTestNAS\FY27-28` (+ canary) created; activation row once; run_history has COMPLETE rows for BOTH FYs on both legs; bucket has prefix `FY27-28/`; second invocation creates nothing new | delete FY27-28 folders (source, NAS), `rclone purge <bucket>/FY27-28` (versioned bucket: objects become noncurrent - accepted), restore snapshot 1, start agent |
| LS7 Prefect server stops | (a) idle: [ELEV] `Stop-Service AamPrefectServer`; (b) mid `Run-Leg lan`: `taskkill /F` the server python | (a) watchdog restarts it within check_interval*threshold; (b) watchdog DEFERS while robocopy/lock alive (log line), run still records a terminal row; server back after the run; no duplicate run | `Start-Service AamPrefectServer`, wait health, `Start-Service AamBackupAgent` |
| LS8 manifest.db renamed | (a) services running: `Rename-Item $DB manifest.db.lost` (with -wal/-shm) then `Run-Leg lan`; (b) services stopped, rename, start | (a) run either fails loud (row + mail) or reopens a fresh DB and is BLOCKED for first-night approval (I2) - never silently COMPLETE with no history; (b) agent boots fresh, first runs BLOCKED+approval if deletions | stop services, put the original files back, start |
| LS8b old schema | stopped; place a v5 file (from U10) as `manifest.db`; start | agent exits; watchdog/NSSM restarts limited by `max_starts_per_window`; exactly one ACTION_REQUIRED/CRITICAL mail per window in mails.jsonl. No mail at all = SILENCE-3, release-blocking | restore snapshot |
| LS9 watchdog killed | `taskkill /F` the watchdog python | NSSM restarts it (`sc query AamWatchdog` RUNNING within 60 s); a later `Stop-Service AamBackupAgent` is still undone by it | none |
| LS10 power loss | `Run-Leg all`; while rclone uploads the 1 GB file [ELEV] `taskkill /F /T` on the three service PIDs + `Stop-Process -Name rclone,robocopy -Force`; then start Server -> Agent -> Watchdog | all RUNNING rows -> ABANDONED, one mail per leg; lock taken over; slot free <= 300 s; next `Run-Leg all` COMPLETE; audit MD5 clean (no half object counted) | none |
| LS11 SMTP sink down | stop the sink process; force a failure (LS3 fault) | backup outcome recorded; notifications rows FAILED; dashboard shows mail problem (see U7); after sink restart the next alert arrives | restart sink, LS3 restore |
| LS12 secret scan | after all steps: `Select-String -Path C:\AAMTest\runtime\* ,$MAILS -Pattern '<svc password>','PRIVATE KEY','<smtp pw>' -SimpleMatch` recursive, also inside the support bundle zip | zero hits | rotate anything found |
| LS13 bad config at boot | stopped; add `lan: {foo: 1}` to config; start all | at least one ACTION_REQUIRED mail or a documented gap (SILENCE-2) | restore config |

## D. Verification protocol (Sonnet reviewers)

D1 Non-vacuity (per Haiku test): (1) run it green; (2) apply the mutation below in the working tree (never commit),
run only that test, it MUST go red with the oracle assertion (not an import/fixture error); (3) `git checkout -- <file>`
and confirm `git status` clean for production files. Record "test / mutation / red line" in the run report.

| Card | Temporary break | Must go red |
|---|---|---|
| U1 | in the lock reader, treat an unparsable lock as "held by live process" | cases 1,2,4 |
| U2 | sweep compares PID only (drop create_time) | row 1 assertion |
| U8 | remove the watchdog iteration try/except | "no exception escapes" |
| U10 | make `ManifestDB` migrate/accept user_version 5 | exit-code / hash assertion |
| U12 | add `logger.info(config.notification...password)` in the mail send path | marker found |
| U14 | make `scan_rclone_log_for_errors` ignore the final stats `errors` field | case (b) |
| U16/U17 | in `active_folders`, build the 2nd NAS path from the share root | parent assertion |
| U20 | drop ABANDONED from the problem category in `core/statuses.py` | problem count |
| U23 | approval path skips G4 when `approval_id` is set | transfer-call assertion |
| U24 | remove the active-FY check on approve | mock call count |
| U25 | set the stale repeat guard to never suppress | upper bound |
| L1/L6 | (lab) pass `--size-only` for the audit / disable G2 | audit row / hash compare |
Also reject a test if: its expected value is computed by the code under test; it only asserts "no exception"; it
mocks the decision function it claims to test; a `pytest.skip` path can be hit on this PC; it passes with the
production module import replaced by a stub (`sys.modules` swap spot check on 3 random tests).

D2 Is a reported bug real: (1) reproduce with the smallest standalone script against current HEAD `17fa758` + fixes
(not an old pyc: delete `__pycache__`); (2) check the plan/ARCHITECTURE gate table - if the behaviour is documented
as intended (e.g. LAN success rests on robocopy's summary, robocopy skips same-size/mtime) it is NOT a bug; (3) confirm
the harm path to a customer (data loss / false success / silence / wrong folder) in one sentence; (4) check it is not
a test-fixture artefact (invalid FY leaf, missing PREFECT_HOME, lab without Prefect -> 503); (5) only then file it with
repro, expected (cite the gate ID), actual.

D3 Release gate.
Must be green: full unit suite incl. the new files; audit lab (except the known `test_w16_schedule_collisions_across_year`
and the excluded w7); LS1, LS2, LS3, LS4, LS6, LS10, LS12; mutation table above for every committed Haiku test.
Release-blocking: any silent false success (a COMPLETE/VERIFIED row while the destination differs from the source),
any write outside the active FY folders or bucket prefixes, any deletion beyond G3 limits without approval, any
leaked secret, a stuck lock/slot that a restart does not clear, a boot failure with zero mail (SILENCE-2/3), an alert
storm > 1 mail per key per cooldown.
Accepted / owner decision (not blocking): DST behaviour for non-IST zones (U3), mail-failure banner missing (U7 ->
owner), locked-file PARTIAL nights, ransomware vs LAN mirror, GCS ~2 versions, noncurrent test objects up to 92 days,
the open WP review nits in SESSION_HANDOFF (N1-N8, WP2 (2)-(6)), first-night approvals after a fresh DB (I2).
