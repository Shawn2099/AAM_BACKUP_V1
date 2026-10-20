# Plan: audit fixes and deletion limits in Settings (2026-10-20, rev 3)

Status: APPROVED by the owner 2026-10-20 (rev 3 = rev 2 minus the first-upload button, minus the hung-tool/Stop work).
(File name kept for history: the "first full backup" idea was DROPPED, see section "Dropped".)
Origin: final code audit (4 read-only reviewers + advisor), a second pass by 3 independent verifiers who re-read the
code and reproduced the claims, and an Opus review of the plan. This file contains only what was confirmed.
The feature freeze of 2026-10-19 is lifted for exactly the items below.

## 0. Decisions already taken (owner) - do not re-ask

- LAN /MIR has no in-run delete cap; the preflight G3 check is enough. Accepted.
- Dangerous approvals = dashboard login + typed phrase. The Advanced password is for tech support.
- Basic settings (smtp_host, lan_destination, recipients, public_url ...) stay editable with the dashboard login.
  No SMTP re-entry, no URL check, no change-notice email.
- One pair of deletion limits for both legs.
- NO first-full-backup button, NO hung-tool detector, NO Stop button, NO `RunLimits`, NO `*_DEFERRED` status: the
  existing run budget already uploads in chunks (see "Dropped").
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (CLAUDE.md wins).
- Rule for every item: root-cause fix in the one shared place, no per-call-site special cases, test first (red on the old
  code), GitNexus impact before the edit and detect_changes before the commit, one commit per item.

## Part A - bug fixes (all VERIFIED by reproduction or end-to-end reading)

