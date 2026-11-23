# Workflow gap analysis (2026-11-23)

Read-only analysis of the current code (branch `fy-overlap`, equal to the released production-release) against
the owner's requirements W1..W47 in `docs/WORKFLOW_OWNER_INTENDED_2026-11-23.md`. Nothing was run (no pytest,
no services, no commits). Source of truth = code; tests and live results are listed as evidence of coverage only.

Paths are relative to the repo root `C:\Users\malliath\Desktop\aam_backup`. `file:line` numbers were read from
the working tree at the time of writing. Items marked UNCLEAR are guesses or owner decisions the code cannot settle.

Status legend: SATISFIED = the code does it; PARTIAL = done with a real limit; GAP = not done or done the opposite way;
UNCLEAR = cannot be decided from code or the owner's notes.

Sources read: `docs/ARCHITECTURE.md` (sections 1-10), `models/config.py`, `core/notify.py`, `core/email_compose.py`,
`core/report.py`, `core/alert_class.py`, `core/attention.py`, `core/fy_overlap.py`, `core/fy_rollover.py`,
`core/manual_leg.py`, `core/manifest.py`, `flow.py`, `ui.py`, `watchdog.py`, `serve.py`, `launch.py`, `restore.py`,
`core/restore.py`, `core/logging.py`, `core/run_artefacts.py`, `docs/PLAN_RECIPIENTS_AND_SCHEDULE_ALERTS_2026-10-22.md`,
`docs/RELEASE_BREAK_SESSION_2026-11-22_PART6.md`, `C:\AAMTest\live\LIVE_RESULTS.md`, `tests/test_recipients_callers.py`,
`tests/test_l1_undelivered_digest.py`, `tests/test_d5_db_retention_floor.py`, `test_support/sim_calendar.py`,
`tests/test_sim_e2e_*.py`, `audit_lab/` test list.

---

## 0. Summary (read this first)

1. **Partners (W35/W36) - GAP, confirmed from code.** Partners (the `report_recipients` list) receive four kinds of
   mail that are not weekly or monthly reports, on the default configuration: the "New financial year started"
   notice, the "FY now backed up as well" notice, the "password changed" notice and the dashboard test email. The
   test suite pins this behaviour. In addition, when `notifications.recipients` (the IT/alerts list) is empty,
   every alert of every kind is sent to the partner list. Only an amber dashboard item warns about that. See section 2.
2. **Retention (W40) - GAP.** The defaults keep application logs, full run logs and service logs for 90 days, and
   the application log setting cannot be set above 365 days at all. Only the run-history table is kept for two years
   (730 days), and the configured floor is 35 days. See section 3.
3. **Resolved alerts (W39) - GAP.** A normally recovered problem is mailed as "RESOLVED" by default; the owner asked
   for the audit log only.
4. **Fixed alerts after a mail outage (W38) - PARTIAL.** Nothing re-sends an alert body, but the outage digest names
   failed alerts whose problem has since been fixed, and nothing tests that case.
5. **Force FY activation (W33) - GAP.** There is no UI route to create the next year's folder before the rollover
   window; "Run FY rollover now" is offered only when the 1 April rollover is due.
6. **NAS wake (W2) - PARTIAL, and an owner decision not reflected in the requirement.** The Sunday integrity audit
   and the 1 April final LAN backup wake the NAS even in manual-only LAN mode (owner decision D-1, 2026-11-23, in
   `docs/RELEASE_BREAK_SESSION_2026-11-22_PART6.md` line 51).
7. **Mode A, Mode C and the power-timer coordination have no end-to-end test.** Mode B has one simulation test
   (`tests/test_sim_e2e_14_manual_lan_old_year.py`). Live WoL and NAS shutdown were disabled on the trial box
   (LIVE_RESULTS "Rules kept"), so no live run has woken or switched off a real NAS.

---

## 1. Legend for TESTED-BY

- Unit tests: `tests/<name>.py` (names are file names; test functions are listed where the file name is not enough).
- Lab tests: `audit_lab/<name>.py` (real robocopy / rclone rig, about 20 minutes).
- Calendar simulation: `tests/test_sim_e2e_*.py` on `test_support/sim_calendar.py` (fake tools, real flows and DB).
- Live: `C:\AAMTest\live\LIVE_RESULTS.md` (trial box, step IDs B1-B8, LS1-LS13, Task 0-4 of the 2026-11-23 block).
  Live steps that ran with WoL and NAS shutdown disabled are marked "live: none for WoL/shutdown".

---

## 2. Partner mail audit (W35, W36, W37) - the main feature

### 2.1 Routing rules as coded

- Three audiences exist: `alerts`, `reports`, `both` (`core/notify.py:53-56`).
- `resolve_recipients` (`core/notify.py:66-90`): `alerts` uses `notifications.recipients`; if that list is empty it
  falls back to `notifications.report_recipients`. `reports` uses `notifications.report_recipients`; if that list is
  empty it falls back to `notifications.recipients`. `both` sends to the union of the two lists. Only the empty-list
  fallback to the partner list matters for the partners.
- `AlertEvent.audience` defaults to `alerts` (`core/notify.py:143`). Every producer that does not state an audience
  is an alert.
- Config keys: `notifications.recipients` (models/config.py, NotificationConfig, about line 503; ALERTS list),
  `notifications.report_recipients` (about line 513; REPORTS list).
- The fallback is known and visible only as an amber item: `alerts_list_empty` (`core/attention.py:416-425`, text
  "The alerts list is empty, so problem alerts go to the reports list").

### 2.2 Every mail type with its audience

"Partner?" = can a partner address receive it on the default configuration with both lists set.

