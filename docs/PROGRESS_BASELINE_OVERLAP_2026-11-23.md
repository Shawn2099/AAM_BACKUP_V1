# Progress and handoff - FY overlap on the baseline (written 2026-11-23)

Read this first when resuming. Binding plan: `docs/PLAN_BASELINE_OVERLAP_2026-11-23.md` (rev 2 FINAL, tasks A1-D3). Project rules: `CLAUDE.md`.

## Setup facts
- Branch `baseline-v1-overlap`, cut from tag `baseline-v1` (9461ac2 = version on the trial box). Worktree: `C:\Users\malliath\Desktop\aam_baseline_overlap`. The main checkout `C:\Users\malliath\Desktop\aam_backup` is on branch `fy-overlap` (older, heavy attempt; reference only, never edited by this work).
- Build and test ONLY on this PC (owner, 2026-11-23). The trial box (`ssh innoviztawinserver2016`) is untouched until the owner says we are confident. Push to git only when the owner asks; no secrets scan needed (owner: all passwords are fake and will be rotated).
- No `.venv` in the worktree. Run tests with `C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe -m pytest <files> -q -p no:cacheprovider -o addopts="" --timeout=600` with cwd = the worktree and `PYTHONPATH=.`. Full suite about 5.5 min.
- Known failures that are NOT ours: `tests/test_v113_round3.py::test_r3_repo_config_timeouts` and `::test_r3_loaded_runtime_timeouts` (need the untracked real `config.yaml`). `tests/test_m5_reports_plain.py::test_fy_switch_sends_an_info_email` failed once on untouched code and passed afterwards (flaky, cause not found).
- GitNexus: the worktree is indexed (`node .gitnexus/run.cjs analyze --index-only`; re-index after big changes; `status` may read "stale" because of a runner mismatch, harmless). Run `impact` before editing and `detect-changes --scope all --repo .` before each commit.
- Model split (owner): Haiku builds, Sonnet fixes after testing, Opus only verifies and decides. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (project CLAUDE.md rule; early commits carry the Haiku line).

## Owner decisions (all recorded)
- Real layout: source `E:\FY26-27`, `E:\FY27-28` (drive root parent); NAS `\\nas\backup\FY26-27`, `\\nas\backup\FY27-28`.
- Activation 31 Dec: create next FY source folder (+ placeholder); NAS folder is made by the first next-FY LAN pass after the NAS is awake. Config switch only on 1 April.
- From 1 Jan both FYs are backed up on schedule: cloud FY26-27 then cloud FY27-28, LAN FY26-27 then LAN FY27-28. Manual: four cards (cloud/LAN x two FYs), verification per FY.
- Overlap ON by default in code and every config (a config without the key counts as on).
- April gate: required = every switched-on leg; a "final copy" = successful run of the OLD FY started on or after 1 April 00:00 IST (cloud COMPLETE/NO_CHANGES_COMPLETE after verify, LAN LAN_COMPLETE). Manual run counts. When the last blocker clears the software rolls over by itself on the next daily check. Forced rollover (typed phrase) stays. Both FYs keep being backed up until then.
- Manual-only legs in scope: `cloud.scheduled` / `lan.scheduled` (default true); unscheduled leg works from the UI, no overdue/watchdog alarms, needs one manual final copy after 1 April; cloud runs normally when LAN is disabled or unscheduled.
- Weekly audit covers both FYs in the existing 8 h budget (no new key). Activation needs a proven clock; waits and mails (AAM-ROL-07) if not.
- Log file names of a next-FY pass carry the FY (`..._lan_FY27-28_robocopy.log`), not only the folder.
- No database schema change (user_version stays 5); the next FY's pass is told apart by an FY suffix on run/audit ids.

