# Review: dashboard MANUAL backup buttons (2026-11-23)

Branch `fy-overlap`. Read-only review: no tracked file edited, no tests run, no services touched, nothing committed.
Only this file was created. Line numbers refer to the working tree as read on 2026-11-23.

Tags used below:
- CONFIRMED-FROM-CODE: read in the code at the cited line.
- COVERED-BY-TEST (name): an existing test asserts this behaviour.
- UNTESTED / UNCLEAR: no test found, or the code does not say what is intended.

Files read: `docs/ARCHITECTURE.md`, `ui.py`, `flow.py`, `serve.py`, `static/js/dashboard.js`, `templates/dashboard.html`,
`core/wol.py`, `core/shutdown.py`, `core/guards.py`, `core/approvals.py`, `core/lan_preflight.py` (grep), `core/lan_sync.py` (grep),
`core/fy_overlap.py`, `core/statuses.py`, `core/cloud_verify.py` (grep), `core/attention.py` (grep), `models/config.py`,
`audit_lab/test_w16_flow_first.py`, `audit_lab/labconf.py` (docstring), and the test files listed in section F.

---

## 0. Summary (ranked findings are in section D)

1. A manual start while the agent is down (Prefect API up) returns "The ... backup started" and the top card says
   "Copying files ... now", although nothing runs. The queued run has no expiry and starts when the agent returns,
   at an unplanned time, and can wake and then switch off the NAS.
2. The NAS is switched off after every LAN_COMPLETE, including a run that did not wake the NAS itself. The confirm text
   says "switches the NAS off afterwards" whenever `shutdown_after_backup` is on, even when WoL is off (then nothing is switched off).
3. The "failure rule" NAS shutdown also fires on a guard BLOCK, not only on PARTIAL/FAILED as the config text says.
4. The classic "Back up" button can be left disabled and greyed at "Starting..." after a run that finished within the wait.
5. Checks are check-then-create with no lock (the slot serialises the runs, but a second NAS wake and shutdown follow).
6. There is no JavaScript behaviour test for any of the backup buttons.

The overlap "both folders" case is not reachable from the backup buttons: the no-`fy` button is hidden while two folders exist,
so each click starts exactly one folder (section C).

---

## A. CLOUD button: `POST /trigger/cloud`

### A1. Chain, step by step

1. Click. `dashboard.js:11-31` `triggerBackup(event,'cloud')`: confirm text "Start the online backup now?" (18-20), button
   disabled and faded (23), `fetch POST /trigger/cloud` (25). CONFIRMED-FROM-CODE.
   The template button is `dashboard.html:91-92`. The classic card is hidden when more than one FY folder is active
   (`dashboard.js:460-461`).
2. Cross-site guard. `ui.py:83-96` `_refuse_cross_site_writes`: a POST with an `Origin` that is not this host, or with
   `Sec-Fetch-Site: cross-site`, gets 403. A request with neither header passes this middleware. CONFIRMED-FROM-CODE.
   COVERED-BY-TEST: `tests/test_break_ui.py::test_cross_site_post_with_a_valid_session_is_refused` (530).
3. Auth. `ui.py:495-509` `_require_auth`: a valid session cookie is required, and the session is bound to the current dashboard
   password fingerprint (`ui.py:138-153`). CONFIRMED-FROM-CODE. COVERED-BY-TEST: `tests/test_break_ui.py::test_every_route_refuses_a_caller_without_a_session` (117).
   Session cookie `ui.py:471-474` is `httponly, samesite=lax`. The admin cookie (`ui.py:1086`) is `strict`.
4. Rate limit. `ui.py:797-799`: key `trigger:<ip>`, 5 starts per 300 s (`models/config.py:1085-1089`). 429 when exceeded.
   COVERED-BY-TEST: `test_break_ui.py::test_double_click_starts_one_backup_and_the_sixth_start_is_rate_limited` (270, sequential).
5. Leg enabled. `ui.py:800-801` returns 409 `disabled` when `cloud.enabled` is false.
6. `?fy=`. `ui.py:802`, `_requested_fy` at `ui.py:746-758`. No `fy` means None (the flow then uses every active folder).
   A value is matched case-insensitively against the active set and the canonical name is sent; anything else is 409 (section C).
7. Busy check. `ui.py:805` `_refuse_if_busy` -> `_busy_detail` (`ui.py:761-784`):
   - `_is_running` for cloud, lan, rollover in that order (`ui.py:766`). Any running or due run -> 400 `already_running`.
   - Prefect API unreachable (`_prefect_has_active_run` returns None, `ui.py:259-261`) -> 503 `SCHEDULER_NOT_ANSWERING`.
   - Then `_is_verify_running` (safety check / audit, `ui.py:778-783`).
   CONFIRMED-FROM-CODE. COVERED-BY-TEST: `test_break_ui.py::test_trigger_refuses_while_a_backup_or_rollover_runs_and_never_queues` (244),
   `test_break_ui.py::test_trigger_answers_503_and_starts_nothing_while_prefect_is_silent` (263),
   `test_ov7_dashboard.py::test_manual_backup_refused_while_the_other_leg_runs` (180),
   `test_ov7_dashboard.py::test_manual_backup_refused_while_a_safety_check_runs` (188),
   `test_ov7_dashboard.py::test_manual_backup_fails_closed_when_the_scheduler_is_silent` (198),
   `test_ui_rollover_busy.py::test_manual_backup_is_refused_while_the_rollover_runs` (30).
8. Start. `ui.py:813-818` `arun_deployment(name="aam-backup/backup-cloud", parameters={"fy"} only if given, timeout=trigger_wait)`.
   Exception -> 500 `trigger_failed` (`ui.py:819-825`). CONFIRMED-FROM-CODE.
   COVERED-BY-TEST: `test_f2_trigger_remediation.py::test_endpoint_creation_failure_is_500_trigger_failed` (361).
9. Wait and map. `ui.py:381-407` `_trigger_result`: completed -> 200; cancelled -> 409; other final state -> 500
   "ended as <state name>"; still running after the wait -> 202 "started". The default wait is 180 s (`models/config.py:1109-1111`,
   `ui.py:374-378`). CONFIRMED-FROM-CODE. COVERED-BY-TEST: `test_f2_trigger_remediation.py` mapping tests (136-186) and endpoint tests (278-361).
10. Flow. `flow.py:3494` `backup(mode="cloud")`: config load, `_activate_overlap` (3532), mode/leg guards (3538-3539),
    orphan robocopy log cleanup (3547-3551), `folders = _active_fys` (3566, `flow.py:3318-3342`), `fy` narrowing (3571-3575),
    then `_run_leg_folders("cloud")` (3579-3584). CONFIRMED-FROM-CODE.