| ID | Verified fact | Fix (root cause, one place) | Red test |
|----|---------------|-----------------------------|----------|
| A6 | `run_tool` (core/tool_runner.py:184-194) has no `finally`/`except BaseException`: an interrupted or cancelled flow leaves rclone/robocopy running and skips the manifest record. Reproduced (child alive after KeyboardInterrupt). Also: `proc.wait(timeout)` is ONE blocking Windows wait, so Prefect's cancel (a flag set in the main thread via `interrupt_main`) is not seen until the tool exits by itself. The real Windows cancel path could not be proven by reading (does CTRL_BREAK reach the flow process under NSSM?). | `run_tool` waits in short polling steps (new config key with bounds) so an interrupt is seen, and kills the whole process tree + writes the manifest record on ANY BaseException, then re-raises. A Windows Job Object (kill on close) is NOT built now; build it only if the lab proves orphaned children after a hard kill. | fake child + KeyboardInterrupt: child gone, manifest record written, exception re-raised; polling: an interrupt raised while the child still runs is seen within the poll interval. Lab test with real tools (see Order). |
| A2 | `except Exception` only (flow.py:1727 cloud, :2023 LAN); `finally` records the stale `status`. Reproduced with KeyboardInterrupt: pre -> CLOUD_SKIPPED, **sync -> CLOUD_SKIPPED (untruthful, data may be half copied)**, verify -> CLOUD_COMPLETE + recovery mail, post -> CLOUD_COMPLETE + recovery mail. LAN: same structure, not run. Prefect 3.7.2 delivers `TerminationSignal` (BaseException) once Python code runs again. | Add `EXC_INTERRUPTED` to `core/statuses.decide_terminal_status` (-> `*_FAILED`, clear "interrupted" error and fault code). ONE handler body for `except BaseException` that always re-raises and shares the alert/metrics code with the Exception path (no duplicate block). Same change in both pipelines. | KeyboardInterrupt and TerminationSignal in pre/sync/verify/post, both legs: recorded status non-success, no recovery mail, re-raised. |
| A1 | `latest_audit` (core/manifest.py:907) returns the newest row of any status. Week 1 VERIFICATION_FAILED, week 2 AUDIT_INCOMPLETE -> ui says AUDIT_INCOMPLETE, report row "AUDIT COULD NOT RUN", headline "all backups OK", banner amber. Reproduced. Readers: attention.py:87-103, report.py:252 and :267, ui.py:1498 `_integrity_summary`. | Define the verdict statuses (VERIFIED, VERIFICATION_FAILED, PENDING_BACKUP) once in `core/statuses.py`; new `ManifestDB.latest_audit_verdict(mode, scope)`; `latest_audit` stays "newest row". All three readers show the verdict row and the newer INCOMPLETE additionally ("the last check could not run"). | seed FAILED then INCOMPLETE: banner red, headline "action needed", hero not green. The docstring promise gets its own test. |
| A3 | "Nothing was copied or deleted" (flow.py:1051) / "Backup blocked before any change" (statuses.py:210) is false when GuardBlocked comes after a sync attempt (backstop stop, or re-measure between retries). `already_deleted` alone misses "attempt 1 copied files, re-measure then blocks". | The signal is "a sync attempt ran" (`sync_state["attempts"]` non-empty, already stored as `extended_metrics.sync_attempts`). Pass it to `_open_block_request` and to `statuses.describe` (it must receive the metrics). Wording: "Some files may already have been changed by this run." | backstop-blocked and retry-re-measure-blocked runs: text must not say nothing changed; a pre-sync block keeps the old text. |
| A4 | `smb_path_usable` treats any FileNotFoundError as "share answers". Reproduced: missing share -> winerror 67, unreachable host -> 53, missing file on an existing share -> 2. The 90 s stability re-check calls the same function. | Probe the SHARE ROOT (`\\host\share`) with os.stat/listdir: must succeed. A missing canary/FY folder (winerror 2/3 below an answering share) is a separate, allowed result. `getattr(e, "winerror", None)`. | fake os.stat raising winerror 53/67 -> not ready; 2/3 under a good share root -> ready. Update existing tests that raise a bare FileNotFoundError. |
| A5 | 03_setup_system.bat:199-205 opens 8080 and 4200 to any address, no duplicate check (re-run after 06 undoes the LocalSubnet scope). 09_restore_from_gcs.bat:48,52,63 uses `"%UV_EXE%" run` and on failure silently falls back to bucket `aam-cloudbackup`. | Delete both firewall rules from 03 (06 owns the firewall). 09: `.venv\Scripts\python.exe`, FAIL CLOSED if the config read fails (no default bucket). | text tests in the style of tests/test_h2_h3_deploy_scripts.py. |
| A7 | (1) `_backup_slot` finally swallows an unlink OSError and leaves the lock when `read_lock_alive` returns (True,-1); the leftover lock holds this process's own live PID, so the NEXT pipeline in the same process (mode=all, rollover final cloud + final LAN) fails with SLOT_TIMEOUT. Reproduced. (2) If `acquire_lock` raises OSError the run proceeds WITHOUT backup.lock; the watchdog then sees no lock and, with an unhealthy API, restarts the agent mid-transfer after 8 h of deferrals. (3) **Test blind spot:** `_backup_slot` has a `PREFECT_TEST_MODE` branch (flow.py:215-232) that tests force on; it never calls `acquire_lock` or `concurrency()`, so tests of (1)/(2) would exercise the wrong branch. | One shared acquire/release function used by BOTH branches. (1) release by ownership with a short retry; a lock this process owns is re-entrant. (2) the lock is a requirement: if it cannot be written, record a `*_SLOT_TIMEOUT`-style failure, never run lockless. | unit tests for (1) and (2) that run the production branch (test mode off for the lock part). |

Note (not a task): Prefect's `concurrency()` lease is 300 s and non-strict; a Prefect server stall longer than that frees the slot
silently, and `backup.lock` is then the only guard. That is why A7 makes the lock mandatory.

## Part B - deletion limits in Settings

Verified facts: pass rule is `planned <= max_delete_files AND 100*planned/dest <= max_delete_percent`; base = files CURRENTLY
in the destination (cloud: `rclone size` count under the FY prefix; LAN: files in the NAS FY folder, marker excluded);
planned = files only (rclone `"skipped":"delete"` lines; robocopy `*EXTRA File` lines); retries add `already_deleted` to both
numbers; a destination with 5 files at 10 % allows 0 deletions. rclone `--max-delete 0` means "delete nothing" (unlimited is -1)
and the repo passes 0 correctly. LAN has no in-run cap (accepted).