| # | Mail (key pattern) | Kind | Producer | Audience | Partner? | Owner rule |
|---|---|---|---|---|---|---|
| 1 | weekly / monthly report `report:weekly`, `report:monthly` | REPORT | `flow.py:2565` (`_send_summary_report`) -> `core/report.py:783` | reports | yes (intended) | OK |
| 2 | dashboard "send weekly / monthly report" button | REPORT | `ui.py:1775-1800`, `ui.py:1815-1840` | reports | yes (intended, manual) | OK |
| 3 | backup failure / partial / blocked / missed / abandoned: `backup:`, `missed:`, `blocked:`, `nas_shutdown:`, `rollover:abandoned` | FAILURE / WARNING / ACTION | `flow.py:347, 566, 1248, 1469`; `core/backup_repository.py:146-157` | alerts | only if alerts list is empty | NO (fallback) |
| 4 | RESOLVED after a failure: `recovered:<fy>:<leg>` | RECOVERED | `flow.py:402-425` (`_send_recovery_if_needed`) | alerts | fallback only | NO (W39: log only) |
| 5 | rollover: `rollover:<fy>:not_fully_copied`, `rollover:<fy>:final_wait`, `rollover:<to>:<step>`, `rollover:switched_by_hand`, `rollover:reentered:<to>`, `rollover:clock_*`, `rollover:<to>:archive`, `blocked:rollover`, `overlap:active_set` | WARNING / ACTION | `flow.py:2445, 2897-3215, 3531` | alerts | fallback only | NO (fallback) |
| 6 | RESOLVED for rollover: `recovered:<key>` | RECOVERED | `flow.py:2468, 2730` | alerts | fallback only | NO (W39) |
| 7 | overlap alarms: `overlap:sibling:*`, `overlap:unprotected:*` | ACTION | `flow.py:2752-2753` | alerts | fallback only | NO (fallback) |
| 8 | overlap deferred: `overlap:activation:<fy>` and its RESOLVED | ACTION / RESOLVED | `core/fy_overlap.py:417-452` | alerts | fallback only | NO (fallback) |
| 9 | audit results: `audit:<fy>:<mode>:...` | ACTION / WARNING | `flow.py:3496-3497` | alerts | fallback only | NO (fallback) |
| 10 | watchdog: `watchdog:<key>` (ACTION), `watchdog:restarted:<key>` (WARNING), `recovered:watchdog:*` (RESOLVED), `watchdog:recovered` | ACTION / WARNING / RESOLVED | `watchdog.py:356, 415, 469, 487, 527` | alerts | fallback only | NO (fallback) |
| 11 | overdue: `stale:<fy>:<leg>` | ACTION | `watchdog.py:576-577` | alerts | fallback only | NO (fallback) |
| 12 | config invalid (raw send, no notify) | ACTION | `watchdog.py:392-393` (`audience="alerts"`) | alerts | fallback only | NO (fallback) |
| 13 | undelivered-alerts digest `undelivered:digest` | WARNING | `core/notify.py:742-800` (`audience=AUDIENCE_ALERTS`) | alerts | fallback only | NO (fallback) |
| 14 | database recreated: `db:recreated` | ACTION | `core/db_identity.py:65-66` | alerts | fallback only | NO (fallback) |
| 15 | **FY now backed up as well**: `overlap:start:<fy>` (INFO) | INFO | `core/fy_overlap.py:469-482` (audience at line 481) | **reports** | **YES** | **NO - GAP** |
| 16 | **New financial year started**: `rollover:done:<to_fy>` (INFO) | INFO | `flow.py:3101-3106` (audience at line 3106) | **reports** | **YES** | **NO - GAP** |
| 17 | **password changed**: `password:<name>` (INFO) | INFO | `ui.py:1302-1312` (BOTH at line 1310) | **both** | **YES** | **NO - GAP** |
| 18 | **dashboard test email**: `test:<ts>` (INFO) | INFO | `ui.py:1608-1630` (BOTH at line 1623); `deploy/send_test_mail.py:20-37` | **both** | **YES** | **NO - GAP** |
| 19 | `report.send_failure_alert` (`core/report.py`, alerts) | FAILURE | not called in production (tests only) | alerts | - | dead code |
| 20 | preview tool `tools/preview_emails.py` | sample | `--to` address only | - | no | OK |

Rows 15-18 reach partners on every configuration where the partner list is set. Rows 3-14 reach partners only when
the alerts list is empty (the amber item is then the only warning).

### 2.3 Answers to the W36 question ("can partners receive anything other than weekly/monthly?")

**Yes.** Four kinds (rows 15-18) are routed to the partner list by design, and the test suite asserts it:
`tests/test_recipients_callers.py` contains `test_the_password_changed_notice_goes_to_both_lists`,
`test_the_test_email_goes_to_both_lists_once_each` and `test_the_fy_switch_notice_is_a_reports_audience_event`.
The plan (`docs/PLAN_RECIPIENTS_AND_SCHEDULE_ALERTS_2026-10-22.md` line 140) records these as "defaults chosen, change
on request", not as a decision of the owner. Owner rule (2026-11-23): partners get only weekly and monthly mail.
Open point 5 in the workflow file asks the owner to confirm this; the code is the only current behaviour.

### 2.4 W37 (group 1 gets alerts and reports)

- Alerts: group 1 receives them (`notifications.recipients`).
- Reports: group 1 does **not** receive the weekly and monthly reports when the partner list is non-empty. The
  reports audience uses the partner list only (`core/notify.py:83-89`, `if own: return Recipients(own)`), and the
  dashboard button does the same (`ui.py:1793`). Tested by `tests/test_recipients_callers.py::
  test_the_dashboard_weekly_button_sends_to_the_reports_list` and `test_the_scheduled_report_goes_to_the_reports_list`.

---

## 3. Retention (W40) - the two-year rule

| Data | Where | Default | Range | Purged by | Two-year rule met by default? |
|---|---|---|---|---|---|
| Run history rows (`run_history`) | models/config.py `maintenance.db_retention_days` (~667) | 730 days | 35..3650 (floor is 35, not 730) | `ManifestDB.purge_old_runs` (`core/manifest.py:1712-1731`), called only after a successful backup (`flow.py:3829`) | Borderline: 730 days is less than two calendar years when the span includes 29 February. No floor at 730. |
| Application logs `backup_*.log`, `watchdog_app.log` | `maintenance.log_retention_days` (~676), loguru (`core/logging.py:41`, `watchdog.py:175`) | 90 days | **1..365** | loguru retention | **NO** (and cannot reach 730: range stops at 365) |
| Full run logs (`run_logs\<day>\...`) | `run_logs.retention_days` (~749) | 90 days | 7..3650 | `flow.log_retention_task` (`flow.py:2624-2650`) via `core/run_artefacts.purge_older_than` (`core/run_artefacts.py:246`) | **NO** (default), possible if set |
| NSSM service logs (`agent_svc-*`, `prefect_svc-*`, `watchdog_svc-*`) | `run_logs.service_log_retention_days` (~762) | 90 days | 7..3650 | same function | **NO** (default), possible if set |
| Mail audit log `notifications` | - | never purged (no DELETE) | - | - | yes |
| Approvals, config changes, integrity audits, file entries, FY journal, overlap activations | - | never purged | - | - | yes |
| Weekly / monthly report documents | not stored (CSV attached to the mail only; dashboard downloads are generated on request) | - | - | - | **not kept at all** (only the run rows behind them are kept) |
| Support report and logs zip | attached to alert mails only | - | - | - | not kept |
| Config backups `config.yaml.bak-*` | `maintenance.config_backups_keep` | 50 files | 5..1000 | count-based | not time-based |