11. Per-folder leg loop. `flow.py:3420-3491`:
    - `RUNNING` row for the first folder is written BEFORE the slot wait (3442-3443, `_record_running` 454-472). CONFIRMED-FROM-CODE.
    - Slot: `_backup_slot` (`flow.py:200-234`) = Prefect concurrency `aam-backup`, `occupy=1`, `timeout_seconds=maintenance.concurrency_wait_seconds`
      (default 3600, `models/config.py:606-615`), plus `backup.lock` (`flow.py:237-273`). On timeout: `_slot_timeout_for_folders`
      (3351-3366) writes `CLOUD_SLOT_TIMEOUT` per folder and alerts (AAM-SYS-01, or AAM-SYS-08 when the lock shows no live holder).
    - `_rederive_after_slot` (3395-3417) re-reads the config and re-derives the folder set after the wait.
    COVERED-BY-TEST: `test_ov3_orchestration.py::test_slot_is_held_once_per_leg_around_both_folders_new_fy_first` (96),
    `test_ov3_orchestration.py::test_slot_timeout_records_each_folder_and_runs_nothing` (217),
    `test_slot_orphan.py` (52-76), `test_break_release_approvals` (grep only).
12. Cloud pipeline (`flow.py:1619-1941`):
    - Budget: `_missed_if_no_budget` (491-519) -> `CLOUD_MISSED` + PartialRun if no budget is left (manual runs count from their actual start).
    - Pre phase (1687-1692): `health_check_task`, `cloud_preflight_task` (retries from config), `cloud_guards_task` (656-752: G1, G2, G4, G3;
      no G5 on cloud), `cloud_access_task`.
    - Sync (1713-1721): `_sync_with_retries` (772-845) with `cloud_sync_task` (851) = rclone sync, `--max-delete` from the guards.
      Retries re-measure the guards first.
    - Verify (1752-1785): `cloud_verify_and_report_task` = `rclone check --one-way --size-only` (`core/cloud_verify.py:1-3, 127-148`).
      Evidence gate (1761-1769): verified, normal termination, no added/modified, no `_partial`, no manifest or size error.
      On failure: `CLOUD_VERIFY_FAILED`, alert, RuntimeError.
      The check compares sizes only, not content. The weekly audit does content (`ARCHITECTURE.md` section 3, audit). CONFIRMED-FROM-CODE.
    - Record (1799) only after the gate. Terminal policy (1871-1875): a non-success status raises PartialRun -> Prefect FAILED.
    - `finally` (1911-1942): `_record_run` (1925-1933) writes the run row; `CLOUD_BLOCKED` opens an approval (1938-1940);
      `_send_recovery_if_needed` (1941-1942, `flow.py:393-416`) mails RECOVERED only after a failed/partial/blocked previous run.
13. Dashboard during the run: `/status` `cloud.running = _is_running("cloud")` (`ui.py:573-574`, 661 for folders),
    which counts RUNNING, PENDING and a SCHEDULED run that is due within 60 s (`ui.py:275-290`). The top card says
    "Copying files to the cloud now." (`dashboard.js:282`). The button shows "Running..." (276). Each FY card shows "Running" from the
    DB RUNNING row, and "Copying files now." (`dashboard.js:442`).
    Polling every 2 s (`dashboard.js:328`).
14. After the run: the run row and label (`/status` recent runs). Mail: a failure alert per failed run (`flow.py:332-352`, key `backup:<fy>:cloud:<status>`).
    A successful run sends NO mail unless it recovers from a failure (RECOVERED). CONFIRMED-FROM-CODE.
    COVERED-BY-TEST for the alert: `test_m3_alerts.py` (names not read in detail); UNTESTED for the "no success mail" rule.

### A2. Which FY folder(s)

- No `fy`: every folder of `_active_fys` (new FY first). During overlap that is two folders in ONE flow run, one slot hold, two
  run rows (`flow.py:3564-3566`, `3579-3584`). CONFIRMED-FROM-CODE. COVERED-BY-TEST: `test_ov3_orchestration.py::test_slot_is_held_once_per_leg_around_both_folders_new_fy_first`.
- `?fy=X`: one folder (`flow.py:3571-3575`). The dashboard never sends "no fy" while two folders are shown (see C).
- Cloud prefix follows the folder: `gs://bucket/<fy>` from the folder view (`core/fy_overlap.folder_view` 178-188; ARCHITECTURE 3.4).

### A3. Edge cases

- Nightly cloud run active: refused, 400 `already_running`, "Wait for the running backup to finish" (`ui.py:776-777`).
  COVERED-BY-TEST: `test_break_ui.py::test_trigger_refuses_while_a_backup_or_rollover_runs_and_never_queues`.
- Nightly LAN run active: refused the same way for cloud (`_busy_detail` loops over legs). COVERED-BY-TEST: `test_ov7_dashboard.py::test_manual_backup_refused_while_the_other_leg_runs`.
- Previous MANUAL run still SCHEDULED: counted as active when its due time is within 60 s or unset (`ui.py:275-290`).
  A manual run is created due now, so it counts immediately. COVERED-BY-TEST: `test_f2_trigger_remediation.py::test_guard_scheduled_due_now_is_active` (213),
  `test_guard_scheduled_far_future_is_ignored` (224), `test_guard_scheduled_missing_due_time_is_fail_closed` (232).
- Double click: the browser `confirm()` blocks a second click; after OK the button is disabled (`dashboard.js:23`). Server side, two
  concurrent POSTs both pass `_busy_detail` before either run exists (no lock, `ui.py:787-826`). The slot then serialises the two runs.
  COVERED-BY-TEST (sequential only): `test_break_ui.py::test_double_click_starts_one_backup_and_the_sixth_start_is_rate_limited` (270).
  Slot serialisation is covered by `audit_lab/test_w5b_scenarios.py::test_w5b_double_trigger` (270) and `audit_lab/test_w8_prefect.py::test_w8_concurrent_triggers_serialise` (436), both at flow level, not endpoint level.
  The endpoint race itself: UNTESTED.
- Prefect server down: `_is_running` -> None -> 503 (no run created). CONFIRMED-FROM-CODE. COVERED-BY-TEST: `test_verify_trigger.py::test_prefect_unreachable_fails_closed` (124),
  `audit_lab/test_w8_prefect.py::test_w8_dashboard_trigger_fails_closed_without_prefect` (309).
  Note: the trigger starts with `arun_deployment` only if the API answered; if the API drops between the check and `arun_deployment`, the
  exception path returns 500 `trigger_failed` (`ui.py:819-825`).
