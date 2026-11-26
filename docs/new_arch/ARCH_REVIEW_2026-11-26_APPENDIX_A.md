# Appendix A to ARCH_REVIEW_2026-11-26.md - file-level review (2026-11-26)

Read-only reviewers, static reading, not re-verified by the main session. The rollover/audit/launch/watchdog flow review was still running when this was written; add its results in section A7 when it reports.

## A1. core sync / verify / integrity
- HIGH core/cloud_verify.py:264-295 `verify_cloud_and_diff`: no `--exclude` for NTFS_SYSTEM_EXCLUDES (the sync has them in the uncommitted edit). With a drive-root source (`D:\`) recycle-bin entries show as missing, so verify never reports verified. Applies only if the source is a drive root.
- HIGH core/integrity.py:495-514 `audit_cloud`: no excludes (`audit_lan` has LAN_EXCLUDES) -> weekly VERIFICATION_FAILED on a drive-root source.
- MED core/lan_sync.py:228-232: a stalled LAN sync is always retryable; a retry gets a fresh --max-delete budget (the cloud side's uncommitted edit already guards this).
- LOW cloud_sync.py:696-733 exit 0 with no final stats stays CLOUD_COMPLETE (LAN flags LAN_SUSPECT). cloud_sync.py:338 literal `track_renames=True` default (should be a config key). rclone_config.py:60-78 env var restore not thread-safe, temp file leak. lan_sync.py:113 `_LAN_PARTIAL_EXITS` dead.
- Clean: cloud_preflight, lan_preflight, lan_versions, hashing, rclone_errors.

## A2. core guards / safety / process
- MED approvals.py:29-34 a G5 approval is matched on destination only (NAS-09 empty vs NAS-10 marker-missing not distinguished).
- MED process.py:117-121 any PermissionError on the lock file = "alive": a lock written by an old account blocks every night silently.
- MED process.py:197-221 check-then-act race when replacing a stale lock (two processes can both own it).
- MED health.py:73-116 rclone version fault is cached for the process lifetime, including a transient timeout.
- LOW-MED wol.py:145-153 `already_up` rests on one probe -> status "woken" while staff use the NAS -> it is shut down. shutdown.py:63-95 target check misses the server's own LAN IP / a hostname resolving locally.
- LOW process.py:33-35 AccessDenied = "gone"; health.py:162-172 only OSError caught; health.py:67-68 disk-usage failure treated as safe; app_restart.py:49-61 forced exit with no re-check; approvals.py:156-163 reminder lost if the exact day is missed; shutdown.py:140-152 a failed probe = "closed"; smb_ready.py:68-77 os.stat has no timeout; literals at health.py:77, wol.py:142, budget.py:185 (checks 2027 only).
- Clean: guards.py (incl. uncommitted edits), permissions, run_artefacts, backup_repository, time_utils, netinfo, services, wol_target, tool_runner.

## A3. core manifest / notify / reports
- MED manifest.py (insert_run, record_notification, record_audit, _set_meta, ...): bare execute+commit with no rollback; after "database is locked" the transaction stays open on the long-lived connection and reads go stale (reproduced).
- MED attention.py:132-157,255 the `catching_up` exemption turns RED stale into AMBER and suppresses the overdue mail for up to 14 days if the scheduler stops after a PARTIAL.
- MED notify.py:89-115,209-212,685-702 RecipientsPartlyRefused is recorded FAILED though delivered; FAILED does not feed the cooldown -> re-sent to everyone every tick; a transient 4xx for one recipient is treated as permanent.
- LOW manifest.py:1361-1384 `latest_run_since` filters `status != 'RUNNING'` but the real statuses are CLOUD_RUNNING/LAN_RUNNING/ROLLOVER_RUNNING. Only run_history is purged; other tables grow forever; retention by age can drop the last success of a dead leg. statuses.py:244-250 ROLLOVER_COMPLETE renders as "Backup complete - 0 file(s)". report.py:539-545 a disabled leg's runs counted in totals. support_bundle.py:201-215 zips all logs in memory before the size cap, before the cooldown test. notify.py:361-378 attachment-stripped resend after a later connection error.
- Clean: rollover_journal, alert_class, overdue, messages, report_period, cloud_reporter, email_compose, audit_pending, night_strip, logging, faults.

## A4. core rollover / config / secrets
- MED config_edit.py:483-504 settings save vs rollover config_switch race (the save validates against the old FY and overwrites the new path).
- MED config_edit.py:48-65 `_RISKY_KEYS` lacks lan.shutdown_only_if_woken, cloud.verify_recheck_enabled, cloud.track_renames, lan.track_renames.
- MED config_file.py:79-105 write_atomic has no flush/fsync before os.replace; a power cut can leave an empty config.yaml (a .bak exists, nothing restores it).
- LOW config_file.py:184 non-atomic restore; create_source_folder `mkdir(parents=True)` can invent the parent tree and a placeholder satisfies G1 (fy_overlap.py:293, fy_rollover.py:325,368); FY_PATTERN accepts FY26-99; archive gs:// names with `[*?` may be globbed by gcloud (unverified); admin_auth.py:35-39 compare_digest TypeError on a non-ASCII hash, unbounded iterations; secrets.py constant DPAPI entropy + LOCAL_MACHINE scope, UnicodeDecodeError is not a SecretsError; gcs_key.py:44 non-atomic write; config_edit.py:445 the smtp password cannot be cleared.
- Clean: Apr 1 / IST logic, final_copy_since, password fingerprint logic, source_picker, password_setup, secret_setup, email_samples.

## A5. flow.py nightly pipelines
- No path found that ends in success while a leg failed/blocked/partial; the run row is always written in `finally`; slot/lock released on every exit.
- MED flow.py:2492-2514 the failure-path NAS shutdown ignores shutdown_only_if_woken / nas_already_up (fires on BLOCKED, health failure, WoL failure).
- MED flow.py:3707 + 2172/3696-3700 overlap LAN: only the last pass may shut down; if that pass never reaches its body no pass shuts down (NAS stays on).
- MED flow.py:1795/2232 copy-only branch: the access check inside `except GuardBlocked` can raise and replace the block -> LAN_FAILED, no approval request or mail, repeats nightly.
- LOW flow.py:1800 cloud copy-only reads `gb.record["planned_deletes"]`, which `enforce()` never writes (always "0 planned deletions held"); the copy-only note ignores the copy's own PARTIAL/budget status; flow.py:2482/2038 error_message overwritten by generic text; faults.py:384-389 blocked_guard_code ignores approved guards.
- Illogical: rollover NAS folder created before G4/G5; LAN G1 approval consumed then the run fails; metrics["moves"] uses the first plan; ARCHITECTURE.md:621 lists the removed cloud G2; cloud vs LAN disagree on max_delete_reached; cloud exit 5 never retried though listed retryable; `_record_run` ManifestDB.from_config raise escapes the retry loop.
- Order as implemented, cloud: approval filter -> missed-budget check -> RUNNING row -> slot+lock -> health -> preflight -> guards G1/G4/G3 (dry-run) -> [G3-only: copy-only then BLOCKED] -> access checks -> sync loop (guards re-run per retry) -> verify (+1 recheck) -> entries/metrics -> finally: run row + alerts.
- LAN: same, with WoL after health, SMB probe, guards G1/G2/G4/G3/G5, arrival check + hash check; post: purge versions, then shutdown if allowed and woken.

## A6. ui.py / models/config.py / dashboard.js
- HIGH (probable; confirm on live Prefect) ui.py:229-246,298-341,1365-1393 the RUNNING/PENDING/SCHEDULED query is capped at 200 runs with ~330 future cron runs; the one RUNNING run can be cut off -> dashboard shows idle, a manual trigger passes the duplicate guard, a second backup is queued. Disappears if Prefect is removed.
- MED ui.py:886-948 check-then-create race (double trigger / double approval re-run); ui.py:1551-1574 rollover and leg triggers do not see each other; ui.py:1296-1346 no CSRF/Origin check (SameSite=lax only).
- LOW-MED models/config.py:788-846 guards can be switched off by bounds (percent=100, files=1e8, no floor<max validator); the schedule check omits rollover_cron; no load-time wol.server_ip vs lan_destination check; wol/cloud default factories fail validation; /docs, /openapi.json, /health unauthenticated; login rate limit counts successes and is keyed on IP; a non-dict JSON body -> 500.
- LOW ui.py:810-813 `_leg_timing` swallows errors -> "on_time"; report endpoints outside try; dashboard.js:34-36 misleading toast on 401.
- Correct: auth on every route except /login, /health, /static; approvals atomic/expiring/single-use; status labels from core.statuses; Prefect unreachable -> "unknown".

## A7. rollover / audit / serve / launch / watchdog flow review
Reviewer ran 35 rollover/report-period tests (pass); none exercise the reminder code.
- MED flow.py:2792 `db.list_approvals(...)` does not exist on ManifestDB (the real method is `open_approvals()`); a `hasattr` guard silently returns `[]`, so approval reminders (day 1/2/4/7/weekly) are NEVER sent.
- MED (hidden behind the row above) flow.py:2807-2814 + manifest.py:985: reminder text is always "mass-delete approval", status CLOUD_BLOCKED, AAM-DEL-01 for every non-lan leg incl. rollover; a PENDING request only ends when replaced/approved/dismissed; rollover request has fy=None so `supersede_approvals_of_fy` misses it -> weekly wrong reminders for up to a year after a self-cleared G6 block.
- LOW-MED core/approvals.py:170-175 `waiting_summary` reads req["results"]/["metrics"] but rows store `guards` (producer/consumer key mismatch; the test builds a wrong-shape row): dashboard shows "0 deletions waiting" for a 5,000-delete block.
- MED ui.py:1371-1376 "Apply" busy check: 200-run cap with random uuid4 ordering vs ~300 scheduled runs; Apply can restart the agent while a run is in flight -> run killed, ABANDONED. (Same root cause as A6 HIGH; disappears with Prefect.)
- MED models/config.py:1342-1357 `schedule_problems` omits the 06:00 rollover check: a cloud run with a 6 h budget still running at 06:00 makes the April 1 final cloud backup wait 3600 s -> CLOUD_SLOT_TIMEOUT -> G6 block + ROL-02 mail; a Sunday 10:00 audit can be starved (AUDIT_INCOMPLETE).
- LOW flow.py:3121-3125 `step_nas_shutdown` ignores lan.shutdown_only_if_woken (LAN pipeline and audit respect it).
- LOW flow.py:3231 ROLLOVER_COMPLETE success row is written every morning while archive/nas_folder keep failing (10 days of "FY rolled over").
- LOW flow.py:2909-2919 "superseded" branch also fires when config.yaml is hand-restored to the old year; later restore -> journal complete with archive skipped.
- LOW flow.py:3655-3667 race guard checks FY before the slot wait and not at all without an overlap: a baseline run waiting for the slot during the switch backs up the old year with stale config; its untagged success row counts as the new FY's last success for the overdue check.
- LOW watchdog.py:148-162 `_reload_tunables` does not refresh BACKUP_LOCK_PATH: after `paths.database_path` changes the watchdog reads the old lock and could restart the stack during a backup.
- Rollover order as implemented: log retention -> load config/open DB -> activate_if_due -> unprotected-files alarm -> approval reminders (dead) -> rollover_incomplete (crash-after-switch close / hand-switch block) -> supersede old journal -> classify year + reentered/clock-behind blocks -> clock proof -> rollover_open + ROLLOVER_RUNNING row -> D-17 reset + load final-copy state -> early source_folder -> final_backups (each takes the slot) + G6 + approval -> one slot hold: source_folder, nas_folder, config_switch (CAS + end_overlap), archive, nas_shutdown, announce -> end_overlap again, fail if a blocking step failed else ROLLOVER_COMPLETE.
- Startup order (launch.py): wait for API (300 s) -> dashboard thread -> upsert `aam-backup` limit -> cancel orphan runs (respects a live lock) -> sweep abandoned rows -> reconcile deployments (pause disabled legs, drop cron of manual-only) -> deployments() + serve(). Minor: dashboard is live before cleanup; schedule-in-effect recorded before serve() succeeds (harmless).
- Correct: serve.py (flow, mode, timezone, defaults match ARCHITECTURE s2); journal steps idempotent; slot+lock holder consistent; audit truthful (INCOMPLETE never reads OK); report periods half-open; watchdog never restarts under a live lock/running rclone and never deletes a live lock; orphan cancel and abandoned-run sweep respect liveness.