Notes:
- The "2 years for everything" rule has no floor in the configuration for the log settings, and the default for each
  is 90 days. A config change alone can fix it for the log settings (up to 3650 for run and service logs); the
  application log setting needs a code change because its range stops at 365.
- `tests/test_d5_db_retention_floor.py` asserts default 730 and floor 35; `tests/test_config_validation.py:75` asserts
  the 730 default. No test covers the log retention defaults or the two-year rule.
- Live (LIVE_RESULTS LS8 and LS8b): a renamed or old-schema `manifest.db` was re-created silently at boot (LS8,
  "FAIL-ish"; LS8b, "SILENT, release-blocking"). That loses the two-year history without a mail. Whether this is fixed
  now is UNCLEAR (not checked against the later commits).

---

## 4. Requirement table W1..W47

Columns: status, evidence (file:line, config key), TESTED-BY (unit / lab / sim / live).

### W1-W10: deployment, NAS, modes, manual LAN

| ID | Status | Evidence | TESTED-BY |
|---|---|---|---|
| W1 Program on main server, NAS separate air-gapped | SATISFIED | ARCHITECTURE.md section 1 (topology); `paths.lan_destination` (UNC, models/config.py PathsConfig ~line 75). Air-gap is an environment fact, not enforced by code. G4 `guards.forbid_local_destination` (models/config.py GuardsConfig ~769) refuses a local destination. | tests/test_nas_host_for_alerts.py; live LS1 (NAS path under C:\AAMTest\nas) |
| W2 NAS off by default, on only when required | PARTIAL | Woken without a manual press in three places: (a) Sunday audit `flow.py:3416` (`wol_check_task` in `_audit_lan_leg`), also in manual-only mode (ARCHITECTURE 5.9 "Weekly audit: still wakes the NAS"); (b) 1 April final LAN backup `flow.py:3258-3270` and NAS-folder step `flow.py:3067`; (c) every nightly LAN run `flow.py:2120`. The owner decided YES to (a) and (b) on 2026-11-23 (D-1, PART6 line 51) but the workflow file does not record it. After a failed run the NAS stays on (`lan.shutdown_on_all_retries_exhausted` models/config.py:148, default false; amber `nas_left_on`, core/attention.py:345-347). | tests/test_manual_lan_rollover_audit.py, audit_lab/test_w6b_rollover_nas_off.py, tests/test_sim_e2e_14_manual_lan_old_year.py; live: none for WoL |
| W3 Three modes A / B / C | SATISFIED | Per leg, derived in one place: `core/manual_leg.py` `mode()` (off / nightly / manual) from `cloud.enabled`/`cloud.scheduled` (models/config.py ~325) and `lan.enabled`/`lan.scheduled` (~134). A = both `scheduled: false`; B = cloud true, LAN false; C = both true. Schedules: `serve.py:107-112` (`_nightly_schedules`). There is no single "mode" key. | tests/test_manual_cloud_schedule.py, tests/test_manual_lan_schedule.py, tests/test_manual_lan_config.py, tests/test_manual_lan_cloud_only_facts.py; live B8 (`schedules=[]` on real Prefect, both legs manual) |
| W4 Mode B is the client's current configuration | UNCLEAR | The repo `config.yaml` is a sandbox (`wol.enabled: false`, `server_ip: 127.0.0.1`); the client's live config is not in the repo and was not read. | none |
| W5 Cloud automatic and always running in B and C | SATISFIED | `cloud.scheduled` default true (models/config.py ~325-330); `schedule.cloud_cron` default `0 18 * * *` (~1205); `serve.py:38-46`. Trial box cron differs (02:00). | tests/test_sim_e2e_01_best_jan.py (nightly cloud rows), audit_lab/test_w27a_calendar_best.py; live B1 |
| W6 Mode A cloud started by hand | SATISFIED | `ui.py:919` `POST /trigger/cloud`; manual deployment is registered without cron (`serve.py:38-46`, `_nightly_schedules` returns `[]`). | tests/test_manual_cloud_schedule.py; live B1, B8 |
| W7 UI "run sync" button for LAN | SATISFIED | `ui.py:953` `POST /trigger/lan`; button "Run LAN Backup..." `static/js/dashboard.js:340` (label differs from the owner's wording; cosmetic). | tests/test_manual_lan_dashboard_launch.py; live B2 (LAN_COMPLETE via button) |
| W8 Manual LAN powers on NAS if off | SATISFIED (needs `wol.enabled: true`) | `flow.py:2120` -> `core/wol.py:133` `ensure_server_online`; with WoL off it returns True without waking (`core/wol.py:139-141`). `wol.enabled` default true (models/config.py ~202); wake timing `wol.wake_timeout_seconds` (~219). | tests/test_wol_comprehensive.py, tests/test_wol_no_default_address.py; live: none (WoL disabled on trial box) |
| W9 Manual LAN copies, then verifies | PARTIAL | Copy = robocopy `/MIR` (`core/lan_sync.py` `build_robocopy_command`); success = completed summary + exit bitmask (`core/lan_sync.py` `decide_lan_result`, ARCHITECTURE 10.5 LS1-LS3); pre/post destination snapshot (`flow.py` `lan_snapshot_after_task`). No content check runs after a LAN copy (ARCHITECTURE 10.5 note). Hash verification happens only in the weekly audit (`flow.py:3301`) or on demand (`ui.py:1679` `POST /trigger/verify/{mode}`). | tests/test_verify_trigger.py, tests/test_manual_verify_button.py; live LS1 (NAS md5 = source; audit VERIFIED), B2 |
| W10 NAS switched off after a manual LAN run | PARTIAL | Success: always shut down (`lan.shutdown_after_backup` default true, models/config.py ~147; owner D-2 in PART6 line 52). Failure: stays on with amber `nas_left_on` (see W2). The owner's wording covers only the success path. | tests/test_manual_lan_nas_left_on.py, tests/test_lan_shutdown_policy.py, tests/test_m4_shutdown.py, tests/test_a4_final_lan_no_shutdown.py; live: none (shutdown disabled on trial box) |
| W11 Cloud copy after LAN, never at the same time | PARTIAL | Not simultaneous: one global slot (`aam-backup` limit 1 plus `backup.lock`; ARCHITECTURE section 2, `flow._backup_slot`). Order not enforced: separate deployments, no LAN-then-cloud chaining. `backup(mode="all")` runs cloud first (`flow.py:3784-3785` and the `# ── Cloud ──` block at `flow.py:3801`, then `# ── LAN ──` at `flow.py:3809`). Neither leg reads the other's output (both read the source), so "after" is not a data dependency. | tests/test_flow_orchestration.py (mode routing), tests/test_sim_e2e_14_manual_lan_old_year.py; audit_lab/test_w16_flow_first.py (not read in detail) |

### W12-W17: financial-year folders and retirement

| ID | Status | Evidence | TESTED-BY |
|---|---|---|---|
| W12 Outside Jan-Mar only one folder | SATISFIED | `core/fy_overlap.py:155-180` `active_folders`: base `[configured]` unless the window, switch pending or kept condition holds (lines 167-171). The only extra folder after 1 April is an unfinished old year (section 4 of this file, W17). `rollover.overlap_enabled` (models/config.py:885, default true). | tests/test_ov2_fy_overlap.py, tests/test_ov10_tools_active_fy.py, tests/test_fy_old_year_copy.py; sim: tests/test_sim_e2e_02_best_apr.py; lab audit_lab/test_w25_fy_overlap.py (15-month scenario) |
| W13 1 Jan to 1 Apr both folders | SATISFIED | `core/fy_overlap.py:128-135` `in_window` (calendar FY = configured and month between `rollover.early_start_month` (models/config.py:892, default 1, range 1..3) and March); `active_folders` `[next, configured]` (fy_overlap.py:168-171). Activation `core/fy_overlap.py:485` (`ensure_overlap_activated`). Calendar months are used, so the window starts on 1 January, not at a time of day. | tests/test_sim_e2e_01_best_jan.py (`test_jan1_0200_activates_fy27_28_exactly_once`), tests/test_sim_e2e_05_straddle_jan.py, tests/test_sim_e2e_13_toggle.py; lab audit_lab/test_w25_fy_overlap.py |
| W14 Old FY retired only after final backup copied AND verified | PARTIAL | `flow.py:2477` `_settle_old_fy` closes only when `core/fy_overlap.py:238` `copy_state` finds, for every enabled leg, a success row started after the year ended. "Verified" means: cloud = `CLOUD_COMPLETE` (size-only daily check plus evidence gate, ARCHITECTURE V1, `--size-only`); LAN = `LAN_COMPLETE` (robocopy completed; no hash). Hash verification of the old year is not required before closing. | tests/test_fy_old_year_copy.py, tests/test_sim_e2e_14_manual_lan_old_year.py (`test_s14_manual_lan_nas_off_at_rollover_then_the_old_year_is_copied_and_closed`), tests/test_sim_e2e_11_late.py (`test_A_year_closed_while_waiting_for_the_slot_is_skipped`); live: none for a real old-year close |
| W15 Marked closed only after copy and verify | SATISFIED (depends on W14 meaning of "verified") | `flow.py:2414` `_close_old_fy` -> `core/manifest.py:1329` `close_fy` (approvals and incidents closed; no data deleted); tombstone `fy_overlap.mark_year_closed` called at `flow.py:2486`. Closing is automatic; no UI sets it (open point 8). | tests/test_manifest_close_fy_approvals.py, tests/test_sim_e2e_14_manual_lan_old_year.py (last tests) |
| W16 Same retirement rule for cloud and LAN | SATISFIED | `copy_state` loops every enabled leg (`core/fy_overlap.py:238-266`); a manual-only LAN leg is required (ARCHITECTURE 5.9b). | tests/test_fy_old_year_copy.py, tests/test_sim_e2e_14_manual_lan_old_year.py |
| W17 Both folders tracked until retirement | SATISFIED | `core/fy_overlap.py:173-179`: unfinished old years are appended to the active set; derived from finished journals and `run_history` (`unfinished_old_years`, fy_overlap.py:267). Caveat: an unfinished year is kept only while its rollover journal is present (UNCLEAR if the journal is lost, see open point 7). | tests/test_sim_e2e_10_block_unapproved.py (`test_s10_fy26_27_cloud_is_backed_up_on_each_night_while_the_switch_is_pending`), tests/test_sim_e2e_14_manual_lan_old_year.py (`test_s14_a_restart_between_the_switch_and_the_copy_keeps_the_old_year_tracked`) |

### W18-W24: retry, reasons, verification record, overrides

| ID | Status | Evidence | TESTED-BY |
|---|---|---|---|
| W18 Retry in cloud and LAN, about 3 months or until verified | PARTIAL | In-run attempts: LAN `lan.max_attempts` 2, `lan.retry_delay_seconds` 600 (models/config.py:156-157); cloud `cloud.max_attempts` 3, `cloud.retry_delay_seconds` 300 (~356-357); `flow.py` `_sync_with_retries` (line 822). Across nights: scheduled legs run every night; an unfinished old year is kept with no give-up (ARCHITECTURE 5.9b). No three-month window exists in code. A manual-only LAN leg is retried only when a person presses (amber `manual_not_final` / `manual_old`; one WARNING mail per year). | tests/test_sim_e2e_12_three_nights.py (`test_s12_three_failing_lan_nights_across_jan1`), tests/test_sim_e2e_14_manual_lan_old_year.py (manual presses that fail, then succeed) |
| W19 Retry until done is preferred over any override | PARTIAL (owner intent UNCLEAR) | Transient failures are retried (W18). Guard blocks (G1-G6) do not retry: they stop and raise an approval (`flow.py:1248` `blocked:` ACTION mail; `core/approvals.py`). That is fail-closed by design; it is not "retry until done". | tests/test_sim_e2e_09_block_approved.py, tests/test_sim_e2e_10_block_unapproved.py, tests/test_m2_approvals.py |
| W20 After 2-3 failed attempts the UI shows the reason | PARTIAL | The reason is shown from the first failure, not after 2-3: `ui.py:603-639` `/status` recent runs carry `error` (line 616) and fault title/code (`ui.py:1910-1935` `_status_fields`); the last-run summary also carries `error` (`ui.py` ~1937-1955); `static/js/dashboard.js:601` renders the error text in the run list. There is no failure counter. The attention item (`core/attention.py:302-306`) shows only the status label and time, not the reason. Leg cards: UNCLEAR (not traced line by line). | tests/test_d2_attention_latest_run.py, tests/test_f2_trigger_remediation.py; live B6 (failure text on the button), LS3 |
| W21 Copy failure reason visible in UI | SATISFIED | Same evidence as W20: run row `error_message`, `fault_code`/`fault_title` from `core/faults.py` (catalog), `static/js/dashboard.js:580, 601`. | tests/test_d2_attention_latest_run.py, tests/test_f2_trigger_remediation.py; live B6, B7 (ACTION mail and button text), LS3 |
| W22 Verification pass/fail visible and recorded | PARTIAL | Cloud: `CLOUD_VERIFY_FAILED` status, evidence gate (`flow.py` `cloud_verify_and_report_task`), `integrity_audits` rows, `audit:` attention items (`core/attention.py:316-330`). LAN: robocopy status only, no verification record of its own (see W9). | tests/test_cloud_verify.py, tests/test_cloud_verify_comprehensive.py, tests/test_verify_trigger.py, tests/test_a1_audit_verdict_authoritative.py; live LS1, LS10 (audits VERIFIED, 0 mismatches) |
| W23 UI offers overrides wherever a workflow needs them | PARTIAL | Present: per-leg run buttons (`ui.py:919, 953`), per-FY run (`?fy=`), approvals approve / dismiss / run with typed phrase (`ui.py:1075-1140`), rollover run (`ui.py:1581`), verify (`ui.py:1679`), settings. Missing: force activation of the next FY folder (no route, see W33). | tests/test_m2_approval_dashboard.py, tests/test_m2_approval_workflow.py, tests/test_trigger_lock.py, tests/test_ui_rollover_busy.py; live B1-B7, approval cleared by itself (LIVE_RESULTS ~line 165) |
| W24 Manual override of the retry rule (OPEN) | UNCLEAR | No override of the retry rule exists. Approvals override guard blocks only. Owner decision pending (open point 2). | none |

### W25-W27: catch-up after outages

| ID | Status | Evidence | TESTED-BY |
|---|---|---|---|
| W25 Pending work done at next start / power-on | PARTIAL | Activation runs at the start of every backup flow (`flow.py:3752` `_activate_overlap` inside `backup()`) and in the daily rollover check (`flow.py:2683-2686`). The agent boot (`launch.py:362-440`) does not run activation. Missed scheduled runs: the next scheduled run catches up; a run that starts after its budget becomes a `*_MISSED` row plus a WARNING mail (ARCHITECTURE 5.6). Rollover journal resumed by the daily check (`flow.py:2814` `_rollover_resume`). On 1 April with the NAS off: a scheduled LAN leg blocks the switch until approval (G6; `test_sim_e2e_14_manual_lan_old_year.py` last test); a manual-only LAN leg records `LAN_NOT_REACHED` and the switch proceeds (ARCHITECTURE 5.9). | tests/test_sim_e2e_03_nas_down.py (`test_s3_nas_down_on_dec31_fails_one_run_and_jan1_recovers`), tests/test_sim_e2e_11_late.py, tests/test_sim_e2e_12_three_nights.py, tests/test_sim_e2e_13_toggle.py; lab audit_lab/test_w6b_rollover_nas_off.py; live: none for power-off (LS7b, LS10 are kill tests) |
| W26 Catch-up includes folder creation, activation, rollover | PARTIAL | Activation: retried each run, escalated to ACTION after `rollover.activation_alert_after_hours` (`core/fy_overlap.py:353` `_defer`; models/config.py ~879). Rollover journal: resumable steps (`core/fy_rollover.py`, `core/rollover_journal.py`). NAS folder for the next year: created inside the next LAN run (`flow.py:2132`); for the rollover completed by the next LAN pipeline (`flow.py:2009`). Nothing creates the NAS folder without a LAN run. | tests/test_a7_pending_nas_folder.py, tests/test_a5_rollover_steps.py, tests/test_a6_rollover_journal_flow.py, tests/test_ov2_activation.py, tests/test_fy_rollover.py |
| W27 Catch-up has priority over normal work | UNCLEAR | Within each flow run, catch-up runs first (`flow.py:2683-2686` rollover check; `flow.py:3752` backup). There is no global queue or priority across flows; the owner did not define "priority". | tests/test_sim_e2e_14_manual_lan_old_year.py (`test_s14_a_restart_between_the_switch_and_the_copy_keeps_the_old_year_tracked`) |

### W28-W33: folders, marker, FY rollover and force

| ID | Status | Evidence | TESTED-BY |
|---|---|---|---|
| W28 Client-made FY folder adopted if it matches the naming | SATISFIED | `core/fy_overlap.py:52` `_CANONICAL_FY` (`^FY(\d{2})-(\d{2})$`); `_find_canonical` (`fy_overlap.py:344-350`) matches case-insensitively; `ensure_overlap_activated` adopts an existing folder (`created=False`, fy_overlap.py:~520-535) and never uses `parents=True`. Non-canonical FY-looking names are not adopted: they raise a sibling ACTION (`fy_overlap.py:588` `sibling_findings`, kind misnamed). | tests/test_ov2_activation.py, tests/test_break_fy_overlap.py, tests/test_fy6_unprotected_new_fy.py, tests/test_fy7_early_source_folder.py |
| W29 Same adopt rule for every folder | SATISFIED (with a confirmation step) | NAS folder: `core/fy_overlap.py:733-772` `ensure_overlap_nas_folder`: existing with marker = `ok`; empty = marker written; non-empty without marker = `needs_confirmation` (G5 asks, nothing written). Rollover: `core/fy_rollover.py:325` `create_source_folder` and `:369` `create_nas_folder` use `exist_ok`. A non-empty NAS folder without the marker is therefore never adopted silently. | tests/test_a7_pending_nas_folder.py, tests/test_g5_target_not_approvable.py, tests/test_ov10_protected_fy.py |
| W30 Clients never copy into NAS; they only copy from it | SATISFIED (program side); RISK noted | The program is the only writer: robocopy `/MIR` (`core/lan_sync.py`), probe files (`.aam_write_probe_<run_id>`, LA7). Risk: `/MIR` deletes any file a user places on the NAS at the next run, up to the G3 limits (`guards.max_delete_files` 500, `max_delete_percent` 10; models/config.py GuardsConfig ~769). Small deletions are not blocked. | tests/test_b1_delete_allowance.py (G3 limits); no test for a user-placed file on the NAS |
| W31 Restore from the NAS manual only | SATISFIED | No flow or dashboard route restores (`ui.py` route list has none; `flow.py` does not import restore). `restore.py` is a CLI (argparse, `restore.py:115`) and `core/restore.py:865` `restore_fy` reads from GCS only. A NAS restore is a manual file copy; no tool exists (UNCLEAR whether one is expected). | tests/test_restore_engine.py, tests/test_break_restore_upload.py; live: none (restore drill pending per owner memory) |
| W32 Every newly created NAS folder gets the marker | SATISFIED | `core/fy_overlap.py:748-752` (create then touch marker), `core/fy_overlap.py:765` (marker written on an empty folder), `core/fy_rollover.py:390` (`create_nas_folder`). `core/fy_rollover.py:273` `create_new_fy_folders` also writes it but uses `parents=True`, and has no production caller (dead code, see section 6). | tests/test_a7_pending_nas_folder.py, tests/test_fy_rollover.py; lab audit_lab/test_w25_fy_overlap.py |
| W33 UI offers "force FY rollover / activation" | GAP (activation) / PARTIAL (rollover) | Rollover: `ui.py:1581` `POST /api/rollover/run` only when `core/fy_rollover.py:147-172` `manual_run_state` says due (FORWARD or JUMP); refused while the clock is behind. So it cannot run before 1 April. Activation: no route in `ui.py` (route list lines 506-1801); activation happens only inside the window (`fy_overlap.py:128-135`). A manual backup of the next folder is refused unless the folder is already active (`flow.py` `backup(fy=)` check). | tests/test_fy_rollover.py, tests/test_ui_rollover_busy.py, tests/test_fy3_settings_never_change_the_fy.py; none for a forced activation |

### W34-W39: mail

| ID | Status | Evidence | TESTED-BY |
|---|---|---|---|
| W34 Two recipient groups exist | SATISFIED | `models/config.py` NotificationConfig: `recipients` (~503, alerts) and `report_recipients` (~513, reports); audiences `core/notify.py:53-56`. | tests/test_recipients_config.py, tests/test_recipients_routing.py, tests/test_recipients_settings_ui.py |
| W35 Partners receive ONLY weekly and monthly reports | GAP | Section 2: rows 15-18 are routed to partners on the default setup; rows 3-14 reach partners when the alerts list is empty (`core/notify.py:84-89`, amber item `core/attention.py:416-425`). | tests/test_recipients_callers.py (pins the opposite of the owner rule), tests/test_recipients_routing.py |
| W36 Partner-only rule checked thoroughly | GAP | This section 2 is the check; the result is W35. No test asserts the owner rule. | none for the owner rule |
| W37 Group 1 gets alerts (and reports) | PARTIAL | Alerts: yes. Reports: no, when the partner list is non-empty (section 2.4; `core/notify.py:83-89`). | tests/test_recipients_callers.py (`test_the_scheduled_report_goes_to_the_reports_list`, `test_the_dashboard_weekly_button_sends_to_the_reports_list`) |
| W38 Fixed alerts not resent after a mail failure | PARTIAL | No alert body is ever re-sent after a failure: a FAILED row does not suppress the next occurrence (`core/notify.py:658-681` `_incident` looks only at SENT rows), and no retry queue exists. The digest (`core/notify.py:742-800`, query `core/manifest.py:1383-1395` `undelivered_alerts`) lists the SUBJECTS of every FAILED alert-list row with no later SENT row of the same key, and does not check whether the problem is still open. A problem that was later recovered (different key `recovered:...`) is still listed. Digest window: `alerts.undelivered_digest_max_age_hours` 72 (models/config.py:614); older undelivered alerts are never reported. The live LS11 defect (RECOVERED suppressed after an outage) was fixed by the L1 change (`_incident`, `lost_problem`, notify.py:661-668) after that live run. | tests/test_l1_undelivered_digest.py (`test_three_lost_alerts_then_smtp_back_gives_exactly_one_digest_then_none`, `test_an_alert_whose_key_was_later_sent_is_not_listed`, `test_end_to_end_outage_failure_row_then_digest`), tests/test_l1_recovered_after_lost.py, tests/test_d3_undelivered_alerts_clear.py; no test for a fixed problem. Live: LS11 (before the L1 fix) |
| W39 Recovered alerts kept only in the audit log | GAP | RESOLVED mails are sent by default: `alerts.send_resolved` default true (models/config.py:605; class RESOLVED in `core/alert_class.py`). Storage of the audit log exists (`notifications` table, every row incl. RESOLVED; `core/manifest.py` schema v6). No dashboard view lists the `notifications` table (no route in `ui.py`; the table is read only by attention and incident queries in `core/manifest.py:1256-1347`). | tests/test_alert_switches.py, tests/test_incident_closure.py, tests/test_fy15_files_alert_own_incident.py (send_resolved False case), tests/test_break_notify.py:381; live LS2, LS3, B6, B7 (RESOLVED mails sent) |

### W40-W47: retention, tests, modes in testing

| ID | Status | Evidence | TESTED-BY |
|---|---|---|---|
| W40 Logs and data kept at least 2 years | GAP | Section 3. Default log, run-log and service-log retention = 90 days; app-log range stops at 365; DB run history 730 with a 35-day floor. | tests/test_d5_db_retention_floor.py, tests/test_config_validation.py:75; no test for log retention |
| W41 Test mode A (fully manual) after the release | GAP | No calendar or lab test with both legs manual. Live B8 confirmed `schedules=[]` for both legs (manual) on the trial box, but no full manual run sequence with a NAS. | live B8 (partial) |
| W42 Test mode B (cloud automatic + LAN manual) | PARTIAL | Calendar: `tests/test_sim_e2e_14_manual_lan_old_year.py` (cloud nightly + manual LAN across 1 April). Live: B1, B2, B7, B8 (cloud nightly was not used; manual-only mode shown). No live nightly cloud plus manual LAN with real NAS. | tests/test_sim_e2e_14_manual_lan_old_year.py; live B1, B2, B7, B8 |
| W43 Test mode C (fully automatic) | PARTIAL | Calendar: scheduled LAN is covered by `tests/test_sim_e2e_01_best_jan.py`, `_02_best_apr.py`, `_03_nas_down.py`, `_09_block_approved.py`, `_10_block_unapproved.py`. Live: none with WoL or shutdown. | tests/test_sim_e2e_01_best_jan.py, tests/test_sim_e2e_02_best_apr.py, tests/test_sim_e2e_09_block_approved.py; lab audit_lab/test_w27a_calendar_best.py, audit_lab/test_w27b_calendar_worst.py |
| W44 Every mode works end to end | PARTIAL | Mode B and scheduled-LAN calendar paths pass in the sim rig; mode A has no sim run; the real NAS power path is untested live. | as W41-W43 |
| W45 Manual LAN flow (W7-W11) tested | PARTIAL | Unit: `tests/test_manual_lan_*.py`, `tests/test_manual_verify_button.py`, `tests/test_f1_manual_start_guard.py`, `tests/test_trigger_lock.py`. Calendar: sim S14. Live: B2, B3, B5, B7 (no WoL, no shutdown). | as listed |
| W46 Future automatic LAN flow tested when it exists | PARTIAL | The scheduled LAN path already exists and is covered by the sim rig (see W43). The WoL timer hand-off is not tested (see W47). | as W43 |
| W47 Mode C coordinated with an external power timer (10 min earlier) | PARTIAL / UNCLEAR | The timer is outside the program (by design). The program waits for SMB readiness up to `wol.wake_timeout_seconds` (models/config.py ~219; `core/smb_ready.py`). If the NAS is already on, the run proceeds without waking it (`core/wol.py` `ensure_server_online` + SMB stat). Nothing checks that the timer is set or that its lead time is 10 minutes. If WoL is off and the NAS is off, the LAN preflight fails (ARCHITECTURE 3.3). | tests/test_wol_comprehensive.py; no test for the timer hand-off; live: none |

---

## 5. Open points (workflow file section 4) - what the CODE shows (not the decision)

1. **"TCS"**: no reference anywhere in the code, config, or docs (searched). UNCLEAR.
2. **Manual override of the retry rule**: no override of retries exists. Approvals override guard blocks only
   (`core/approvals.py`; ARCHITECTURE section 4 guards). The decision is the owner's.
3. **Mode A cloud**: code supports `cloud.scheduled: false` (manual only; live B8 confirmed). So both readings are
   possible by config. The choice is the owner's.
4. **What "retire" means for cloud / LAN**: the program does not delete anything at close. Cloud: the 1 April
   rollover moves the old year's objects to the ARCHIVE storage class (`core/fy_rollover.py` `run_archive_transition`,
   ARCHITECTURE 5.2 step 5; `flow.py` `step_archive`). LAN: nothing is moved or removed. "Closed" = tracking stops and
   approvals/incidents close (`core/manifest.py:1329`). Owner to confirm this is the intended meaning.