- Agent down (Prefect API up): `arun_deployment` creates the run; no worker picks it up. The trigger waits 180 s and returns 202
  "The Cloud backup started" (`ui.py:403-407`, `messages.act`). The run stays SCHEDULED, is due, so `_is_running` says RUNNING
  (`ui.py:275-290`), and later triggers are refused "Wait for the running backup". When the agent returns, the queued run starts.
  No expiry is set in code (no expiry or scheduled-time argument in `arun_deployment` calls, `ui.py:813-818`, `ui.py:919-926`, `ui.py:1576-1580`).
  Prefect's own handling of a late-started manual run: UNCLEAR (not in this repo). Manual runs count their budget from their actual
  start (`flow.py:475-488`, `1628-1634`), so such a run gets a full budget. UNTESTED.
- Agent down, FY card view: no RUNNING row exists yet (written by the flow), so the FY card shows idle while the top card shows
  "Copying files ... now". See D.

### A4. Mail and dashboard summary

- Failure: one alert per failed run, `notify` key `backup:<fy>:cloud:<status>` (`flow.py:332-352`). Verify failure and PARTIAL have their own alerts (1562-1617, 1859-1867).
- Blocked (guard): `_open_block_request` (`flow.py:1150-1199`) sends one ACTION REQUIRED mail; the approval card appears on the
  dashboard via `GET /approvals` polled every 10 s (`dashboard.js:625`).
- Success: no mail (except RECOVERED). Dashboard: the top card and the cloud FY card show the run label and description.

---

## B. LAN (NAS) button: `POST /trigger/lan`

### B1. Chain

1. Click. `dashboard.js:11-31` with the classic LAN button. The confirm text is built from data attributes (14-19):
   "Start the office NAS backup now? It wakes the NAS, copies the changes, switches the NAS off afterwards." The clauses are added from
   `data-wol` and `data-shutdown` (`dashboard.html:72-73`), set from `ui.py:527-529`: `lan_wol = wol.enabled`, `lan_shutdown = lan.shutdown_after_backup`.
   CONFIRMED-FROM-CODE. The confirm text does NOT mention that changed files on the NAS can be deleted (a /MIR mirror). UNTESTED.
   The per-FY LAN button uses a different confirm with no WoL, shutdown or deletion text: "Start the office NAS backup of FY.. now?" (`dashboard.js:482`).
2. Middleware, auth, rate limit, enabled, `?fy=`, busy check: identical to cloud (`ui.py:829-842`, same helpers).
3. Start. `ui.py:842-846` `arun_deployment("aam-backup/backup-lan", ...)`. Same 500/202/200 mapping (`ui.py:381-407`).
4. Flow. `backup(mode="lan")` (`flow.py:3494`), folders (3566), then `_run_leg_folders("lan")` (3587-3591).
   - Slot and RUNNING row as cloud (3442-3449). `allow_shutdown=False` and a shared `votes` list are passed to every folder (3468-3470).
   - After the loop, `_lan_shutdown_decision` (3369-3380) decides once: shut down only if every folder ran and voted yes; otherwise the NAS stays on
     (`flow.py:3374-3379`). CONFIRMED-FROM-CODE. COVERED-BY-TEST: `test_ov3_orchestration.py::test_one_shutdown_after_both_folders_complete` (178),
     `test_no_shutdown_when_a_folder_says_stay_on` (183), `test_no_shutdown_when_the_second_folder_never_ran_or_voted` (189),
     `test_crash_before_the_second_folder_means_no_shutdown_and_the_flow_raises` (195), `test_a_cancel_makes_no_nas_decision` (274).
5. LAN pipeline (`flow.py:1989-2281`):
   a. Budget: `_missed_if_no_budget` (2011-2013). Slot (2014). Rule: no slot, no WoL.
   b. `health_check_task` (2054).
   c. WoL: `wol_check_task` (1053-1060) -> `core/wol.ensure_server_online` (`core/wol.py:133-171`):
      - WoL disabled: skipped (`flow.py:1056-1058`, `wol.py:139-141`).
      - SMB already usable (TCP 445 plus stat of the canary path): returns at once, no packet sent (`wol.py:147-154`). CONFIRMED-FROM-CODE.
        COVERED-BY-TEST: `test_m2_smb_ready.py::test_already_online_requires_usable_share` (124). The `test_wol_comprehensive.py::TestEnsureServerOnline::test_already_online` test has NO assertion.
      - Otherwise: magic packets, `wake_retry_count` rounds, global and subnet broadcast (`wol.py:41-88, 157-162`). Then `wait_for_server` (98-123):
        up to `wake_timeout_seconds` (default 900, `models/config.py:199-201`) for SMB readiness plus `stability_wait`.
        Timeout -> `WolTimeout`, code AAM-NAS-02 (`wol.py:121`).
      COVERED-BY-TEST: `test_m2_smb_ready.py::test_port_open_but_share_unusable_triggers_wake_with_config_timings` (136),
      `test_m2_smb_ready.py::test_wake_timeout_message_carries_the_reason` (153), `test_wol_comprehensive.py::TestWaitForServer::test_timeout_raises` (38).
   d. Early FY folder (only when the folder is the next year): `ensure_overlap_nas_folder` (2056-2068).
   e. `_complete_pending_rollover_nas_folder` (2071).
   f. `lan_preflight_task` (2072, `flow.py:1063-1083`) = `core/lan_preflight.run_lan_dry_run`: SMB probe with `preflight_smb_attempts`,
      canary `\\nas\...\.AAM_TARGET_MOUNTED` must be a file, then `robocopy /L /MIR /XJ` (`lan_preflight.py:151`), read-only listing.
      Failure -> AAM-LAN-01 (`flow.py:1080-1082`). This is the "/L preview". CONFIRMED-FROM-CODE.
   g. `lan_snapshot_before_task` (2073). Walk failure -> None (the destination count is unknown).
   h. `lan_guards_task` (2074-2078, `flow.py:1200-1280`):
      - Counts from /L; a missing or inconsistent /L summary raises (1220-1230) -> run fails, nothing copied.
      - G1 source has a real file (`guards.py:75`), G2 source shrink vs baseline (`guards.py:92`), G4 destination identity (`guards.py:210`),
        G3 mass delete = planned deletes from /L vs destination files (`guards.py:138`), G5 mount canary (`guards.py:244`, `destination_files > 0`).
      - `_apply_approval` (`flow.py:1104-1147`) may override blocked guards with a human approval; consumed atomically before any copy.
      - `guards.enforce` raises `GuardBlocked` (`guards.py:306, 326`) -> BLOCKED.
      COVERED-BY-TEST: `test_w26d_guards.py`, `test_m2_guards.py`, `test_m2_guard_wiring.py` (names not read in detail), `test_sim_e2e_09_block_approved.py`,
      `test_sim_e2e_10_block_unapproved.py`, `test_ov4_approvals_per_fy.py::test_apply_approval_refuses_the_other_fy_and_does_not_consume_it` (93).
   i. `lan_access_task` (2079): probes read, write, free space (`permissions.py`).
   j. Sync (2095-2105): `_sync_with_retries` -> `lan_sync_task` (`flow.py:1322-1357`) = `robocopy /MIR /Z /ZB /XJ /MT ... /UNILOG` (`core/lan_sync.py:388-420, 435-477`).
      Only LAN_FAILED (not timed out) and LAN_SUSPECT are retried, after re-measuring G1-G5 (`flow.py:1355-1356`, `2101-2102`).
      LAN_PARTIAL is NOT retried (it returns, not raises).
      Budget stop -> LAN_PARTIAL with `budget_stopped` (1333-1354).
   k. Post: `lan_snapshot_after_task` (walk failure is not fatal, 1298-1317), `lan_record_task` (1360-1383).
   l. Shutdown decision (2162-2200), with `allow_shutdown=False` in the normal loop path, so this is a VOTE:
      - LAN_COMPLETE: `_vote(True)` (2166-2170). Shutdown happens at the loop end (`_lan_shutdown_decision`).
      - LAN_PARTIAL: alert first (2171-2194), then `_vote(shutdown_on_all_retries_exhausted)` (2174-2175).
      - Anything else (LAN_SKIPPED, SUSPECT, FAILED): `_vote(False)` (2198-2200).
   m. Terminal (2204-2208): non-success -> PartialRun -> Prefect FAILED.
   n. Exception path (2211-2249): alert once (2227-2237, except GuardBlocked). Shutdown only if `shutdown_on_all_retries_exhausted`
      and the exception is not a PartialRun (2222-2226, 2238-2242, vote 2224-2226). See D3.
   o. `finally` (2250-2281): `_record_run` with `nas_shutdown` metrics (2257-2258), BLOCKED -> `_open_block_request`, success -> recovery mail.