## Done (commits on `baseline-v1-overlap`)
| Task | Commit | Notes |
|---|---|---|
| plan | b6a19c8, 70cc0ff | plan rev 1, rev 2 FINAL |
| A1 | 640c453 | keys `rollover.overlap_enabled`, `overlap_activate_on` |
| A2 | 55e9041 | `core/fy_overlap.py` pure helpers, `ensure_nas_folder` |
| A3 | ef88f64 | run-log folder name carries FY |
| A4 | 87d417d | DB `fy=` filters, `final_copy_since`, approvals `keep_fy`, overlap/scheduled records |
| A5 | 72d1013 | alerts per FY (`AlertEvent.fy`), watchdog `stale:leg:FY`, AAM-ROL-07 |
| A6 | 9a83294 | `cloud.scheduled`/`lan.scheduled`, `FINAL_COPY_STATUSES` |
| A3b | 13518e5 | FY in run-log file names |
| B1 | c805230 | activation on 31 Dec in `rollover_check_flow` (3 rollover fixtures set `overlap_enabled` false with comments) |
| B2 | bfd5bb2 | pipelines take one FY pass (`FyPass`, per-pass config copy) |
| B3 | 243602a | `backup()` loops FY passes per leg |
| B2 fix | d72f872 | alert keys keep a literal prefix |

Suite after B3: 3229 passed, 2 failed (known), 19 skipped.

## Review fixes landed (Sonnet): eab0aad F1, a423cfb F2, 27758c3 F3, 15cd758 F5, 86a18fd F6, 2e3c00f F7
Suite: 3239 passed, 2 failed (known), 19 skipped. F4 (NAS shutdown when the last LAN pass is missed/skipped) deliberately NOT implemented: a shutdown from `backup()` would run outside the slot and could power the NAS off under a concurrent manual run; failure direction is safe (NAS stays on until next night). Noted in the plan, section 12 - OWNER DECISION pending whether a slot-guarded shutdown-only step is wanted.

## (historic) What was in progress when this file was first written
Sonnet fixer for the Opus review of B2/B3 (verdict PASS WITH FIXES), commits `fix(overlap): B3 review F1..F7`:
- F1 second pass shares the first pass's budget origin when a run starts late (D-7).
- F2 a database error while reading the overlap record must not stop the backup (run configured FY only, end FAILED with a note).
- F3 race guard (config switched mid-run) must end the flow FAILED, not COMPLETED.
- F4 NAS shutdown gap when the last pass is missed/skipped/fails early (fakes only; if unsafe, record under "Found while building").
- F5 `_MISSED`/`_SLOT_TIMEOUT` rows of a next-FY pass carry `fy`/`overlap` metrics.
- F6 one `fake_run_tool` test proving next-FY command lines (`E:\FY27-28` -> `gs://bucket/FY27-28`, `\\nas\backup\FY27-28`).
- F7 abandoned-run sweep alert (`core/backup_repository.py`) carries the FY.
Check `git log --oneline` for which of these landed.

## B4 done (April 1 final-copy rule)
- Owner decision F4 (2026-11-23): the NAS shutdown gap is ACCEPTED - the NAS stays on for a day; no shutdown step (plan section 12 says so).
- `core/fy_overlap.py`: `overlap_open`, `final_copy_state` (per leg: required, scheduled, done, source check/earlier,
  status, outstanding, hint, warning), `outstanding_legs`, `announce_text`, `end_overlap`.
- `flow.py`: `_run_final_backups(skip_unscheduled=, started=)`; `step_final_backups` uses the per-leg state only while the
  overlap is open (baseline call otherwise); forced legs named in the switch mail and the rollover row; `end_overlap`
  after the switch.
- `core/guards.py` `g6_fy_rollover(overlap_active=, hints=)`: text only, verdicts identical (tested).
- `core/fy_rollover.py` `unprotected_new_fy`: silent while the overlap of the next FY is open (AAM-ROL-06).
- `ui.py` `/api/rollover`: `final_copy` while the overlap is open.
- Tests: `tests/test_fy_overlap_rollover.py` (23 cases).

## B5 done (calendar simulation, tests only)
- `tests/test_fy_overlap_calendar.py`: 7 passed, 2 xfail (strict). The scheduled cloud/LAN jobs call the real
  `flow.backup()` (so the FY pass loop runs); pipelines are fakes that record `fy_pass.fy` per pass.