5. **Partners on failure and recovery mails**: the code sends failure, RESOLVED and digest mails to the alerts list,
   and to the partner list only when the alerts list is empty. Current code does NOT give partners weekly/monthly only
   (section 2). The owner's answer is needed to change the tests that pin the current routing.
6. **Duration of retrying ("about 3 months")**: no clock exists. Retry is per run (W18); unfinished years are kept
   until copied; nothing escalates by time except the once-per-year WARNING and the amber `manual_not_final` item.
7. **Two-year scope**: code keeps run history (730 days, floor 35), never purges notifications, approvals,
   integrity audits, config changes, file entries or journals, and purges run logs, service logs and application logs
   at 90 days by default. Owner to confirm each category.
8. **"Marked complete and verified" - who sets it**: the program sets it from the run status and the audit, at the
   end of each run. No UI sets it manually.
9. **Force rollover when the folder already exists**: adopted, not refused. Activation adopts a canonical folder
   (`fy_overlap.py` `_find_canonical`). Rollover uses `exist_ok` for source and NAS folders; a non-empty NAS folder
   without the marker is left untouched and goes to G5 (an approval). Forcing activation is not possible at all (W33).
10. **Mode C and the power timer**: the program waits for SMB readiness up to the wake timeout and does not check the
    timer. If the NAS is on when the run starts, it proceeds. The run shuts the NAS down after success (D-2).