2. The actual switch-off: `lan_shutdown_task` (`flow.py:1386-1416`) runs only if `lan.shutdown_after_backup` AND `wol.enabled` (1394-1396).
   `core/shutdown.shutdown_server` (`core/shutdown.py:59-117`):
   - `shutdown /s /m \\ip /t <shutdown_delay_seconds> /f` (76). The default delay is 300 s (`models/config.py:219-221`).
   - Exit 0 (accepted) or 1190 (already scheduled) -> poll port 445 every `ping_interval_seconds` until it stays closed for `closed_probes`,
     within `shutdown_verify_timeout_seconds` (default 600, must exceed the delay, `models/config.py:224-228, 251-257`) -> VERIFIED (104-107).
   - Still open at the window end -> NOT_VERIFIED (112-117). Other exit codes or a failed command -> FAILED (79-95).
   - Not VERIFIED -> WARNING mail with AAM-NAS-06 (`flow.py:1407-1415`) and an amber attention item (`core/attention.py:322-324`).
     The backup status is NOT changed. CONFIRMED-FROM-CODE.
   - The backup slot is held during the whole verify window (ARCHITECTURE 3.3, lines 195-199; `flow.py:3444`). A queued job waits up to delay + verify window (about 15 min by default).
   COVERED-BY-TEST: `test_m4_shutdown.py::TestShutdownServer` (89-135, fake clock and injected probe), `test_m4_shutdown.py::TestLanShutdownTask` (167-197),
   `test_m4_shutdown.py::test_lan_run_row_records_the_shutdown_result_and_status_stays_complete` (221),
   `test_m4_shutdown.py::test_dashboard_flags_an_unverified_nas_shutdown` (248),
   `test_lan_shutdown_policy.py` (62-193, mocked pipeline), `test_a4_final_lan_no_shutdown.py::test_shutdown_follows_allow_shutdown_at_every_call_site` (44).

### B2. Answers to the specific questions

- WoL when the NAS is OFF: magic packets to 255.255.255.255 and the subnet broadcast (`wol.py:41-88`), then wait up to 900 s for SMB
  readiness (CONFIRMED-FROM-CODE). Real wake is UNTESTED on loopback (see E).
- SMB readiness: `core/smb_ready.wait_for_smb_ready` (TCP 445 plus stat of the canary, then stability wait). COVERED-BY-TEST: `test_m2_smb_ready.py` (42-115; one real loopback TCP check at 83-92, the rest fake clock).
- Guards G1-G5 with the /L preview: `lan_preflight` runs `/L /MIR`; its EXTRA counts feed G3 and its totals feed G2 and G1 (`flow.py:1218-1260`). CONFIRMED-FROM-CODE.
- /MIR: only after G1-G5 and `lan_access_task` pass (`flow.py:2074-2079`).
- Shutdown after backup, conditions: LAN_COMPLETE (or a vote), `lan.shutdown_after_backup`, `wol.enabled`. Failure cases only with `shutdown_on_all_retries_exhausted`
  (default False, `models/config.py:138-145`). CONFIRMED-FROM-CODE.
- WoL times out: `WolTimeout` -> exception path, status LAN_FAILED (phase "pre", `core/statuses.py:261-289`), alert with the NAS note, no shutdown unless the flag is set.
  With the flag set the code still calls the shutdown command against the unreachable NAS (`flow.py:2238-2242`); that results in FAILED and a WARNING mail. UNTESTED for this combination.
- NAS already on: no WoL sent (`wol.py:147-154`), the copy runs, and after LAN_COMPLETE the NAS is switched off anyway. Nothing in code records "this run woke it"
  (`lan_shutdown_task`, `flow.py:1386-1416`). Whether that is intended: UNCLEAR (ARCHITECTURE 633 states the rule without this case). No test asserts either choice.
- A guard blocks: BLOCKED row, one ACTION REQUIRED mail (`flow.py:1150-1199`, no NAS wording in that mail), approval card on the dashboard (`dashboard.js:575-625`).
  The button answers with HTTP 500 "ended as Failed" (`ui.py:398-402`, because PartialRun/GuardBlocked make Prefect FAILED). See D8.
  Approval flow: `POST /approvals/{id}/approve` (`ui.py:944-970`): sign-in, rate limit (877-881), typed phrase APPROVE (`core/approvals.py:60`, `ui.py:954-956`),
  FY still active (957-960), `_ensure_leg_idle` (961; busy refusal 409 before consuming), `approve_request` (964-966), `_start_approved_run` with `approval_id` and `fy` (970, 915-934).
  The run consumes the approval atomically (`flow.py:1104-1147`, `core/approvals.py:73-151`): same leg, guard, FY, not used/dismissed, not expired,
  G1/G4/G5 identical, G2/G3 within tolerance. Start failure keeps the approval APPROVED and `/approvals/{id}/run` retries (`ui.py:930-933, 973-984`).
  COVERED-BY-TEST: `test_m2_approval_dashboard.py::test_approve_with_phrase_records_who_and_reruns_once_with_approval_id` (72),
  `test_leg_busy_or_unknown_changes_nothing` (123), `test_trigger_failure_keeps_approval_and_run_retries_it` (130), `test_run_refuses_pending_used_and_expired` (150),
  `test_break_ui.py::test_two_simultaneous_approve_clicks_start_exactly_one_run` (370), `test_ov4_approvals_per_fy.py` (30-131).