**Real defect:** G3 compares with a float percent, the cloud backstop uses `Fraction(float)` (the binary value: 0.3 is stored
as 0.29999999999999998...). Reproduced: 1 984 (percent, dest, planned) cases where G3 passes but the backstop allows one
deletion fewer (0.3 %, 1000 files, 3 planned -> backstop 2). Runtime: rclone deletes 2, stops, the run becomes a backstop
BLOCK after data was deleted; fail-safe but a spurious block. Making G3 use `Fraction(float)` would be wrong the other way.

- B1 ONE function `delete_allowance(dest_files, max_files, max_percent)` in `core/guards.py`, used by G3 (`planned <=
  allowance`, same number in the message), by the backstop and by `remaining_delete_allowance`. The percent is converted from
  the decimal the owner typed (`Fraction(str(percent))`/Decimal), never from the binary float. Test (hypothesis is in the dev
  group): for random dest/limits G3 passes exactly when `planned <= allowance`, decimal percents; red on the current code.
- B2 Add `guards.max_delete_files` and `guards.max_delete_percent` to `BASIC_KEYS` (core/config_edit.py). Plain labels:
  "Stop the backup if it would delete more than ... files" / "... or more than ... % of the files already backed up". Help:
  percent is of the files in the backup (not the source), the stricter of the two applies, 0 = never delete, example 20 000
  files at 500 / 10 % -> 500. Raising a value is "risky" (typed phrase); lowering needs none (keep how `config_edit` decides).
  Applies to the next backup, Cloud and Office copy.
- B3 Tests: catalog shows the two keys as basic; a saved change is used by G3 on the next run (settings round trip); the help
  example is computed by the real function in a test.

## Part D - small truthfulness/data fixes (owner approved: D2, D7 must; D1, D3, D4, D5 worth doing; D6, D8, D9 deferred)

| ID | Verified defect | Fix |
|----|-----------------|-----|
| D2 | `/health` and `attention` never go red for "latest run failed/abandoned/blocked": 200 "healthy" for up to 26 h after a failed night (reproduced; an existing test encodes it). | RED item when the latest run of an enabled leg is failed/abandoned/blocked (via `core.statuses` categories). A budget-stopped PARTIAL is NOT red (see E1). |
| D7 | Rollover R3: a manual config switch while `final_backups` is failed/pending marks the switch done, then backs up the NEW FY, records final_backups DONE and archives the old FY without its last changes (traced). | In the R3 branch, if `final_backups` is not DONE: ACTION_REQUIRED block (no archive); do not run it against the switched config. |
| D1 | `notify()` (core/notify.py:434-450): if DB open, `_incident`, `compose` or `support_bundle.build` raises, result FAILED with no email, no fallback and no notifications row (reproduced 4 ways). | Minimal plain-text fallback email, and a FAILED row. |
| D3 | `alerts_undelivered` counts every FAILED row for 7 days even after later SENT (false red + `/health` 503). | Only failures newer than the last SENT. |
| D4 | `find_transport_failure` runs over the whole audit log: file names like "403 Smith.pdf" / "Forbidden items" / exactly 401 or 403 differences turn a real VERIFICATION_FAILED (red) into AUDIT_INCOMPLETE/AUTH (amber). Reproduced. | Classify only non-per-file lines, or after the summary check. |
| D5 | `maintenance.db_retention_days` allows 7: when a leg's last success is purged, G2 passes as "first run" (guard silently skipped); monthly report reads 30 days. | Model bound: floor 35 days. |
| D6 (deferred) | `run_logs.dir` is editable in Advanced, not validated; the daily purge deletes old loose files and top-level `YYYY-MM-DD` dirs of whatever it points at (reproduced). | Model validator (not equal/inside/containing source or destination). |
| D8 (deferred) | deploy/06 ends "INSTALLATION COMPLETE" when services fail to start; stops services with no check for a running backup. | Read back every service, non-zero exit; refuse while `backup.lock` is held. |
| D9 (deferred) | `sendmail()` refused-recipient dict ignored. | Log and record a partial refusal. |