11. **"internet thing"** (read as "intended thing"): no code relevance. UNCLEAR.
12. **Verification scope**: cloud = size-only daily check (`rclone check --size-only`, ARCHITECTURE V1) plus the
    evidence gate; cloud weekly audit = MD5 (`rclone check` with hashes, V2); LAN = robocopy completion plus the weekly
    MD5 audit (V3); the dashboard "Check now" runs the same audit on demand (`ui.py:1679`). No LAN hash check follows
    a manual LAN copy automatically.

---

## 6. (a) Ranked gaps and partials

Ranked by the owner's stated risk: partner mail first, then log loss, then resent or lost alerts, then FY state.

1. **GAP - partner mail (W35, W36).** Four non-report kinds go to the partner list by design (section 2 rows 15-18):
   `rollover:done` (`flow.py:3101-3106`), `overlap:start` (`core/fy_overlap.py:469-482`), `password:` (`ui.py:1310`,
   BOTH) and `test:` (`ui.py:1623`, BOTH). Tests pin them (`tests/test_recipients_callers.py`). Fix needs the owner's
   answer (open point 5), then new red tests and an audience change in these four producers.
2. **GAP - partner mail by fallback (W35).** Empty `notifications.recipients` sends every alert to the partner list
   (`core/notify.py:84-89`). Only an amber item warns. Fail-closed fix: an empty alerts list must not fall back to the
   partner list (record FAILED and show red), or the owner accepts it.