- robocopy partial (exit codes with bit 3 or 2 on a complete log): LAN_PARTIAL, alert with NAS note, no shutdown unless the flag is set, no retry.
  Button: 500 "ended as Failed". `core/lan_sync.decide_lan_result` (215-); COVERED-BY-TEST: `test_lan_shutdown_policy.py::test_default_f3_partial_skips_shutdown_and_alerts` (62).
- Shutdown fails: `shutdown` refused (e.g. access denied, exit 5) -> FAILED with the tool text, WARNING mail, run status unchanged. Button: 200 "completed".
  NAS left on. COVERED-BY-TEST (fake): `test_m4_shutdown.py::TestShutdownServer::test_access_denied_is_failed_with_the_tool_text_and_no_polling` (110).
- Does a failed run leave the NAS on, and is it visible: YES by default (flag False) after PARTIAL, FAILED, BLOCKED, a WoL timeout or a preflight failure
  (`flow.py:2166-2200`, `2222-2242`). It is visible in the failure mail ("The NAS was NOT shut down; the next scheduled run will re-sync", `flow.py:371-382`)
  and in the run row text. It is NOT a persistent dashboard item: `core/attention.py:322-324` only flags an unverified shutdown.
  The BLOCKED mail says nothing about the NAS (`flow.py:1150-1199`). UNTESTED for the dashboard view.

---

## C. "Both" / "all" and `?fy=`

- No "both legs" backup button and no `/trigger/cloud` or `/trigger/lan` with `all`. `serve.py:35-55` registers only `backup-cloud` and `backup-lan`.
  `backup(mode="all")` exists in the flow (`flow.py:3495`) but no button or route starts it. CONFIRMED-FROM-CODE.
- "Check both copies now" is the safety check, not a backup: `dashboard.html:118` -> `triggerVerify('all')` (`dashboard.js:374-395`) ->
  `POST /trigger/verify/all` (`ui.py:1536-1589`) -> `integrity-audit` with `mode=all` (`ui.py:1576-1578`), which runs both legs under ONE slot
  (`ARCHITECTURE.md` section 3.x audit, `flow.py:3114`). COVERED-BY-TEST: `test_manual_verify_button.py` (string checks only, lines 16-40),
  `test_verify_trigger.py::test_enabled_modes_proceed_with_mode_parameter` (76).
- Overlap, backup buttons: while two folders are shown the classic card (and its no-`fy` button) is hidden (`dashboard.js:460-461`).
  Each FY card button sends `?fy=<that folder>` (`dashboard.js:450-451, 485`), so one click = one folder = one flow run.
  "Both folders" therefore needs two clicks, and the second is refused while the first runs. CONFIRMED-FROM-CODE.
  COVERED-BY-TEST: `test_ov7_dashboard.py::test_backup_trigger_passes_the_active_fy` (152), `test_backup_trigger_without_fy_sends_no_parameters` (173), `test_dashboard_draws_one_card_per_fy` (230).
- `?fy=` handling (`ui.py:746-758`): trimmed; case-insensitive match against `_active_fy_list()` (`ui.py:623-635`); the CANONICAL name is passed on (`ui.py:816`).
  Anything else -> 409 with the list of active folders, BEFORE the busy check. The flow re-checks (`flow.py:3571-3575`) and raises ValueError if it is not active.
  COVERED-BY-TEST: `test_break_ui.py::test_fy_parameter_must_name_an_active_folder_and_is_never_passed_through` (288, includes `../../etc`, SQL text, CRLF, 5000 chars),
  `test_break_ui.py::test_a_valid_fy_is_canonicalised_before_it_reaches_the_deployment` (293), `test_ov3_orchestration.py::test_manual_fy_must_be_active_and_narrows_the_run` (135),
  `test_ov7_dashboard.py::test_backup_trigger_refuses_an_fy_that_is_not_active` (162), `test_verify_trigger_passes_fy_and_refuses_inactive` (205),
  `test_ov7_dashboard.py::test_flows_accept_fy_but_the_rollover_check_does_not` (220).
- Race on `fy` (UNCLEAR, low): the UI decides the active set at request time; if the FY switch happens between the UI check and the flow's own check,
  the flow refuses (`flow.py:3572-3574`). This is fail-closed, not a data risk. UNTESTED.
- "Check now" backup-first question (`ui.py:1507-1533`): for `mode=all` or a single leg it looks at the CONFIGURED FY only
  (`fy or _configured_fy()`, `ui.py:1519`). During overlap the early folder is not considered. UNTESTED for overlap; `test_verify_backup_first.py` (52-99) has no overlap case. See D10.

---

## D. Findings, ranked by risk

Risk = likelihood times impact. Operational/safety first.

**D1. High (ops): a manual start with the agent down reports success and the dashboard says the copy is running. No expiry on the queued run.**
- `ui.py:403-407` 202 "The Cloud/Office backup started" with the agent down.
- `ui.py:275-290` a due SCHEDULED run counts as running; `ui.py:573-574` and `dashboard.js:282, 313` show "Copying ... now".
- `ui.py:638-677` (FY card) shows idle (no RUNNING row yet). Top card and FY card disagree.
- The queued run starts when the agent returns, at an unplanned time, with a full budget (`flow.py:475-488`, `1628-1634`). For LAN that means a WoL, copy and possibly a switch-off in office hours.
- Test: none (UNTESTED). Audit-lab W8 covers "fails closed without Prefect" only (309), not agent-down.

**D2. Medium-high (safety perception): the NAS is switched off after a manual LAN run that did not wake it.**
- `flow.py:2166-2170` and `1386-1416`; `wol.py:147-154` returns early when the NAS is already up. Nothing records the wake.
- The confirm text says "switches the NAS off afterwards" (`dashboard.js:17`, `dashboard.html:73`) without the caveat.
- Intent: UNCLEAR. ARCHITECTURE 633 states the rule without this case. No test asserts either choice.
- The owner should decide: switch off only if this run woke the NAS, or always. The staff NAS-use case: a 14:00 manual run switches off a NAS in use after the 300 s delay (`models/config.py:219-221`).