- Passing: one pass per leg until 31 Dec 06:00, then two (configured FY first), back to one after the 1 Apr switch
  (day table + all 190 runs of the window); three windows (2026-27, 2027-28 incl. 29 Feb 2028, 2028-29) each with
  every run's passes checked; clock a day behind on 31 Dec -> WARNING mail, nothing created, activation on 1 Jan;
  server off all of 31 Dec -> activation at the first check on 1 Jan; LAN final fails 1-2 Apr -> BLOCKED twice, manual
  LAN run 2 Apr 14:00 counted, switch on the 3 Apr 06:00 check with no approval (the switch's own LAN final failed);
  unscheduled LAN never run by the check, blocks the switch 1-3 Apr; FY27-28 gets no overdue item before its deadline.
- xfail (strict), verified to fail for the stated reason with `--runxfail`:
  - `test_next_fy_overdue_is_counted_from_activation` - B7: `compute_attention` has no `stale:cloud:FY27-28` item.
  - `test_unscheduled_lan_leg_is_never_overdue_in_the_whole_window` - C2: the manual-only LAN leg raises
    `first_run:lan` from 30 Dec on.
- Bugs found in built code: none.
- Limits: the windows use sibling folders under a temp root (`tmp/source/FY26-27`), not the drive-root layout
  `E:\FY26-27`; the clock is faked per call (direction is not modelled: WorldClock uses abs skew).
  The unscheduled leg is modelled by leaving its cron out of the simulated schedule (C1 rule).
- Runtime of the file: about 5 min.

## B6 done (integrity audit per FY)
- `flow.integrity_audit_flow(config_path, mode, fy=None)`: per audited leg the next FY's pass first, then the configured
  FY, in one slot (`_backup_slot` entered once), on one `RunBudget`. Next-FY rows carry the suffix (`<uuid>-cloud-full-FY27-28`);
  the configured pass keeps today's id. Alert outcome keys `cloud@FY27-28=<status>` (baseline keys unchanged).
- LAN leg: one `wol_check_task` per leg (the task itself does nothing while WoL is off, as before), a failed wake is not
  retried for the second pass, one `lan_shutdown_task` after the last pass (also after a budget-exhausted last pass,
  only when a wake happened). `_pending_context(config, db, leg, fy=None)`: each pass's safety reference is its own FY's
  last success (`last_successful_run(leg, fy=)`).
- `fy=` selects one active FY (inactive -> ValueError before anything runs). Overlap off or no record -> one baseline pass per leg.
- Scheduled vs manual: `_auto_scheduled_run()` (Prefect `auto_scheduled`, same source as the I2 lateness rule). The weekly
  cron run skips a leg with `scheduled: false`; a run started by hand (the dashboard's 'Check now', which sends mode
  `all` too) audits every enabled leg. Outside a flow run it is "by hand" (baseline). Owner-visible decision: the schedule
  flag, not the mode, separates the two.
- A DB error reading the activation record: the configured FY is still audited, the flow ends FAILED (as in backup()).
- Tests: `tests/test_fy_overlap_audit.py` (10 cases): order and single slot; suffixed rows and `latest_audit(fy=)`; mismatch key
  `audit:all:cloud@FY27-28=VERIFICATION_FAILED`; one wake and one shutdown in the order wake/LAN/shutdown; failed wake
  (both LAN passes AUDIT_INCOMPLETE, no second wake, no shutdown); budget used up by the next FY's cloud pass (configured
  FY and every LAN pass AUDIT_INCOMPLETE, no wake); scheduled skip vs manual; `fy=` (one FY, inactive refused); baseline
  without a record (unsuffixed ids, baseline key); safety reference per FY.