3. **GAP - logs and data under two years (W40).** Defaults: application logs 90 days (`maintenance.log_retention_days`,
   range capped at 365), run logs 90 days (`run_logs.retention_days`), service logs 90 days
   (`run_logs.service_log_retention_days`), run history 730 days with a 35-day floor (`maintenance.db_retention_days`).
   Reports and support zips are not stored at all. Fix: defaults and floors of 730 days; app-log range above 730.
4. **GAP - resolved alerts mailed (W39).** Normal recovery sends RESOLVED mail by default (`alerts.send_resolved`
   true). Owner wants the audit log only. Fix: default false, or a new rule. Also no dashboard view of the audit log.
5. **GAP - no force activation (W33, W23).** Next year's folder cannot be created from the UI before the window opens
   (no route). "Run FY rollover now" is offered only when due.
6. **PARTIAL - reports not reaching the IT list (W37).** When the partner list is set, group 1 gets no weekly or
   monthly reports (`core/notify.py:83-89`). Owner's notes say group 1 gets "alerts (and the reports)".
7. **PARTIAL - digest names fixed problems (W38).** `core/notify.py:742-800` lists subjects of failed alerts whose
   problem has since been fixed; no test covers it. Failed alerts older than 72 hours are never reported.
8. **PARTIAL - NAS woken unattended (W2).** Sunday audit (`flow.py:3416`) and 1 April LAN final backup
   (`flow.py:3258-3270`) wake the NAS in manual-only mode. Owner decided this (D-1, PART6 line 51); the workflow file
   W2 wording contradicts it. Record the decision in the workflow file or change the code.