**D3. Medium: the failure-rule switch-off also fires for a guard BLOCK.**
- `flow.py:2222-2226` and `2238-2242`: `shutdown_on_fail = allow_shutdown and isinstance(e, Exception) and flag`; only PartialRun is excluded.
- `GuardBlocked` is `RuntimeError` (`core/guards.py:306`), not PartialRun, so a BLOCKED run with the flag set shuts the NAS down.
- The config text (`models/config.py:138-145`) names only LAN_PARTIAL and LAN_FAILED.
- Default is False, so impact depends on the setting. UNTESTED for this combination (`test_lan_shutdown_policy.py` and `test_a4_final_lan_no_shutdown.py` use no BLOCKED status).

**D4. Medium (UX): the classic "Back up" button can stay disabled at "Starting..." after a run that finished inside the 180 s wait.**
- `dashboard.js:23` disables and fades; `:27` success path only toasts; `:30` calls updateStatus.
- `:277` and `:308` reset the button only when its text is NOT "Starting...", and the reset is inside that `else if`. A run that is already done is not "running", so nothing resets it.
- Result: the button keeps `pointer-events:none` and opacity 0.5 until a page reload. The FY cards are not affected (`renderFolders` rebuilds them).
- Triggered by any run that completes within 180 s (`ui.py:374-378`). UNTESTED (no JS behaviour tests; `test_manual_verify_button.py` checks strings only).

**D5. Medium-low (race): the start check and the run creation are not atomic.**
- `ui.py:787-826` and `838-854`: `_busy_detail` awaits three Prefect reads, then `arun_deployment` creates the run. Two concurrent POSTs (two tabs, a retry) can both pass.
- Consequence: two back-to-back runs, each with its own NAS wake and switch-off. The second can end `LAN_SLOT_TIMEOUT` if the first holds the slot for more than `concurrency_wait_seconds` (3600 s default, `flow.py:232`).
- The slot prevents overlap of the copies themselves. Approvals have an atomic DB update (`ui.py:964-967`, tested).
- Test: sequential only (`test_break_ui.py:270`). Flow-level slot tests: `audit_lab/test_w5b_scenarios.py:270`, `audit_lab/test_w8_prefect.py:436`. The endpoint race: UNTESTED.

**D6. Medium-low (UX text, safety-relevant): the LAN confirm text does not say that /MIR deletes files on the NAS that are gone from the source.**
- `dashboard.js:14-19`, `482` (per-FY). Guards G3 limit this, but the operator is not told.
- The cloud confirm (`dashboard.js:18`) says nothing about deleting in the bucket (`rclone sync`). UNTESTED.

**D7. Medium-low (UX text): the per-FY confirm omits WoL, switch-off and deletion; in overlap it is the ONLY manual route.**
- `dashboard.js:482`: "Start the office NAS backup of FY27-28 now?" vs classic text at 14-19. UNTESTED.

**D8. Low-medium (UX): a BLOCKED or PARTIAL run is reported to the button as "ended as Failed".**
- `ui.py:398-402` (500, "Open Recent backups to see why"). The approval card appears only on the 10 s poll (`dashboard.js:625`).
- The message does not point at the approval. UNTESTED for the text.

**D9. Low-medium (ops): a long manual cloud run blocks a queued LAN run until it times out.**
- Slot wait is `concurrency_wait_seconds` (3600 default, `flow.py:232`, `models/config.py:606-615`). A manual cloud run longer than that makes a queued LAN run end `LAN_SLOT_TIMEOUT` (`flow.py:3351-3366`).
- The schedule validator (`models/config.py:1243-1245`) covers the schedule only, not manual runs.
- Operator note already in `docs/INSTALL_FRESH.md:190`. UNTESTED for a manual run.

**D10. Low: the backup-first question ignores the early folder during overlap.**
- `ui.py:1519` uses `fy or _configured_fy()`; with "Check both" during overlap the next-year folder is never asked about. UNTESTED for overlap.

**D11. Low: per-FY "running" can show a stale RUNNING row while Prefect is unreachable.**
- `ui.py:661` is `running = row_running and scheduler is not False`. When the scheduler is None (unknown) the row decides. The docstring (`ui.py:640-642`) says the scheduler must agree. Contradiction. UNTESTED for the None case.

**D12. Low: a failed run leaves the NAS on with no persistent dashboard item.**
- See B2. Visible only in the mail and the run row. The BLOCKED mail does not mention the NAS (`flow.py:1150-1199`).

**D13. Low (info for the owner): a successful manual run sends no mail.**
- Only RECOVERED after a failure (`flow.py:393-416`). Not a bug per the code; the confirm and the toast say nothing about mail either. UNTESTED for the "no success mail" rule.

**D14. Low: `ui.py:406` "Leave this page open to follow it" and "started" wording.**
- The run continues after the page is closed, so "leave this page open" is advice, not a requirement. Shown only for 202 (`messages.act`). Low.

**D15. Low (docs): ARCHITECTURE.md is out of date in two places.**
- Section 3.3 (line 166) shows `with _backup_slot(config):` around the whole LAN pipeline. The slot is now held per leg by `_run_leg_folders` (`flow.py:3444-3446`); pipelines take `slot_held=True`.
- Section 3.3 (SMB probe) says "HARD-CODED 2 s x 4"; the flow passes `preflight_smb_attempts` from config (`flow.py:1074-1075`).

**D16. Low (test quality): `test_wol_comprehensive.py::TestEnsureServerOnline::test_already_online` has no assertion.**

**D17. Low (defence in depth): the cross-site check runs only when `Origin` or `Sec-Fetch-Site` is present.**
- `ui.py:83-96`. A non-browser client with a valid session cookie passes. The dashboard session cookie is `samesite=lax` (`ui.py:471-474`), the admin cookie is `strict` (`ui.py:1086`). Not a manual-button defect; noted.

**Checked, and not a problem:**
- No-changes nights do switch off the NAS: `core/lan_sync.decide_lan_result` (215-) only yields LAN_COMPLETE, LAN_PARTIAL, LAN_SUSPECT, LAN_FAILED. `LAN_NO_CHANGES_COMPLETE` is legacy only (`core/statuses.py:71-72`).
- A slot timeout never wakes the NAS: the slot is taken before WoL (`flow.py:2014` before `2055`; `flow.py:3446-3449` returns early).
- A missing folder vote keeps the NAS on (`flow.py:3374-3379`).
- A cancel makes no NAS decision (`flow.py:3475-3476`; `test_ov3_orchestration.py::test_a_cancel_makes_no_nas_decision`).
- `?fy=` is never passed through raw (tested, section C).

---

## E. Live test steps on the test stack