- Red before the change: 9 of 10 failed (9 with the missing behaviour; the baseline test was green from the start).
- Impact (GitNexus, before editing): `integrity_audit_flow` LOW (1 direct: `audit_lab/w22_child.py`); `_pending_context` LOW
  (2 direct: `_audit_cloud_leg`, `_audit_lan_leg`). `detect-changes --scope all` reported "critical" for the commit: it lists
  `backup`, `_overlap_record`, `_configured_fy_switched`, `_missed_if_no_budget`, `_nullable_audit_count` as changed, but
  their bodies are untouched (`git diff -U0` shows the hunks: the audit flow, `_pending_context`, the new helper). The index is
  17 commits behind HEAD, so the flag is a line-range artefact of the edit; not a real risk to those functions.
- Existing audit tests green: test_m4_budget, test_m5_audit_alerts, test_audit_pending_verdict, test_audit_remediation,
  test_serve, test_prefect_integration, test_chaos_harness_batch1/2 (121 passed, 2 skipped).

## Opus review findings kept as facts
- Verified: baseline path identical with overlap off (except F2); no data-loss path found; every `config.paths.*` use in the pipelines reads the per-pass config; next-FY pass writes no `file_entries`; the refusal when the next-FY NAS folder name is not its FY is the last barrier against a mirror into `\\nas\backup` (keep it); an FY27-28 success cannot mark FY26-27 healthy or send its recovery mail.
- NOT safe to deploy past 31 Dec until B8 and B9 are done: `report.py` (~317) and the dashboard night strip (`ui.py` ~600) still mix both FYs' rows.
- Builder deviations accepted: pipeline tests patch tasks; one WoL achieved through the `ensure_server_online` probe (not run on a real NAS); approval pre-check `_approval_of_pass`.

## Remaining tasks (plan section 9)
B4 final-copy state machine (April gate, auto rollover when blocker clears, forced rollover, silence AAM-ROL-06 while overlap active, G6 text keyword) - B5 calendar simulation Dec 30 - Apr 3 incl. Feb 29 2028 - B6 integrity audit per FY - B7 attention/overdue/watchdog - B8 reports - B9 dashboard API + UI (four cards, `?fy=` triggers, `/status` overlap block) - C1 registration of unscheduled legs (probe on a local Prefect server with clean `PREFECT_HOME`) - C2 manual-only behaviour - D1 docs (ARCHITECTURE.md, SESSION_HANDOFF.md) - D2 targeted audit lab test with real robocopy/rclone on a sandbox - D3 trial box (only when the owner says; tell the owner before any restart).
After every Part: full unit suite, then an Opus review of the diff before the next Part.

## Found while building
(append here; also keep the plan's own section up to date)
- Pipeline tests patch tasks instead of `fake_run_tool` (see F6).
- The unprotected-new-FY alarm (AAM-ROL-06) still fires for office files in the next-year folder until B4 silences it while overlap is active.

## B4 review fixes (Sonnet, after the Opus verifier)
- G1 `/api/rollover`: the `final_copy` block is wrapped in try/except (log, `final_copy=None`); 6 red endpoint tests fixed through fixtures with `rollover=RolloverConfig(overlap_enabled=False)` plus cloud/lan namespaces; new tests: `final_copy` returned while open, `None` when off, a raising reader still returns the card.
- G2 `flow.py` `step_announce`: message built into a variable before `notify(...)` (tests/test_recipients_callers.py green, test unchanged).
- G3 resume after `config_switch` done and `announce` not: the final-backup step saves `final_copy`, `forced_legs`, `final_status` into the overlap record (`db_meta overlap:<fy>`, key `final_copy_state`, no schema change; helpers `fy_overlap.save_final_copy/load_final_copy`) and the resume reloads them when `final_backups` is finished. Two tests (forced wording, earlier-copy time).
- G4 comment corrected. No retry added: once the journal is finished the check no longer enters the rollover resume, so a retry of `end_overlap` would need new code elsewhere. Consequence: the record can stay without `ended_at` after a DB error at that moment (harmless, config already points at the new FY).
- G5 tests: next-FY run after 1 April is not a final copy; boundary 00:00 IST counts / 23:59 does not (and UTC spellings) through the whole check; `overlap_open` False after `ended_at`.
- Owner question: manual-only NAS leg with overlap off is kept (plan section 12).