9. **PARTIAL - no LAN content verification after a copy (W9, W14, W22).** "Verified" for LAN means robocopy completed.
   Hash proof is weekly or on demand only. If the owner means hash-verified before retirement, this is a gap.
10. **PARTIAL - retry rule (W18, W19, W20).** No three-month window; per-run attempts (LAN 2, cloud 3); no failure
    counter; the attention banner has no reason text.
11. **PARTIAL - catch-up depends on a later run (W25, W26).** Activation is not run at agent boot (`launch.py`). A
    scheduled LAN leg with the NAS off on 1 April blocks the switch until approval (G6), by design.
12. **PARTIAL - order of legs (W11).** `backup(mode="all")` runs cloud before LAN (`flow.py:3784-3809`). Not simultaneous
    (slot), but the LAN-first order in W11 is not enforced anywhere.
13. **PARTIAL - NAS left on after a failed manual run (W10).** By design (amber item). Owner wording covers success only.
14. **PARTIAL - user files on the NAS are mirrored away (W30).** `/MIR` removes them at the next run below the G3 limits.
15. **PARTIAL - silent fresh database (W40 supporting).** LIVE_RESULTS LS8 and LS8b: a renamed or old-schema
    `manifest.db` is re-created silently at boot (live finding; current fix status UNCLEAR).