Scope: test stack only, real GCS bucket `aam-backup-qa-bilcvx`, TEST PREFIXES ONLY, loopback NAS share.
Do NOT run these on the production trial box without telling the owner first (CLAUDE.md: stopping a backup or touching backup data needs the owner).

**Warning, shutdown on loopback.** `shutdown /m \\127.0.0.1` shuts down the machine that runs the test. The audit lab patches
`shutdown_server` to raise for this reason (`audit_lab/labconf.py`, per the docstring of `test_w16_flow_first.py:786`).
On the QA rig keep `lan.shutdown_after_backup: false` for every loopback run. Never point `wol.server_ip` at 127.0.0.1 with shutdown on.

Cloud (E-C):
1. C1 happy path. Source with a few small files, cloud enabled, test prefix. Click "Run Cloud Backup" (the classic card; a single folder).
   Expect: confirm text; toast "completed"; run row `CLOUD_COMPLETE` (or `CLOUD_NO_CHANGES_COMPLETE` on a second click); no success mail.
   Observe: the button after the toast (expected defect D4: stays "Starting..." until reload).
2. C2 busy refusal. Make the source large so the run lasts minutes. Click again during the run. Expect 400 "Wait for the running backup to finish".
3. C3 double start. With `curl` and the session cookie, send two POST /trigger/cloud?wait=false at the same moment (parallel). Record the statuses. Expected per the code: one 202 and one 400, or two 202s (D5). Count the flow runs in Prefect.
4. C4 Prefect down. Stop AamPrefectServer on the test stack. Click. Expect 503 "The scheduler is not answering", no run row, and the button restored.
5. C5 agent down (D1). Stop the agent service (launch.py) with Prefect up. Click. Expect 202 "started" after about 180 s. Check the top card (expected defect: "Copying files to the cloud now"). Restart the agent. Record how long the queued run takes to start, and whether it starts.
6. C6 failure mail. Use a test config with a wrong rclone credentials path. Click. Expect `CLOUD_FAILED`, one failure mail (`backup:<fy>:cloud:CLOUD_FAILED`), toast 500 "ended as Failed".
7. C7 verify gate. Make a size mismatch on purpose (edit a test object in the test prefix). Run "Check now" for cloud, then a backup. Expect the safety check to show the difference and the backup to leave the bucket unchanged for that file.
8. C8 overlap (only with the rehearsal config): `tools/fy_overlap_rehearsal.py` dry run first. Expect two FY cards, each with its own Back up button sending `?fy=`. Send `?fy=FY99-00` by hand: expect 409 with the active list. Expect the classic card hidden.

LAN (E-L):
9. L1 happy path, loopback share, `wol.enabled: false`, `shutdown_after_backup: false`. Click "Run LAN Backup". Expect: confirm WITHOUT "wakes the NAS" and WITHOUT "switches the NAS off" (note the D2/UI-text question); LAN_COMPLETE; no NAS action in the run row (`nas_shutdown` absent).
10. L2 NAS already on (WoL on, loopback SMB reachable). Expect the log line "already online (SMB usable)" and no magic packet logged. Shutdown stays off (see warning).
11. L3 guard BLOCK on G3: delete more source files than `guards.max_delete_files` / `max_delete_percent` allows (in the test copy only). Click. Expect: run BLOCKED, one ACTION REQUIRED mail, approval card on the dashboard, toast 500. Type APPROVE, approve: expect one run, approval `USED`, backup completes. Click "Run approved backup" again: expect 409.
12. L4 G5 canary: remove `.AAM_TARGET_MOUNTED` from the test NAS folder. Expect G5 BLOCK; approve; expect the marker recreated and the run to continue.
13. L5 robocopy partial: open a destination file with an exclusive handle during the run. Expect LAN_PARTIAL, alert, no retry, no switch-off (flag false), toast 500 "ended as Failed".
14. L6 WoL timeout (no real NAS needed): set `wol.enabled: true`, `server_ip` to an unused TEST-NET address (for example 192.0.2.1), `wake_timeout_seconds: 60` (minimum). Shutdown stays off. Click. Expect WolTimeout AAM-NAS-02, LAN_FAILED after about 60 s, failure mail with "The NAS was NOT shut down", NAS sections untouched.
    With `shutdown_on_all_retries_exhausted: true` as well: expect the attempted shutdown to fail, WARNING mail AAM-NAS-06 (D3-related behaviour, check it).
15. L7 LAN_SLOT_TIMEOUT (D9): set `maintenance.concurrency_wait_seconds: 60`. Start a long cloud run. Start a backup-lan run directly through Prefect (not the dashboard, which refuses). Expect LAN_SLOT_TIMEOUT after about 60 s, no WoL attempted, alert per folder.
16. L8 queued run while the agent is down (D1, LAN): as C5 but for LAN. Confirm that the queued LAN run does not wake the NAS until the agent returns.
17. L9 approval race: send two simultaneous POST /approvals/<id>/approve. Expect one run (existing test, re-check on the rig).

Cannot be tested on loopback. Needs a NAS that is really powered off, or a fake:
- Real WoL packet and cold-boot wait (NAS off, on the same LAN). Covered only by fakes: `tests/test_wol_comprehensive.py` (mocked), `tests/test_m2_smb_ready.py` (fake clock; one loopback TCP check), `audit_lab/test_w17_ops.py:77-102` (port probe with monkeypatch), `audit_lab/test_w18_ops.py:596, 634` (SMB reachability, dry run with SMB unreachable), `audit_lab/test_w5c_ops.py:137` (SMB false positive).
- NAS power-off and the verify window (a real switch-off through shutdown.exe): fakes only. `tests/test_m4_shutdown.py` (fake clock, injected probe), `tests/test_lan_shutdown_policy.py` (mocked pipeline), `audit_lab/test_w16_flow_first.py:786` (`test_w16_shutdown_command_contract`: Popen faked, argv `shutdown /s /m \\ip /t <delay> /f`, FAILED on a refused command). Real power-off: deferred until the owner is at the NAS (memory note, 2026-10-18).
- A real `shutdown.exe` refusal (exit 5, access denied) needs a second machine or a non-admin account: fake only (`test_m4_shutdown.py:110`).
- NAS off during a rollover, then next LAN night: `audit_lab/test_w6b_rollover_nas_off.py:18` (rig).
- Real NAS-side behaviour of a staff member cancelling with `shutdown /a` during the 300 s delay: not testable without the NAS.

---

## F. Existing tests that cover triggers, manual runs, WoL and shutdown