## E1 - DROPPED (owner 2026-10-20: "no need"): calm status during the first upload

Verified today: a budget-stopped `CLOUD_PARTIAL` is not a success, so a multi-night first upload sends a PARTIAL email every night,
the Prefect run shows failed, and after 26 h the stale banner and the watchdog "overdue" email (every 12 h) start. The upload
itself is correct and resumes by itself. E1 = a budget-stopped partial that made progress is shown as "first upload in
progress, continues tonight", the stale/overdue alert stays quiet while that holds and still fires when progress stalls
(bound with a config key). Skip it if the owner is simply told to expect the notices.

## Dropped (and why)

First full backup button, readiness checklist in the dashboard, hung-tool detector, Stop button, `limits_for`/RunLimits,
`*_DEFERRED` status, "Apply changes" block, unlimited run: the run budget already does chunked, resumable nightly uploads
(`budget.cloud_max_hours`, default 6, up to 72; `flow.py:1575-1601`), the verify runs on the final night, and the readiness
proof is `04_check_readiness.ps1` as the service account + Settings "Send test email" + the guards of the first real run.
An unlimited single run would need the cancel path proven on Windows, a no-progress alert, and gives up nightly LAN backups
for days (one global slot): not worth it.

## Accepted / not fixing

Plain-HTTP dashboard; unauthenticated loopback Prefect API; no security headers; per-IP rate limit; GET logout;
`config.yaml.bak-*` keeps the old plaintext SMTP password; redaction gaps; status-suffix matching in faults/email_compose
(clean up only if touched anyway); unknown counts shown as 0 / "null files"; no RECOVERED for stale/audit/rollover alerts;
the `last_synced_at` columns (never read); `dashboard.port` 4200 (locked key); `max_clock_skew_seconds` up to 3600;
service-account Modify on the project folder; LAN in-run delete cap; settings trust model; a masked `dpapi:` password gives
HTTP 500 on `/api/settings` (config unchanged).

## Order of work

1. A6 -> A2 -> A7 -> A1 -> A3 -> A4 -> A5. One commit each.
2. B1 -> B2/B3.
3. D2, D7, then D1, D3, D4, D5.
4. Lab: add one real-tool test that cancels a flow mid-sync through the real `prefect serve` process (proves A6/A2; if the
   cancel never reaches the tool, report it and decide about the Job Object). Then the full unit suite and the full audit lab
   ONCE at the end (do not edit imported modules while it runs).
5. Update `SESSION_HANDOFF.md`, tick the boxes.
6. Infra phase on the box: readiness as the service account, email previews, 10.4 scenarios (NAS shutdown only with the owner at
   the NAS), 7 nights, restore drill.

## Task checklist

Done 2026-10-20 (each test first, full unit suite green, one commit each):
- [x] A6 `83a283a`  - [x] A2 `5806619` (+ record-first-alert-second `d073df7`)  - [x] A7 `b51cbbd`
- [x] A1 `5941699`  - [x] A3 `452cbcb`  - [x] A4 `f08f52c`  - [x] A5 `c7b2f83`
- [x] B1 `4608408`  - [x] B2/B3 `b1c3ce6`
- [x] D2 `8c82271`  - [x] D7 `93e7dc4`  - [x] D1 `d3c04a9`  - [x] D3 `96c6433`  - [x] D4 `e18b546`  - [x] D5 `fcd92a9`
- [x] real-cancel lab test W23 `8570edb` (cloud + LAN pass: tool stopped ~1 s after the cancel, row *_FAILED / AAM-SYS-02,
      lock released, next run converges)
- [x] unit suite: 2675 passed / 19 skipped / 0 failed
- [ ] full audit lab re-run (started after the last commit; result in SESSION_HANDOFF.md)
- [x] Handoff update
- E1 dropped by the owner; D6, D8, D9 deferred