16. **Dead code to remove (no requirement):** `core/fy_rollover.py:273` `create_new_fy_folders` (uses `parents=True`,
    no production caller); `core/report.py` `send_failure_alert` (production caller none; tests only).
17. **Tests missing (W41-W47):** mode A sim run; mode C timer hand-off test; partner-only rule test (currently the
    opposite is pinned); fixed-problem digest test; log-retention two-year test; forced activation test; NAS
    user-file test (W30).

---

## 7. (b) Proposed test list (workflow tests A, B, C)

All tests use the calendar rig (`test_support/sim_calendar.py`: `Sim`, events `lan(d)`, `cloud(d)`, `rollover(d)`,
`audit(d)`, `weekly(d)`, `monthly(d)`, `sim.set_answer(...)`, `sim.runs()`, `sim.subjects()`, `sim.argv()`,
`sim.tree()`, `sim.smtp_down()`, `sim.power_cut()`). Each new test is written red first on the current code (owner
rule in CLAUDE.md). Live follow-up: the trial box after each change (deploy by git bundle, as in SESSION_HANDOFF).

### A. Fully manual (both legs `scheduled: false`)

- **A1 - manual cloud and manual LAN, whole year.** Setup: `cloud.scheduled: false`, `lan.scheduled: false`, WoL
  scripted (`sim` wake counter), NAS answers from 2 January. Events: manual cloud press on each weekday, manual LAN
  press on 3 days a month, rollover check daily, FY activation on first press in January. Expected: no scheduled rows;
  activation exactly once in January; both folders backed up on each press in January-March; on 1 April the final LAN
  is `LAN_NOT_REACHED` if the NAS is off and the switch still happens; the old year stays tracked until a LAN success
  after 1 April; no mail to partners.
- **A2 - manual LAN with NAS off, then on.** Setup: WoL enabled, NAS scripted off then on. Expected: wake attempted on
  press; a failed run leaves `nas_left_on` (amber); the next successful press shuts the NAS down; one row per press.
- **A3 - live (trial box, WoL and shutdown off).** Press cloud and LAN buttons, check rows and mails; real NAS
  power-off test deferred until the owner is at the NAS (memory note).

### B. Cloud automatic, LAN manual (client's current mode)

- **B1 - cloud nightly plus manual LAN across 1 April.** Extend `tests/test_sim_e2e_14_manual_lan_old_year.py`
  with the Sunday audit: expected per decision D-1 = the audit wakes the NAS once and votes shutdown; assert it so a
  change is noticed.
- **B2 - manual LAN pressed during the nightly cloud window.** Setup: cloud at 02:00, LAN press at 02:10. Expected: the
  second waits for the slot (or `LAN_SLOT_TIMEOUT`), never both in one slot; rows in order; no simultaneous robocopy and
  rclone argv in the sim.
- **B3 - outage then digest, fixed problem case (NEW, red first).** Setup: SMTP down for a cloud failure, then the
  cloud succeeds (RECOVERED lost), SMTP back. Expected after the fix: ONE digest lists nothing about the fixed failure;
  today it lists it.
- **B4 - manual-only LAN for three weeks (amber, no mail).** Expected: amber `manual_old` after 30 days, ONE WARNING per
  year (`rollover:<FY>:not_fully_copied` only if unfinished), no mail to partners.

### C. Fully automatic (both legs scheduled; LAN woken by the external timer)

- **C1 - timer on, NAS already awake.** Setup: NAS scripted on at 00:50, LAN cron 01:00. Expected: WoL check sees SMB
  open, no second wake; success; shutdown after success; next night the timer switches it on again.
- **C2 - NAS off at 01:00, WoL wakes it.** Expected: one magic packet set per `wol.wake_retry_count`; success; shutdown.
- **C3 - server off 31 December night, first run 2 January.** Setup: `sim.power_cut()` or no run on 1 January. Expected:
  activation exactly once on the first run; both folders; the INFO mail "FY now backed up" goes to the INTERNAL list only
  after the fix (today: partner list).
- **C4 - NAS off on 1 April, scheduled LAN.** Expected per current design: switch blocked (G6), one approval, no switch
  until approval; after approval and a successful LAN run, switch and closing as in S10. Owner to confirm (open point 9/10).
- **C5 - partner-only routing across a full calendar year (NEW, red first).** Setup: both lists set, all calendar
  events from 1 December to 31 December of the next year. Expected after the fix: the reports list receives only
  `report:weekly` and `report:monthly` subjects; the alerts list receives everything else; with the alerts list empty
  nothing reaches the reports list (send FAILED and red, not a silent fallback). Today this test fails on the four
  rows of section 2.
- **C6 - two-year retention (NEW, red first).** Setup: fabricate `run_logs` day folders at 89, 91, 400 and 800 days
  old, a service log and an app log at the same ages, and a `notifications` row older than two years. Run
  `flow.log_retention_task` with the default config. Expected after the fix: nothing younger than 730 days removed.
  Today: folders at 91 days removed; the app-log range cannot express 730.
- **C7 - run-history floor (NEW).** `maintenance.db_retention_days` below 730 refused at load (after the owner's answer).
- **C8 - forced activation (NEW, after the feature exists).** Setup: December 15, no folder; press "activate next year".
  Expected: canonical folder created with marker and placeholder; no change to the config; INFO mail to the internal list.
- **C9 - client-made folders.** Pre-create `FY27-28` (empty) and `FY 27-28` (misnamed) and a non-empty `FY27-28` NAS
  folder without the marker. Expected: canonical adopted; misnamed raises ACTION and is not created or renamed; the
  non-empty NAS folder gets G5 approval and nothing is written.
- **C10 - restore is manual only.** Assert that no flow, watchdog or UI route calls `core.restore` or `restore.py`
  (static check), and that the CLI requires `--fy` and `--target`.

### Live (trial box) follow-up

- **L1 - rehearsal:** `tools/fy_overlap_rehearsal.py` (dry run by default; `--execute` on the sandbox config, real
  robocopy and rclone, fake clock to 15 January and 1 April) covers A1/C3/C4 on real tools.
- **L2 - mode switch:** toggle `cloud.scheduled` and `lan.scheduled` on the box and check `/status` and Prefect schedules
  (B8 already did this); then restore the box config.
- **L3 - real NAS power path (C1, C2, A2 with WoL):** only when the owner is at the NAS (memory note
  `nas-shutdown-test-deferred.md`).

---

## 8. Notes on method and limits

- No test was run. Coverage is named from file names and test function names read in this session; results were not
  re-run.
- Line numbers were read at the time of writing (`fy-overlap` working tree). Lines in `.claude/worktrees/` copies were
  ignored.
- The workflow file was not edited. This file is the only output.