Unit / UI (tests/):
- `test_break_ui.py`: `test_every_route_refuses_a_caller_without_a_session` (117), `test_trigger_refuses_while_a_backup_or_rollover_runs_and_never_queues` (244), `test_verify_refuses_while_a_safety_check_or_backup_runs_even_with_anyway` (251), `test_trigger_answers_503_and_starts_nothing_while_prefect_is_silent` (263), `test_double_click_starts_one_backup_and_the_sixth_start_is_rate_limited` (270), `test_fy_parameter_must_name_an_active_folder_and_is_never_passed_through` (288), `test_a_valid_fy_is_canonicalised_before_it_reaches_the_deployment` (293), `test_garbage_wait_parameter_gets_the_configured_timeout_not_the_raw_value` (303), `test_verify_mode_path_parameter_accepts_only_three_words` (311), `test_rollover_run_refuses_while_a_rollover_runs_or_prefect_is_silent` (318), approval tests (340-419), `test_cross_site_post_with_a_valid_session_is_refused` (530).
- `test_f2_trigger_remediation.py`: `test_trigger_wait_constant_is_180_seconds` (118), `test_trigger_timeout_wait_false_variants_return_zero` (125), `test_trigger_timeout_default_is_the_180s_bound` (130), mapping tests (136-186), guard tests (203-268, scheduled/pending/unknown/API-unreachable), endpoint tests (278-361: `test_endpoint_wait_false_returns_202_with_run_id` 278, `test_endpoint_default_wait_passes_180s_bound` 299, `test_endpoint_completed_run_is_200_completed` 314, `test_endpoint_failed_run_is_500_failed_not_triggered` 323, `test_endpoint_running_guard_returns_400_without_creating_run` 334, `test_endpoint_api_unreachable_refuses_with_503` 347, `test_endpoint_creation_failure_is_500_trigger_failed` 361).
- `test_verify_trigger.py`: `test_invalid_mode_rejected` (50), `test_disabled_legs_rejected_without_deployment` (57), `test_enabled_modes_proceed_with_mode_parameter` (76), `test_refuses_while_backup_running` (95), `test_refuses_while_verify_running` (109), `test_prefect_unreachable_fails_closed` (124), `test_trigger_creation_failure_is_500` (132), `test_wait_false_passes_zero_timeout` (146).
- `test_ov7_dashboard.py`: `test_one_fy_status_is_todays_payload_plus_one_folder` (87), `test_running_comes_from_the_running_row_of_that_fy` (121), `test_backup_trigger_passes_the_active_fy` (152), `test_backup_trigger_refuses_an_fy_that_is_not_active` (162), `test_backup_trigger_without_fy_sends_no_parameters` (173), `test_manual_backup_refused_while_the_other_leg_runs` (180), `test_manual_backup_refused_while_a_safety_check_runs` (188), `test_manual_backup_fails_closed_when_the_scheduler_is_silent` (198), `test_verify_trigger_passes_fy_and_refuses_inactive` (205), `test_flows_accept_fy_but_the_rollover_check_does_not` (220), `test_dashboard_draws_one_card_per_fy` (230).
- `test_ov3_orchestration.py`: see section B1 and C (96-282).
- `test_ov4_approvals_per_fy.py` (30-216), `test_m2_approval_dashboard.py` (63-184), `test_m2_approval_workflow.py`, `test_m2_approvals.py` (names not read).
- `test_ui_rollover_busy.py`: `test_manual_backup_is_refused_while_the_rollover_runs` (30), `test_approved_rerun_is_refused_while_the_rollover_runs` (36), `test_safety_check_is_refused_while_the_rollover_runs` (42).
- `test_verify_backup_first.py`: `test_old_backup_asks_first_and_starts_nothing` (52), `test_recent_backups_start_the_check_straight_away` (62), `test_check_anyway_skips_the_question` (68), `test_a_leg_with_no_good_backup_yet_is_named` (74), `test_only_the_legs_being_checked_are_asked_about` (80), `test_a_broken_pre_check_never_blocks_the_safety_check` (86), `test_the_script_offers_check_anyway_after_the_question` (99). No overlap case.
- `test_manual_verify_button.py`: string checks on template and JS (16-40). No behaviour test.
- `test_ui_auth.py` (sessions, sign-in, rate limit), `test_ui_rollover_busy.py`.
- WoL / SMB: `test_wol_comprehensive.py` (TestSmbPortOpen 18, TestSendMagicPacket 26, TestWaitForServer 37-51, TestEnsureServerOnline 53-70; `test_already_online` has no assertion), `test_wol_no_default_address.py`, `test_m2_smb_ready.py` (42-172), `test_i4_settings_wol_target.py`.
- Shutdown: `test_m4_shutdown.py` (TestShutdownServer 89-135, TestShutdownConfig 143-165, TestLanShutdownTask 167-197, `test_lan_run_row_records_the_shutdown_result_and_status_stays_complete` 221, `test_dashboard_flags_an_unverified_nas_shutdown` 248, `test_dashboard_is_quiet_after_a_verified_shutdown` 257), `test_lan_shutdown_policy.py` (62-193), `test_a4_final_lan_no_shutdown.py` (44-52), `test_slot_orphan.py` (52-76), `test_f4_service_shutdown.py` (Windows service stop, not the NAS).
- Guards: `test_m2_guards.py`, `test_m2_guard_wiring.py`, `test_sim_e2e_09_block_approved.py`, `test_sim_e2e_10_block_unapproved.py`, `test_break_release_approvals.py` (names not read in detail).

Audit lab (audit_lab/, run separately, ~20 min):
- `test_w8_prefect.py`: `test_w8_dashboard_trigger_and_duplicate_guard` (232), `test_w8_dashboard_trigger_fails_closed_without_prefect` (309), `test_w8_concurrent_triggers_serialise` (436, flow-level slot).
- `test_w5b_scenarios.py::test_w5b_double_trigger` (270, flow-level).
- `test_w19_ui.py`: `test_w19_trigger_result_branches` (291), `test_w19_trigger_timeout` (313), `test_w19_trigger_cloud_branches` (358), `test_w19_trigger_lan_branches` (402), `test_w19_trigger_verify_branches` (431).
- `test_w16_flow_first.py`: `test_w16_shutdown_command_contract` (786, fake Popen), `test_w16_ui_trigger_verify_endpoint` (1378).
- `test_w17_ops.py` (77-102 SMB port with monkeypatch), `test_w18_ops.py` (596 `test_w18_is_smb_reachable`, 634 `test_w18_lan_dry_run_smb_unreachable`), `test_w5c_ops.py:137`, `test_w6b_rollover_nas_off.py:18`, `test_w24_server_restart.py:99` (slot freed after server kill), `test_w25_fy_overlap.py` (overlap scenario).
- Known intentional red: `test_w16_schedule_collisions_across_year` (CLAUDE.md), unrelated to these buttons.
